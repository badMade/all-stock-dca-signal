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
import os
import subprocess
import sys
import tempfile
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Repo root too: the scripts modules under test import the shared top-level
# dca_fileio helper, which direct invocation (python scripts/tests/test_*.py)
# cannot resolve from scripts/ alone.
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

from run_classification import (  # noqa: E402
    ACCEPTED_STATE_SCHEMA_VERSIONS,
    COMPLETION_REASON_BLOCKED_BEFORE_SPREAD_GATE,
    COMPLETION_REASON_NO_LADDER_CANDIDATES,
    COMPLETION_REASON_NO_QUALIFYING_ORDERS,
    ERROR_CLASS_DEGRADED_MARKET_DATA,
    ERROR_CLASS_MISSING_SPREAD_DATA,
    FINAL_MANUAL_OVERRIDE_RUN_TYPE,
    INTRADAY_RECOVERY_SLOTS,
    ORDER_SUBMIT_STATUS_CONFIRMATION_REQUIRED,
    ORDER_SUBMIT_STATUS_ERROR,
    ORDER_SUBMIT_STATUS_NO_ORDERS,
    RECOVERY_RUN_TYPES,
    RECOVERY_STATE_SCHEMA_VERSIONS,
    RECOVERY_STREAM_CONTRACTS,
    RECOVERY_STREAMS,
    RECOVERY_TRIGGER_REASONS,
    RUN_CLASSIFICATION_COMPLETED_NO_ORDERS,
    RUN_CLASSIFICATION_PLACEMENT_FAILED,
    RUN_ORIGIN_SCHEDULED_RECOVERY,
    SPREAD_STATUS_MISSING_BID_ASK,
    SPREAD_STATUS_NOT_EVALUATED,
    SPREAD_STATUS_NOT_OK,
    SPREAD_STATUS_OK,
    SPREAD_STATUS_UNAVAILABLE,
    STATE_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION_V2,
    _already_covered_by_prior_allocation_loader,
    _dedupe_rows_by_order_id,
    _merge_prior,
    _order_ids_from_state,
    _prior_from_rows,
    _read_csv_rows,
    _referenced_orders_csvs,
    _referenced_orders_csvs_within,
    _rows_from_buy_candidates,
    build_final_manual_override_classification,
    build_manual_classification,
    build_raw_persistence_state,
    build_recovery_state,
    build_scheduled_classification,
    classify_no_order_run,
    classify_non_scheduled_order_ids,
    collect_earlier_scheduled_order_ids,
    collect_manual_order_ids,
    collect_recovery_context_order_ids,
    cron_tracking_root,
    exclude_manual_orders,
    historicals_batch_filename,
    incremental_top_up_amount,
    is_confirmation_blocked,
    is_manual_state,
    is_preauthorized_v2,
    is_scheduled_state,
    is_top_up_context_state,
    is_valid_recovery_context_state,
    latest_scheduled_run_path,
    legacy_scheduled_run_dir,
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
    recovery_context_roots,
    recovery_escalation_run_state,
    recovery_root_for,
    row_blocked_for_missing_spread,
    save_raw_input,
    scheduled_run_id,
    select_scheduled_state_for_reporting,
    spread_gate_fields,
    top_up_context_roots,
    validate_classification,
    validate_recovery_classification,
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
    assert scheduled_run_id(TRADING_DAY) == "scheduled_20260706_1530ET"
    mid = manual_run_id("2026-07-06T11:00:00Z")
    assert mid.startswith("manual_20260706_")
    assert "1530ET" not in mid


def test_scheduled_classification_fields():
    rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "scheduled_live_test"
    assert rc["cron_id"] == "10ce362c"
    assert rc["supersedes_cron_id"] == "ce16ba6b"
    assert rc["scheduled_for_et"] == "15:30"
    assert rc["scheduled_window_start_et"] == "15:25"
    assert rc["scheduled_window_end_et"] == "15:35"
    assert rc["manual_run"] is False
    assert rc["run_id"] == "scheduled_20260706_1530ET"
    assert rc["triggered_at_utc"] == "2026-07-06T19:20:01Z"
    assert rc["state_schema_version"] == STATE_SCHEMA_VERSION
    assert validate_classification(rc).ok


def test_scheduled_classification_default_run_id_uses_scheduled_for_et():
    rc = build_scheduled_classification(TRADING_DAY, scheduled_for_et="10:30", stream="morning")
    assert rc["run_id"] == "scheduled_20260706_1030ET"


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
    rc["run_id"] = "scheduled_20260706_1530ET"
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


def test_collectors_skip_unreadable_run_state_files():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()

        manual_written = write_run_artifacts(_manual_state(order_ids=("MAN-OK",)), base=base)
        manual_dir = Path(manual_written["run_state"]).parent
        (manual_dir / "run_state_corrupt.json").write_bytes(b"\xff")
        assert collect_manual_order_ids(base) == {"MAN-OK"}

        earlier_dir = cron_tracking_root(base) / "intraday_dca" / "20260706_1430ET"
        earlier_dir.mkdir(parents=True)
        (earlier_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_20260706_1430ET",
                "trading_day": TRADING_DAY,
                "run_classification": build_scheduled_classification(TRADING_DAY, "2026-07-06T18:30:00Z"),
                "buy_candidates": [
                    {"ticker": "EVR", "status": "submitted", "order_id": "EARLY-OK"}
                ],
            }),
            encoding="utf-8",
        )
        (earlier_dir / "run_state_corrupt.json").write_bytes(b"\xff")
        assert "EARLY-OK" in collect_earlier_scheduled_order_ids(base, TRADING_DAY)

        recovery_dir = _write_recovery_run(base, "midday_dca/20260727_1230ET_recovery_1305ET", order_ids=("REC-OK",))
        (recovery_dir / "run_state_corrupt.json").write_bytes(b"\xff")
        assert collect_recovery_context_order_ids(base, "2026-07-27") == {"REC-OK"}


def test_scheduled_write_updates_pointer():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        written = write_run_artifacts(_scheduled_state(), base=base)
        state_path = Path(written["run_state"])
        assert state_path.exists()
        assert "scheduled_runs" in str(state_path)
        pointer = latest_scheduled_run_path(base)
        assert pointer.exists()
        ptr = json.loads(pointer.read_bytes())
        assert ptr["run_id"] == "scheduled_20260706_1530ET"
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
        ptr = json.loads(pointer.read_bytes())
        assert ptr["run_state_path"] == str(external_path)
        assert ptr["run_id"] == "scheduled_20260706_1530ET"
        # No second copy of run_state.json was created anywhere under base.
        assert list(base.rglob("run_state.json")) == [external_path]


def test_update_latest_scheduled_run_pointer_uses_the_classifications_own_stream():
    # Codex review discussion_r3694494924 ("Write the blocked pointer with the
    # morning stream identity"): the pointer's "stream" field was hardcoded to
    # "afternoon" regardless of what run_classification.stream actually said,
    # so scripts.same_day_pointer.validate_pointer(..., expected_stream="morning")
    # rejected a freshly written pointer for a 10:30/12:30 roster run as
    # wrong_stream -- the pointer-based watchdog could never observe those
    # slots, including one blocked before placement.
    from run_classification import update_latest_scheduled_run_pointer

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = _scheduled_state()
        state["run_classification"]["stream"] = "morning"
        state_path = base / "run_state.json"
        state_path.write_text(json.dumps(state))

        pointer = update_latest_scheduled_run_pointer(state, state_path, base)

        ptr = json.loads(pointer.read_bytes())
        assert ptr["stream"] == "morning"

    # Omitting stream entirely (an older/afternoon-only classification) still
    # falls back to "afternoon", matching build_scheduled_classification's own
    # default and preserving every existing caller's behavior.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = _scheduled_state()
        state["run_classification"].pop("stream", None)
        state_path = base / "run_state.json"
        state_path.write_text(json.dumps(state))

        pointer = update_latest_scheduled_run_pointer(state, state_path, base)

        ptr = json.loads(pointer.read_bytes())
        assert ptr["stream"] == "afternoon"

    # Codex review discussion_r3694664... ("Default null legacy streams before
    # writing the pointer"): an EXPLICIT null (present, not merely omitted)
    # must ALSO fall back to "afternoon". rc.get("stream", "afternoon")'s
    # default only applies when the key is ABSENT -- validate_classification()
    # treats an explicit null the same as absent, so a classification with
    # "stream": null present passed validation but wrote a null stream into
    # the pointer, which scripts.same_day_pointer.validate_pointer normalizes
    # to "" and rejects as wrong_stream.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        state = _scheduled_state()
        state["run_classification"]["stream"] = None
        state_path = base / "run_state.json"
        state_path.write_text(json.dumps(state))

        pointer = update_latest_scheduled_run_pointer(state, state_path, base)

        ptr = json.loads(pointer.read_bytes())
        assert ptr["stream"] == "afternoon"


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

        ptr = json.loads(pointer.read_bytes())
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


def test_is_scheduled_state_false_for_contract_rejected():
    # A run the artifact contract rejected (top-level artifact_contract_failure
    # marker) must not be reported as a valid scheduled run to the 15:35 reporting
    # selector, even though its classification still says scheduled.
    sched = _scheduled_state()
    assert is_scheduled_state(sched)
    sched["artifact_contract_failure"] = {
        "failure": "artifact_contract_failure",
        "reason": "compute-only terminal state",
    }
    assert not is_scheduled_state(sched)


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


def test_select_scheduled_falls_back_to_legacy_directory():
    # Codex review discussion_r3653976743 ("Fall back to legacy directories
    # for historical reporting"): 10ce362c was retimed from 3:20 PM to 3:30 PM
    # ET on 2026-07-27. scheduled_run_dir() always resolves the canonical
    # _1530ET directory, which never existed for a pre-retime trading day --
    # those runs live under the legacy _1520ET suffix. Before the fix,
    # select_scheduled_state_for_reporting only ever looked at the canonical
    # directory, so it reported "no scheduled run_state" for every historical
    # day even though a valid legacy-identity artifact was on disk.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        legacy_dir = legacy_scheduled_run_dir(TRADING_DAY, base)
        legacy_dir.mkdir(parents=True)
        rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
        rc["scheduled_for_et"] = "15:20"
        rc["scheduled_window_start_et"] = "15:15"
        rc["run_id"] = "scheduled_20260706_1520ET"
        state = _scheduled_state(order_ids=("SCHED-1", "SCHED-2"))
        state["run_classification"] = rc
        (legacy_dir / "run_state.json").write_text(json.dumps(state))

        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.ok
        assert sel.order_ids == ["SCHED-1", "SCHED-2"]
        assert sel.state_path == str(legacy_dir / "run_state.json")

        # The canonical directory still wins when both exist -- the retime
        # only matters for dates that never got a canonical artifact.
        write_run_artifacts(_scheduled_state(order_ids=("SCHED-3",)), base=base)
        sel_canonical = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel_canonical.ok
        assert sel_canonical.order_ids == ["SCHED-3"]
        assert "1530ET" in sel_canonical.state_path


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
    # _scheduled_state() carries the canonical 1530ET identity, so the
    # attribution window starts at 15:25 (Codex review r3654117272: "Select
    # the fallback attribution window from the run identity") -- 15:20 is
    # now correctly excluded even though it was accepted before that fix.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # Scheduled state exists but has no order IDs (simulated write failure).
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        write_run_artifacts(_manual_state(order_ids=("MAN-9",)), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "BRK-1", "submitted_at_et": "15:20"},
                {"order_id": "MAN-9", "submitted_at_et": "15:26"},
                {"order_id": "BRK-2", "submitted_at_et": "15:34"},
            ],
            scheduled_state_write_failed=True,
        )
        assert sel.ok
        assert sel.used_broker_fallback
        assert sel.order_ids == ["BRK-2"]  # BRK-1 before 15:25, MAN-9 still excluded


def test_broker_fallback_excludes_recovery_context_orders():
    # Codex review: "Exclude recovery IDs from the reporting window fallback".
    # This branch attributes orders purely on their timestamp, so a delayed
    # earlier-stream recovery landing inside the afternoon window was assigned
    # to the afternoon scheduled run and flowed into scheduled slippage and
    # reliability -- contradicting the contract that declares recovery IDs
    # non-scheduled.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)

        # A 14:30 intraday recovery that actually ran at 15:27, same trading day.
        run_dir = cron_tracking_root(base) / "intraday_dca" / f"{TRADING_DAY.replace('-', '')}_1430ET_recovery_1527ET"
        run_dir.mkdir(parents=True)
        (run_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_recovery_1527ET",
                "trading_day": TRADING_DAY,
                "run_classification": _recovery_classification(
                    run_type="intraday_dca_live_recovery",
                    stream="intraday_1430_recovery",
                    state_schema_version="intraday-live-v2-recovery",
                    run_id="intraday_recovery_1527ET",
                    trading_day=TRADING_DAY,
                ),
                "buy_candidates": [
                    {"ticker": "EVR", "status": "filled", "order_id": "REC-1527"}
                ],
            }),
            encoding="utf-8",
        )

        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "REC-1527", "submitted_at_et": "15:27"},
                {"order_id": "BRK-2", "submitted_at_et": "15:30"},
            ],
            scheduled_state_write_failed=True,
        )

        assert sel.used_broker_fallback
        # In-window, but recovery context -- not the afternoon run's order.
        assert sel.order_ids == ["BRK-2"]
        assert "REC-1527" in sel.excluded_recovery_context_order_ids


def test_fallback_excludes_normal_earlier_stream_scheduled_orders():
    # Codex review: "Exclude all earlier-stream claims from the broker
    # fallback". The contract excludes any order already claimed by an earlier
    # stream artifact, but only manual and RECOVERY claims were collected. A
    # delayed NORMAL 14:30 scheduled run placing at 15:27 falls inside the
    # canonical 15:25-15:35 window, so it was attributed to the final run and
    # flowed into final-run slippage and reliability.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)

        # A NORMAL intraday 14:30 scheduled run -- no _recovery_ in the path.
        run_dir = (
            cron_tracking_root(base)
            / "intraday_dca"
            / f"{TRADING_DAY.replace('-', '')}_1430ET"
        )
        run_dir.mkdir(parents=True)
        (run_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_20260706_1430ET",
                "trading_day": TRADING_DAY,
                "run_classification": build_scheduled_classification(
                    TRADING_DAY, "2026-07-06T18:30:00Z"
                ),
                "buy_candidates": [
                    {"ticker": "EVR", "status": "submitted", "order_id": "INTRA-1527"}
                ],
            }),
            encoding="utf-8",
        )

        # A GENUINE intraday state, with the intraday stream's own run_type,
        # cron_id, schema version, and 14:30 slot -- NOT the afternoon shape.
        # is_scheduled_state() rejects this (its validator hard-requires
        # run_type='scheduled_live_test', the 15:30 cron lineage, and a
        # scheduled_YYYYMMDD_1530ET run_id), so the collector saw nothing in
        # production while an afternoon-shaped fixture made the test pass
        # (Codex review: "Validate earlier streams with their own contracts").
        real_dir = (
            cron_tracking_root(base)
            / "intraday_dca"
            / f"{TRADING_DAY.replace('-', '')}_1130ET"
        )
        real_dir.mkdir(parents=True)
        (real_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_20260706_1130ET",
                "trading_day": TRADING_DAY,
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "intraday_dca_live",
                    "stream": "intraday",
                    "cron_id": "7be82b97",
                    "manual_run": False,
                    "trading_day": TRADING_DAY,
                    "run_id": "intraday_20260706_1130ET",
                    "scheduled_for_et": "11:30",
                    "state_schema_version": "intraday-live-v2",
                },
                "buy_candidates": [
                    {"ticker": "GS", "status": "submitted", "order_id": "REAL-1527"}
                ],
            }),
            encoding="utf-8",
        )
        assert not is_scheduled_state(json.loads((real_dir / "run_state.json").read_bytes()))

        assert "INTRA-1527" in collect_earlier_scheduled_order_ids(base, TRADING_DAY)
        assert "REAL-1527" in collect_earlier_scheduled_order_ids(base, TRADING_DAY)

        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "INTRA-1527", "submitted_at_et": "15:27"},
                {"order_id": "REAL-1527", "submitted_at_et": "15:28"},
                {"order_id": "BRK-2", "submitted_at_et": "15:30"},
            ],
            scheduled_state_write_failed=True,
        )

        assert sel.used_broker_fallback
        # In-window, but already claimed by the 14:30 stream.
        assert sel.order_ids == ["BRK-2"]


