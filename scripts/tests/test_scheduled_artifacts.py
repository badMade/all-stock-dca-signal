"""Tests for scripts/scheduled_artifacts.py.

Runnable with ``pytest scripts/tests`` or
``python scripts/tests/test_scheduled_artifacts.py``.

Covers the scheduled-run artifact contract:
  * run_classification builders emit a non-null object with the required fields
  * validate_run_artifacts fails on null/absent run_classification
  * validate_run_artifacts fails on a missing signals.csv / submitted_orders.csv
  * submitted_orders.csv is written with a header row even when no order was
    submitted, and pending/unconfirmed responses still produce a row
  * the four required files are written at the RUN ROOT on every outcome, and a
    file nested under artifacts/ is reported as a nested-artifact failure
"""

import csv
import json
import sys
from pathlib import Path

import pytest

# Must precede the conftest/scripts imports below: the documented standalone
# invocation (python scripts/tests/test_scheduled_artifacts.py) puts THIS
# directory on sys.path, not the repository root, so importing either before
# the bootstrap raises ModuleNotFoundError (Codex review, PR #219).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import FIXTURE_ACCOUNT_NUMBER  # noqa: E402
from scripts.scheduled_artifacts import (  # noqa: E402
    AFTERNOON_LIVE_CRON_ID,
    AFTERNOON_LIVE_SUPERSEDES_CRON_ID,
    CANONICAL_RUN_SUMMARY_FILENAME,
    COMPLETION_MARKER_FIELDS,
    ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT,
    ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE,
    MORNING_LIVE_CRON_ID,
    ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE,
    PREFLIGHT_CRON_ID,
    PREFLIGHT_RUN_TYPE,
    RUN_STATE_FILENAME,
    SCHEDULED_LIVE_REQUIRED_FILENAMES,
    SIGNALS_CSV_FILENAME,
    SUBMITTED_ORDERS_FIELDS,
    await_finalized_run_artifacts,
    build_afternoon_live_run_classification,
    build_morning_live_run_classification,
    build_preflight_run_classification,
    build_run_summary,
    completion_marker,
    has_planned_orders,
    is_compute_only_terminal_failure,
    mirror_run_classification,
    nested_only_artifacts,
    placement_unreachable_state_fields,
    read_run_state,
    resolve_live_account_number,
    serialize_submitted_orders_csv,
    submitted_order_rows,
    validate_run_artifacts,
    write_scheduled_run_artifacts,
    write_signals_csv,
    write_submitted_orders_csv,
)


def _morning_run_classification_schema():
    schema_path = (
        Path(__file__).resolve().parents[2] / "schemas" / "morning_run_classification.schema.json"
    )
    return json.loads(schema_path.read_bytes())


def test_preflight_classification_is_non_null_object_with_required_fields():
    rc = build_preflight_run_classification(
        trading_day="2026-07-21",
        run_id="morning_preflight_20260721_0945ET",
        valid_scheduled_evidence=True,
    )
    assert rc is not None and isinstance(rc, dict)
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == PREFLIGHT_RUN_TYPE == "morning_dca_preflight"
    assert rc["trades"] is False
    assert rc["scheduled_for_et"] == "09:45"
    assert rc["scheduled_window_start_et"] == "09:40"
    assert rc["scheduled_window_end_et"] == "09:50"
    assert rc["manual_run"] is False
    assert rc["state_schema_version"] == "morning-preflight-v2-machine-complete"
    assert rc["cron_id"] == PREFLIGHT_CRON_ID == "bb4f6ef1"
    assert rc["stream"] == "morning"
    assert rc["valid_afternoon_evidence"] is False
    assert rc["trading_day"] == "2026-07-21"
    assert rc["same_day_required"] is True
    assert rc["valid_scheduled_evidence"] is True
    assert rc["run_id"] and rc["triggered_at_utc"]
    # Codex review r3624039531 (same gap, the preflight builder): every field
    # schemas/morning_run_classification.schema.json unconditionally requires,
    # plus the fields its scheduled-preflight allOf branch additionally
    # requires, must be present -- not just the same-day pointer fields.
    schema = _morning_run_classification_schema()
    assert set(schema["required"]).issubset(rc.keys())
    assert {"scheduled_window_start_et", "scheduled_window_end_et"}.issubset(rc.keys())


def test_morning_live_classification_is_non_null_object_with_required_fields():
    rc = build_morning_live_run_classification(
        trading_day="2026-07-21",
        run_id="morning_live_20260721_1030ET",
        valid_scheduled_evidence=True,
    )
    assert rc is not None and isinstance(rc, dict)
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "morning_dca_live"
    assert rc["trades"] is True
    assert rc["manual_run"] is False
    assert rc["stream"] == "morning"
    assert rc["scheduled_for_et"] == "10:30"
    assert rc["scheduled_window_start_et"] == "10:25"
    assert rc["scheduled_window_end_et"] == "10:35"
    assert rc["cron_id"] == MORNING_LIVE_CRON_ID == "42173154"
    assert rc["execution_broker"] == "robinhood"
    assert rc["account_number"] == "999000111"
    assert rc["preauthorized_direct_place"] is True
    assert rc["valid_morning_evidence"] is True
    assert rc["trading_day"] == "2026-07-21"
    assert rc["same_day_required"] is True
    assert rc["valid_afternoon_evidence"] is False
    assert rc["valid_scheduled_evidence"] is True
    # Codex review r3624039531: the live builder must be a schema-valid
    # drop-in replacement for the producer, not just carry the same-day
    # pointer fields -- every unconditionally-required field, plus every
    # field the scheduled morning_dca_live / v2-preauthorized allOf branches
    # additionally require, must be present.
    schema = _morning_run_classification_schema()
    assert set(schema["required"]).issubset(rc.keys())
    assert {
        "scheduled_window_start_et",
        "scheduled_window_end_et",
        "execution_broker",
        "account_number",
        "preauthorized_direct_place",
        "valid_morning_evidence",
    }.issubset(rc.keys())


