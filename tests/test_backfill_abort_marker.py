"""A backfilled fire names the abort its evidence-free marker recorded, at the time it really fired.

2026-11-11: the 2026-11-09 Claude app outage pushed the 14:30 and 15:30 fires to 19:24 ET, where Gate 1
refused both as off-window and ``scripts/record_evidence_free_abort.sh`` wrote a marker for each. The
2026-11-10 backfill still labelled them ``BACKFILL_GATE2_ABORT_ZERO_DEPLOYMENT`` / ``backfill_gate2_abort``
with a "no Python 3.11" reason and the NOMINAL trigger time, and the operator had to correct the four
live artifacts by hand. These tests pin what the backfill must do instead:

* with exactly one well-formed pre-broker marker: outcome, error_class, reason and triggered_at_utc come
  from it, and the state is still afternoon evidence (``dca_risk.run_timestamp_et`` floors at the nominal
  time, it does not cap there);
* any ambiguity in the marker file (two markers, a malformed line, mixed gates, a later gate, another day
  or slot, a naive time) refuses and writes nothing;
* the broker history must cover the acceptance window AND the off-window fire, and an order near that
  fire needs attribution exactly as an in-window one does;
* with no marker file the Gate 2 labels and the nominal time are unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_backfill_attribute_orders import _attribute, _order

import dca_risk
from scripts import backfill_afternoon_evidence as bf
from scripts.run_classification import validate_classification

ET = ZoneInfo("America/New_York")
DAY = "2026-11-09"
# The real 2026-11-09 fire times, from the live abort markers: 19:24:28 and 19:24:58 ET.
FIRED = {"1430": "2026-11-10T00:24:28Z", "1530": "2026-11-10T00:24:58Z"}
RUN_DIR = {"1430": "intraday_dca/20261109_1430ET", "1530": "ce16ba6b/scheduled_runs/20261109_1530ET"}
ALERT = {"1430": "intraday_dca/alerts/2026-11-09-1430-gate1.txt",
         "1530": "ce16ba6b/scheduled_runs/alerts/2026-11-09-1530-gate1.txt"}
COVER = ["--orders-covers-from", "2026-11-09T00:00:00Z", "--orders-covers-through", "2026-11-11T00:00:00Z"]


def _marker(slot: str, **overrides) -> dict:
    """One line exactly as record_evidence_free_abort.sh writes it."""
    return {
        "alert_file": f"/cron/{ALERT[slot]}", "fired_at_utc": FIRED[slot], "gate": "gate1",
        "kind": "evidence_free_abort", "reason_code": "GATE1_OFF_WINDOW", "slot_id": slot,
        "trading_day": DAY, "writer": "scripts/record_evidence_free_abort.sh", **overrides,
    }


def _write_markers(root: Path, slot: str, *lines) -> Path:
    path = root / "abort_markers" / "20261109" / f"{slot}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines),
                    encoding="utf-8")
    return path


def _run(root: Path, slot: str, orders=(), *extra, cover=COVER) -> int:
    alert = root / ALERT[slot]
    alert.parent.mkdir(parents=True, exist_ok=True)
    alert.write_text("SLOT ABORT. Failing gate: Gate 1 (GATE1_OFF_WINDOW)", encoding="utf-8")
    orders_file = root / "orders.json"
    orders_file.write_text(json.dumps(list(orders)), encoding="utf-8")
    return bf.main(["--fire", f"{DAY}:{slot}", "--orders-json", str(orders_file), *cover,
                    "--root", str(root), *extra])


def _state(root: Path, slot: str) -> dict:
    return json.loads((root / RUN_DIR[slot] / "run_state.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("slot", ["1430", "1530"])
def test_gate1_marker_names_the_abort_and_the_real_fire_time(slot, tmp_path):
    _write_markers(tmp_path, slot, _marker(slot))

    assert _run(tmp_path, slot) == 0

    state = _state(tmp_path, slot)
    assert state["status"] == "BACKFILL_GATE1_OFF_WINDOW_ZERO_DEPLOYMENT"
    assert state["error_class"] == "backfill_gate1_off_window"
    assert state["order_submit_status"] == "not_submitted_error"
    rc = state["run_classification"]
    fired = datetime.fromisoformat(FIRED[slot].replace("Z", "+00:00"))
    assert datetime.fromisoformat(rc["triggered_at_utc"].replace("Z", "+00:00")) == fired
    # The run's identity is still the nominal slot's, on its ET trading day (not the UTC date of the fire).
    assert rc["scheduled_for_et"] == f"{slot[:2]}:{slot[2:]}"
    assert state["trading_day"] == DAY
    assert validate_classification(rc).ok
    reconstruction = state["gate9_report"]["reconstruction"]
    assert reconstruction["abort_source"] == "abort_marker"
    assert (reconstruction["abort_gate"], reconstruction["abort_reason_code"]) == ("gate1", "GATE1_OFF_WINDOW")
    assert reconstruction["abort_marker_file"] == f"abort_markers/20261109/{slot}.jsonl"
    assert "GATE1_OFF_WINDOW" in reconstruction["reason"] and "19:24 ET" in reconstruction["reason"]
    assert "Python 3.11" not in reconstruction["reason"]
    assert datetime.fromisoformat(reconstruction["fired_at_et"]) == fired


@pytest.mark.parametrize("slot", ["1430", "1530"])
def test_off_window_fire_is_still_afternoon_evidence(slot, tmp_path):
    """run_timestamp_et floors at the nominal time and never caps there, so 19:24 ET still counts."""
    _write_markers(tmp_path, slot, _marker(slot))
    assert _run(tmp_path, slot) == 0

    state = _state(tmp_path, slot)

    stamp = dca_risk.run_timestamp_et(state)
    assert stamp is not None and (stamp.date().isoformat(), stamp.hour, stamp.minute) == (DAY, 19, 24)
    assert dca_risk.is_afternoon_scheduled_evidence(state)


def test_fire_before_the_window_is_floored_to_the_nominal_time(tmp_path):
    _write_markers(tmp_path, "1430", _marker("1430", fired_at_utc="2026-11-09T19:00:00Z"))  # 14:00 ET
    assert _run(tmp_path, "1430") == 0

    state = _state(tmp_path, "1430")

    assert dca_risk.run_timestamp_et(state) == datetime(2026, 11, 9, 14, 30, tzinfo=ET)
    assert dca_risk.is_afternoon_scheduled_evidence(state)


def test_gate2_marker_keeps_its_own_reason_code(tmp_path):
    _write_markers(tmp_path, "1430", _marker("1430", gate="gate2", reason_code="GATE2_ABORT_NO_PYTHON311",
                                             fired_at_utc="2026-11-09T19:31:00Z"))
    assert _run(tmp_path, "1430") == 0

    state = _state(tmp_path, "1430")

    assert state["status"] == "BACKFILL_GATE2_ABORT_NO_PYTHON311_ZERO_DEPLOYMENT"
    assert state["error_class"] == "backfill_gate2_abort_no_python311"


def test_no_marker_keeps_the_gate2_labels_and_the_nominal_time(tmp_path):
    assert _run(tmp_path, "1430") == 0

    state = _state(tmp_path, "1430")

    assert state["status"] == bf.OUTCOME
    assert state["error_class"] == bf.ERROR_CLASS
    assert state["gate9_report"]["reconstruction"]["abort_source"] == "assumed_gate2_no_marker"
    assert dca_risk.run_timestamp_et(state) == datetime(2026, 11, 9, 14, 30, tzinfo=ET)


AMBIGUOUS = {
    "two identical markers": lambda: [_marker("1430"), _marker("1430")],
    "mixed gates": lambda: [_marker("1430"), _marker("1430", gate="gate2", reason_code="GATE2_ABORT_NO_PYTHON311")],
    "malformed line": lambda: ['{"kind": "evidence_free_abort", '],
    "malformed beside a good one": lambda: [_marker("1430"), "not json"],
    "duplicate key": lambda: ['{"kind": "evidence_free_abort", "kind": "evidence_free_abort"}'],
    "not a marker": lambda: [_marker("1430", kind="something_else")],
    "later gate": lambda: [_marker("1430", gate="gate4", reason_code="GATE4_ROGUE_EXECUTOR")],
    "reason from another gate": lambda: [_marker("1430", reason_code="GATE2_ABORT_NO_PYTHON311")],
    "lowercase reason": lambda: [_marker("1430", reason_code="gate1_off_window")],
    "another slot": lambda: [_marker("1430", slot_id="1530")],
    "another day": lambda: [_marker("1430", trading_day="2026-11-10")],
    "fire on another ET day": lambda: [_marker("1430", fired_at_utc="2026-11-10T06:00:00Z")],
    "naive fire time": lambda: [_marker("1430", fired_at_utc="2026-11-09T19:24:28")],
    "no fire time": lambda: [_marker("1430", fired_at_utc=None)],
    "empty file": lambda: [""],
}


@pytest.mark.parametrize("case", AMBIGUOUS, ids=list(AMBIGUOUS))
def test_any_ambiguity_in_the_marker_file_refuses_and_writes_nothing(case, tmp_path, capsys):
    _write_markers(tmp_path, "1430", *AMBIGUOUS[case]())

    assert _run(tmp_path, "1430") == 2

    out = json.loads(capsys.readouterr().out)
    assert out["written"] is False and "abort marker" in out["refused"]
    assert not (tmp_path / RUN_DIR["1430"]).exists()


def test_a_bad_marker_refuses_every_fire_in_the_batch(tmp_path):
    """All-or-nothing: one fire's ambiguous marker blocks the good fire beside it too."""
    _write_markers(tmp_path, "1430", _marker("1430"))
    _write_markers(tmp_path, "1530", _marker("1530"), _marker("1530"))
    for slot in ("1430", "1530"):
        (tmp_path / ALERT[slot]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / ALERT[slot]).write_text("abort", encoding="utf-8")
    orders = tmp_path / "orders.json"
    orders.write_text("[]", encoding="utf-8")

    rc = bf.main(["--fire", f"{DAY}:1430", "--fire", f"{DAY}:1530", "--orders-json", str(orders), *COVER,
                  "--root", str(tmp_path)])

    assert rc == 2
    assert not (tmp_path / RUN_DIR["1430"]).exists() and not (tmp_path / RUN_DIR["1530"]).exists()