def test_earlier_scheduled_collector_honors_a_relocated_stream_root():
    # Codex review: "Honor relocated roots when excluding earlier scheduled
    # orders". DCA_INTRADAY_LIVE_STATE_ROOT REPLACES the conventional
    # intraday_dca tree. Both callers default stream_roots to None, so scanning
    # only cron_tracking/*_dca meant a delayed normal run under the relocated
    # tree was never collected and stayed eligible for the final-run time
    # fallback -- misattributed to the afternoon run.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        cron_tracking_root(base).mkdir(parents=True, exist_ok=True)

        relocated = Path(d) / "elsewhere" / "intraday"
        slot = relocated / "20260706_1430ET"
        slot.mkdir(parents=True)
        (slot / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_20260706_1430ET",
                "trading_day": TRADING_DAY,
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "intraday_dca_live",
                    "cron_id": "7be82b97",
                    "manual_run": False,
                    "trading_day": TRADING_DAY,
                    "run_id": "intraday_20260706_1430ET",
                    "scheduled_for_et": "14:30",
                },
                "buy_candidates": [
                    {"ticker": "EVR", "status": "submitted", "order_id": "MOVED-1527"}
                ],
            }),
            encoding="utf-8",
        )

        prior = os.environ.get("DCA_INTRADAY_LIVE_STATE_ROOT")
        os.environ["DCA_INTRADAY_LIVE_STATE_ROOT"] = str(relocated)
        try:
            # Found with stream_roots omitted, which is how both callers call it.
            assert "MOVED-1527" in collect_earlier_scheduled_order_ids(base, TRADING_DAY)

            sel = select_scheduled_state_for_reporting(
                TRADING_DAY, base=base,
                broker_window_orders=[
                    {"order_id": "MOVED-1527", "submitted_at_et": "15:27"},
                    {"order_id": "BRK-2", "submitted_at_et": "15:30"},
                ],
                scheduled_state_write_failed=True,
            )
            assert sel.used_broker_fallback
            assert sel.order_ids == ["BRK-2"]

            # An override naming the cron-tracking root is a misconfiguration,
            # not a relocation: the conventional fallback must survive it.
            os.environ["DCA_INTRADAY_LIVE_STATE_ROOT"] = str(cron_tracking_root(base))
            assert "MOVED-1527" not in collect_earlier_scheduled_order_ids(base, TRADING_DAY)
        finally:
            if prior is None:
                os.environ.pop("DCA_INTRADAY_LIVE_STATE_ROOT", None)
            else:
                os.environ["DCA_INTRADAY_LIVE_STATE_ROOT"] = prior


def test_earlier_scheduled_collector_requires_a_valid_same_day_state():
    # A directory-name match is not evidence, matching the recovery collector.
    # A prior-DAY artifact must not explain away today's orders, and a manual
    # state contributes nothing even when it claims a scheduled origin.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        cron_tracking_root(base).mkdir(parents=True, exist_ok=True)

        stale = cron_tracking_root(base) / "midday_dca" / "20260705_1230ET"
        stale.mkdir(parents=True)
        (stale / "run_state.json").write_text(
            json.dumps({
                "run_id": "midday_20260705",
                "trading_day": "2026-07-05",
                "run_classification": build_scheduled_classification(
                    "2026-07-05", "2026-07-05T16:30:00Z"
                ),
                "buy_candidates": [
                    {"ticker": "AAPL", "status": "submitted", "order_id": "STALE-1"}
                ],
            }),
            encoding="utf-8",
        )

        rejected = cron_tracking_root(base) / "morning_dca" / "20260706_0935ET"
        rejected.mkdir(parents=True)
        (rejected / "run_state.json").write_text(
            json.dumps({
                "run_id": "morning_20260706",
                "trading_day": TRADING_DAY,
                "artifact_contract_failure": "root artifacts missing",
                "run_classification": build_scheduled_classification(
                    TRADING_DAY, "2026-07-06T13:35:00Z"
                ),
                "buy_candidates": [
                    {"ticker": "MSFT", "status": "submitted", "order_id": "REJECTED-1"}
                ],
            }),
            encoding="utf-8",
        )

        ids = collect_earlier_scheduled_order_ids(base, TRADING_DAY)

        manual_shaped = cron_tracking_root(base) / "intraday_dca" / "20260706_1130ET"
        manual_shaped.mkdir(parents=True)
        (manual_shaped / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_manual",
                "trading_day": TRADING_DAY,
                "run_classification": {
                    "run_origin": "scheduled",
                    "run_type": "intraday_dca_live",
                    "manual_run": True,
                    "trading_day": TRADING_DAY,
                    "run_id": "intraday_manual",
                },
                "buy_candidates": [
                    {"ticker": "GS", "status": "submitted", "order_id": "MANUAL-1"}
                ],
            }),
            encoding="utf-8",
        )

        ids = collect_earlier_scheduled_order_ids(base, TRADING_DAY)

        assert "STALE-1" not in ids
        # Revised for "Exclude contract-failed earlier orders from final
        # attribution": an artifact_contract_failure marker means the run is not
        # valid scheduled EVIDENCE, not that it placed no orders. A contract
        # failure recorded after submission leaves real orders owned by that
        # earlier stream, and dropping them from the exclusion set handed them
        # to the final run's time fallback. Ownership, not evidence validity, is
        # what an exclusion set answers.
        assert "REJECTED-1" in ids
        # manual_run wins over a 'scheduled' origin, as everywhere else.
        assert "MANUAL-1" not in ids


def test_non_fallback_paths_do_not_collect_recovery_ids():
    # The recovery scan runs ONLY in the fallback branch: the other paths use
    # the scheduled state's own order IDs, and filtering those against a
    # recovery set could drop a genuine scheduled order.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        write_run_artifacts(_scheduled_state(order_ids=("SCHED-1",)), base=base)

        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)

        assert sel.order_ids == ["SCHED-1"]
        assert sel.excluded_recovery_context_order_ids == []


def test_broker_fallback_excludes_out_of_window_orders():
    # Canonical 1530ET identity -> window is 15:25-15:35 inclusive.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "EARLY", "submitted_at_et": "15:10"},   # before 15:25
                {"order_id": "PRE-RETIME", "submitted_at_et": "15:20"},  # before 15:25 (legacy-only window)
                {"order_id": "IN-1", "submitted_at_et": "15:25"},    # inclusive start
                {"order_id": "IN-2", "submitted_at_et": "2026-07-06T15:35:00-04:00"},  # inclusive end, ISO
                {"order_id": "LATE", "submitted_at_et": "15:50"},    # after 15:35
                {"order_id": "NOTIME"},                              # no timestamp -> dropped
            ],
            scheduled_state_write_failed=True,
        )
        assert sel.ok
        assert sel.used_broker_fallback
        assert sel.order_ids == ["IN-1", "IN-2"]  # out-of-window and untimed dropped


def test_broker_fallback_uses_wider_window_for_legacy_identity():
    # Codex review r3654117272: a legacy 1520ET state keeps the wider
    # 15:15-15:35 attribution window (orders from legacy 3:20 PM runs), while
    # a canonical 1530ET state (covered above) is tighter at 15:25-15:35.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        legacy_dir = legacy_scheduled_run_dir(TRADING_DAY, base)
        legacy_dir.mkdir(parents=True)
        rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
        rc["scheduled_for_et"] = "15:20"
        rc["scheduled_window_start_et"] = "15:15"
        rc["run_id"] = "scheduled_20260706_1520ET"
        state = _scheduled_state(order_ids=())
        state["run_classification"] = rc
        (legacy_dir / "run_state.json").write_text(json.dumps(state))

        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "EARLY", "submitted_at_et": "15:10"},   # before 15:15
                {"order_id": "IN-1", "submitted_at_et": "15:15"},    # inclusive start for legacy
                {"order_id": "IN-2", "submitted_at_et": "15:34"},
            ],
            scheduled_state_write_failed=True,
        )
        assert sel.ok
        assert sel.used_broker_fallback
        assert sel.order_ids == ["IN-1", "IN-2"]