def test_afternoon_live_classification_is_non_null_object_with_required_fields():
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22",
        run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    assert rc is not None and isinstance(rc, dict)
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "scheduled_live_test"
    assert rc["manual_run"] is False
    assert rc["stream"] == "afternoon"
    assert rc["scheduled_for_et"] == "15:30"
    assert rc["scheduled_window_start_et"] == "15:25"
    assert rc["scheduled_window_end_et"] == "15:35"
    assert rc["cron_id"] == AFTERNOON_LIVE_CRON_ID == "10ce362c"
    assert rc["supersedes_cron_id"] == AFTERNOON_LIVE_SUPERSEDES_CRON_ID == "ce16ba6b"
    assert rc["run_id"] == "scheduled_20260722_1530ET"
    assert rc["trading_day"] == "2026-07-22"
    assert rc["same_day_required"] is True
    assert rc["valid_scheduled_evidence"] is True
    assert rc["valid_afternoon_scheduled_evidence"] is True
    assert rc["triggered_at_utc"]


def test_afternoon_evidence_defaults_and_derives_from_completed_state():
    # valid_afternoon_scheduled_evidence defaults to valid_scheduled_evidence
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=False,
    )
    assert rc["valid_scheduled_evidence"] is False
    assert rc["valid_afternoon_scheduled_evidence"] is False
    # but can be set independently
    rc2 = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True, valid_afternoon_scheduled_evidence=False,
    )
    assert rc2["valid_scheduled_evidence"] is True
    assert rc2["valid_afternoon_scheduled_evidence"] is False


def test_has_planned_orders_detects_positive_incremental():
    assert has_planned_orders({"buy_candidates": [{"ticker": "A", "incremental_usd": 2.0}]})
    assert has_planned_orders({"buy_candidates": [{"ticker": "A", "planned_amount_usd": 4.0}]})
    assert not has_planned_orders({"buy_candidates": [{"ticker": "A", "incremental_usd": 0.0}]})
    assert not has_planned_orders({"buy_candidates": []})
    assert not has_planned_orders({})


def test_is_compute_only_terminal_failure():
    # planned orders + compute-only error_class + no submission => failure
    state = {
        "buy_candidates": [{"ticker": "A", "incremental_usd": 2.0}],
        "error_class": ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT,
    }
    assert is_compute_only_terminal_failure(state)
    # bare not_submitted with planned orders also fails
    state2 = {
        "buy_candidates": [{"ticker": "A", "incremental_usd": 2.0}],
        "order_submit_status": "not_submitted",
    }
    assert is_compute_only_terminal_failure(state2)
    # an actual placed order is not a failure
    state3 = {
        "buy_candidates": [{"ticker": "A", "incremental_usd": 2.0, "order_id": "o1", "status": "submitted"}],
        "error_class": ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT,
    }
    assert not is_compute_only_terminal_failure(state3)
    # no planned orders is not a failure
    assert not is_compute_only_terminal_failure(
        {"buy_candidates": [], "order_submit_status": "not_submitted"}
    )
    # dry run is exempt
    assert not is_compute_only_terminal_failure(
        {"dry_run": True, "buy_candidates": [{"incremental_usd": 2.0}],
         "error_class": ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT}
    )


def test_partial_unplaced_planned_order_is_compute_only_failure():
    # One candidate placed, another positive planned candidate left at the default
    # not_submitted with no order_id (placement loop stopped after the first
    # success). The whole-run "any submitted order" short-circuit used to accept
    # this; every positive planned row must enter placement.
    state = {
        "order_submit_status": "submitted",
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "order_id": "o1", "status": "submitted"},
            {"ticker": "B", "planned_amount_usd": 3.0, "status": "not_submitted"},
        ],
    }
    assert is_compute_only_terminal_failure(state)
    # Every positive planned row entered placement (submitted / deliberate skip /
    # confirmation-blocked) => not a failure.
    ok = {
        "order_submit_status": "submitted",
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "order_id": "o1", "status": "submitted"},
            {"ticker": "B", "planned_amount_usd": 3.0, "status": "skipped"},
            {"ticker": "C", "planned_amount_usd": 4.0, "status": "not_submitted_confirmation_required"},
        ],
    }
    assert not is_compute_only_terminal_failure(ok)
    # The approved placement_phase_unreachable terminal state (BOTH paired markers,
    # exactly as placement_unreachable_state_fields writes them) leaves rows
    # unplaced but is explicitly valid, not a compute-only failure.
    unreachable = {
        **placement_unreachable_state_fields(),
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "status": "not_submitted"},
            {"ticker": "B", "planned_amount_usd": 3.0, "status": "not_submitted"},
        ],
    }
    assert not is_compute_only_terminal_failure(unreachable)
    # A single unreachable marker must NOT grant the exemption -- both the paired
    # status and error_class are required, else a malformed run bypasses the check.
    half_marked = {
        "error_class": ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE,
        "order_submit_status": "submitted",
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "status": "not_submitted"},
        ],
    }
    assert is_compute_only_terminal_failure(half_marked)
    status_only = {
        "order_submit_status": ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE,
        # error_class missing -> not the approved paired terminal state
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "status": "not_submitted"},
        ],
    }
    assert is_compute_only_terminal_failure(status_only)