def test_broker_history_must_cover_the_off_window_fire_not_just_the_window(tmp_path, capsys):
    _write_markers(tmp_path, "1430", _marker("1430"))
    # Spans 14:25-14:55 ET with room to spare, but ends at 17:00 ET, before the 19:24 fire.
    short = ["--orders-covers-from", "2026-11-09T00:00:00Z", "--orders-covers-through", "2026-11-09T22:00:00Z"]

    assert _run(tmp_path, "1430", (), cover=short) == 2

    assert "marker fire time" in json.loads(capsys.readouterr().out)["refused"]
    assert not (tmp_path / RUN_DIR["1430"]).exists()


def test_same_short_history_is_enough_without_a_marker(tmp_path):
    short = ["--orders-covers-from", "2026-11-09T00:00:00Z", "--orders-covers-through", "2026-11-09T22:00:00Z"]
    assert _run(tmp_path, "1430", (), cover=short) == 0


NEAR_FIRE = _order("order-near-fire", "MA", "2026-11-10T00:40:00.000000Z", "1.000000")  # 19:40 ET


def test_order_near_the_off_window_fire_needs_a_human(tmp_path, capsys):
    _write_markers(tmp_path, "1430", _marker("1430"))

    assert _run(tmp_path, "1430", [NEAR_FIRE]) == 2

    assert "human must investigate" in json.loads(capsys.readouterr().out)["refused"]