def test_broker_fallback_converts_utc_timestamps_to_et():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_orders=[
                {"order_id": "UTC-IN", "submitted_at_et": "2026-07-06T19:30:00Z"},   # 15:30 EDT -> in
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
        ptr = json.loads(pointer.read_bytes())
        assert ptr["run_id"] == "scheduled_20260706_1530ET"


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


def test_legacy_1520et_scheduled_classification_still_validates():
    # 10ce362c was retimed from 3:20 PM to 3:30 PM ET on 2026-07-27. Writers only
    # emit the canonical identity, but run_states written before the retime must
    # stay valid scheduled evidence.
    rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
    rc["scheduled_for_et"] = "15:20"
    rc["scheduled_window_start_et"] = "15:15"
    rc["run_id"] = "scheduled_20260706_1520ET"
    assert validate_classification(rc).ok


def test_validation_rejects_hybrid_canonical_legacy_identity():
    # Codex review discussion_r3653976747 ("Validate canonical and legacy
    # identity fields as a unit"): scheduled_for_et, scheduled_window_start_et,
    # and run_id must be validated as ONE coherent identity tuple, not
    # independently. Before the fix, each field was checked against
    # (canonical OR legacy) on its own, so a hybrid classification like
    # scheduled_for_et='15:30' (canonical) paired with a run_id of
    # '..._1520ET' (legacy) passed as valid scheduled evidence even though no
    # real scheduled slot ever produces that combination.
    rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:30:01Z")
    assert rc["scheduled_for_et"] == "15:30"
    assert rc["scheduled_window_start_et"] == "15:25"
    rc["run_id"] = "scheduled_20260706_1520ET"  # legacy run_id, canonical times
    res = validate_classification(rc)
    assert not res.ok
    assert any("coherent identity" in e for e in res.errors)

    # The inverse hybrid (legacy times, canonical run_id) must also be rejected.
    rc2 = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:30:01Z")
    rc2["scheduled_for_et"] = "15:20"
    rc2["scheduled_window_start_et"] = "15:15"
    # run_id stays canonical '..._1530ET' from build_scheduled_classification.
    res2 = validate_classification(rc2)
    assert not res2.ok
    assert any("coherent identity" in e for e in res2.errors)


def test_mixed_canonical_legacy_identity_fields_rejected():
    # Mixing canonical and legacy scheduled identity fields must be rejected.
    # e.g. legacy scheduled_for_et with canonical scheduled_window_start_et.
    rc = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
    rc["scheduled_for_et"] = "15:20"          # legacy
    rc["scheduled_window_start_et"] = "15:25"  # canonical
    rc["run_id"] = "scheduled_20260706_1530ET"  # canonical
    res = validate_classification(rc)
    assert not res.ok
    assert any("canonical" in e or "legacy" in e for e in res.errors)

    # Legacy for_et, legacy window, but canonical run_id
    rc2 = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z")
    rc2["scheduled_for_et"] = "15:20"
    rc2["scheduled_window_start_et"] = "15:15"
    rc2["run_id"] = "scheduled_20260706_1530ET"  # canonical — mismatched
    res2 = validate_classification(rc2)
    assert not res2.ok

    # Canonical for_et, canonical window, but legacy run_id
    rc3 = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:30:01Z")
    rc3["scheduled_for_et"] = "15:30"
    rc3["scheduled_window_start_et"] = "15:25"
    rc3["run_id"] = "scheduled_20260706_1520ET"  # legacy — mismatched
    res3 = validate_classification(rc3)
    assert not res3.ok


def test_raw_input_dir_under_scheduled_run():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        assert raw_input_dir(TRADING_DAY, base).name == "raw"
        assert raw_input_dir(TRADING_DAY, base).parent.name == "20260706_1530ET"


def test_save_raw_input_persists_before_compute():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        assert list_raw_inputs_saved(TRADING_DAY, base) == []
        p1 = save_raw_input(TRADING_DAY, quotes_batch_filename(1), {"AAPL": 190.0}, base=base)
        save_raw_input(TRADING_DAY, historicals_batch_filename(1), {"AAPL": [1, 2, 3]}, base=base)
        assert p1.exists()
        # Compute reads the persisted file, not an in-memory response.
        assert json.loads(p1.read_bytes())["AAPL"] == 190.0
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
        saved = json.loads(path.read_bytes())
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
        assert not is_manual_state(state)
        # NOT a valid scheduled run: it fired, it never computed a signal or
        # reached placement, and it must not be published as evidence the slot
        # executed (Codex review). The state is still written to the scheduled
        # path and still refreshes the pointer -- proving the slot fired is the
        # whole reason it exists -- but the pointer records it as invalid.
        assert state["scheduled_evidence_denied"] is True
        assert not is_scheduled_state(state)
        written = write_run_artifacts(state, base=base)
        assert "latest_scheduled_run" in written
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
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
    assert not is_manual_state(state)
    # Same denial as the raw-persistence sibling: an escalated recovery fired but
    # never completed the slot (Codex review).
    assert state["scheduled_evidence_denied"] is True
    assert not is_scheduled_state(state)
    # It still writes to the scheduled path and refreshes the pointer.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        written = write_run_artifacts(state, base=base)
        assert "latest_scheduled_run" in written
        sel = select_scheduled_state_for_reporting(TRADING_DAY, base=base)
        assert sel.order_ids == []


def test_recovery_triggers_match_config_and_schema():
    # Keep the runtime constant synchronized with the declarative policy/schema
    # (config/scheduled_tasks.json failure_recovery_policy.recovery_triggers and
    # schemas/run_state.schema.json recovery.trigger_reason.enum) so they can
    # never silently drift apart.
    repo_root = Path(__file__).resolve().parents[2]
    config = json.loads((repo_root / "config" / "scheduled_tasks.json").read_bytes())
    assert tuple(config["failure_recovery_policy"]["recovery_triggers"]) == RECOVERY_TRIGGER_REASONS
    schema = json.loads((repo_root / "schemas" / "run_state.schema.json").read_bytes())
    assert tuple(schema["properties"]["recovery"]["properties"]["trigger_reason"]["enum"]) == RECOVERY_TRIGGER_REASONS


def test_recovery_state_validates_against_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    Registry, Resource = referencing.Registry, referencing.Resource

    repo_root = Path(__file__).resolve().parents[2]
    schemas_dir = repo_root / "schemas"
    main = json.loads((schemas_dir / "run_state.schema.json").read_bytes())
    resources = [
        (name, Resource.from_contents(json.loads((schemas_dir / name).read_bytes())))
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


def test_schema_rejects_hybrid_canonical_legacy_identity():
    # Codex review discussion_r3653976747 ("Validate canonical and legacy
    # identity fields as a unit"): the JSON schema must mirror
    # validate_classification's coupling -- scheduled_for_et,
    # scheduled_window_start_et, and run_id must form ONE coherent identity,
    # not be accepted independently. A hybrid classification (canonical
    # scheduled_for_et/scheduled_window_start_et paired with a legacy
    # ..._1520ET run_id) has no coherent scheduled slot and must fail schema
    # validation, not silently pass as valid scheduled evidence.
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    Registry, Resource = referencing.Registry, referencing.Resource

    repo_root = Path(__file__).resolve().parents[2]
    schemas_dir = repo_root / "schemas"
    rc_schema = json.loads((schemas_dir / "run_classification.schema.json").read_bytes())
    resources = [
        (name, Resource.from_contents(json.loads((schemas_dir / name).read_bytes())))
        for name in ("run_classification.schema.json",)
    ]
    registry = Registry().with_resources(resources)
    validator = jsonschema.Draft7Validator(rc_schema, registry=registry)

    canonical = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:30:01Z")
    validator.validate(canonical)  # sanity: the fully-canonical tuple still passes

    legacy = dict(canonical)
    legacy["scheduled_for_et"] = "15:20"
    legacy["scheduled_window_start_et"] = "15:15"
    legacy["run_id"] = "scheduled_20260706_1520ET"
    validator.validate(legacy)  # sanity: the fully-legacy tuple still passes

    hybrid = dict(canonical)
    hybrid["run_id"] = "scheduled_20260706_1520ET"  # canonical times, legacy run_id
    assert not validator.is_valid(hybrid)

    # recovery_rerun=true with no recovery block must fail the schema's if/then.
    missing_recovery = _scheduled_state()
    missing_recovery["recovery_rerun"] = True
    try:
        validator.validate(missing_recovery)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError: recovery is required when recovery_rerun=true")


def test_schema_accepts_the_seven_slot_rosters_morning_and_midday_identities():
    # Codex review discussion_r3694371454/r3694407934 ("Emit classifications
    # that satisfy the declared stream schema"): run_dca.run_slot("1030ET")/
    # ("1230ET") stamp their own morning/midday identity via
    # _slot_classification_overrides + build_scheduled_classification, e.g.
    # stream="morning", cron_id="42173154", scheduled_for_et="10:30",
    # scheduled_window_start_et="10:25", scheduled_window_end_et="10:35",
    # run_id="scheduled_YYYYMMDD_1030ET" -- but this generic schema only
    # recognized the afternoon 15:30/legacy 15:20 identities, so every
    # non-afternoon roster artifact was schema-invalid and rejectable by a
    # schema-aware watchdog or consumer.
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    Registry, Resource = referencing.Registry, referencing.Resource

    repo_root = Path(__file__).resolve().parents[2]
    schemas_dir = repo_root / "schemas"
    rc_schema = json.loads((schemas_dir / "run_classification.schema.json").read_bytes())
    resources = [
        (name, Resource.from_contents(json.loads((schemas_dir / name).read_bytes())))
        for name in ("run_classification.schema.json",)
    ]
    registry = Registry().with_resources(resources)
    validator = jsonschema.Draft7Validator(rc_schema, registry=registry)

    morning = build_scheduled_classification(
        TRADING_DAY,
        "2026-07-06T14:30:01Z",
        stream="morning",
        cron_id="42173154",
        supersedes_cron_id=None,
        scheduled_for_et="10:30",
        scheduled_window_start_et="10:25",
        scheduled_window_end_et="10:35",
        run_id="scheduled_20260706_1030ET",
        valid_afternoon_scheduled_evidence=False,
    )
    validator.validate(morning)

    midday = dict(morning)
    midday.update(
        stream="midday",
        cron_id="a6239dae",
        scheduled_for_et="12:30",
        scheduled_window_start_et="12:25",
        scheduled_window_end_et="12:35",
        run_id="scheduled_20260706_1230ET",
    )
    validator.validate(midday)

    # A morning cron_id claiming the afternoon stream is not a real identity
    # and must still be rejected.
    mislabeled = dict(morning)
    mislabeled["stream"] = "afternoon"
    assert not validator.is_valid(mislabeled)

    # A 10:30 time paired with a 1230ET run_id has no coherent slot either.
    hybrid_slot = dict(morning)
    hybrid_slot["run_id"] = "scheduled_20260706_1230ET"
    assert not validator.is_valid(hybrid_slot)

    # Codex review discussion_r3694494920 ("Bind the window end to each slot
    # identity"): a morning time/run_id paired with the afternoon or midday
    # window end has no coherent slot and must be rejected, not accepted via
    # the shared enum alone.
    wrong_window_end = dict(morning)
    wrong_window_end["scheduled_window_end_et"] = "15:35"
    assert not validator.is_valid(wrong_window_end)

    # Codex review discussion_r3694494922 ("Validate the cron ID with the
    # complete slot identity"): the morning cron_id must not validate
    # alongside the canonical afternoon time/run_id (or vice versa) just
    # because each was independently acceptable somewhere.
    afternoon_time_morning_cron = build_scheduled_classification(
        TRADING_DAY,
        "2026-07-06T19:30:01Z",
        cron_id="42173154",
        supersedes_cron_id=None,
        stream="afternoon",
    )
    assert not validator.is_valid(afternoon_time_morning_cron)

    # Codex review discussion_r3694494927 ("Deny afternoon evidence in
    # non-afternoon schema branches"): a morning-shaped record must not be
    # able to also claim valid_afternoon_scheduled_evidence=true.
    claims_afternoon_evidence = dict(morning)
    claims_afternoon_evidence["valid_afternoon_scheduled_evidence"] = True
    assert not validator.is_valid(claims_afternoon_evidence)

    # Codex review discussion_r3694494926 ("Exclude the new scheduled IDs
    # from manual classifications"): a manual run must not be able to claim
    # one of the new scheduled-slot run_ids either.
    manual_claiming_slot_id = build_manual_classification("2026-07-06T14:30:01Z")
    manual_claiming_slot_id["run_id"] = "scheduled_20260706_1030ET"
    assert not validator.is_valid(manual_claiming_slot_id)

    # Codex review discussion_r3694539232 ("Require the afternoon stream in
    # afternoon identity branches"): the canonical/legacy afternoon time+cron
    # identity must not validate alongside a morning/midday stream claim.
    afternoon_shape_morning_stream = build_scheduled_classification(
        TRADING_DAY, "2026-07-06T19:30:01Z", stream="morning"
    )
    assert not validator.is_valid(afternoon_shape_morning_stream)

    # Codex review discussion_r3694585153 ("Reject afternoon supersession
    # lineage on roster slots"): a morning classification must not be able to
    # also claim the afternoon task's own supersedes_cron_id lineage marker.
    claims_afternoon_lineage = dict(morning)
    claims_afternoon_lineage["supersedes_cron_id"] = "ce16ba6b"
    assert not validator.is_valid(claims_afternoon_lineage)

    # Codex review discussion_r3694664... ("Allow null supersession markers in
    # roster branches"): an EXPLICIT null (present in the object, not merely
    # omitted) must still validate. build_scheduled_classification always
    # deletes the key when None, so this constructs the present-but-null case
    # by hand -- the global property and validate_classification() both
    # already treat null the same as absent, and a 'not: required' constraint
    # (which rejects the key whenever PRESENT, regardless of value) wrongly
    # rejected this same case in an earlier revision.
    explicit_null_supersedes = dict(morning)
    explicit_null_supersedes["supersedes_cron_id"] = None
    validator.validate(explicit_null_supersedes)

    # Codex review discussion_r3694682778 ("Accept null legacy streams in the
    # schema"): the afternoon branches' own 'stream, when present, must be
    # afternoon' constraint has the SAME null-vs-absent gap the roster
    # branches had above. An explicit stream=null on an afternoon-shaped
    # record must still validate -- validate_classification() treats it the
    # same as absent, and update_latest_scheduled_run_pointer normalizes both
    # to "afternoon" when writing the pointer, so a bare 'const': 'afternoon'
    # rejected a present-but-null case those two already accept and publish.
    afternoon = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:30:01Z")
    afternoon_explicit_null_stream = dict(afternoon)
    afternoon_explicit_null_stream["stream"] = None
    validator.validate(afternoon_explicit_null_stream)
    # A non-null, non-"afternoon" stream must still be rejected.
    afternoon_garbage_stream = dict(afternoon)
    afternoon_garbage_stream["stream"] = "midday_recovery"
    assert not validator.is_valid(afternoon_garbage_stream)


def test_validate_classification_accepts_the_seven_slot_rosters_morning_and_midday_identities():
    # Codex review discussion_r3694864526 ("Mirror roster identities in the
    # runtime validator"): validate_classification() -- not the JSON schema --
    # is what is_scheduled_state(), dca_risk.classify_stream(), and
    # near_close_dca's scheduled-pointer-update gate actually call at
    # runtime. Widening only the JSON schema in the two Codex rounds above
    # left this function still afternoon-only, so every 10:30/12:30 roster
    # classification failed here regardless of the schema fix -- and the
    # pointer-update gate SKIPS its update when this fails.
    morning = build_scheduled_classification(
        TRADING_DAY,
        "2026-07-06T14:30:01Z",
        stream="morning",
        cron_id="42173154",
        supersedes_cron_id=None,
        scheduled_for_et="10:30",
        scheduled_window_start_et="10:25",
        scheduled_window_end_et="10:35",
        run_id="scheduled_20260706_1030ET",
        valid_afternoon_scheduled_evidence=False,
    )
    res = validate_classification(morning)
    assert res.ok, res.errors

    midday = dict(morning)
    midday.update(
        stream="midday",
        cron_id="a6239dae",
        scheduled_for_et="12:30",
        scheduled_window_start_et="12:25",
        scheduled_window_end_et="12:35",
        run_id="scheduled_20260706_1230ET",
    )
    res = validate_classification(midday)
    assert res.ok, res.errors

    # Every adversarial case the schema rejects must also fail the runtime
    # validator -- the whole point of this fix is that the two agree.
    mislabeled = dict(morning)
    mislabeled["stream"] = "afternoon"
    assert not validate_classification(mislabeled).ok

    hybrid_slot = dict(morning)
    hybrid_slot["run_id"] = "scheduled_20260706_1230ET"
    assert not validate_classification(hybrid_slot).ok

    wrong_window_end = dict(morning)
    wrong_window_end["scheduled_window_end_et"] = "15:35"
    assert not validate_classification(wrong_window_end).ok

    afternoon_cron_morning_time = dict(morning)
    afternoon_cron_morning_time["cron_id"] = "10ce362c"
    assert not validate_classification(afternoon_cron_morning_time).ok

    claims_afternoon_evidence = dict(morning)
    claims_afternoon_evidence["valid_afternoon_scheduled_evidence"] = True
    assert not validate_classification(claims_afternoon_evidence).ok

    # Codex review discussion_r3694864524 ("Reject afternoon supersession
    # lineage on roster slots"): a morning classification must not also carry
    # the afternoon task's own supersedes_cron_id lineage marker.
    claims_afternoon_lineage = dict(morning)
    claims_afternoon_lineage["supersedes_cron_id"] = "ce16ba6b"
    assert not validate_classification(claims_afternoon_lineage).ok

    manual_claiming_slot_id = build_manual_classification("2026-07-06T14:30:01Z")
    manual_claiming_slot_id["run_id"] = "scheduled_20260706_1030ET"
    assert not validate_classification(manual_claiming_slot_id).ok

    # The existing afternoon/legacy path must be completely unaffected.
    afternoon = build_scheduled_classification(TRADING_DAY, "2026-07-06T19:30:01Z")
    assert validate_classification(afternoon).ok

    # Codex review discussion_r3694864531 ("Reject unknown streams before
    # using the afternoon validator"): an afternoon-shaped identity paired
    # with an unrecognized stream (a typo, or an unrelated value like
    # "midday_recovery") must not fall through to the afternoon branch just
    # because it is neither "morning" nor "midday" -- mirroring the schema's
    # own 'stream, when present, must be afternoon' constraint on the
    # canonical/legacy branches.
    garbage_stream = dict(afternoon)
    garbage_stream["stream"] = "midday_recovery"
    assert not validate_classification(garbage_stream).ok

    # stream absent entirely (legacy artifacts predating the field) must
    # still validate under the afternoon branch.
    no_stream = dict(afternoon)
    no_stream.pop("stream", None)
    assert validate_classification(no_stream).ok

    # And an explicit stream="afternoon" must still validate too.
    explicit_afternoon = dict(afternoon)
    explicit_afternoon["stream"] = "afternoon"
    assert validate_classification(explicit_afternoon).ok


def test_validate_classification_accepts_market_open_and_intraday_executor_identities():
    # Codex review discussion_r3705349451 ("Register intraday identities with
    # the runtime validator"): market_open_dca.py/intraday_dca.py (both
    # subclassing morning_dca.MorningDCAExecutor) stamp their OWN run_type and
    # state_schema_version -- not build_scheduled_classification()'s afternoon
    # shape reused by the seven-slot roster above -- so accepting only the
    # roster's morning/midday identities left every genuine market_open/
    # intraday scheduled run still rejected here. dca_risk.classify_stream()
    # calls this function, so the gap silently excluded real 14:30 intraday
    # deployment from the afternoon kill-switch's trailing-7-day total.
    market_open = {
        "run_origin": "scheduled",
        "run_type": "market_open_dca_live",
        "stream": "market_open",
        "trades": True,
        "cron_id": "2ba19526",
        "supersedes_cron_id": None,
        "scheduled_for_et": "09:35",
        "scheduled_window_start_et": "09:30",
        "scheduled_window_end_et": "09:50",
        "manual_run": False,
        "run_id": "market_open_live_20260803_0935ET",
        "triggered_at_utc": "2026-08-03T13:35:00Z",
        "state_schema_version": "market-open-live-v1",
    }
    res = validate_classification(market_open)
    assert res.ok, res.errors

    intraday_1130 = {
        "run_origin": "scheduled",
        "run_type": "intraday_dca_live",
        "stream": "intraday",
        "trades": True,
        "cron_id": "7be82b97",
        "supersedes_cron_id": None,
        "scheduled_for_et": "11:30",
        "scheduled_window_start_et": "11:25",
        "scheduled_window_end_et": "11:35",
        "manual_run": False,
        "run_id": "intraday_live_20260803_1130ET",
        "triggered_at_utc": "2026-08-03T15:30:00Z",
        "state_schema_version": "intraday-live-v1",
    }
    res = validate_classification(intraday_1130)
    assert res.ok, res.errors

    intraday_1430 = dict(intraday_1130)
    intraday_1430.update(
        scheduled_for_et="14:30",
        scheduled_window_start_et="14:25",
        scheduled_window_end_et="14:35",
        run_id="intraday_live_20260803_1430ET",
        triggered_at_utc="2026-08-03T18:30:00Z",
    )
    res = validate_classification(intraday_1430)
    assert res.ok, res.errors

    # Adversarial cases: identity fields must form a coherent tuple, mirroring
    # the morning/midday checks above.
    wrong_run_type = dict(market_open)
    wrong_run_type["run_type"] = "morning_dca_live"
    assert not validate_classification(wrong_run_type).ok

    wrong_cron = dict(intraday_1130)
    wrong_cron["cron_id"] = "2ba19526"
    assert not validate_classification(wrong_cron).ok

    mismatched_slot_window = dict(intraday_1130)
    mismatched_slot_window["scheduled_window_end_et"] = "13:35"
    assert not validate_classification(mismatched_slot_window).ok

    hybrid_slot_run_id = dict(intraday_1130)
    hybrid_slot_run_id["run_id"] = "intraday_live_20260803_1430ET"
    assert not validate_classification(hybrid_slot_run_id).ok

    unknown_scheduled_for_et = dict(intraday_1130)
    unknown_scheduled_for_et["scheduled_for_et"] = "10:30"
    assert not validate_classification(unknown_scheduled_for_et).ok

    claims_afternoon_lineage = dict(market_open)
    claims_afternoon_lineage["supersedes_cron_id"] = "ce16ba6b"
    assert not validate_classification(claims_afternoon_lineage).ok

    # The stream-identity fix must actually reach dca_risk's kill switch: a
    # genuine 14:30 intraday run now classifies as scheduled evidence and, per
    # is_afternoon_scheduled_evidence's own (unchanged) time floor, counts as
    # afternoon deployment; an 11:30 run does not.
    import dca_risk

    state_1430 = {
        "trading_day": "2026-08-03",
        "generated_at": "2026-08-03T18:30:00Z",  # 14:30 ET
        "run_classification": intraday_1430,
    }
    assert dca_risk.classify_stream(state_1430) == dca_risk.STREAM_SCHEDULED
    assert dca_risk.is_afternoon_scheduled_evidence(state_1430) is True

    state_1130 = {
        "trading_day": "2026-08-03",
        "generated_at": "2026-08-03T15:30:00Z",  # 11:30 ET
        "run_classification": intraday_1130,
    }
    assert dca_risk.classify_stream(state_1130) == dca_risk.STREAM_SCHEDULED
    assert dca_risk.is_afternoon_scheduled_evidence(state_1130) is False


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
    # Must never collide with the scheduled_YYYYMMDD_1530ET pattern.
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

_EASTERN = ZoneInfo("America/New_York")


def _stamp_mtime_on_trading_day(path: Path, trading_day: str) -> None:
    """Set path's mtime to noon Eastern on trading_day (see _mtime_matches_trading_day)."""
    stamp = datetime.combine(
        datetime.strptime(trading_day, "%Y-%m-%d").date(), time(12, 0), tzinfo=_EASTERN
    ).timestamp()
    os.utime(path, (stamp, stamp))

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


def test_top_up_context_skip_sink_records_unreadable_bare_manual_csv():
    # Codex review ("Include bare manual CSV failures in scan completeness"):
    # a bare manual run (no buy_candidates, no artifacts.orders_csv_path --
    # the exact shape this loader exists to handle, mirroring
    # _write_jul21_manual_midday) has its fills live ONLY in a sibling CSV
    # found via _referenced_orders_csvs' glob fallback. If that CSV fails to
    # read/parse, _read_csv_rows silently returned [] with no signal
    # anywhere -- the caller's completeness flag (skip_sink, threaded from
    # run_dca.py's _compute_auto_derived_prior_allocations) never learned
    # this scan was incomplete, so the bounded reload never triggered even
    # though this ticker's only fill evidence was invisible.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = _write_jul21_manual_midday(base)
        csv_path = run_dir / "submitted_orders.csv"
        # Overwrite with invalid UTF-8 bytes so csv_path.open(encoding="utf-8")
        # raises UnicodeDecodeError -- it must remain a real file so
        # _referenced_orders_csvs' own path.is_file() glob filter still lets
        # it through to _read_csv_rows (a directory of the same name would be
        # filtered out before ever reaching the read/parse attempt).
        csv_path.write_bytes(b"ticker,status\n\xff\xfe,filled\n")

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert ctx == {}
        assert skip_sink, (
            "an unreadable bare manual orders CSV must be recorded in "
            "skip_sink so the caller's completeness flag reflects it"
        )


def test_top_up_context_skip_sink_records_unreadable_manual_run_state():
    # Codex review ("Record manual run-state parse failures in the skip
    # sink"): the run_state*.json read at the top of the scan loop (before
    # the CSV-read/parse handling below it) previously swallowed
    # JSONDecodeError/OSError with a bare `continue` and no skip_sink
    # signal. A run_state under a manual-context-only root (e.g.
    # final_manual_review) that is unreadable or mid-write is exactly as
    # unverifiable as a referenced CSV read failure -- without recording it,
    # scan_had_failures (bool(skip_sink) in run_dca.py) stayed False even
    # though this run's fills, if any, were never actually read. Stamped
    # same-day (see _mtime_matches_trading_day) so the plausible-same-day
    # scoping below doesn't itself exclude it.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = Path(d) / "final_manual_review" / "20260721_1345ET"
        run_dir.mkdir(parents=True)
        run_state_path = run_dir / "run_state.json"
        run_state_path.write_text("{not valid json")
        _stamp_mtime_on_trading_day(run_state_path, JUL21)

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert ctx == {}
        assert skip_sink, (
            "an unreadable/unparseable manual run_state.json must be "
            "recorded in skip_sink so the caller's completeness flag "
            "reflects the incomplete scan"
        )


def test_top_up_context_skip_sink_excludes_historical_unreadable_manual_run_state():
    # Codex review ("Scope parse failures to plausible same-day states"):
    # these roots are scanned recursively (rglob), so an unconditional
    # skip_sink record would let a single permanently corrupted historical
    # run_state (from an unrelated, long-past trading day) poison
    # scan_had_failures forever, forcing an unnecessary bounded reload on
    # every future run regardless of trading day. mtime-scoping, mirroring
    # near_close_dca's core loader, must exclude a file whose mtime falls on
    # a different day than the one being evaluated.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = Path(d) / "final_manual_review" / "20250101_0900ET"
        run_dir.mkdir(parents=True)
        run_state_path = run_dir / "run_state.json"
        run_state_path.write_text("{not valid json")
        _stamp_mtime_on_trading_day(run_state_path, "2025-01-01")

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert ctx == {}
        assert not skip_sink, (
            "a corrupted run_state from an unrelated historical trading day "
            "must not poison this trading day's completeness signal"
        )


def test_mtime_matches_trading_day_is_on_or_after_not_strict_equality():
    # Codex review ("Fail closed on later-modified prior-day artifacts"): an
    # earlier-weekday artifact being backfilled or repaired on a LATER day
    # (e.g. Monday's run_state.json rewritten while a Tuesday run's
    # earlier-week scan reads it) is exactly as unverifiable as a same-day
    # race, but its mtime can never equal Monday -- a strict equality check
    # silently dropped that failure from skip_sink. A file genuinely older
    # than trading_day (real historical garbage) must still be excluded.
    from run_classification import _mtime_matches_trading_day

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "run_state.json"
        path.write_text("{")

        _stamp_mtime_on_trading_day(path, "2026-06-29")
        assert _mtime_matches_trading_day(path, "2026-06-29") is True

        # Rewritten/backfilled the NEXT day -- still plausibly relevant.
        _stamp_mtime_on_trading_day(path, "2026-06-30")
        assert _mtime_matches_trading_day(path, "2026-06-29") is True

        # Genuinely predates the trading day -- real historical garbage.
        _stamp_mtime_on_trading_day(path, "2026-06-28")
        assert _mtime_matches_trading_day(path, "2026-06-29") is False


def test_top_up_context_skip_sink_records_later_day_rewrite_of_earlier_artifact():
    # End-to-end wiring for the >= fix: an earlier-weekday (Jul 21) corrupt
    # run_state that was rewritten/backfilled on a LATER day (mtime Jul 22)
    # must still be recorded in skip_sink when scanned as Jul 21's context --
    # a strict mtime-equality check would have silently dropped it, since a
    # file can never have an mtime equal to a day earlier than when it was
    # last touched.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = Path(d) / "final_manual_review" / "20260721_1345ET"
        run_dir.mkdir(parents=True)
        run_state_path = run_dir / "run_state.json"
        run_state_path.write_text("{not valid json")
        _stamp_mtime_on_trading_day(run_state_path, "2026-07-22")

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert ctx == {}
        assert skip_sink, (
            "a corrupted run_state rewritten a day AFTER trading_day must "
            "still be recorded in skip_sink, not silently dropped"
        )


def test_top_up_context_skip_sink_records_truncated_bare_manual_csv():
    # Codex review ("Reject incomplete bare manual CSV snapshots"): a bare
    # manual CSV read immediately after a writer truncates it (or while its
    # final row is only partially written) commonly returns zero rows, or a
    # row with missing trailing fields filled in as None -- csv.DictReader
    # does not raise for either case, so the exception-only skip_sink fix
    # from the previous round never fires. _read_csv_rows now mirrors the
    # same structural checks (fieldnames is None / final row has a None
    # value) already added to near_close_dca's core CSV readers.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = _write_jul21_manual_midday(base)
        csv_path = run_dir / "submitted_orders.csv"
        # A complete first row, then a truncated final row missing its
        # trailing columns entirely (cut off mid-write, no newline).
        csv_path.write_text(
            "symbol,amount,order_id,status,average_price,quantity\n"
            "MSFT,2.0,6a5fb151-043b-4063-b317-40b666bb4bb7,filled,399.1364,0.0\n"
            "GOOGL,2.0"
        )

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert "GOOGL" not in ctx, "the truncated tail row must not be trusted"
        assert skip_sink, (
            "a truncated final row in a bare manual orders CSV must be "
            "recorded in skip_sink so the caller's completeness flag "
            "reflects the incomplete scan"
        )


def test_top_up_context_skip_sink_records_empty_bare_manual_csv():
    # Codex review ("Reject incomplete bare manual CSV snapshots"): a bare
    # manual CSV observed in the instant right after a truncating open(path,
    # "w") but before even the header was flushed is completely empty (0
    # bytes) -- csv.DictReader returns rows == [] here exactly as it does
    # for a header-only, genuinely empty CSV, so fieldnames is the only
    # reliable signal to tell them apart.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = _write_jul21_manual_midday(base)
        csv_path = run_dir / "submitted_orders.csv"
        csv_path.write_text("")

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert ctx == {}
        assert skip_sink, (
            "a completely empty bare manual orders CSV (no header at all) "
            "must be recorded in skip_sink so the caller's completeness "
            "flag reflects the incomplete scan"
        )


def test_top_up_context_skip_sink_records_missing_referenced_manual_csv():
    # Codex review ("Report missing manual CSVs before filtering them out"):
    # the run_state records an artifacts.orders_csv_path, but that file is
    # missing entirely AND no *orders.csv sibling exists in the run's own
    # directory either. _referenced_orders_csvs filters every candidate out
    # via `not path.is_file()` before the caller's loop body ever runs, so
    # _read_csv_rows(..., skip_sink=skip_sink) -- the read-failure signal
    # fixed two rounds ago -- is never even called. Both the returned
    # context and skip_sink stayed empty, silently reporting the scan as
    # complete while this run's only fill evidence was invisible.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1400ET"
        run_dir.mkdir(parents=True, exist_ok=True)
        # Deliberately do NOT write any CSV at all -- neither the recorded
        # path nor any *orders.csv glob sibling exists in run_dir.
        state = {
            "run_id": "manual_midday_20260721_1400ET",
            "generated_at": "2026-07-21T14:00:00",
            "trading_day": JUL21,
            "broker": "robinhood",
            "manual_run": True,
            "run_origin": "manual",
            "order_submit_status": "submitted",
            "artifacts": {
                "orders_csv_path": str(run_dir / "submitted_orders.csv"),
            },
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "run_id": "manual_midday_20260721_1400ET",
                "stream": "midday_manual",
                "trading_day": JUL21,
                "state_schema_version": STATE_SCHEMA_VERSION,
                "same_day_top_up_context_only": True,
                "valid_scheduled_evidence": False,
                "valid_afternoon_scheduled_evidence": False,
            },
        }
        (run_dir / "run_state.json").write_text(json.dumps(state, indent=2))

        skip_sink: list = []
        ctx = load_same_day_top_up_context(JUL21, base=base, skip_sink=skip_sink)
        assert ctx == {}
        assert skip_sink, (
            "a run_state referencing a completely missing orders CSV (no "
            "basename or glob sibling either) must be recorded in "
            "skip_sink even though _referenced_orders_csvs filtered out "
            "every candidate before _read_csv_rows was ever called"
        )


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


def test_top_up_context_retains_sub_minute_generated_at_ordering():
    # Codex review ("Retain sub-minute generated-at ordering"): two manual
    # artifacts generated in the SAME ET minute but different seconds must
    # not collapse to a tie decided by scan order. The 12:30:05 run is
    # scanned after (lexically sorts after) the 12:30:45 run, but the
    # 12:30:45 fill is the genuinely more recent one and must win.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        earlier_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1230ET_a_earlier"
        later_dir = base.parent / "midday_dca" / "manual_runs" / "20260721_1230ET_b_later"
        earlier_dir.mkdir(parents=True)
        later_dir.mkdir(parents=True)
        (earlier_dir / "run_state.json").write_text(json.dumps({
            "trading_day": JUL21,
            "generated_at": "2026-07-21T12:30:45-04:00",
            "run_classification": _top_up_classification("manual_midday_1230_45"),
        }))
        (earlier_dir / "filled_orders.csv").write_text(
            "ticker,order_id,status,avg_fill_price,quantity_filled,amount_usd\n"
            "MSFT,MID-MSFT-EARLIER,filled,90.0,0.01,0.9\n"
        )
        (later_dir / "run_state.json").write_text(json.dumps({
            "trading_day": JUL21,
            "generated_at": "2026-07-21T12:30:05-04:00",
            "run_classification": _top_up_classification("manual_midday_1230_05"),
        }))
        (later_dir / "filled_orders.csv").write_text(
            "ticker,order_id,status,avg_fill_price,quantity_filled,amount_usd\n"
            "MSFT,MID-MSFT-LATER,filled,100.0,0.01,1.0\n"
        )

        ctx = load_same_day_top_up_context(JUL21, base=base)
        # The 12:30:45 fill (genuinely later) must win, not the 12:30:05 one
        # despite scanning after it (directory name sorts later: "_b_later").
        assert ctx["MSFT"].reference_price == 90.0


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
    # Top up difference is below the broker minimum order size -> skip and $0.
    assert incremental_top_up_amount(2.5, 2.0, 390.0, 399.1364, minimum_order_usd=1.0) == (0.0, "below_broker_minimum_order_size")
    # Binary float subtraction precision: 2.30 - 1.30 in binary floats produces 0.9999999999999998,
    # but cent-quantization must treat it as exactly $1.00 and authorize the order.
    assert incremental_top_up_amount(2.30, 1.30, 390.0, 399.1364, minimum_order_usd=1.0) == (1.0, "incremental_top_up")
    # Non-positive and boolean minimum_order_usd must fail fast.
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        incremental_top_up_amount(3.0, 2.0, 390.0, 399.1364, minimum_order_usd=0.0)
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        incremental_top_up_amount(3.0, 2.0, 390.0, 399.1364, minimum_order_usd=-1.0)
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        incremental_top_up_amount(3.0, 2.0, 390.0, 399.1364, minimum_order_usd=True)
    with pytest.raises(ValueError, match="minimum_order_usd must be a positive number"):
        incremental_top_up_amount(3.0, 2.0, 390.0, 399.1364, minimum_order_usd=False)


# --- No-order outcome vs failure (Jul 27 2026 1:30 PM ET) -------------------


def test_no_candidate_reaches_the_spread_gate_is_not_a_spread_failure():
    """The 1:30 PM ET incident: no ticker qualified on the ladder, so nothing ever
    reached the spread gate. Recording missing_spread_data there blamed the spread
    for an outcome it did not produce."""
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=0, candidates_blocked_missing_spread=0
    )

    assert outcome.run_classification == RUN_CLASSIFICATION_COMPLETED_NO_ORDERS
    assert outcome.order_submit_status == ORDER_SUBMIT_STATUS_NO_ORDERS
    assert outcome.completion_reason == COMPLETION_REASON_NO_LADDER_CANDIDATES
    assert outcome.error_class is None
    assert outcome.is_trading_failure is False