def test_placement_unreachable_requires_false_completed_order_evidence():
    # The approved terminal state pairs both markers with
    # valid_completed_order_evidence=False. A state that keeps the markers but
    # claims completed-order evidence (or omits the field) must NOT be exempted.
    base = {
        **placement_unreachable_state_fields(),
        "buy_candidates": [{"ticker": "A", "planned_amount_usd": 2.0, "status": "not_submitted"}],
    }
    assert base["valid_completed_order_evidence"] is False
    assert not is_compute_only_terminal_failure(base)  # approved terminal state
    tampered = dict(base)
    tampered["valid_completed_order_evidence"] = True
    assert is_compute_only_terminal_failure(tampered)
    missing = dict(base)
    del missing["valid_completed_order_evidence"]
    assert is_compute_only_terminal_failure(missing)


def test_mirror_check_ignores_stale_latest_run_summary(tmp_path):
    # A reused same-day recovery dir can still hold a prior attempt's report-generated
    # latest_run_summary.json whose classification differs (e.g. triggered_at_utc).
    # Only the canonical run_summary.json is mirror-checked, so the stale report
    # summary must NOT fail the newly written run_state.
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    # canonical summary mirrors the new state
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_classification": mirror_run_classification({"run_classification": rc})}),
        encoding="utf-8",
    )
    # stale report summary from a prior attempt with a DIVERGENT classification
    stale = dict(rc)
    stale["triggered_at_utc"] = "2000-01-01T00:00:00Z"
    (tmp_path / "latest_run_summary.json").write_text(
        json.dumps({"run_classification": stale}), encoding="utf-8"
    )
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_require_summary_rejects_noncanonical_latest_run_summary(tmp_path):
    # require_summary must require the CANONICAL run_summary.json; a report-only
    # latest_run_summary.json left behind is not sufficient.
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    mirror = mirror_run_classification({"run_classification": rc})
    (tmp_path / "latest_run_summary.json").write_text(
        json.dumps({"run_classification": mirror}), encoding="utf-8"
    )
    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("run_summary.json" in e for e in result.errors)
    # The canonical file clears it.
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_classification": mirror}), encoding="utf-8"
    )
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_require_submitted_orders_rejects_empty_or_bad_header(tmp_path):
    # Existence is not enough: a zero-byte or wrong-header submitted_orders.csv
    # must fail rather than passing as a valid no-order run.
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    # zero-byte -> no header row
    (tmp_path / "submitted_orders.csv").write_text("", encoding="utf-8")
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("submitted_orders.csv header" in e for e in result.errors)
    # wrong header
    (tmp_path / "submitted_orders.csv").write_text("foo,bar\n", encoding="utf-8")
    assert not validate_run_artifacts(tmp_path)
    # correct header (written by the helper) passes
    write_submitted_orders_csv(tmp_path, [])
    assert validate_run_artifacts(tmp_path)


def test_validate_fails_on_partial_unplaced_planned_order(tmp_path):
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=False,
    )
    state = {
        "run_classification": rc,
        "order_submit_status": "submitted",
        "buy_candidates": [
            {"ticker": "A", "planned_amount_usd": 2.0, "order_id": "o1", "status": "submitted"},
            {"ticker": "B", "planned_amount_usd": 3.0, "status": "not_submitted"},
        ],
    }
    (tmp_path / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    write_signals_csv(tmp_path, [{"ticker": "A"}, {"ticker": "B"}])
    write_submitted_orders_csv(tmp_path, [{"ticker": "A", "order_id": "o1", "status": "submitted"}])
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("compute-only terminal state" in e for e in result.errors)


def test_validate_fails_on_compute_only_terminal_failure(tmp_path):
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=False,
    )
    state = {
        "run_classification": rc,
        "buy_candidates": [{"ticker": "A", "incremental_usd": 2.0}],
        "error_class": ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT,
    }
    (tmp_path / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("compute-only terminal state" in e for e in result.errors)


def test_placement_unreachable_is_a_valid_terminal_state(tmp_path):
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=False,
    )
    fields = placement_unreachable_state_fields(detail="fallback runner import failed")
    assert fields["order_submit_status"] == ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE
    assert fields["valid_completed_order_evidence"] is False
    state = {
        "run_classification": rc,
        "buy_candidates": [{"ticker": "A", "incremental_usd": 2.0}],
        **fields,
    }
    (tmp_path / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    # placement_phase_unreachable is not a compute-only failure; it validates
    assert validate_run_artifacts(tmp_path)


def test_validate_fails_when_summary_classification_missing(tmp_path):
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    # summary present but null classification => contract failure
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_id": "x", "run_classification": None}), encoding="utf-8"
    )
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("run_summary.json run_classification is null" in e for e in result.errors)


