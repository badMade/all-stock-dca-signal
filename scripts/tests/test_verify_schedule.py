"""Tests for scripts/verify_schedule.py: the per-slot daily evidence audit.

Pins the slot layout to the writers it mirrors, walks every status in
``classify_slot_evidence``, and reproduces the 2026-09-22 failure (run
directories present, no run_state.json) end to end through the CLI.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import verify_schedule  # noqa: E402
from scripts.verify_schedule import (  # noqa: E402
    SLOT_EVIDENCE_LAYOUT,
    classify_slot_evidence,
    slot_run_dir_relative,
    verify_day,
)

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 22)  # a Tuesday the calendar has open
AFTER_CLOSE = datetime(2026, 9, 22, 18, 0, tzinfo=ET)


def scheduled_state(slot_id="1030", day=DAY, **overrides):
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot_id]
    rc = {"run_origin": "scheduled", "manual_run": False, "stream": stream,
          "scheduled_for_et": f"{slot_id[:2]}:{slot_id[2:]}"}
    rc.update(overrides.pop("rc", {}))
    return {"trading_day": day.isoformat(), "run_classification": rc, "order_submit_status": "no_orders_submitted",
            **overrides}


def classify(slot_id="1030", now=AFTER_CLOSE, *, run_dir=True, state=None, present=None, closed=False):
    return classify_slot_evidence(slot_id, DAY, now, run_dir_present=run_dir, run_state=state,
                                  run_state_present=state is not None if present is None else present,
                                  market_closed=closed)


def test_layout_matches_every_executor_profile_and_the_afternoon_writer():
    import intraday_dca
    import market_open_dca
    import midday_dca
    import morning_dca
    from dca_runner import SLOTS
    from scripts.run_classification import scheduled_run_dir

    assert set(SLOT_EVIDENCE_LAYOUT) == set(SLOTS)
    profiles = {"0935": market_open_dca.MARKET_OPEN_PROFILES["live"], "1030": morning_dca.MORNING_PROFILES["live"],
                "1230": midday_dca.MIDDAY_PROFILES["live"], **intraday_dca.INTRADAY_SLOT_PROFILES}
    for slot_id, profile in profiles.items():
        assert slot_run_dir_relative(slot_id, DAY) == Path(profile["state_root_stream"]) / f"20260922_{profile['folder_suffix']}"
        assert profile["scheduled_for_et"] == "%02d:%02d" % SLOTS[slot_id].nominal_et
    base = Path("/root")
    assert base / slot_run_dir_relative("1530", DAY) == scheduled_run_dir("2026-09-22", base / "ce16ba6b")


def test_verified_needs_scheduled_same_day_state_for_this_slot():
    row = classify(state=scheduled_state())
    assert row["status"] == "verified" and row["reasons"] == []
    assert row["run_dir"] == "morning_dca/20260922_1030ET"


@pytest.mark.parametrize("state, reason", [
    (scheduled_state(rc={"manual_run": True}), "not scheduled evidence"),
    (scheduled_state(rc={"run_origin": "manual"}), "not scheduled evidence"),
    (scheduled_state(artifact_contract_failure={"reason": "x"}), "not scheduled evidence"),
    (scheduled_state(day=date(2026, 9, 21)), "trading_day"),
    (scheduled_state(rc={"stream": "midday"}), "run_classification.stream"),
])
def test_non_evidence_states_are_incomplete_with_a_reason(state, reason):
    row = classify(state=state)
    assert row["status"] == "evidence_incomplete"
    assert any(reason in text for text in row["reasons"])


def test_an_1130_state_in_the_1330_folder_does_not_verify_1330():
    row = classify("1330", state=scheduled_state("1130"))
    assert row["status"] == "evidence_incomplete"
    assert any("scheduled_for_et" in reason for reason in row["reasons"])


def test_run_dir_without_run_state_is_the_2026_09_22_failure():
    row = classify(state=None)
    assert row["status"] == "evidence_incomplete"
    assert "never written" in row["reasons"][0]


def test_a_valid_state_with_artifact_contract_errors_is_not_verified():
    row = classify_slot_evidence("1030", DAY, AFTER_CLOSE, run_dir_present=True, run_state=scheduled_state(),
                                 run_state_present=True, market_closed=False,
                                 artifact_errors=["run_summary.json is missing"])
    assert row["status"] == "evidence_incomplete"
    assert row["reasons"] == ["artifact contract: run_summary.json is missing"]


def test_unreadable_run_state_is_incomplete():
    assert classify(state=None, present=True)["status"] == "evidence_incomplete"


def test_nothing_at_all_after_the_deadline_is_no_evidence():
    assert classify(run_dir=False)["status"] == "no_evidence"


def test_time_based_statuses():
    before = datetime(2026, 9, 22, 10, 29, tzinfo=ET)
    inside_grace = datetime(2026, 9, 22, 11, 29, tzinfo=ET)  # window end 11:00 + 30 min
    assert classify(now=before, run_dir=False)["status"] == "not_due"
    assert classify(now=inside_grace, run_dir=True)["status"] == "awaiting_evidence"
    assert classify(now=datetime(2026, 9, 22, 11, 31, tzinfo=ET), run_dir=True)["status"] == "evidence_incomplete"
    # Evidence written before the nominal time (clock skew) is still evidence.
    assert classify(now=before, state=scheduled_state())["status"] == "verified"


def test_market_closed_only_excuses_a_slot_that_wrote_nothing():
    assert classify(run_dir=False, closed=True)["status"] == "market_closed"
    assert classify(run_dir=True, closed=True)["status"] == "evidence_incomplete"
    assert classify(state=scheduled_state(), closed=True)["status"] == "verified"
    # An unknown calendar (None) is treated as open, never as closed.
    assert classify(run_dir=False, closed=None)["status"] == "no_evidence"


def test_calendar_verdicts_come_from_gate0():
    assert verify_schedule.market_closed_for_slot(date(2026, 9, 7), "1030") is True  # Labor Day
    assert verify_schedule.market_closed_for_slot(DAY, "1030") is False
    assert verify_schedule.market_closed_for_slot(date(1999, 1, 4), "1030") is None  # uncovered year


def test_verify_day_and_cli_reproduce_the_incident(tmp_path, capsys):
    for slot_id in ("1030", "1130", "1230", "1430"):
        (tmp_path / slot_run_dir_relative(slot_id, DAY)).mkdir(parents=True)
    report = verify_day(DAY, AFTER_CLOSE, root=tmp_path)
    statuses = {row["slot_id"]: row["status"] for row in report["slots"]}
    assert statuses == {"0935": "no_evidence", "1030": "evidence_incomplete", "1130": "evidence_incomplete",
                        "1230": "evidence_incomplete", "1330": "no_evidence", "1430": "evidence_incomplete",
                        "1530": "no_evidence"}
    assert report["ok"] is False and len(report["failing_slots"]) == 7

    assert verify_schedule.main(["--date", "2026-09-22", "--root", str(tmp_path), "--json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["date"] == "2026-09-22" and printed["ok"] is False


def test_verify_day_passes_once_every_slot_is_verified(tmp_path):
    for slot_id in SLOT_EVIDENCE_LAYOUT:
        run_dir = tmp_path / slot_run_dir_relative(slot_id, DAY)
        run_dir.mkdir(parents=True)
        state = scheduled_state(slot_id)
        (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
        (run_dir / "run_summary.json").write_text(json.dumps({"run_classification": state["run_classification"]}))
        (run_dir / "signals.csv").write_text("ticker\n")
        (run_dir / "submitted_orders.csv").write_text("ticker,order_id,submitted_amount_usd,status\n")
    report = verify_day(DAY, AFTER_CLOSE, root=tmp_path)
    assert report["ok"] is True and {row["status"] for row in report["slots"]} == {"verified"}