def test_all_candidates_blocked_at_the_gate_records_missing_spread_data():
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=4, candidates_blocked_missing_spread=4
    )

    assert outcome.run_classification == RUN_CLASSIFICATION_COMPLETED_NO_ORDERS
    assert outcome.error_class == ERROR_CLASS_MISSING_SPREAD_DATA
    assert outcome.completion_reason is None
    # Still a safe no-order skip: the run fired, evaluated, and wrote artifacts.
    assert outcome.is_trading_failure is False


def test_partial_spread_availability_does_not_blame_the_spread_for_the_run():
    """Only the unproven rows fail closed; the rest were evaluated and simply did
    not qualify, so the run as a whole is not a missing-spread outcome."""
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=5, candidates_blocked_missing_spread=2
    )

    assert outcome.run_classification == RUN_CLASSIFICATION_COMPLETED_NO_ORDERS
    assert outcome.error_class is None
    assert outcome.completion_reason == COMPLETION_REASON_NO_QUALIFYING_ORDERS
    assert outcome.candidates_blocked_missing_spread == 2
    assert outcome.is_trading_failure is False


def test_placement_failure_after_connector_native_attempt_is_a_failure():
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=3,
        candidates_blocked_missing_spread=0,
        placement_attempted_and_failed=True,
    )

    assert outcome.run_classification == RUN_CLASSIFICATION_PLACEMENT_FAILED
    assert outcome.error_class == RUN_CLASSIFICATION_PLACEMENT_FAILED
    assert outcome.is_trading_failure is True


def test_blocked_count_can_never_exceed_the_rows_that_reached_the_gate():
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=2, candidates_blocked_missing_spread=9
    )

    assert outcome.candidates_blocked_missing_spread == 2
    assert outcome.error_class == ERROR_CLASS_MISSING_SPREAD_DATA


def test_no_order_outcome_run_state_fields_are_complete():
    fields = classify_no_order_run(
        candidates_reaching_spread_gate=0, candidates_blocked_missing_spread=0
    ).as_run_state_fields()

    assert fields["run_classification_outcome"] == RUN_CLASSIFICATION_COMPLETED_NO_ORDERS
    assert fields["orders_submitted_count"] == 0
    assert fields["candidates_reaching_spread_gate"] == 0
    assert fields["candidates_blocked_missing_spread"] == 0
    assert fields["is_trading_failure"] is False


def test_spread_gate_fields_classify_each_row_outcome():
    ok = spread_gate_fields(spread_pct=0.2, max_spread_pct=1.0)
    assert ok == {"spread_ok": True, "spread_pct": 0.2, "spread_status": SPREAD_STATUS_OK}

    unavailable = spread_gate_fields(spread_pct=None, max_spread_pct=1.0)
    assert unavailable["spread_ok"] is False
    assert unavailable["spread_status"] == SPREAD_STATUS_UNAVAILABLE

    too_wide = spread_gate_fields(spread_pct=3.5, max_spread_pct=1.0)
    assert too_wide["spread_ok"] is False
    assert too_wide["spread_status"] == SPREAD_STATUS_NOT_OK


def test_a_row_that_never_reached_the_gate_is_never_marked_spread_ok():
    fields = spread_gate_fields(spread_pct=None, max_spread_pct=1.0, evaluated=False)

    assert fields["spread_ok"] is None
    assert fields["spread_status"] == SPREAD_STATUS_NOT_EVALUATED
    assert row_blocked_for_missing_spread(fields) is False


def test_row_blocked_for_missing_spread_accepts_the_legacy_status():
    assert row_blocked_for_missing_spread({"spread_status": SPREAD_STATUS_UNAVAILABLE})
    # The Jul 27 2026 intraday runs persisted this spelling.
    assert row_blocked_for_missing_spread({"spread_status": SPREAD_STATUS_MISSING_BID_ASK})
    assert not row_blocked_for_missing_spread({"spread_status": SPREAD_STATUS_NOT_OK})
    assert not row_blocked_for_missing_spread(None)


def test_candidates_blocked_before_the_gate_are_not_no_ladder_candidates():
    """They DID qualify on the ladder; a cap/ceiling check stopped them before the
    spread lookup, so nothing carries a spread verdict (Codex review)."""
    outcome = classify_no_order_run(
        candidates_evaluated=3,
        candidates_reaching_spread_gate=0,
        candidates_blocked_missing_spread=0,
    )

    assert outcome.completion_reason == COMPLETION_REASON_BLOCKED_BEFORE_SPREAD_GATE
    assert outcome.error_class is None
    assert outcome.is_trading_failure is False


def test_candidates_evaluated_defaults_to_the_reaching_gate_count():
    """Omitting the count preserves the older two-argument behavior."""
    assert (
        classify_no_order_run(
            candidates_reaching_spread_gate=0, candidates_blocked_missing_spread=0
        ).completion_reason
        == COMPLETION_REASON_NO_LADDER_CANDIDATES
    )


def test_candidates_evaluated_can_never_be_below_the_reaching_gate_count():
    outcome = classify_no_order_run(
        candidates_evaluated=0,
        candidates_reaching_spread_gate=4,
        candidates_blocked_missing_spread=4,
    )

    assert outcome.error_class == ERROR_CLASS_MISSING_SPREAD_DATA


def test_provider_errors_are_not_recorded_as_a_clean_ladder_no_op():
    """Tickers degraded to NO_DATA by a raising provider were not genuinely
    evaluated, so the zero-order result must not read as healthy (Codex review)."""
    outcome = classify_no_order_run(
        candidates_evaluated=0,
        candidates_reaching_spread_gate=0,
        candidates_blocked_missing_spread=0,
        data_errors=3,
    )

    assert outcome.run_classification == RUN_CLASSIFICATION_COMPLETED_NO_ORDERS
    assert outcome.completion_reason == COMPLETION_REASON_NO_LADDER_CANDIDATES
    assert outcome.error_class == ERROR_CLASS_DEGRADED_MARKET_DATA


def test_a_concrete_spread_cause_outranks_the_degraded_data_marker():
    outcome = classify_no_order_run(
        candidates_reaching_spread_gate=2,
        candidates_blocked_missing_spread=2,
        data_errors=5,
    )

    assert outcome.error_class == ERROR_CLASS_MISSING_SPREAD_DATA


def test_no_data_errors_leaves_the_error_class_null():
    outcome = classify_no_order_run(
        candidates_evaluated=0,
        candidates_reaching_spread_gate=0,
        candidates_blocked_missing_spread=0,
        data_errors=0,
    )

    assert outcome.error_class is None


def test_completed_no_order_status_suppresses_the_broker_window_fallback():
    """A run that submitted nothing by design must never absorb unrelated broker
    orders from the attribution window (Codex review)."""
    import run_classification as rc_module

    assert ORDER_SUBMIT_STATUS_NO_ORDERS in _no_submit_statuses_in_fallback_guard(rc_module)


def _no_submit_statuses_in_fallback_guard(rc_module):
    """The literal status set select_scheduled_state_for_reporting excludes.

    Read from the source so the test fails if a status is dropped from the guard,
    which no behavioral fixture would catch without a full write-failure setup.
    """
    import inspect
    import re

    src = inspect.getsource(rc_module.select_scheduled_state_for_reporting)
    guard = re.search(r"oss not in \{(.*?)\}", src, re.S)
    assert guard, "the no-submit guard is no longer a literal set"
    names = re.findall(r"ORDER_SUBMIT_STATUS_[A-Z_]+", guard.group(1))
    return {getattr(rc_module, name) for name in names}