def test_validate_fails_when_summary_classification_does_not_mirror(tmp_path):
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    divergent = dict(rc)
    divergent["run_origin"] = "manual"
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_classification": divergent}), encoding="utf-8"
    )
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("does not mirror" in e for e in result.errors)


def test_validate_passes_when_summary_mirrors_state(tmp_path):
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_classification": mirror_run_classification({"run_classification": rc})}),
        encoding="utf-8",
    )
    assert validate_run_artifacts(tmp_path)


def test_preflight_summary_without_classification_still_validates(tmp_path):
    # A read-only pre-flight run (require_submitted_orders=False) writes a
    # run_summary.json WITHOUT a run_classification by design; validate_run_artifacts
    # is also the contract validator for those dirs. The summary-mirroring check
    # must be gated to scheduled live runs, so a valid 9:45 preflight whose
    # run_state classification is present still passes the contract even though its
    # summary carries no run_classification.
    rc = build_preflight_run_classification(
        trading_day="2026-07-21",
        run_id="morning_preflight_20260721_0945ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    # preflight writer emits run_summary.json without a run_classification
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_id": "morning_preflight_20260721_0945ET"}), encoding="utf-8"
    )
    result = validate_run_artifacts(tmp_path, require_submitted_orders=False)
    assert result, result.errors


def test_require_summary_fails_when_no_summary_present(tmp_path):
    # A live dir with only run_state.json + signals.csv + submitted_orders.csv and
    # no summary file at all silently passed the mirror check (which skips absent
    # summaries). require_summary=True is what the afternoon live run passes so the
    # missing canonical run_summary.json is detected.
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1530ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "A"}])
    write_submitted_orders_csv(tmp_path, [])
    # Without require_summary the dir passes (mirror check skips the absent summary)
    assert validate_run_artifacts(tmp_path)
    # With require_summary the missing run_summary.json is a contract failure
    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("run_summary.json" in e for e in result.errors)
    # Writing a mirroring run_summary.json clears it
    (tmp_path / "run_summary.json").write_text(
        json.dumps({"run_classification": mirror_run_classification({"run_classification": rc})}),
        encoding="utf-8",
    )
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_validate_fails_on_null_run_classification(tmp_path):
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_id": "x", "run_classification": None}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "BIG"}])
    write_submitted_orders_csv(tmp_path, [])
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("run_classification is null" in e for e in result.errors)


def test_validate_fails_on_absent_run_classification(tmp_path):
    (tmp_path / "run_state.json").write_text(json.dumps({"run_id": "x"}), encoding="utf-8")
    write_signals_csv(tmp_path, [{"ticker": "BIG"}])
    write_submitted_orders_csv(tmp_path, [])
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("run_classification is null or absent" in e for e in result.errors)


def test_validate_fails_on_missing_signals_and_submitted_orders(tmp_path):
    rc = build_morning_live_run_classification(
        trading_day="2026-07-21", run_id="morning_live_20260721_1030ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    result = validate_run_artifacts(tmp_path)
    assert not result
    assert any("missing signals.csv" in e for e in result.errors)
    assert any("missing submitted_orders.csv" in e for e in result.errors)


def test_validate_passes_for_complete_live_artifacts(tmp_path):
    rc = build_morning_live_run_classification(
        trading_day="2026-07-21", run_id="morning_live_20260721_1030ET",
        valid_scheduled_evidence=True,
    )
    (tmp_path / "run_state.json").write_text(
        json.dumps({"run_classification": rc}), encoding="utf-8"
    )
    write_signals_csv(tmp_path, [{"ticker": "BIG", "action": "BUY"}])
    write_submitted_orders_csv(tmp_path, [])
    assert validate_run_artifacts(tmp_path)


def test_submitted_orders_csv_has_header_when_empty(tmp_path):
    path = write_submitted_orders_csv(tmp_path, [])
    assert path.exists()
    rows = list(csv.reader(path.open()))
    assert rows and rows[0] == list(SUBMITTED_ORDERS_FIELDS)
    assert len(rows) == 1  # header only, no data rows


def test_submitted_orders_serializer_matches_writer_bytes(tmp_path):
    rows = [
        {
            "ticker": "AAPL",
            "order_id": "order-1",
            "submitted_amount_usd": 2.5,
            "status": "submitted",
        }
    ]
    expected = serialize_submitted_orders_csv(rows)
    path = write_submitted_orders_csv(tmp_path, rows)
    assert path.read_bytes() == expected


def test_submitted_order_rows_include_pending_and_unconfirmed():
    outcomes = {
        "BIG": {"status": "submitted", "order_id": "o1", "submitted_amount_usd": 2.0},
        "PEND": {"status": "pending", "order_id": "o2", "submitted_amount_usd": 4.0},
        "UNCON": {"status": "unconfirmed", "submitted_amount_usd": 6.0},
        "SKIP": {"status": "skipped"},
        "DRY": {"status": "dry_run"},
    }
    rows = submitted_order_rows(outcomes)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"BIG", "PEND", "UNCON"}
    statuses = {r["ticker"]: r["status"] for r in rows}
    assert statuses["PEND"] == "pending"
    assert statuses["UNCON"] == "unconfirmed"


# --- Root-level artifact set (Jul 27 2026 12:30 / 1:30 incidents) -----------


