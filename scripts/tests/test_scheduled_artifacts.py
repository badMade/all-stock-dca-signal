"""Tests for scripts/scheduled_artifacts.py.

Runnable with ``pytest scripts/tests`` or
``python scripts/tests/test_scheduled_artifacts.py``.

Covers the scheduled-run artifact contract:
  * run_classification builders emit a non-null object with the required fields
  * validate_run_artifacts fails on null/absent run_classification
  * validate_run_artifacts fails on a missing signals.csv / submitted_orders.csv
  * submitted_orders.csv is written with a header row even when no order was
    submitted, and pending/unconfirmed responses still produce a row
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.scheduled_artifacts import (  # noqa: E402
    AFTERNOON_LIVE_CRON_ID,
    AFTERNOON_LIVE_SUPERSEDES_CRON_ID,
    ERROR_CLASS_COMPUTE_ONLY_NO_PLACEMENT,
    ERROR_CLASS_PLACEMENT_PHASE_UNREACHABLE,
    MORNING_LIVE_CRON_ID,
    ORDER_SUBMIT_STATUS_PLACEMENT_UNREACHABLE,
    PREFLIGHT_CRON_ID,
    PREFLIGHT_RUN_TYPE,
    SUBMITTED_ORDERS_FIELDS,
    build_afternoon_live_run_classification,
    build_morning_live_run_classification,
    build_preflight_run_classification,
    has_planned_orders,
    is_compute_only_terminal_failure,
    mirror_run_classification,
    placement_unreachable_state_fields,
    submitted_order_rows,
    validate_run_artifacts,
    write_signals_csv,
    write_submitted_orders_csv,
)


def _morning_run_classification_schema():
    schema_path = (
        Path(__file__).resolve().parents[2] / "schemas" / "morning_run_classification.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


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
    assert rc["cron_id"] == PREFLIGHT_CRON_ID == "morning-dca-0945"
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
    assert rc["account_number"] == "562057042"
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
        run_id="scheduled_20260722_1520ET",
        valid_scheduled_evidence=True,
    )
    assert rc is not None and isinstance(rc, dict)
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "scheduled_live_test"
    assert rc["manual_run"] is False
    assert rc["stream"] == "afternoon"
    assert rc["scheduled_for_et"] == "15:20"
    assert rc["scheduled_window_start_et"] == "15:15"
    assert rc["scheduled_window_end_et"] == "15:35"
    assert rc["cron_id"] == AFTERNOON_LIVE_CRON_ID == "10ce362c"
    assert rc["supersedes_cron_id"] == AFTERNOON_LIVE_SUPERSEDES_CRON_ID == "ce16ba6b"
    assert rc["run_id"] == "scheduled_20260722_1520ET"
    assert rc["trading_day"] == "2026-07-22"
    assert rc["same_day_required"] is True
    assert rc["valid_scheduled_evidence"] is True
    assert rc["valid_afternoon_scheduled_evidence"] is True
    assert rc["triggered_at_utc"]


def test_afternoon_evidence_defaults_and_derives_from_completed_state():
    # valid_afternoon_scheduled_evidence defaults to valid_scheduled_evidence
    rc = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
        valid_scheduled_evidence=False,
    )
    assert rc["valid_scheduled_evidence"] is False
    assert rc["valid_afternoon_scheduled_evidence"] is False
    # but can be set independently
    rc2 = build_afternoon_live_run_classification(
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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
        trading_day="2026-07-22", run_id="scheduled_20260722_1520ET",
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


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