# --- Scheduled-recovery context roots ----------------------------------------
#
# Codex review: "Add the recovery root to watchdog classification". A recovery
# pass writes a dated sibling of the scheduled slot dir it recovered, which sits
# outside every manual root, so its orders reconciled against nothing and
# surfaced as an unexplained broker/log discrepancy.


def _recovery_classification(**overrides):
    rc = {
        "run_origin": "scheduled_recovery",
        "run_type": "midday_dca_live_recovery",
        "stream": "midday_recovery",
        "manual_run": False,
        "run_id": "midday_recovery_20260727_1305ET",
        "trading_day": "2026-07-27",
        "state_schema_version": "midday-live-v2-recovery",
        "valid_scheduled_evidence": False,
        "valid_recovery_context": True,
        "same_day_top_up_context_only": True,
    }
    rc.update(overrides)
    return rc


def _write_recovery_run(base, subdir, order_ids=("REC-1",)):
    """Write a recovery run_state under cron_tracking_root(base)/subdir."""
    run_dir = cron_tracking_root(base) / subdir
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": "midday_recovery_20260727_1305ET",
        "trading_day": "2026-07-27",
        "run_classification": _recovery_classification(),
        "buy_candidates": [
            {"ticker": "SPCX", "status": "filled", "order_id": oid} for oid in order_ids
        ],
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    return run_dir


def test_recovery_context_roots_match_dated_recovery_dirs():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(base, "midday_dca/20260727_1230ET_recovery_1305ET")
        _write_recovery_run(base, "morning_dca/20260727_1030ET_recovery_1105ET")
        # A plain scheduled slot dir must NOT match -- otherwise the whole
        # scheduled tree would be pulled into the context/manual scan.
        _write_recovery_run(base, "midday_dca/20260727_1230ET")

        names = sorted(p.name for p in recovery_context_roots(base))

        assert names == [
            "20260727_1030ET_recovery_1105ET",
            "20260727_1230ET_recovery_1305ET",
        ]


def test_recovery_context_roots_skips_env_overridden_subroots():
    # Codex review: "Honor stream overrides when scanning recovery fallbacks".
    # run_dca._resolve_cross_stream_prior_allocation_roots DROPS the conventional
    # sibling root when its env override is configured, but this function
    # re-added it unconditionally -- so a stale recovery in the old tree could
    # still be merged into the afternoon prior allocation and suppress a top-up.
    for env_var, overridden, kept in (
        ("DCA_INTRADAY_LIVE_STATE_ROOT", "intraday_dca", "midday_dca"),
        ("DCA_MARKET_OPEN_LIVE_STATE_ROOT", "market_open_dca", "morning_dca"),
    ):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "ce16ba6b"
            base.mkdir()
            _write_recovery_run(base, f"{overridden}/20260727_1130ET_recovery_1205ET")
            _write_recovery_run(base, f"{kept}/20260727_1230ET_recovery_1305ET")

            # Without the override BOTH conventional trees are scanned.
            prior = os.environ.pop(env_var, None)
            try:
                assert len(recovery_context_roots(base)) == 2, env_var
                # With it configured, only the non-overridden tree survives.
                os.environ[env_var] = str(Path(d) / "elsewhere")
                names = [p.name for p in recovery_context_roots(base)]
                assert names == ["20260727_1230ET_recovery_1305ET"], env_var
                # A blank value is not a configuration; the tree stays scanned.
                os.environ[env_var] = "   "
                assert len(recovery_context_roots(base)) == 2, env_var
            finally:
                os.environ.pop(env_var, None)
                if prior is not None:
                    os.environ[env_var] = prior


def test_recovery_context_roots_scans_the_override_path_by_default():
    # Codex review: "Scan the active override when stream roots are omitted".
    # Suppressing the conventional tree without substituting the override left
    # every caller that omits stream_roots -- classify_non_scheduled_order_ids()
    # and select_scheduled_state_for_reporting() both default it to None --
    # discovering NEITHER tree, so recovery IDs went unexplained.
    env_var = "DCA_INTRADAY_LIVE_STATE_ROOT"
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        # The relocated tree, outside the conventional cron-tracking layout.
        elsewhere = Path(d) / "relocated" / "intraday"
        moved = elsewhere / "20260727_1430ET_recovery_1432ET"
        moved.mkdir(parents=True)
        (moved / "run_state.json").write_text(
            json.dumps({
                "run_id": "intraday_recovery_20260727_1432ET",
                "trading_day": "2026-07-27",
                "run_classification": _recovery_classification(
                    run_type="intraday_dca_live_recovery",
                    stream="intraday_1430_recovery",
                    state_schema_version="intraday-live-v2-recovery",
                ),
                "buy_candidates": [
                    {"ticker": "EVR", "status": "filled", "order_id": "REC-MOVED"}
                ],
            }),
            encoding="utf-8",
        )
        # A stale copy left behind in the conventional tree.
        _write_recovery_run(base, "intraday_dca/20260727_1130ET_recovery_1205ET", ("REC-STALE",))

        prior = os.environ.pop(env_var, None)
        try:
            os.environ[env_var] = str(elsewhere)

            names = [p.name for p in recovery_context_roots(base)]
            assert names == ["20260727_1430ET_recovery_1432ET"], names

            # The default-argument path -- no stream_roots -- must find the
            # relocated recovery's IDs and must NOT find the stale tree's.
            ids = collect_recovery_context_order_ids(base, "2026-07-27")
            assert "REC-MOVED" in ids
            assert "REC-STALE" not in ids

            # Passing the same root explicitly is idempotent, not duplicated.
            assert len(recovery_context_roots(base, [str(elsewhere)])) == 1
        finally:
            os.environ.pop(env_var, None)
            if prior is not None:
                os.environ[env_var] = prior


def test_already_covered_requires_an_actual_candidate_list():
    # Codex review: "Propagate normalized candidates to fallback coverage". The
    # core loader normalizes a non-list buy_candidates to [] and falls through
    # to its orders-CSV path, but this check used truthiness -- so a populated
    # dict reported "covered" and the run was dropped from BOTH loaders.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = cron_tracking_root(base) / "midday_dca" / "20260727_1230ET_recovery_1305ET"
        run_dir.mkdir(parents=True)

        for malformed in (1, 1.5, "candidates", {"SPCX": 3.0}, True):
            state = {"run_classification": _recovery_classification(), "buy_candidates": malformed}
            assert _already_covered_by_prior_allocation_loader(state, run_dir) is False, malformed
        # A real non-empty list still reports covered; empty and absent do not.
        assert _already_covered_by_prior_allocation_loader(
            {"buy_candidates": [{"ticker": "SPCX", "status": "filled"}]}, run_dir
        ) is True
        assert _already_covered_by_prior_allocation_loader({"buy_candidates": []}, run_dir) is False
        assert _already_covered_by_prior_allocation_loader({}, run_dir) is False


def test_prior_from_rows_sizes_a_partial_by_executed_quantity():
    # Codex review: "Compute partial-fill notional from executed quantity".
    # _read_csv_rows now KEEPS a partially-executed row, but _prior_from_rows
    # still preferred its recorded amount_usd -- the amount REQUESTED. A $10
    # order with $2 executed loaded as $10 of prior allocation and suppressed
    # the remaining $8 top-up, diverging from near_close_dca._orders_csv_fills.
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "filled_orders.csv"
        csv_path.write_text(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            # $10 requested, only 0.2 x $10 = $2 executed.
            "GFUZ,10.00,partially_filled,10.000000,0.200000\n"
            # A genuine full fill still uses its recorded amount.
            "SPCX,3.50,filled,109.989900,0.031821\n",
            encoding="utf-8",
        )

        prior = _prior_from_rows(_read_csv_rows(csv_path))

        assert abs(prior["GFUZ"]["prior_usd"] - 2.00) < 1e-9
        assert abs(prior["SPCX"]["prior_usd"] - 3.50) < 1e-9


def test_prior_from_rows_counts_each_row_as_an_attempt():
    # Codex review: "Count each fallback CSV order as an attempt". Two
    # distinct orders for the SAME ticker in one bare CSV must count as two
    # attempts, not be collapsed into a single per-run count.
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "filled_orders.csv"
        csv_path.write_text(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "GFUZ,ORD-1,2.00,filled,10.0,0.2\n"
            "GFUZ,ORD-2,3.00,filled,10.0,0.3\n"
            "SPCX,ORD-3,3.50,filled,109.99,0.032\n",
            encoding="utf-8",
        )

        prior = _prior_from_rows(_read_csv_rows(csv_path))

        assert prior["GFUZ"]["attempt_count"] == 2
        assert prior["SPCX"]["attempt_count"] == 1


def test_merge_prior_sums_attempt_count_across_merged_sources():
    # A second run contributing a distinct new order for the same ticker
    # must ADD its own attempt count on top, not overwrite or ignore it.
    dst = {}
    _merge_prior(
        dst,
        {"GFUZ": {"prior_usd": 2.0, "reference_price": None, "order_ids": ["ORD-1"], "attempt_count": 1}},
        "run1",
        run_ts=100,
    )
    _merge_prior(
        dst,
        {"GFUZ": {"prior_usd": 3.0, "reference_price": None, "order_ids": ["ORD-2"], "attempt_count": 1}},
        "run2",
        run_ts=200,
    )

    assert dst["GFUZ"].attempt_count == 2
    assert dst["GFUZ"].prior_usd == pytest.approx(5.0)


def test_recovery_root_contains_a_root_level_sibling_csv():
    # Codex review (P1): "Resolve recovery CSVs against the artifact root".
    # A recovery run may write artifacts/run_state.json beside a ROOT-level
    # filled_orders.csv and record it as '../filled_orders.csv' -- in-root and
    # legitimate. Containing against run_state.json's OWN directory rejected it
    # and retried a basename one level down that does not exist, so both
    # allocation loaders omitted real deployed capital while the watchdog
    # collector called its order ID explained.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        recovery_root = (
            cron_tracking_root(base) / "intraday_dca" / "20260727_1430ET_recovery_1432ET"
        )
        (recovery_root / "artifacts").mkdir(parents=True)
        (recovery_root / "filled_orders.csv").write_text(
            "ticker,order_id,state,amount_usd,average_fill_price,quantity\n"
            "EVR,REC-EVR,filled,1.00,336.916900,0.002968\n",
            encoding="utf-8",
        )
        state = {
            "run_id": "intraday_recovery_20260727_1432ET",
            "trading_day": "2026-07-27",
            "run_classification": _recovery_classification(
                run_type="intraday_dca_live_recovery",
                stream="intraday_1430_recovery",
                state_schema_version="intraday-live-v2-recovery",
            ),
            "artifacts": {"orders_csv_path": "../filled_orders.csv"},
        }
        state_dir = recovery_root / "artifacts"
        (state_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")

        assert recovery_root_for(state_dir) == recovery_root
        # The in-root sibling survives containment instead of being rejected.
        kept = _referenced_orders_csvs_within(
            state, state_dir, recovery_root_for(state_dir)
        )
        assert [p.name for p in kept] == ["filled_orders.csv"], kept

        # A path OUTSIDE the recovery root is still rejected.
        outside = Path(d) / "elsewhere"
        outside.mkdir()
        (outside / "other_orders.csv").write_text("ticker,order_id\nAAPL,X\n", encoding="utf-8")
        escaping = dict(state, artifacts={"orders_csv_path": str(outside / "other_orders.csv")})
        assert _referenced_orders_csvs_within(
            escaping, state_dir, recovery_root_for(state_dir)
        ) == []


def test_coverage_check_uses_the_recovery_root_boundary():
    # Codex review: "Use the recovery root when checking loader coverage".
    # Last round moved the core loader and the fallback's CSV resolution onto
    # recovery_root_for(); this site still compared against run_dir, so a
    # legitimate '../filled_orders.csv' from an artifacts/run_state.json was
    # rewritten to a nonexistent artifacts/filled_orders.csv and reported NOT
    # covered. The fallback then re-read the SAME root CSV the core loader had
    # already read, and merge_manual_top_up_context() double-counted the fill.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        recovery_root = (
            cron_tracking_root(base) / "midday_dca" / "20260727_1230ET_recovery_1305ET"
        )
        state_dir = recovery_root / "artifacts"
        state_dir.mkdir(parents=True)
        (recovery_root / "filled_orders.csv").write_text(
            "ticker,order_id,state,amount_usd\nSPCX,REC-1,filled,3.50\n", encoding="utf-8"
        )
        state = {
            "run_classification": _recovery_classification(),
            "artifacts": {"orders_csv_path": "../filled_orders.csv"},
        }

        # The core loader reads this CSV, so the fallback must report COVERED
        # and leave it alone.
        assert _already_covered_by_prior_allocation_loader(state, state_dir) is True

        # A path outside the recovery root is still not covered: the core loader
        # will not follow it, so the fallback must still process the run.
        outside = Path(d) / "elsewhere"
        outside.mkdir()
        (outside / "other_orders.csv").write_text("ticker,order_id\nAAPL,X\n", encoding="utf-8")
        escaping = dict(state, artifacts={"orders_csv_path": str(outside / "other_orders.csv")})
        assert _already_covered_by_prior_allocation_loader(escaping, state_dir) is False


def test_recovery_root_falls_back_to_the_run_dir():
    # No ancestor looks like a recovery directory: keep the narrower boundary
    # rather than walking up into an unrelated tree.
    with tempfile.TemporaryDirectory() as d:
        plain = Path(d) / "scheduled_runs" / "20260727_1530ET"
        plain.mkdir(parents=True)
        assert recovery_root_for(plain) == plain


def test_recovery_optional_fields_are_validated_when_present():
    # Codex review: "Validate optional recovery fields before returning true".
    # The schema constrains scheduled_slot_et / recovery_pass_started_et to
    # HH:MM and the root/cron fields to strings, but this validator returned
    # true without looking, so a schema-aware consumer and this helper
    # disagreed about the same artifact.
    for bad_clock in ("bogus", {}, [], 1430, "14:3", "2:30", "14:30:00", " 14:30", "14:30\n"):
        for field in ("scheduled_slot_et", "recovery_pass_started_et"):
            rc = _recovery_classification(**{field: bad_clock})
            assert validate_recovery_classification(rc) is False, (field, bad_clock)
    for bad_root in ([], {}, 1, True, None):
        for field in ("cron_id", "recovered_slot_artifact_root", "artifact_root"):
            rc = _recovery_classification(**{field: bad_root})
            assert validate_recovery_classification(rc) is False, (field, bad_root)
    # Well-formed optional values, and absence, both still pass.
    assert validate_recovery_classification(
        _recovery_classification(
            scheduled_slot_et="14:30",
            recovery_pass_started_et="14:32",
            cron_id="7be82b97",
            artifact_root="/home/user/workspace/cron_tracking/intraday_dca/x_recovery_y",
        )
    ) is True
    assert validate_recovery_classification(_recovery_classification()) is True


def test_rows_from_buy_candidates_survives_a_malformed_collection():
    # Codex review: "Validate final-review candidate arrays before iterating".
    # `state.get(k, []) or []` returns the raw value for buy_candidates: 1, and
    # iterating it raised TypeError out of load_same_day_top_up_context() --
    # aborting the whole afternoon context scan over one corrupt artifact.
    for malformed in (1, 1.5, "candidates", {"AAPL": 3.0}, True, None):
        assert _rows_from_buy_candidates({"buy_candidates": malformed}) == [], malformed
    assert _rows_from_buy_candidates({}) == []
    # A well-formed list is still converted.
    rows = _rows_from_buy_candidates(
        {"buy_candidates": [{"ticker": "AAPL", "status": "filled", "amount_usd": 3.0}]}
    )
    assert [r["ticker"] for r in rows] == ["AAPL"]
    assert rows[0]["_confirmed_fill"] is True


def test_partial_notional_uses_the_execution_price_not_the_target():
    # Codex review: "Use the executed fill price when sizing partials".
    # _PRICE_COLS leads with target_price/decision_price -- correct for a
    # REFERENCE price, wrong for sizing cash. Multiplying executed quantity by
    # the target sized the row by what we hoped to pay: target 12, fill 10,
    # quantity 0.2 gave $2.40 instead of $2.00, overstating prior allocation.
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "filled_orders.csv"
        csv_path.write_text(
            "ticker,amount_usd,state,target_price,average_fill_price,quantity\n"
            "GFUZ,10.00,partially_filled,12.000000,10.000000,0.200000\n",
            encoding="utf-8",
        )

        prior = _prior_from_rows(_read_csv_rows(csv_path))

        assert abs(prior["GFUZ"]["prior_usd"] - 2.00) < 1e-9, prior["GFUZ"]


def test_broker_state_filled_confirms_a_submitted_row():
    # Codex review: "Let the broker state confirm filled recovery rows".
    # status is what we ASKED for; state is what the broker DID. A row carrying
    # status='submitted' with state='filled' was treated as unconfirmed, which
    # poisoned the ticker's reference price to unknown and forced the
    # conservative $0 manual-review path despite a real fill price and quantity.
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "submitted_orders.csv"
        csv_path.write_text(
            "ticker,status,state,amount_usd,average_fill_price,quantity\n"
            "EVR,submitted,filled,1.00,336.916900,0.002968\n",
            encoding="utf-8",
        )

        rows = _read_csv_rows(csv_path)

        assert len(rows) == 1
        assert rows[0]["_confirmed_fill"] is True
        # A confirmed fill contributes a known reference price.
        assert _prior_from_rows(rows)["EVR"]["reference_price"] == 336.9169