def _live_run_state():
    return {
        "run_id": "midday_live_20260727_1230ET",
        "trading_day": "2026-07-27",
        "generated_at": "2026-07-27T16:30:00+00:00",
        "broker": "robinhood",
        "dry_run": False,
        "order_submit_status": "no_orders_submitted",
        "completion_reason": "no_ladder_candidates",
        "error_class": None,
        "orders_submitted_count": 0,
        "run_classification": build_morning_live_run_classification(
            trading_day="2026-07-27",
            run_id="midday_live_20260727_1230ET",
            valid_scheduled_evidence=True,
        ),
    }


def test_write_scheduled_run_artifacts_writes_all_four_at_the_root(tmp_path):
    paths = write_scheduled_run_artifacts(tmp_path, _live_run_state())

    for name in SCHEDULED_LIVE_REQUIRED_FILENAMES:
        assert (tmp_path / name).exists(), f"{name} missing at the run root"
    assert set(paths) == {"run_state", "run_summary", "signals", "submitted_orders"}
    assert all(path.parent == tmp_path for path in paths.values())
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_zero_order_run_still_writes_both_csv_headers(tmp_path):
    """A run that evaluated nothing is still a complete artifact, not empty files."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state(), signal_rows=[], order_rows=[])

    for name in ("signals.csv", "submitted_orders.csv"):
        rows = list(csv.reader((tmp_path / name).open(newline="")))
        assert len(rows) == 1, f"{name} should be header-only"
        assert rows[0] and any(col.strip() for col in rows[0]), f"{name} has no header"
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_run_summary_mirrors_run_classification_and_carries_the_no_order_outcome(tmp_path):
    state = _live_run_state()
    write_scheduled_run_artifacts(tmp_path, state)

    summary = json.loads((tmp_path / "run_summary.json").read_bytes())
    assert summary["run_classification"] == state["run_classification"]
    assert summary["order_submit_status"] == "no_orders_submitted"
    assert summary["completion_reason"] == "no_ladder_candidates"
    assert summary["error_class"] is None


def test_build_run_summary_omits_absent_outcome_fields():
    summary = build_run_summary({"run_id": "r1", "run_classification": {"run_origin": "scheduled"}})
    assert "completion_reason" not in summary
    assert summary["run_classification"] == {"run_origin": "scheduled"}


def test_artifacts_written_only_under_a_nested_subfolder_fail_the_contract(tmp_path):
    """Jul 27 2026 1:30 PM ET: all four files landed under artifacts/ and the run
    root was empty, so every path-based consumer saw a slot that never fired."""
    nested = tmp_path / "artifacts"
    write_scheduled_run_artifacts(nested, _live_run_state())

    reported = nested_only_artifacts(tmp_path)
    assert set(reported) == set(SCHEDULED_LIVE_REQUIRED_FILENAMES)

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert all("only at" in err for err in result.errors if "exists" in err)
    assert any("run_state.json exists only at" in err for err in result.errors)


def test_root_level_artifacts_are_not_reported_as_nested(tmp_path):
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "run_state.json").write_text("{}", encoding="utf-8")

    assert nested_only_artifacts(tmp_path) == {}
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_stream_specific_signals_filename_satisfies_the_root_contract(tmp_path):
    write_scheduled_run_artifacts(
        tmp_path, _live_run_state(), signals_filename="midday_signals.csv"
    )

    assert (tmp_path / "midday_signals.csv").exists()
    assert not (tmp_path / "signals.csv").exists()
    assert nested_only_artifacts(tmp_path) == {}
    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_headerless_signals_csv_fails_the_contract(tmp_path):
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "signals.csv").write_text("", encoding="utf-8")

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("no header row" in err for err in result.errors)


def test_a_stale_empty_signals_csv_does_not_invalidate_a_valid_stream_file(tmp_path):
    """A reused/recovery directory can hold a stale empty signals.csv beside a
    valid morning_signals.csv; any recognized name satisfies the contract
    (Codex review)."""
    write_scheduled_run_artifacts(
        tmp_path, _live_run_state(), signals_filename="morning_signals.csv"
    )
    (tmp_path / "signals.csv").write_text("", encoding="utf-8")

    assert validate_run_artifacts(tmp_path, require_summary=True)


def test_all_signals_candidates_headerless_still_fails(tmp_path):
    write_scheduled_run_artifacts(
        tmp_path, _live_run_state(), signals_filename="morning_signals.csv"
    )
    (tmp_path / "signals.csv").write_text("", encoding="utf-8")
    (tmp_path / "morning_signals.csv").write_text("", encoding="utf-8")

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("no header row" in err for err in result.errors)


def test_a_waived_artifact_is_not_failed_by_a_stale_nested_copy(tmp_path):
    """The raw-persistence failure path validates with require_signals=False
    because no signal could be computed; a stale artifacts/signals.csv in a
    reused directory must not fail it (Codex review)."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "signals.csv").unlink()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "signals.csv").write_text("ticker\n", encoding="utf-8")

    assert validate_run_artifacts(tmp_path, require_signals=False, require_summary=True)
    # Still reported when the caller does require it.
    assert not validate_run_artifacts(tmp_path, require_signals=True, require_summary=True)


def test_nested_scan_can_be_narrowed_to_the_required_filenames(tmp_path):
    nested = tmp_path / "artifacts"
    write_scheduled_run_artifacts(nested, _live_run_state())

    assert set(nested_only_artifacts(tmp_path)) == set(SCHEDULED_LIVE_REQUIRED_FILENAMES)
    narrowed = nested_only_artifacts(tmp_path, filenames=["run_state.json"])
    assert set(narrowed) == {"run_state.json"}