def test_order_near_a_pre_broker_abort_refuses_even_when_attributed(tmp_path, capsys):
    """PR #2234 review: a Gate 1/2 abort runs before the broker read, so it cannot have placed an order."""
    _write_markers(tmp_path, "1430", _marker("1430"))

    assert _run(tmp_path, "1430", [NEAR_FIRE], *_attribute("order-near-fire")) == 2

    refused = json.loads(capsys.readouterr().out)["refused"]
    assert "pre-broker abort cannot have placed" in refused and "human must investigate" in refused
    assert not (tmp_path / RUN_DIR["1430"]).exists()


def test_order_in_the_acceptance_window_of_a_marker_fire_refuses_even_when_attributed(tmp_path, capsys):
    _write_markers(tmp_path, "1430", _marker("1430"))
    in_window = _order("order-in-window", "MA", "2026-11-09T19:40:00.000000Z", "1.000000")  # 14:40 ET

    assert _run(tmp_path, "1430", [in_window], *_attribute("order-in-window")) == 2

    assert "pre-broker abort cannot have placed" in json.loads(capsys.readouterr().out)["refused"]


def test_a_marker_has_no_attributed_outcome():
    marker = bf.AbortMarker("gate1", "GATE1_OFF_WINDOW", datetime(2026, 11, 9, 19, 24, tzinfo=ET), Path("m"))
    assert marker.outcome == "BACKFILL_GATE1_OFF_WINDOW_ZERO_DEPLOYMENT"
    with pytest.raises(bf.BackfillRefused, match="cannot carry attributed orders"):
        bf.write_fire(Path("unused"), marker.fired_at.date(), "1430", {}, [{"order_id": "x"}], marker)