def test_recovery_context_roots_ignores_an_ancestor_override():
    # Codex review: "Keep the fallback for invalid ancestor overrides".
    # _resolve_cross_stream_prior_allocation_roots rejects an override that is a
    # strict ancestor of the tree -- it names the whole deployment, not a stream
    # -- and KEEPS the conventional fallback. Treating it as a valid replacement
    # here suppressed the conventional pattern and then found nothing, so
    # recovery IDs went unexplained.
    env_var = "DCA_INTRADAY_LIVE_STATE_ROOT"
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(base, "intraday_dca/20260727_1430ET_recovery_1432ET", ("REC-CONV",))
        parent = cron_tracking_root(base)

        prior = os.environ.pop(env_var, None)
        try:
            # A STRICT ANCESTOR of the cron-tracking root: invalid, so the
            # conventional tree must still be scanned.
            os.environ[env_var] = str(parent.parent)
            names = [p.name for p in recovery_context_roots(base)]
            assert names == ["20260727_1430ET_recovery_1432ET"], names
            assert "REC-CONV" in collect_recovery_context_order_ids(base, "2026-07-27")

            # EQUAL to the cron-tracking root is ALSO too broad, and this
            # assertion previously read `== []` on the mistaken reasoning that
            # equal is not an ancestor. That carve-out belongs one level down:
            # _resolve_cross_stream_prior_allocation_roots compares against the
            # AFTERNOON root, and the cron root is a strict ancestor of that, so
            # the resolver rejects an override equal to the cron root and keeps
            # the conventional fallback. Accepting it here suppressed
            # intraday_dca/*_recovery_* and then found nothing (Codex review:
            # "Keep the fallback when an override equals the cron root").
            os.environ[env_var] = str(parent)
            assert [p.name for p in recovery_context_roots(base)] == [
                "20260727_1430ET_recovery_1432ET"
            ]
            assert "REC-CONV" in collect_recovery_context_order_ids(base, "2026-07-27")
        finally:
            os.environ.pop(env_var, None)
            if prior is not None:
                os.environ[env_var] = prior


def test_referenced_csvs_resolve_relative_paths_from_the_run_dir():
    # Codex review: "Resolve relative recovery CSV paths from the run
    # directory". Path(recorded) resolved against the PROCESS cwd, so a
    # subdirectory path like 'artifacts/executions.csv' was missed here while
    # near_close_dca's core loader (which joins onto the run dir) found it --
    # the fills reduced a later top-up while the order IDs stayed out of the
    # watchdog exclusion set and were reported as unexplained.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = cron_tracking_root(base) / "intraday_dca" / "20260727_1430ET_recovery_1432ET"
        (run_dir / "artifacts").mkdir(parents=True)
        nested = run_dir / "artifacts" / "executions.csv"
        nested.write_text(
            "ticker,order_id,status,amount_usd\nEVR,REC-EVR,filled,1.00\n", encoding="utf-8"
        )
        state = {
            "run_id": "intraday_recovery_20260727_1432ET",
            "trading_day": "2026-07-27",
            "run_classification": _recovery_classification(
                run_type="intraday_dca_live_recovery",
                stream="intraday_1430_recovery",
                state_schema_version="intraday-live-v2-recovery",
            ),
            "artifacts": {"orders_csv_path": "artifacts/executions.csv"},
        }
        (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")

        found = _referenced_orders_csvs(state, run_dir)
        assert nested in [p for p in found], found

        # Containment still holds: the resolved path is inside the run root, so
        # the constrained variant keeps it rather than filtering it out.
        assert nested in _referenced_orders_csvs_within(state, run_dir, run_dir)

        # And the IDs now reach the watchdog exclusion set.
        assert "REC-EVR" in collect_recovery_context_order_ids(base, "2026-07-27")


def test_fallback_csv_reader_honors_the_recovery_state_column():
    # Codex review: "Filter recovery state in the fallback CSV reader". The
    # state gate was added to near_close_dca._orders_csv_fills but not here, so
    # a recovery discoverable only through the bare-CSV fallback still counted a
    # state='rejected' row as deployed capital.
    #
    # Revised for "Preserve executed partial fills in recovery CSVs": the gate is
    # on EXECUTED QUANTITY, not the state name. A rejected/canceled row that
    # executed nothing is dropped; one that executed real shares before ending is
    # kept, because that is deployed capital and discarding it would let the
    # later run re-buy the full amount.
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "filled_orders.csv"
        csv_path.write_text(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.989900,0.031821\n"
            "EVR,1.00,rejected,250.000000,0\n"
            "DXYZ,1.00,canceled,22.601700,\n"
            "GS,1.00,,1041.181500,-\n"
            # Executed capital despite a non-'filled' state -- both must survive.
            "FLKR,4.00,partially_filled,51.627900,0.040000\n"
            "AMZN,1.00,canceled,231.429900,0.002000\n",
            encoding="utf-8",
        )

        rows = _read_csv_rows(csv_path)

        # Revised again for "Retain nonterminal submitted rows in the fallback
        # reader": only a TERMINAL non-fill state with zero execution is
        # dropped. GS carries a BLANK state, which means the order is still
        # live and may yet fill -- dropping it let a later run see no prior
        # allocation and re-submit the full amount against a live order.
        # Revised again for "Require explicit zero before dropping submitted
        # notionals": only EVR states a definitive zero (quantity=0) and is
        # dropped. DXYZ is canceled with a BLANK quantity -- the artifact does
        # not establish whether it executed, so it stays as unresolved context
        # rather than being assumed to have filled nothing.
        assert [r["ticker"] for r in rows] == ["SPCX", "DXYZ", "GS", "FLKR", "AMZN"]

        by_ticker = {r["ticker"]: r for r in rows}
        # A row kept ONLY because its state is still live is conservative
        # context, never a confirmed fill: it executed nothing, so its logged
        # price must not become a trusted reference.
        for ticker in ("GS", "DXYZ"):
            assert by_ticker[ticker]["_confirmed_fill"] is False, ticker
            assert by_ticker[ticker]["_partial_execution"] is False, ticker
        # Only a FULL fill is confirmed. Revised for "Keep statusless partial
        # executions unconfirmed": FLKR (partially_filled) and AMZN (canceled
        # after executing) moved real capital, so their notionals count, but
        # their remainders were still live or cancelled -- their prices never
        # settled, so neither may serve as a reference price. The earlier
        # version of this test asserted True for both, encoding that bug.
        assert by_ticker["SPCX"]["_confirmed_fill"] is True
        for ticker in ("FLKR", "AMZN"):
            assert by_ticker[ticker]["_confirmed_fill"] is False, ticker
            assert by_ticker[ticker]["_partial_execution"] is True, ticker

        # No state column: the statusless manual-fill shape is unchanged.
        plain = Path(d) / "plain_filled_orders.csv"
        plain.write_text("ticker,amount_usd\nAAPL,5.00\n", encoding="utf-8")
        plain_rows = _read_csv_rows(plain)
        assert [r["ticker"] for r in plain_rows] == ["AAPL"]
        assert plain_rows[0]["_confirmed_fill"] is True


def test_recovery_context_roots_excludes_afternoon_tree():
    # An afternoon recovery is the afternoon run's own repair, never prior
    # context for it.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(base, "ce16ba6b/scheduled_runs/20260727_1530ET_recovery_1605ET")

        assert recovery_context_roots(base) == []


def test_recovery_context_roots_empty_when_no_recovery_dirs():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()

        assert recovery_context_roots(base) == []


def test_top_up_context_roots_includes_recovery_dirs():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(base, "midday_dca/20260727_1230ET_recovery_1305ET")

        roots = top_up_context_roots(base)

        assert any(p.name == "20260727_1230ET_recovery_1305ET" for p in roots)
        # The fixed manual subroots are still returned.
        assert any(p.name == "final_manual_review" for p in roots)


def test_collect_recovery_context_order_ids():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(
            base, "midday_dca/20260727_1230ET_recovery_1305ET", order_ids=("REC-1", "REC-2")
        )

        assert collect_recovery_context_order_ids(base) == {"REC-1", "REC-2"}


def test_classify_non_scheduled_order_ids_separates_manual_and_recovery():
    # The watchdog needs both buckets: an ID in either is classified-and-excluded
    # rather than an unexplained broker/log discrepancy, but a recovery ID must
    # not be reported as manual.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(base, "midday_dca/20260727_1230ET_recovery_1305ET", order_ids=("REC-1",))

        buckets = classify_non_scheduled_order_ids(base)

        assert buckets["recovery_context"] == {"REC-1"}
        assert "REC-1" not in buckets["manual"]


def test_is_top_up_context_state_accepts_recovery_classification():
    # The top-level trading_day is now REQUIRED on a recovery state (Codex
    # review: "Require a top-level trading day on recovery states") -- the
    # allocation loader filters on it, so accepting a state without one made
    # the two consumers disagree about which artifacts exist.
    state = {"trading_day": "2026-07-27", "run_classification": _recovery_classification()}
    assert is_top_up_context_state(state, "2026-07-27") is True
    assert is_top_up_context_state({"run_classification": _recovery_classification()}) is False


def test_is_top_up_context_state_rejects_recovery_missing_positive_flags():
    # A recovery run must positively assert recovery context and disclaim
    # scheduled evidence; failing closed keeps a half-written or misclassified
    # artifact from reducing a later buy.
    for override in (
        {"valid_recovery_context": None},
        {"valid_scheduled_evidence": True},
        {"manual_run": True},
        {"same_day_top_up_context_only": False},
    ):
        state = {"run_classification": _recovery_classification(**override)}
        assert is_top_up_context_state(state, "2026-07-27") is False, override


def test_is_top_up_context_state_still_rejects_plain_scheduled_run():
    state = {
        "run_classification": _recovery_classification(
            run_origin="scheduled", valid_recovery_context=None
        )
    }
    assert is_top_up_context_state(state, "2026-07-27") is False


def test_recovery_state_schema_version_stays_out_of_accepted_versions():
    # The recovery version must never be ingestible as scheduled evidence.
    assert "midday-live-v2-recovery" not in ACCEPTED_STATE_SCHEMA_VERSIONS
    assert RUN_ORIGIN_SCHEDULED_RECOVERY == "scheduled_recovery"
    assert RUN_ORIGIN_SCHEDULED_RECOVERY != "scheduled"


def test_recovery_classification_validates_against_its_schema():
    jsonschema = pytest.importorskip("jsonschema")

    repo_root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo_root / "schemas" / "recovery_run_classification.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft7Validator(schema)

    assert list(validator.iter_errors(_recovery_classification())) == []
    # A scheduled classification, one claiming scheduled evidence, and one
    # claiming the policy recovery_rerun flag must all be rejected.
    assert list(validator.iter_errors(_recovery_classification(run_origin="scheduled")))
    assert list(validator.iter_errors(_recovery_classification(valid_scheduled_evidence=True)))
    assert list(validator.iter_errors(_recovery_classification(recovery_rerun=True)))


def test_scheduled_midday_schema_still_rejects_recovery_classification():
    # The recovery values must stay OUT of the scheduled midday contract, so a
    # scheduled consumer rejects the artifact rather than ingesting it.
    jsonschema = pytest.importorskip("jsonschema")

    repo_root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo_root / "schemas" / "midday_run_classification.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(jsonschema.Draft7Validator(schema).iter_errors(_recovery_classification()))



def test_collect_recovery_context_order_ids_validates_states():
    # Codex review: "Validate states before excluding recovery order IDs". A
    # directory-name match is not evidence: these are glob-matched dated dirs,
    # so a malformed, manual, or scheduled-evidence artifact accidentally
    # written under one must NOT be labelled recovery context and silently
    # excluded from discrepancy reporting.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        root = cron_tracking_root(base) / "midday_dca" / "20260727_1230ET_recovery_1305ET"
        root.mkdir(parents=True)

        def write(name, classification, order_id):
            (root / name).write_text(
                json.dumps({
                    "run_id": "r",
                    "trading_day": "2026-07-27",
                    "run_classification": classification,
                    "buy_candidates": [
                        {"ticker": "SPCX", "status": "filled", "order_id": order_id}
                    ],
                }),
                encoding="utf-8",
            )

        # A scheduled-evidence state must not be laundered into recovery context.
        write(
            "run_state_scheduled.json",
            {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "midday",
                "valid_scheduled_evidence": True,
            },
            "SCHEDULED-1",
        )
        # Nor a state missing the positive recovery assertions.
        write(
            "run_state_partial.json",
            {"run_origin": "scheduled_recovery", "manual_run": False},
            "PARTIAL-1",
        )
        # Nor a manual state (it belongs in the manual bucket, not this one).
        write(
            "run_state_manual.json",
            {
                "run_origin": "manual",
                "manual_run": True,
                "same_day_top_up_context_only": True,
                "valid_scheduled_evidence": False,
            },
            "MANUAL-1",
        )
        # A well-formed recovery state IS collected.
        write("run_state.json", _recovery_classification(), "REC-1")

        ids = collect_recovery_context_order_ids(base)

        assert ids == {"REC-1"}


def test_collect_recovery_context_order_ids_honors_trading_day():
    # A stale prior-day recovery directory must not explain away today's orders.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        _write_recovery_run(base, "midday_dca/20260727_1230ET_recovery_1305ET", order_ids=("REC-1",))

        assert collect_recovery_context_order_ids(base, "2026-07-27") == {"REC-1"}
        assert collect_recovery_context_order_ids(base, "2026-07-28") == set()


# --- The canonical recovery contract -----------------------------------------


def test_validate_recovery_classification_accepts_conforming():
    assert validate_recovery_classification(_recovery_classification()) is True
    assert validate_recovery_classification(_recovery_classification(), "2026-07-27") is True


def test_validate_recovery_classification_rejects_bogus_identity_values():
    # Codex review: "Validate recovery identity values against the contract".
    # Non-blank is not enough -- the values must be the contract's own.
    for override in (
        {"run_type": "bogus"},
        {"state_schema_version": "bogus"},
        {"stream": "midday"},
        {"stream": "afternoon_recovery"},
        {"run_id": "   "},
        {"run_id": None},
    ):
        assert validate_recovery_classification(_recovery_classification(**override)) is False, override


def test_validate_recovery_classification_requires_stream_correspondence():
    # A stream paired with ANOTHER stream's run_type or schema version is
    # rejected, so a recovery artifact cannot borrow a different slot's identity.
    assert validate_recovery_classification(
        _recovery_classification(run_type="morning_dca_live_recovery")
    ) is False
    assert validate_recovery_classification(
        _recovery_classification(state_schema_version="morning-live-v2-recovery")
    ) is False
    # The morning triple, fully consistent, passes.
    assert validate_recovery_classification({
        **_recovery_classification(),
        "stream": "morning_recovery",
        "run_type": "morning_dca_live_recovery",
        "state_schema_version": "morning-live-v2-recovery",
    }) is True


def test_validate_recovery_classification_requires_classification_trading_day():
    # A top-level state trading_day must not substitute for the classification's
    # own, and a mismatch against the expected day is rejected.
    rc = _recovery_classification()
    del rc["trading_day"]
    assert validate_recovery_classification(rc) is False
    assert validate_recovery_classification(_recovery_classification(), "2026-07-28") is False


def test_validate_recovery_classification_rejects_flag_violations():
    for override in (
        {"run_origin": "scheduled"},
        {"manual_run": True},
        {"valid_recovery_context": None},
        {"valid_scheduled_evidence": True},
        {"same_day_top_up_context_only": False},
        {"recovery_rerun": True},
        {"valid_afternoon_scheduled_evidence": True},
        {"use_for_slippage_scheduled_proof": True},
    ):
        assert validate_recovery_classification(_recovery_classification(**override)) is False, override
    assert validate_recovery_classification(None) is False
    assert validate_recovery_classification("nope") is False


def test_recovery_contract_matches_its_schema():
    # The code tables and the schema enums must not drift apart.
    pytest.importorskip("jsonschema")
    repo_root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo_root / "schemas" / "recovery_run_classification.schema.json").read_text(
            encoding="utf-8"
        )
    )
    props = schema["properties"]
    assert tuple(props["stream"]["enum"]) == RECOVERY_STREAMS
    assert sorted(props["run_type"]["enum"]) == sorted(RECOVERY_RUN_TYPES)
    assert sorted(props["state_schema_version"]["enum"]) == sorted(
        RECOVERY_STATE_SCHEMA_VERSIONS
    )


def test_all_three_consumers_share_one_contract():
    # The guard, the pointer validator, and is_top_up_context_state must agree
    # on every artifact: three hand-rolled copies had already drifted apart.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from same_day_pointer import derive_valid_recovery_context

    from near_close_dca import _is_scheduled_recovery_context

    for rc in (
        _recovery_classification(),
        _recovery_classification(run_type="bogus"),
        _recovery_classification(recovery_rerun=True),
        _recovery_classification(valid_scheduled_evidence=True),
    ):
        state = {"trading_day": "2026-07-27", "run_classification": rc}
        expected = is_valid_recovery_context_state(state)
        assert validate_recovery_classification(rc, "2026-07-27") is expected, rc
        assert _is_scheduled_recovery_context(state) is expected, rc
        assert derive_valid_recovery_context(state) is expected, rc
        assert is_top_up_context_state(state) is expected, rc

    # And the state-level marker every consumer must now honour: a
    # contract-failed artifact is never recovery context, however valid its
    # classification looks.
    failed = {
        "trading_day": "2026-07-27",
        "run_classification": _recovery_classification(),
        "artifact_contract_failure": {"reason": "missing artifact"},
    }
    assert is_valid_recovery_context_state(failed) is False
    assert _is_scheduled_recovery_context(failed) is False
    assert derive_valid_recovery_context(failed) is False
    assert is_top_up_context_state(failed) is False