def test_a_signals_header_without_a_symbol_column_fails(tmp_path):
    """An arbitrary non-blank header would otherwise pass while the drift detector
    reads no ticker from it, hiding expected-order drift (Codex review)."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "signals.csv").write_text("garbage\n", encoding="utf-8")

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("no symbol column" in err for err in result.errors)


def test_either_recognized_symbol_column_satisfies_the_signals_header(tmp_path):
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    for header in ("ticker,action\n", "symbol,action\n", "Symbol,Action\n"):
        (tmp_path / "signals.csv").write_text(header, encoding="utf-8")
        assert validate_run_artifacts(tmp_path, require_summary=True), header


def test_a_synthetic_outcome_key_does_not_become_the_ticker():
    """A retry placing a SECOND order for a ticker keys that outcome
    ``AAPL:<order-id>`` so it is not lost to a collision. Taking the key as the
    symbol wrote that string into the canonical order row (Codex review)."""
    rows = submitted_order_rows(
        {
            "AAPL": {"status": "submitted", "order_id": "o-1", "submitted_amount_usd": 2.0},
            "AAPL:o-2": {
                "ticker": "AAPL",
                "status": "submitted",
                "order_id": "o-2",
                "submitted_amount_usd": 3.0,
            },
        }
    )

    assert [row["ticker"] for row in rows] == ["AAPL", "AAPL"]
    # Both orders survive; only the key disambiguates them.
    assert sorted(row["order_id"] for row in rows) == ["o-1", "o-2"]


def test_a_corrupt_signals_body_under_a_valid_header_fails_the_contract(tmp_path):
    """Validating only the header accepted a file the drift detector cannot read.

    read_csv_rows() consumes the whole CSV and its wrapper merely logs a detector
    failure, so a valid header over a corrupt body published valid scheduled
    evidence while missing-order drift went unreconciled (Codex review).
    """
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    limit = csv.field_size_limit()
    (tmp_path / "signals.csv").write_text(
        "ticker,action\nAAPL," + "x" * (limit + 1024) + "\n", encoding="utf-8"
    )

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("not readable" in err for err in result.errors)


def test_a_corrupt_submitted_orders_body_fails_the_contract(tmp_path):
    """Same standard for the orders CSV; the detector reads its rows too."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    limit = csv.field_size_limit()
    (tmp_path / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nAAPL," + "x" * (limit + 1024) + "\n",
        encoding="utf-8",
    )

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("not readable" in err for err in result.errors)


def test_a_csv_parser_error_is_a_contract_failure_not_an_exception(tmp_path):
    """A corrupt file whose header field exceeds the CSV field-size limit raised
    csv.Error out of the validator, turning validation itself into another
    scheduled-run exception (Codex review)."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    limit = csv.field_size_limit()
    (tmp_path / "signals.csv").write_text("x" * (limit + 1024) + "\n", encoding="utf-8")

    result = validate_run_artifacts(tmp_path, require_summary=True)
    assert not result
    assert any("not readable" in err for err in result.errors)


def test_a_recognized_sibling_still_satisfies_the_contract_after_a_parser_error(tmp_path):
    write_scheduled_run_artifacts(
        tmp_path, _live_run_state(), signals_filename="morning_signals.csv"
    )
    limit = csv.field_size_limit()
    (tmp_path / "signals.csv").write_text("x" * (limit + 1024) + "\n", encoding="utf-8")

    assert validate_run_artifacts(tmp_path, require_summary=True)


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


def test_a_nested_only_artifact_is_reported_once_not_twice(tmp_path):
    """Naming the nested case and THEN repeating "missing X" for the same file
    gave the caller two errors for one cause and buried the specific one
    (Copilot review)."""
    nested = tmp_path / "artifacts"
    write_scheduled_run_artifacts(nested, _live_run_state())

    result = validate_run_artifacts(tmp_path, require_summary=True)

    assert not result
    for name in SCHEDULED_LIVE_REQUIRED_FILENAMES:
        nested_errs = [e for e in result.errors if e.startswith(f"{name} exists only at")]
        assert len(nested_errs) == 1, name
        # The redundant generic error is gone.
        assert f"missing {name}" not in result.errors
    assert not any(e.startswith(f"missing {CANONICAL_RUN_SUMMARY_FILENAME}") for e in result.errors)


def test_a_genuinely_missing_file_still_reports_missing(tmp_path):
    """Suppression is scoped to files the nested scan explained; anything else
    still gets the plain error."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "signals.csv").unlink()

    result = validate_run_artifacts(tmp_path, require_summary=True)

    assert not result
    assert f"missing {SIGNALS_CSV_FILENAME}" in result.errors


# --- The live account is resolved per call, not bound at import --------------
# This module is a standalone scripts/ helper, so an import that happened before
# the environment was populated used to freeze the unresolved value into every
# artifact built later in the process -- and that value satisfies neither the
# schema's ^[0-9]{6,12}$ account pattern nor its fail-closed form, so the
# blocked/dry-run artifact it was reasoned to be safe for was invalid evidence
# (Codex review, PR #219).

