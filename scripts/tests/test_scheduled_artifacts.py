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
    MORNING_LIVE_CRON_ID,
    PREFLIGHT_CRON_ID,
    PREFLIGHT_RUN_TYPE,
    SUBMITTED_ORDERS_FIELDS,
    build_morning_live_run_classification,
    build_preflight_run_classification,
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