def test_late_marker_does_not_swallow_the_next_slots_window(tmp_path):
    """PR #2234 review: the window and the fire's interval are checked apart, never as one hull.

    A 14:30 marker at 16:10 ET used to make the checked span 14:25-17:10, which covers the whole 15:30
    window, so the 15:30 slot's own order blocked the 14:30 backfill (or could be attributed to it).
    """
    _write_markers(tmp_path, "1430", _marker("1430", fired_at_utc="2026-11-09T21:10:00Z"))  # 16:10 ET
    slot_1530_order = _order("order-1530", "MA", "2026-11-09T20:35:00.000000Z", "1.000000")  # 15:35 ET

    assert _run(tmp_path, "1430", [slot_1530_order]) == 0

    reconstruction = _state(tmp_path, "1430")["gate9_report"]["reconstruction"]
    assert reconstruction["broker_orders_in_window"] == 0
    window, fire = reconstruction["broker_checked_et"]
    assert [datetime.fromisoformat(t) for t in fire] == [
        datetime(2026, 11, 9, 16, 10, tzinfo=ET), datetime(2026, 11, 9, 17, 10, tzinfo=ET)]
    assert datetime.fromisoformat(window[1]) <= datetime(2026, 11, 9, 15, 0, tzinfo=ET)


def test_order_inside_the_late_fires_own_interval_still_refuses(tmp_path):
    _write_markers(tmp_path, "1430", _marker("1430", fired_at_utc="2026-11-09T21:10:00Z"))  # 16:10 ET
    near = _order("order-near-late", "MA", "2026-11-09T21:30:00.000000Z", "1.000000")  # 16:30 ET
    assert _run(tmp_path, "1430", [near]) == 2


def test_an_order_in_two_fires_intervals_is_never_attributed_twice(tmp_path, monkeypatch, capsys):
    """PR #2234 review: one order in two fires' intervals would be written into both run_states."""
    shared = (datetime(2026, 11, 9, 19, 0, tzinfo=ET), datetime(2026, 11, 9, 20, 0, tzinfo=ET))
    monkeypatch.setattr(bf, "order_spans", lambda slot, day, marker: [shared])
    for slot in ("1430", "1530"):
        alert = tmp_path / ALERT[slot]
        alert.parent.mkdir(parents=True, exist_ok=True)
        alert.write_text("abort", encoding="utf-8")
    orders = tmp_path / "orders.json"
    orders.write_text(json.dumps([NEAR_FIRE]), encoding="utf-8")

    rc = bf.main(["--fire", f"{DAY}:1430", "--fire", f"{DAY}:1530", "--orders-json", str(orders), *COVER,
                  "--root", str(tmp_path), *_attribute("order-near-fire")])

    assert rc == 2
    assert "inside more than one fire's checked intervals" in json.loads(capsys.readouterr().out)["refused"]
    assert not (tmp_path / RUN_DIR["1430"]).exists() and not (tmp_path / RUN_DIR["1530"]).exists()


def test_order_long_after_the_fire_does_not_block(tmp_path):
    _write_markers(tmp_path, "1430", _marker("1430"))
    late = _order("order-late", "MA", "2026-11-10T02:00:00.000000Z", "1.000000")  # 21:00 ET, > 1 h after
    assert _run(tmp_path, "1430", [late]) == 0


def test_trigger_time_stamp_is_scoped_to_the_one_finalize_call(tmp_path, monkeypatch):
    """The wrapper never outlives its call, even when finalize refuses."""
    from scripts import finalize_run_evidence as fre

    original = fre.build_classification
    _write_markers(tmp_path, "1430", _marker("1430"))
    assert _run(tmp_path, "1430") == 0
    assert fre.build_classification is original

    def refuse(*_args, **_kwargs):
        raise fre.FinalizeRefused("synthetic refusal")

    monkeypatch.setattr(bf, "finalize", refuse)
    _write_markers(tmp_path, "1530", _marker("1530"))
    assert _run(tmp_path, "1530") == 2
    assert fre.build_classification is original