MORNING_LIVE_KWARGS = {
    "trading_day": "2026-07-28",
    "run_id": "morning_live_20260728_1030ET",
    "valid_scheduled_evidence": True,
}


def _validate_morning_live(monkeypatch, account_value):
    jsonschema = pytest.importorskip("jsonschema")
    if account_value is None:
        monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    else:
        monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", account_value)
    rc = build_morning_live_run_classification(**MORNING_LIVE_KWARGS)
    jsonschema.validate(rc, _morning_run_classification_schema())
    return rc


def test_a_populated_environment_is_read_at_call_time(monkeypatch):
    """The staleness case: the variable is set AFTER this module was imported."""
    rc = _validate_morning_live(monkeypatch, FIXTURE_ACCOUNT_NUMBER)
    assert rc["account_number"] == FIXTURE_ACCOUNT_NUMBER
    assert "account_pinning_failed" not in rc


@pytest.mark.parametrize("account_value", [None, "", "   ", "not-an-account", "12345", "0" * 13])
def test_an_unusable_account_emits_the_fail_closed_form(monkeypatch, account_value):
    """No placeholder is ever stamped in: the schema defines a form for this."""
    rc = _validate_morning_live(monkeypatch, account_value)
    assert rc["account_pinning_failed"] is True
    assert "account_number" not in rc


