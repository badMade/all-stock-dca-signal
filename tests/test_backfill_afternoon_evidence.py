"""Contract tests for scripts.backfill_afternoon_evidence (2026-10-20).

Behavior under test: a missed afternoon fire is backfilled as zero-deployment
evidence ONLY when a durable alert exists for it and the supplied broker history both
spans the slot window and shows no order in it; everything is checked before anything
is written; a backfilled state is valid afternoon evidence and says it is a reconstruction.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_risk
from scripts import backfill_afternoon_evidence as bf

COVER = ["--orders-covers-from", "2026-10-13T00:00:00Z", "--orders-covers-through", "2026-10-21T00:00:00Z"]


def _setup(tmp_path: Path, orders=None, alerts=("intraday_dca/alerts/2026-10-13-1430-gate2.txt",)):
    for rel in alerts:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("SLOT ABORT. Failing gate: Gate 2", encoding="utf-8")
    f = tmp_path / "orders.json"
    f.write_text(json.dumps(orders or []), encoding="utf-8")
    return f


def _run(tmp_path, orders_file, *extra, fire="2026-10-13:1430"):
    return bf.main(["--fire", fire, "--orders-json", str(orders_file), *COVER, "--root", str(tmp_path), *extra])


def test_backfill_writes_valid_afternoon_zero_deployment_evidence(tmp_path):
    assert _run(tmp_path, _setup(tmp_path)) == 0
    state = json.loads((tmp_path / "intraday_dca/20261013_1430ET/run_state.json").read_text())
    assert dca_risk.is_afternoon_scheduled_evidence(state)
    assert state["run_classification"]["scheduled_for_et"] == "14:30"
    assert state["gate9_report"]["reconstruction"]["backfilled"] is True
    assert state["gate9_report"]["reconstruction"]["alert_found"] is True


def test_1530_fire_lands_in_scheduled_runs_tree(tmp_path):
    orders = _setup(tmp_path, alerts=("ce16ba6b/scheduled_runs/alerts/2026-10-13-1530-gate2.txt",))
    assert _run(tmp_path, orders, fire="2026-10-13:1530") == 0
    state = json.loads((tmp_path / "ce16ba6b/scheduled_runs/20261013_1530ET/run_state.json").read_text())
    assert dca_risk.is_afternoon_scheduled_evidence(state)


def test_order_inside_window_is_refused_and_nothing_written(tmp_path, capsys):
    orders = _setup(tmp_path, [{"id": "abc", "created_at": "2026-10-13T18:41:00Z"}])  # 14:41 ET
    assert _run(tmp_path, orders) == 2
    assert "needs a human" in capsys.readouterr().out
    assert not (tmp_path / "intraday_dca/20261013_1430ET").exists()


def test_order_outside_window_does_not_block(tmp_path):
    orders = _setup(tmp_path, [{"id": "abc", "created_at": "2026-10-13T16:30:00Z"}])  # 12:30 ET
    assert _run(tmp_path, orders) == 0


def test_missing_alert_is_refused_unless_explicitly_allowed(tmp_path, capsys):
    orders = _setup(tmp_path, alerts=())
    assert _run(tmp_path, orders) == 2
    assert "no durable alert" in capsys.readouterr().out
    assert _run(tmp_path, orders, "--allow-no-alert", "2026-10-13:1430") == 0
    state = json.loads((tmp_path / "intraday_dca/20261013_1430ET/run_state.json").read_text())
    assert state["gate9_report"]["reconstruction"]["alert_found"] is False


def test_broker_history_that_does_not_span_the_window_is_refused(tmp_path, capsys):
    orders = _setup(tmp_path)
    rc = bf.main(["--fire", "2026-10-13:1430", "--orders-json", str(orders), "--root", str(tmp_path),
                  "--orders-covers-from", "2026-10-13T19:00:00Z", "--orders-covers-through", "2026-10-21T00:00:00Z"])
    assert rc == 2
    assert "does not span" in capsys.readouterr().out


def test_all_or_nothing_when_one_fire_fails(tmp_path):
    orders = _setup(tmp_path)  # alert only for 2026-10-13 14:30
    rc = bf.main(["--fire", "2026-10-13:1430", "--fire", "2026-10-14:1430", "--orders-json", str(orders),
                  *COVER, "--root", str(tmp_path)])
    assert rc == 2
    assert not (tmp_path / "intraday_dca/20261013_1430ET").exists()


def test_existing_run_state_is_never_overwritten(tmp_path, capsys):
    orders = _setup(tmp_path)
    existing = tmp_path / "intraday_dca/20261013_1430ET/run_state.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("{}", encoding="utf-8")
    assert _run(tmp_path, orders) == 2
    assert "already exists" in capsys.readouterr().out
    assert existing.read_text() == "{}"


def test_non_afternoon_slot_is_rejected(tmp_path, capsys):
    assert _run(tmp_path, _setup(tmp_path), fire="2026-10-13:1230") == 2
    assert "not an afternoon slot" in capsys.readouterr().out


def test_dry_run_writes_nothing(tmp_path):
    assert _run(tmp_path, _setup(tmp_path), "--dry-run") == 0
    assert not (tmp_path / "intraday_dca/20261013_1430ET").exists()


def test_default_root_comes_from_cron_tracking_root(tmp_path, monkeypatch):
    # cron_tracking_root() returns a str; the script must not assume a Path (found on the first live dry run).
    monkeypatch.setattr(bf, "cron_tracking_root", lambda: str(tmp_path))
    orders = _setup(tmp_path)
    assert bf.main(["--fire", "2026-10-13:1430", "--orders-json", str(orders), *COVER, "--dry-run"]) == 0


def _pointer_day(tmp_path, rel):
    return json.loads((tmp_path / rel).read_text())["trading_day"]


def test_backfill_does_not_move_a_newer_pointer_backwards(tmp_path):
    # Regression for 2026-10-20: the finalizer pointed both streams back at the backfilled Oct 16.
    orders = _setup(tmp_path, alerts=("intraday_dca/alerts/2026-10-13-1430-gate2.txt",))
    newer = tmp_path / "intraday_dca/latest_intraday_run.json"
    newer.write_text(json.dumps({"trading_day": "2026-10-20", "run_id": "intraday_live_20261020_1430ET"}))
    before = newer.read_bytes()
    assert _run(tmp_path, orders) == 0
    assert newer.read_bytes() == before
    assert (tmp_path / "intraday_dca/20261013_1430ET/run_state.json").is_file()


def test_backfill_still_sets_pointer_when_none_or_older_exists(tmp_path):
    orders = _setup(tmp_path)
    assert _run(tmp_path, orders) == 0
    assert _pointer_day(tmp_path, "intraday_dca/latest_intraday_run.json") == "2026-10-13"


def test_restore_newer_pointer_leaves_older_or_same_day_alone(tmp_path):
    from datetime import date
    path = tmp_path / "p.json"
    path.write_text("{}")
    assert bf.restore_newer_pointer(path, json.dumps({"trading_day": "2026-10-13"}).encode(), date(2026, 10, 13)) is False
    assert bf.restore_newer_pointer(path, None, date(2026, 10, 13)) is False
    assert bf.restore_newer_pointer(path, b"not json", date(2026, 10, 13)) is False


@pytest.mark.parametrize(
    ("slot", "alert", "state_dir", "nominal_hhmm"),
    [
        ("1430", "intraday_dca/alerts/2026-10-13-1430-gate2.txt", "intraday_dca/20261013_1430ET", (14, 30)),
        ("1530", "ce16ba6b/scheduled_runs/alerts/2026-10-13-1530-gate2.txt", "ce16ba6b/scheduled_runs/20261013_1530ET", (15, 30)),
    ],
)
def test_backfilled_record_is_stamped_with_the_fire_time_not_the_wall_clock(
    tmp_path, slot, alert, state_dir, nominal_hhmm
):
    """The timestamp dca_risk reads must be the FIRE's own time, whatever hour the backfill runs.

    Regression for 2026-10-21: the state carried the wall-clock write instant, and
    is_afternoon_scheduled_evidence compares that time of day to the 14:30 ET floor, so the two
    tests above passed from 14:30 to 23:59 ET and failed from 00:00 to 14:29 ET (main went red at
    ET midnight). Those tests are clock-dependent; this one is not: the wall clock is never
    2026-10-13 14:30 or 15:30, so stamping the write instant fails it at every hour. It also
    needs the real write instant kept apart, in the reconstruction block, for the audit trail.
    """
    orders = _setup(tmp_path, alerts=(alert,))
    assert _run(tmp_path, orders, fire=f"2026-10-13:{slot}") == 0
    state = json.loads((tmp_path / state_dir / "run_state.json").read_text())

    stamped = dca_risk.run_timestamp_et(state)
    assert stamped is not None
    expected = datetime(2026, 10, 13, *nominal_hhmm, tzinfo=ZoneInfo("America/New_York"))
    assert stamped == expected, f"run timestamp {stamped.isoformat()} is not the fire time {expected.isoformat()}"
    assert dca_risk.is_afternoon_scheduled_evidence(state)

    backfilled_at = datetime.fromisoformat(state["gate9_report"]["reconstruction"]["backfilled_at"])
    assert backfilled_at.tzinfo is not None
    assert backfilled_at != stamped, "the real write instant must stay distinguishable from the fire time"