def test_recovery_state_day_must_match_classification_day():
    # A state accepted on today's top-level day whose classification names a
    # different day is rejected, so both identities must agree.
    assert is_valid_recovery_context_state({
        "trading_day": "2026-07-27",
        "run_classification": _recovery_classification(trading_day="2026-07-24"),
    }) is False
    assert is_valid_recovery_context_state({
        "trading_day": "2026-07-27",
        "run_classification": _recovery_classification(),
    }) is True


def test_recovery_context_roots_honors_explicit_stream_roots():
    # Codex review: "Honor configured stream roots when discovering recoveries".
    # A relocated stream root (configured artifact_paths.live_run_state_root, or
    # DCA_INTRADAY_LIVE_STATE_ROOT / DCA_MARKET_OPEN_LIVE_STATE_ROOT) lives
    # outside the cron-tracking tree entirely.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        relocated = Path(d) / "elsewhere" / "intraday_dca"
        recovery_dir = relocated / "20260727_1130ET_recovery_1205ET"
        recovery_dir.mkdir(parents=True)
        (recovery_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "r",
                "trading_day": "2026-07-27",
                "run_classification": _recovery_classification(),
                "buy_candidates": [
                    {"ticker": "SPCX", "status": "filled", "order_id": "RELOCATED-1"}
                ],
            }),
            encoding="utf-8",
        )

        # Not found by the conventional layout alone.
        assert recovery_context_roots(base) == []
        assert collect_recovery_context_order_ids(base) == set()

        # Found once the configured stream root is supplied.
        assert recovery_dir in recovery_context_roots(base, [relocated])
        assert collect_recovery_context_order_ids(base, None, [relocated]) == {"RELOCATED-1"}
        assert classify_non_scheduled_order_ids(base, None, [relocated])[
            "recovery_context"
        ] == {"RELOCATED-1"}


def test_recovery_context_roots_accepts_a_recovery_dir_directly():
    # A caller may pass the recovery directory itself rather than its parent.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        recovery_dir = Path(d) / "elsewhere" / "20260727_1130ET_recovery_1205ET"
        recovery_dir.mkdir(parents=True)

        assert recovery_context_roots(base, [recovery_dir]) == [recovery_dir]


def test_recovery_context_roots_dedupes_overlapping_roots():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = _write_recovery_run(base, "midday_dca/20260727_1230ET_recovery_1305ET")
        stream_root = cron_tracking_root(base) / "midday_dca"

        roots = recovery_context_roots(base, [stream_root])

        assert roots.count(run_dir) == 1
        assert len(roots) == 1


def test_validate_recovery_classification_survives_unhashable_stream():
    # Codex review: "Reject unhashable recovery stream values". Persisted JSON
    # can hold a list/object here; an unhashable dict key would raise TypeError
    # and abort the afternoon guard, the pointer validator, and the watchdog
    # collector instead of failing closed.
    for bad in ([], {}, ["midday_recovery"], {"a": 1}, 3, None, True):
        assert validate_recovery_classification(_recovery_classification(stream=bad)) is False, bad


def test_recovery_schema_binds_the_identity_triple():
    # Codex review: "Couple recovery identity fields in the schema". Three
    # independent enums let a cross-stream combination validate, so a
    # schema-only consumer would accept what every in-repo consumer rejects.
    jsonschema = pytest.importorskip("jsonschema")

    repo_root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo_root / "schemas" / "recovery_run_classification.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft7Validator(schema)

    # Every conforming triple validates against the schema AND the code.
    for stream, (run_type, version) in RECOVERY_STREAM_CONTRACTS.items():
        rc = _recovery_classification(
            stream=stream, run_type=run_type, state_schema_version=version
        )
        assert list(validator.iter_errors(rc)) == [], stream
        assert validate_recovery_classification(rc) is True, stream

    # Every cross-stream mismatch is rejected by BOTH, so the schema and the
    # validator can never disagree about an artifact's identity.
    for stream, (run_type, version) in RECOVERY_STREAM_CONTRACTS.items():
        for other, (other_type, other_version) in RECOVERY_STREAM_CONTRACTS.items():
            # Skip same-contract pairs: the four intraday streams (bare plus the
            # three slot-qualified ones) deliberately SHARE one run_type and
            # schema version, so pairing them is not a mismatch.
            if (other_type, other_version) == (run_type, version):
                continue
            for rc in (
                _recovery_classification(
                    stream=stream, run_type=other_type, state_schema_version=version
                ),
                _recovery_classification(
                    stream=stream, run_type=run_type, state_schema_version=other_version
                ),
            ):
                assert list(validator.iter_errors(rc)), (stream, other)
                assert validate_recovery_classification(rc) is False, (stream, other)


def test_load_same_day_top_up_context_honors_configured_stream_roots():
    # Codex review (P1): "Thread configured roots into top-up loading". The
    # earlier fix reached the watchdog collector but NOT this loader, which is
    # what actually feeds the top-up guard. A recovery whose fills live only in
    # a bare sibling CSV under a relocated stream root was therefore missed by
    # the core loader AND this fallback, so the later run treated the ticker as
    # untouched and allocated the full amount again.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        relocated = Path(d) / "elsewhere" / "midday_dca"
        recovery_dir = relocated / "20260727_1230ET_recovery_1305ET"
        recovery_dir.mkdir(parents=True)
        # A run_state with NO buy_candidates and no artifacts.orders_csv_path,
        # so only this loader can pick the fills up (the bare-CSV shape).
        (recovery_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "midday_recovery_20260727_1305ET",
                "trading_day": "2026-07-27",
                "generated_at": "2026-07-27T13:05:00-04:00",
                "run_classification": _recovery_classification(),
            }),
            encoding="utf-8",
        )
        (recovery_dir / "filled_orders.csv").write_text(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.989900,0.031821\n",
            encoding="utf-8",
        )

        # Invisible with the conventional roots alone.
        assert load_same_day_top_up_context("2026-07-27", base) == {}

        # Picked up once the resolved stream root is passed through.
        ctx = load_same_day_top_up_context("2026-07-27", base, stream_roots=[relocated])

        assert "SPCX" in ctx
        assert ctx["SPCX"].prior_usd == 3.50
        assert ctx["SPCX"].reference_price == 109.9899


def test_recorded_context_flags_agree_with_the_runtime_validator():
    """Every committed record's context flags must match what the code decides.

    Ten separate copies of the recovery-classification flags were corrected one
    review round at a time during PR #216, each found by a human reader rather
    than by a check: the same artifact is described in config/run_classification.json,
    in two collections inside config/scheduled_tasks.json, and in
    docs/schedule_manifest.md, with nothing keeping them consistent. This walks
    every record in both configs that declares a run_origin and asserts its
    valid_recovery_context agrees with is_top_up_context_state() applied to that
    record's OWN origin -- turning "you corrected N of N+1 copies" from a review
    finding into a test failure.

    A record marked residual_only is expected to DISAGREE, because that marker
    exists precisely to record an artifact the runtime rejects; asserting the
    negative keeps residual_only from becoming a way to opt out of the check.
    """
    repo_root = Path(__file__).resolve().parents[2]
    checked = 0
    for rel in ("config/run_classification.json", "config/scheduled_tasks.json"):
        cfg = json.loads((repo_root / rel).read_bytes())
        for name, record, flags in _records_with_context_flags(cfg):
            origin = record.get("run_origin")
            claimed = flags.get("valid_recovery_context")
            if claimed is None:
                continue
            # What the RUNTIME would say for an artifact with this origin. Only
            # 'manual' and 'scheduled_recovery' can ever be top-up context; any
            # other origin (notably 'manual_live_recovery_rerun') is rejected
            # outright, so a record claiming true is contradicting the loaders.
            runtime_can_load = origin in ("manual", RUN_ORIGIN_SCHEDULED_RECOVERY)
            where = f"{rel}:{name}"
            if record.get("residual_only") is True or flags.get("residual_only") is True:
                assert claimed is False, (
                    f"{where} is residual_only but claims valid_recovery_context=true"
                )
                checked += 1
                continue
            if not runtime_can_load:
                assert claimed is False, (
                    f"{where} claims valid_recovery_context=true but run_origin={origin!r} "
                    f"is rejected by is_top_up_context_state()"
                )
                # The SIBLING eligibility flags must agree too. Correcting only
                # valid_recovery_context left same_day_top_up_context_only,
                # topup_context_eligible, and later_stream_topup_context still
                # declaring the artifact usable, so a consumer keying on any of
                # them got the opposite answer from the runtime -- the same
                # "N of N+1 copies" failure one level down (codex review:
                # "Clear the remaining rerun context flags"). Both spellings of
                # the top-up flag are checked because both are in use.
                for sibling in _CONTEXT_ELIGIBILITY_FLAGS:
                    assert flags.get(sibling) is not True, (
                        f"{where} sets {sibling}=true but run_origin={origin!r} "
                        f"is rejected by is_top_up_context_state()"
                    )
            # The PROSE contract has to agree too. Two rounds running, the flags
            # were corrected while a valid_only_as string one line away still
            # told a consumer the artifact was usable context (codex review:
            # "Mark the remaining rerun descriptions as ineligible context").
            # Convention: when the record is not loadable, valid_only_as must
            # SAY so rather than describing what the pass was for.
            if claimed is False:
                prose = record.get("valid_only_as")
                if isinstance(prose, str) and prose.strip():
                    assert prose.lstrip().upper().startswith("NOT "), (
                        f"{where} has valid_recovery_context=false but its "
                        f"valid_only_as asserts usable context: {prose[:80]!r}"
                    )
            checked += 1
    assert checked >= 6, f"expected the committed recovery/rerun records, found {checked}"


# Flags a consumer reads to decide whether an artifact may be applied as
# same-day top-up context. Deliberately excludes
# same_day_top_up_context_for_later_runs, which a NORMAL scheduled run sets to
# mean "later runs should count my fills" -- that travels the scheduled path,
# not is_top_up_context_state(), and is legitimately true there.
_CONTEXT_ELIGIBILITY_FLAGS = (
    "same_day_top_up_context_only",
    "same_day_topup_context_only",
    "topup_context_eligible",
    "later_stream_topup_context",
    "loadable_by_afternoon_guard",
)