def test_an_explicit_account_argument_still_wins(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", FIXTURE_ACCOUNT_NUMBER)
    rc = build_morning_live_run_classification(**MORNING_LIVE_KWARGS, account_number="123456789")
    assert rc["account_number"] == "123456789"


def test_resolve_returns_none_rather_than_a_placeholder(monkeypatch):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    assert resolve_live_account_number() is None


def test_a_pinning_failure_is_not_counted_as_valid_scheduled_evidence(monkeypatch):
    """A run that could not pin an account placed nothing, so it is not evidence
    that the slot executed. Leaving the caller's valid_scheduled_evidence=True
    beside the failure marker made the credit-usage report -- which rejects only
    an explicit false -- count the blocked slot as a healthy one (Codex review,
    PR #219)."""
    jsonschema = pytest.importorskip("jsonschema")
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    rc = build_morning_live_run_classification(**MORNING_LIVE_KWARGS)

    assert rc["account_pinning_failed"] is True
    assert rc["valid_scheduled_evidence"] is False
    # Pinned to const true by the schema for every stream='morning' document:
    # it identifies the stream, it does not claim the run succeeded.
    assert rc["valid_morning_evidence"] is True
    jsonschema.validate(rc, _morning_run_classification_schema())


def test_a_pinned_run_keeps_the_evidence_flag_it_was_given(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", FIXTURE_ACCOUNT_NUMBER)
    rc = build_morning_live_run_classification(**MORNING_LIVE_KWARGS)
    assert rc["valid_scheduled_evidence"] is True
    assert "account_pinning_failed" not in rc


# --- Finalization grace period (Aug 3 2026 9:35 incident) -------------------
# The 9:35 run was read the instant it fired, when only its raw-input directory
# existed, and the missing run_state/order CSV were reported as a failed slot.
# The run in fact completed normally seconds later. A just-fired run gets a short
# window to finish writing before its artifacts are judged.


class _FakeClock:
    """A monotonic clock that advances only when the code under test sleeps.

    Keeps the grace-period tests deterministic and instant: no test waits on
    real elapsed time, and every sleep the window performs is recorded.
    """

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.slept = []
        self._on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self._on_sleep is not None:
            self._on_sleep(len(self.slept))


def test_a_finalized_run_passes_on_the_first_poll_without_waiting(tmp_path):
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path, require_summary=True, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result and result.ok and result.finalized
    assert result.errors == []
    assert result.polls == 1
    assert clock.slept == [], "a complete run must not be waited on at all"


def test_a_run_still_writing_is_not_failed_but_waited_for(tmp_path):
    """The incident itself: only the raw inputs exist when the check first looks."""
    (tmp_path / "raw_inputs").mkdir()
    (tmp_path / "raw_inputs" / "quotes.json").write_text("{}", encoding="utf-8")
    clock = _FakeClock(
        on_sleep=lambda count: (
            write_scheduled_run_artifacts(tmp_path, _live_run_state()) if count == 3 else None
        )
    )

    result = await_finalized_run_artifacts(
        tmp_path, require_summary=True, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.finalized and result.ok
    assert result.polls == 4, "should re-check after each sleep, not once"
    assert result.waited_seconds == 3.0


def test_a_genuinely_broken_run_still_reports_its_real_errors(tmp_path):
    """The window delays the verdict; it never suppresses one."""
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "submitted_orders.csv").unlink()
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path, grace_seconds=5, require_summary=True, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert not result and not result.ok and not result.finalized
    assert "missing submitted_orders.csv" in result.errors
    assert result.waited_seconds == 5.0, "must not wait past the grace period"


def test_an_empty_directory_fails_after_the_window_like_a_missing_run(tmp_path):
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path, grace_seconds=2, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert not result.ok and not result.finalized
    assert result.completion_marker is None
    assert f"missing {RUN_STATE_FILENAME}" in result.errors


def test_a_zero_grace_validates_once_and_never_sleeps(tmp_path):
    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    (tmp_path / "signals.csv").unlink()
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path, grace_seconds=0, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert not result.ok
    assert result.polls == 1
    assert clock.slept == []


def test_a_missing_completion_marker_is_not_turned_into_a_failure(tmp_path):
    """Fail-open: absence of a terminal marker is 'not yet finalized', not 'bad'.

    A contract-valid run whose writer emits none of COMPLETION_MARKER_FIELDS
    must still report ok=True once the window closes, or the grace period would
    invent a failure validate_run_artifacts never found.
    """
    state = _live_run_state()
    for name in COMPLETION_MARKER_FIELDS:
        state.pop(name, None)
    write_scheduled_run_artifacts(tmp_path, state)
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path, grace_seconds=3, require_summary=True, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.ok and bool(result) and result.errors == []
    assert not result.finalized
    assert result.completion_marker is None
    assert result.waited_seconds == 3.0


def test_the_window_never_overshoots_the_grace_on_a_coarse_poll(tmp_path):
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path, grace_seconds=2.5, poll_seconds=2, sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert clock.slept == [2, 0.5]
    assert result.waited_seconds == 2.5


def test_a_nonpositive_poll_interval_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        await_finalized_run_artifacts(tmp_path, poll_seconds=0)


def test_validation_options_pass_through_to_the_contract(tmp_path):
    """A preflight run waives the order CSV; the window must waive it too."""
    state = _live_run_state()
    write_scheduled_run_artifacts(tmp_path, state)
    (tmp_path / "submitted_orders.csv").unlink()
    clock = _FakeClock()

    result = await_finalized_run_artifacts(
        tmp_path,
        require_submitted_orders=False,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.ok and result.finalized
    assert clock.slept == []


@pytest.mark.parametrize(
    "state, expected",
    [
        ({"completion_reason": "no_ladder_candidates"}, "completion_reason"),
        ({"completion_reason": None, "order_submit_status": "submitted"}, "order_submit_status"),
        ({"completion_reason": "", "status": "ok"}, "status"),
        ({"run_classification_outcome": "scheduled_live"}, "run_classification_outcome"),
        ({"completion_reason": None, "status": None}, None),
        ({}, None),
        (None, None),
        ("run_state.json", None),
    ],
)
def test_completion_marker_reports_the_first_non_empty_terminal_field(state, expected):
    assert completion_marker(state) == expected


def test_read_run_state_returns_none_rather_than_raising(tmp_path):
    assert read_run_state(tmp_path) is None

    (tmp_path / RUN_STATE_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_run_state(tmp_path) is None

    (tmp_path / RUN_STATE_FILENAME).write_text("[1, 2]", encoding="utf-8")
    assert read_run_state(tmp_path) is None, "a non-object run_state is unusable"

    write_scheduled_run_artifacts(tmp_path, _live_run_state())
    assert read_run_state(tmp_path)["run_id"] == "midday_live_20260727_1230ET"


def test_write_submitted_orders_csv_does_not_clobber_a_peer_replacement_during_serialization(
    tmp_path, monkeypatch
):
    """Codex review (finding: "Open the atomic write before serializing
    rows"): ``atomic_open_w`` snapshots the destination's identity at
    ``with`` entry and refuses the final rename if that identity changed by
    the time the write body finishes. ``write_submitted_orders_csv`` used to
    serialize its rows BEFORE opening ``atomic_open_w`` -- unlike
    ``_write_csv`` (``write_signals_csv``'s writer), which opens first and
    serializes directly into the handle -- leaving a window, for as long as
    serialization took, during which an overlapping scheduled invocation
    could replace the destination first. This invocation's snapshot would
    then be taken AFTER that replacement and silently discard it on
    ``os.replace``, with no error.

    Monkeypatches ``serialize_submitted_orders_csv`` to replace the
    destination file as a side effect DURING serialization -- standing in
    for that overlapping peer -- and proves the write now raises the same
    "changed while this write was in progress" error
    ``test_a_destination_replaced_mid_write_is_not_clobbered`` (test_dca_
    fileio.py) proves for an ordinary mid-write replacement, and that the
    peer's file survives untouched.
    """
    import scripts.scheduled_artifacts as scheduled_artifacts
    from dca_fileio import atomic_write_text

    target = tmp_path / "submitted_orders.csv"
    atomic_write_text(target, "original\n")

    real_serialize = scheduled_artifacts.serialize_submitted_orders_csv

    def serialize_then_peer_replaces_destination(rows, *, fieldnames=SUBMITTED_ORDERS_FIELDS):
        serialized = real_serialize(rows, fieldnames=fieldnames)
        # The peer's own write completes in full via its own atomic swap
        # (a NEW inode at the same path, exactly like `write_submitted_
        # orders_csv`'s own `atomic_open_w` would produce -- a plain
        # truncating `open`/`write_text` reuses the SAME inode and would
        # not exercise the identity check this test is proving) WHILE this
        # invocation is still serializing its own rows.
        atomic_write_text(target, "peer's own submitted_orders.csv\n")
        return serialized

    monkeypatch.setattr(
        scheduled_artifacts, "serialize_submitted_orders_csv", serialize_then_peer_replaces_destination
    )

    with pytest.raises(OSError, match="changed while this write was in progress"):
        write_submitted_orders_csv(
            tmp_path, [{"ticker": "AAPL", "order_id": "o1", "status": "submitted"}]
        )

    assert target.read_text(encoding="utf-8") == "peer's own submitted_orders.csv\n"
