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
    STATE_SCHEMA_VERSION,
    build_manual_classification,
    build_scheduled_classification,
    collect_manual_order_ids,
    exclude_manual_orders,
    is_manual_state,
    is_scheduled_state,
    latest_scheduled_run_path,
    manual_run_id,
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
            broker_window_order_ids=["BRK-1"], scheduled_state_write_failed=True,
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
            broker_window_order_ids=["BRK-1", "MAN-9", "BRK-2"],
            scheduled_state_write_failed=True,
        )
        assert sel.ok
        assert sel.used_broker_fallback
        assert sel.order_ids == ["BRK-1", "BRK-2"]  # MAN-9 still excluded


def test_no_fallback_when_not_flagged_write_failure():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        write_run_artifacts(_scheduled_state(order_ids=()), base=base)
        sel = select_scheduled_state_for_reporting(
            TRADING_DAY, base=base,
            broker_window_order_ids=["BRK-1"], scheduled_state_write_failed=False,
        )
        # Valid scheduled state, but no write-failure flag -> no fallback, no orders.
        assert sel.ok
        assert sel.order_ids == []
        assert not sel.used_broker_fallback


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