def _records_with_context_flags(node, name="<root>"):
    """Yield (name, record, flags) for every dict carrying run_origin + context flags.

    Walks nested collections (recovery_context_run_outcomes.runs, the intraday
    events/recovery_pass/rerun_pass blocks) rather than only top-level records,
    since that nesting is exactly where the stale copies kept hiding.
    """
    if isinstance(node, dict):
        flags = node.get("flags") if isinstance(node.get("flags"), dict) else node
        if node.get("run_origin") is not None and "valid_recovery_context" in flags:
            yield name, node, flags
        for key, value in node.items():
            yield from _records_with_context_flags(value, f"{name}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _records_with_context_flags(value, f"{name}[{i}]")


def test_recorded_recovery_records_satisfy_the_contract():
    # Codex review: "Use a recovery stream accepted by the recovery contract".
    # The Jul 27 2:32 PM intraday pass persisted stream='intraday_1430_recovery'
    # -- the intraday task fires at 11:30/13:30/14:30, so its recovery stream is
    # slot-qualified. The contract accepted only the bare 'intraday_recovery',
    # so it REJECTED a real recorded artifact: its fills would not have offset
    # the 3:30 run and the watchdog could not have classified its order IDs.
    #
    # This walks the actual committed records rather than a fixture, so a future
    # recovery recorded with a stream the contract does not accept fails here.
    repo_root = Path(__file__).resolve().parents[2]
    cfg = json.loads((repo_root / "config" / "run_classification.json").read_bytes())

    checked = 0
    residual = 0
    for name, rec in cfg.items():
        if not isinstance(rec, dict) or rec.get("run_origin") != RUN_ORIGIN_SCHEDULED_RECOVERY:
            continue
        rc = {
            k: rec[k]
            for k in (
                "run_origin", "run_type", "stream", "manual_run",
                "trading_day", "run_id", "state_schema_version",
            )
            if k in rec
        }
        rc.update(rec.get("flags", {}))
        if rec.get("residual_only") is True:
            # A residual record deliberately MIRRORS an artifact that fails the
            # contract (the 2:32 EVR pass, whose persisted classification omits
            # same_day_top_up_context_only). Asserting the negative matters as
            # much as the positive: residual_only must not become a way to
            # exempt a record from the walk, so a record marked residual that
            # actually conforms fails here and the marker has to be removed.
            assert not validate_recovery_classification(rc), (
                f"{name} is marked residual_only but SATISFIES the contract; "
                f"drop residual_only and restore its context flags: {rc}"
            )
            residual += 1
            continue
        assert validate_recovery_classification(rc), f"{name} is not accepted by the contract: {rc}"
        checked += 1

    # Guard against the walk silently matching nothing. Both counters are
    # checked: `checked` alone would pass if every record were marked residual,
    # and the total catches the walk matching no records at all.
    assert checked >= 1, f"expected a conforming recorded recovery pass, found {checked}"
    assert residual >= 1, "expected the recorded residual recovery record"
    assert checked + residual >= 2, f"expected the recorded recovery passes, found {checked + residual}"


def test_intraday_recovery_slots_are_accepted():
    for slot in INTRADAY_RECOVERY_SLOTS:
        rc = _recovery_classification(
            stream=f"intraday_{slot}_recovery",
            run_type="intraday_dca_live_recovery",
            state_schema_version="intraday-live-v2-recovery",
        )
        assert validate_recovery_classification(rc) is True, slot
    # A slot the intraday task does not fire is not a recovery stream.
    assert validate_recovery_classification(
        _recovery_classification(
            stream="intraday_0935_recovery",
            run_type="intraday_dca_live_recovery",
            state_schema_version="intraday-live-v2-recovery",
        )
    ) is False


def test_recovery_state_rejects_dry_runs():
    # Codex review discussion_r3660101905: a simulated run moved no capital, so
    # its "fills" are not context and its order IDs are not real broker orders.
    # near_close_dca's allocation loader already skips dry runs; every consumer
    # must agree.
    state = {"trading_day": "2026-07-27", "run_classification": _recovery_classification()}
    assert is_valid_recovery_context_state(state) is True
    assert is_valid_recovery_context_state({**state, "dry_run": True}) is False
    # Only an explicit True is a dry run.
    assert is_valid_recovery_context_state({**state, "dry_run": False}) is True


def test_recovery_state_compares_top_level_day_even_when_expected_day_given():
    # Codex review discussion_r3660101876: with trading_day supplied the
    # top-level state day was never inspected, so a state whose classification
    # said today but whose own day was stale still passed.
    stale_top_level = {
        "trading_day": "2026-07-24",
        "run_classification": _recovery_classification(),  # says 2026-07-27
    }
    assert is_valid_recovery_context_state(stale_top_level, "2026-07-27") is False
    assert is_top_up_context_state(stale_top_level, "2026-07-27") is False
    # Agreement across all three passes.
    agreeing = {"trading_day": "2026-07-27", "run_classification": _recovery_classification()}
    assert is_valid_recovery_context_state(agreeing, "2026-07-27") is True
    # A blank/non-string top-level day is a malformed identity, not an absence.
    assert is_valid_recovery_context_state({**agreeing, "trading_day": "  "}, "2026-07-27") is False
    assert is_valid_recovery_context_state({**agreeing, "trading_day": 20260727}, "2026-07-27") is False
    # No top-level day at all is now REJECTED, not tolerated (Codex review:
    # "Require a top-level trading day on recovery states"). This assertion
    # previously read `is True`: near_close_dca.load_prior_same_day_allocations
    # filters on the top-level day outright, so accepting a state without one
    # let the watchdog classify its order IDs as explained while the allocation
    # loader omitted the same artifact's capital.
    assert is_valid_recovery_context_state(
        {"run_classification": _recovery_classification()}, "2026-07-27"
    ) is False
    assert is_valid_recovery_context_state(
        {"run_classification": _recovery_classification()}
    ) is False


def test_recovery_order_ids_ignore_csvs_outside_the_run_root():
    # Codex review discussion_r3660200494: a recovery state can carry a stale
    # ABSOLUTE artifacts.orders_csv_path naming a run it was copied from. Those
    # IDs must not be classified as this pass's recovery context, or the
    # watchdog would exclude genuinely unexplained broker orders.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        root = cron_tracking_root(base) / "midday_dca" / "20260727_1230ET_recovery_1305ET"
        root.mkdir(parents=True)

        # An unrelated CSV OUTSIDE the recovery root.
        outside = Path(d) / "elsewhere" / "other_run"
        outside.mkdir(parents=True)
        (outside / "submitted_orders.csv").write_text(
            "ticker,order_id\nAAPL,OUTSIDE-1\n", encoding="utf-8"
        )
        # A real CSV beside the state.
        (root / "filled_orders.csv").write_text(
            "ticker,order_id\nSPCX,INSIDE-1\n", encoding="utf-8"
        )
        (root / "run_state.json").write_text(
            json.dumps({
                "run_id": "midday_recovery_20260727_1305ET",
                "trading_day": "2026-07-27",
                "run_classification": _recovery_classification(),
                "artifacts": {"orders_csv_path": str(outside / "submitted_orders.csv")},
            }),
            encoding="utf-8",
        )

        ids = collect_recovery_context_order_ids(base)

        assert "INSIDE-1" in ids
        assert "OUTSIDE-1" not in ids


def test_recovery_order_ids_keep_the_local_basename_fallback():
    # The stale-path basename fallback still resolves the real CSV sitting next
    # to the state that referenced it.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        root = cron_tracking_root(base) / "midday_dca" / "20260727_1230ET_recovery_1305ET"
        root.mkdir(parents=True)
        (root / "submitted_orders.csv").write_text(
            "ticker,order_id\nSPCX,LOCAL-1\n", encoding="utf-8"
        )
        (root / "run_state.json").write_text(
            json.dumps({
                "run_id": "midday_recovery_20260727_1305ET",
                "trading_day": "2026-07-27",
                "run_classification": _recovery_classification(),
                # Stale absolute path; only its basename resolves locally.
                "artifacts": {"orders_csv_path": "/gone/old_run/submitted_orders.csv"},
            }),
            encoding="utf-8",
        )

        assert collect_recovery_context_order_ids(base) == {"LOCAL-1"}


def test_recovery_optional_proof_flags_require_exact_constants():
    # Codex review: "Enforce all optional recovery-proof flags". The schema pins
    # each optional proof flag to an exact constant when present, so a
    # non-boolean is schema-invalid too -- rejecting only a literal True left
    # such an artifact trusted here while a schema consumer refused it.
    # exclude_from_watchdog_scheduled_confirmation is pinned TRUE: a recovery
    # pass claiming it may confirm the scheduled slot ran contradicts the
    # contract outright.
    assert validate_recovery_classification(_recovery_classification()) is True

    for override in (
        {"exclude_from_watchdog_scheduled_confirmation": False},
        {"exclude_from_watchdog_scheduled_confirmation": "true"},
        {"valid_afternoon_scheduled_evidence": True},
        {"valid_afternoon_scheduled_evidence": "false"},
        {"valid_afternoon_scheduled_evidence": 0},
        {"use_for_slippage_scheduled_proof": True},
        {"use_for_slippage_scheduled_proof": "false"},
        {"use_for_slippage_scheduled_proof": 1},
    ):
        assert validate_recovery_classification(_recovery_classification(**override)) is False, override

    # The conforming constants, and absence, both still pass.
    for override in (
        {"exclude_from_watchdog_scheduled_confirmation": True},
        {"valid_afternoon_scheduled_evidence": False},
        {"use_for_slippage_scheduled_proof": False},
    ):
        assert validate_recovery_classification(_recovery_classification(**override)) is True, override


def test_recorded_recovery_records_still_satisfy_the_tightened_flags():
    # The committed records must survive the tightening -- this is the same
    # walk as test_recorded_recovery_records_satisfy_the_contract, re-asserted
    # here so a flag tightening that breaks real data fails loudly.
    repo_root = Path(__file__).resolve().parents[2]
    cfg = json.loads((repo_root / "config" / "run_classification.json").read_bytes())
    checked = 0
    residual = 0
    for name, rec in cfg.items():
        if not isinstance(rec, dict) or rec.get("run_origin") != RUN_ORIGIN_SCHEDULED_RECOVERY:
            continue
        rc = {
            k: rec[k]
            for k in ("run_origin", "run_type", "stream", "manual_run",
                      "trading_day", "run_id", "state_schema_version")
            if k in rec
        }
        rc.update(rec.get("flags", {}))
        if rec.get("residual_only") is True:
            # See test_recorded_recovery_records_satisfy_the_contract: a residual
            # record mirrors an artifact that fails the contract by design, and
            # is asserted to fail rather than merely skipped.
            assert not validate_recovery_classification(rc), f"{name} residual but conforming: {rc}"
            residual += 1
            continue
        assert validate_recovery_classification(rc), f"{name}: {rc}"
        checked += 1
    assert checked >= 1
    assert checked + residual >= 2


def test_recovery_proof_flags_reject_explicit_null():
    # Codex review: "Reject explicit null recovery proof flags". `.get(flag) is
    # not None` made a MISSING key and a persisted JSON null indistinguishable,
    # so null passed while the schema requires the boolean constant whenever the
    # property is present. Key absence is the only exemption.
    for flag in (
        "valid_afternoon_scheduled_evidence",
        "use_for_slippage_scheduled_proof",
        "exclude_from_watchdog_scheduled_confirmation",
    ):
        assert validate_recovery_classification(_recovery_classification(**{flag: None})) is False, flag
        # Absent is still fine.
        rc = _recovery_classification()
        rc.pop(flag, None)
        assert validate_recovery_classification(rc) is True, flag


def test_recovery_rerun_rejects_present_non_false_values():
    # Codex review: "Reject non-boolean recovery-rerun values". The membership
    # test `not in (None, False)` compared by EQUALITY, so 0 and 0.0 passed as
    # boolean false and an explicitly persisted null passed as absence. The
    # schema permits only boolean false when the property is present.
    for value in (None, 0, 0.0, "false", "", [], {}, 1, True):
        rc = _recovery_classification()
        rc["recovery_rerun"] = value
        assert validate_recovery_classification(rc) is False, value
    # Present-and-false and genuinely absent both remain valid.
    ok = _recovery_classification()
    ok["recovery_rerun"] = False
    assert validate_recovery_classification(ok) is True
    absent = _recovery_classification()
    absent.pop("recovery_rerun", None)
    assert validate_recovery_classification(absent) is True


def test_recovery_trading_day_must_be_iso_date():
    # Codex review: "Validate the recovery trading-day format". A non-blank check
    # accepted trading_day='bogus', so a caller that omitted the expected day --
    # the default for collect_recovery_context_order_ids() -- trusted a
    # schema-invalid state and hid its order IDs from watchdog discrepancies.
    # Loop variable named for what these are: the secrets guard in
    # test_config_invariants exempts a date-shaped literal only when a date cue
    # sits near it, and the compact-date fixture below is a malformed-FORMAT
    # case, not an account (Codex review, PR #219).
    for bad_trading_day in ("bogus", "2026/07/27", "20260727", "2026-7-27", "  ", "2026-07-27T00:00:00Z"):
        assert (
            validate_recovery_classification(
                _recovery_classification(trading_day=bad_trading_day)
            )
            is False
        ), bad_trading_day
        state = {"run_classification": _recovery_classification(trading_day=bad_trading_day)}
        assert is_valid_recovery_context_state(state) is False, bad_trading_day
    assert validate_recovery_classification(_recovery_classification()) is True
    # The state's OWN day is format-checked too, not merely compared.
    conforming = _recovery_classification()
    assert is_valid_recovery_context_state(
        {"trading_day": "bogus", "run_classification": conforming}
    ) is False


def test_recovery_trading_day_rejects_surrounding_whitespace():
    # Codex review: "Reject padded recovery trading days". The check ran against
    # day.strip(), so ' 2026-07-27 ' was repaired into validity here while the
    # schema's anchored pattern rejects it -- validation must not normalize
    # malformed metadata. Both the classification day and the state's own
    # top-level day are checked against the PERSISTED value.
    for padded in (" 2026-07-27", "2026-07-27 ", " 2026-07-27 ", "\t2026-07-27", "2026-07-27\n"):
        assert validate_recovery_classification(
            _recovery_classification(trading_day=padded)
        ) is False, padded
        assert validate_recovery_classification(
            _recovery_classification(trading_day=padded), "2026-07-27"
        ) is False, padded
        # A padded TOP-LEVEL day alongside a conforming classification also fails.
        assert is_valid_recovery_context_state(
            {"trading_day": padded, "run_classification": _recovery_classification()}
        ) is False, padded


def test_recovery_dry_run_must_be_boolean_false_when_present():
    # Codex review: "Reject non-boolean dry-run markers". `is True` rejected only
    # the literal boolean, so dry_run=1 / "false" / "true" were accepted as real
    # recovery context here while the allocation loaders -- which test
    # truthiness -- skipped those same states.
    rc = _recovery_classification()
    for value in (True, 1, 1.0, "false", "true", "", 0, 0.0, None, [], {}):
        assert is_valid_recovery_context_state(
            {"trading_day": "2026-07-27", "dry_run": value, "run_classification": rc}
        ) is False, value
    # Present-and-False, and genuinely absent, both remain valid.
    assert is_valid_recovery_context_state(
        {"trading_day": "2026-07-27", "dry_run": False, "run_classification": rc}
    ) is True
    assert is_valid_recovery_context_state(
        {"trading_day": "2026-07-27", "run_classification": rc}
    ) is True


def test_recovery_schema_rejects_whitespace_only_run_id():
    # Codex review: "Require a nonblank recovery run ID in the schema".
    # minLength:1 counted whitespace, so '   ' validated against the schema while
    # validate_recovery_classification() rejected it via run_id.strip().
    jsonschema = pytest.importorskip("jsonschema")
    repo_root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (repo_root / "schemas" / "recovery_run_classification.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft7Validator(schema)
    for blank in ("", " ", "   ", "\t", "\n"):
        rc = _recovery_classification(run_id=blank)
        assert list(validator.iter_errors(rc)), blank
        # The helper and the schema now agree on every one of these.
        assert validate_recovery_classification(rc) is False, blank
    assert list(validator.iter_errors(_recovery_classification())) == []


def test_order_ids_survive_malformed_order_collections():
    # Codex review: "Reject malformed order collections before collecting IDs".
    # `state.get(k, []) or []` returned the raw value for buy_candidates=1, and
    # iterating it raised TypeError out of the whole watchdog collection loop --
    # one corrupt artifact aborted the scan of every other run.
    for bad in (1, 1.5, "abc", {"a": 1}, True):
        assert _order_ids_from_state({"buy_candidates": bad}) == set(), bad
        assert _order_ids_from_state({"orders": bad}) == set(), bad
    # A well-formed collection alongside a malformed one still contributes.
    assert _order_ids_from_state(
        {"buy_candidates": 1, "orders": [{"order_id": "abc123"}]}
    ) == {"abc123"}


def test_fallback_loader_ignores_external_recovery_csv():
    # Codex review (P1): "Keep external recovery CSVs out of the fallback
    # loader". _already_covered_... only decides whether to PROCESS the run;
    # the fallback then read _referenced_orders_csvs unfiltered, so a copied
    # recovery artifact with a stale absolute path and NO same-named local file
    # merged an unrelated CSV's fills into prior allocation.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "ce16ba6b"
        base.mkdir()
        run_dir = cron_tracking_root(base) / "midday_dca" / "20260727_1230ET_recovery_1305ET"
        run_dir.mkdir(parents=True)

        outside = Path(d) / "elsewhere" / "other_run"
        outside.mkdir(parents=True)
        external = outside / "some_other_orders.csv"
        external.write_text(
            "ticker,amount_usd,average_fill_price\nAAPL,99.00,100.0\n", encoding="utf-8"
        )
        # No same-named local file, so the basename fallback finds nothing.
        (run_dir / "run_state.json").write_text(
            json.dumps({
                "run_id": "midday_recovery_20260727_1305ET",
                "trading_day": "2026-07-27",
                "generated_at": "2026-07-27T13:05:00-04:00",
                "run_classification": _recovery_classification(),
                "artifacts": {"orders_csv_path": str(external)},
            }),
            encoding="utf-8",
        )

        ctx = load_same_day_top_up_context("2026-07-27", base)

        assert "AAPL" not in ctx

def test_dedupe_prefers_a_partial_execution_over_a_submission_only_row():
    # Codex review: "Prefer partial executions over duplicate submission rows".
    # The same order can appear as a plain submission row in one sibling CSV and
    # as partially_filled in another (e.g. a stale recorded submitted_orders.csv
    # recovered by basename BEFORE the sibling filled_orders.csv). Preferring
    # only _confirmed_fill kept the submission row, whose amount is what was
    # REQUESTED -- so a $2 partial against a $10 request loaded as $10 of prior
    # allocation and suppressed the remaining $8 top-up.
    submission = {
        "ticker": "EVR",
        "order_id": "ORD-1",
        "notional_usd": "10.00",
        "_confirmed_fill": False,
        "_partial_execution": False,
    }
    partial = {
        "ticker": "EVR",
        "order_id": "ORD-1",
        "notional_usd": "10.00",
        "avg_fill_price": "250.00",
        "quantity_filled": "0.008",
        "_confirmed_fill": False,
        "_partial_execution": True,
    }

    # Either encounter order must resolve to the partial.
    for rows in ([submission, partial], [partial, submission]):
        kept = _dedupe_rows_by_order_id(rows)
        assert len(kept) == 1
        assert kept[0]["_partial_execution"] is True

    # A CONFIRMED fill still outranks a partial, in both encounter orders.
    confirmed = dict(partial, _confirmed_fill=True, _partial_execution=False)
    for rows in ([partial, confirmed], [confirmed, partial]):
        kept = _dedupe_rows_by_order_id(rows)
        assert len(kept) == 1
        assert kept[0]["_confirmed_fill"] is True

    # And the partial is sized by EXECUTED quantity, not the requested amount.
    prior = _prior_from_rows([partial])
    assert abs(prior["EVR"]["prior_usd"] - 2.00) < 1e-9


def test_fallback_reader_keeps_live_submitted_rows_but_drops_terminal_ones():
    # Codex review: "Retain nonterminal submitted rows in the fallback reader".
    # This reader feeds _prior_from_rows, whose job is CONSERVATIVE submitted
    # context. A 'pending'/'queued'/blank-state row may still fill, so dropping
    # it let a later run see no prior allocation and re-submit the full amount
    # against a live order. Only a terminal state with zero execution is dropped.
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "orders.csv"
        csv_path.write_text(
            "ticker,status,state,notional_usd,cumulative_quantity\n"
            "AAPL,submitted,pending,5.00,0\n"
            "MSFT,submitted,queued,6.00,0\n"
            "NVDA,submitted,,7.00,0\n"
            "EVR,submitted,rejected,8.00,0\n"
            "GS,submitted,canceled,9.00,0\n",
            encoding="utf-8",
        )

        rows = _read_csv_rows(csv_path)

        assert [r["ticker"] for r in rows] == ["AAPL", "MSFT", "NVDA"]
        # Live, not filled: conservative context with no trusted reference price.
        for row in rows:
            assert row["_confirmed_fill"] is False, row["ticker"]
            assert row["_partial_execution"] is False, row["ticker"]

        prior = _prior_from_rows(rows)
        assert prior["AAPL"]["reference_price"] is None
        assert abs(prior["AAPL"]["prior_usd"] - 5.00) < 1e-9


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()


def test_a_denied_state_with_orders_is_still_attributable_for_reporting():
    """Denial and attribution are different questions.

    A scheduled run that submits a real order and then raises is denied as
    evidence by both failure handlers -- it did not complete. Rejecting it for
    ATTRIBUTION too made select_scheduled_state_for_reporting discard the whole
    state, so the preserved fills vanished from scheduled fill and slippage
    reporting (Codex review).
    """
    from same_day_pointer import derive_valid_scheduled_evidence

    state = {
        "run_classification": build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z"),
        "scheduled_evidence_denied": True,
        "buy_candidates": [{"ticker": "AAA", "order_id": "o-1", "status": "submitted"}],
    }

    # Not evidence the slot executed...
    assert derive_valid_scheduled_evidence(state) is False
    # ...but its order is still the scheduled slot's to report.
    assert is_scheduled_state(state)


def test_a_denied_state_with_no_orders_is_rejected():
    """The date/holiday-gated stub this check was added for: it says outright
    that the slot never executed and has nothing to attribute."""
    from same_day_pointer import derive_valid_scheduled_evidence

    state = {
        "run_classification": build_scheduled_classification(TRADING_DAY, "2026-07-06T19:20:01Z"),
        "scheduled_evidence_denied": True,
        "buy_candidates": [],
    }

    assert derive_valid_scheduled_evidence(state) is False
    assert not is_scheduled_state(state)


def test_state_root_and_cron_tracking_root_resolve_at_call_time():
    """state_root()/cron_tracking_root() (no explicit base) used to return
    STATE_ROOT/CRON_TRACKING_ROOT -- module constants bound once at import.
    generate_credit_usage_proxy_report.py already resolves the shared
    $DCA_CRON_TRACKING_ROOT override per call (see
    test_cron_tracking_root_honours_the_env_override in
    test_generate_credit_usage_proxy_report.py), so a $DCA_CRON_TRACKING_ROOT
    set after this module's import left every no-base writer here (e.g.
    write_run_artifacts()/pointer updates) writing under the OLD tree while
    that reader looked in the new one -- valid afternoon scheduled evidence
    then appeared to be missing (Codex review, PR #219: "Resolve the
    classification state root at call time")."""
    probe = (
        "import os\n"
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        f"sys.path.insert(1, {str(Path(__file__).resolve().parents[2])!r})\n"
        "import run_classification\n"  # imported BEFORE the override
        "os.environ['DCA_CRON_TRACKING_ROOT'] = os.environ['PROBE_ROOT']\n"
        "print(run_classification.state_root())\n"
        "print(run_classification.cron_tracking_root())\n"
        "print(run_classification.STATE_ROOT)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "DCA_CRON_TRACKING_ROOT"}
    env["PROBE_ROOT"] = "/srv/run-classification-probe"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = out.stdout.splitlines()
    assert lines[0] == "/srv/run-classification-probe/ce16ba6b", out.stdout
    assert lines[1] == "/srv/run-classification-probe", out.stdout
    # STATE_ROOT itself is untouched by the override -- it is still an
    # import-time constant used elsewhere (e.g. its own module docstring
    # references) -- which is exactly why state_root() naming it directly
    # was wrong.
    assert lines[2] == str(Path(__file__).resolve().parents[2] / "cron_tracking" / "ce16ba6b"), out.stdout
