"""Step 1 (the gates) as code: the planner decides the slot, the market day and the window from a real clock.

The prompt used to ask the Perplexity agent to do this date and time reasoning itself, from a calendar pasted into
the Instructions. The expensive model is the wrong place for it: ``backup_planner gates --task <id>`` reads the
clock, applies the same rules the step 1 text states, and prints one JSON status.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import backup_gates

REPO = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
CALENDAR = json.loads((REPO / "config" / "market_calendar.json").read_text(encoding="utf-8"))

T_0935, T_1030, T_1130, T_1230, T_TWO_FIRE, T_1530 = "4412c3c4", "e028361e", "e3590042", "062605c6", "54ab24d2", "f83b8d62"


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET)


def gate(task: str, day: str, hhmm: str) -> dict:
    return backup_gates.evaluate(task, at(day, hhmm), CALENDAR)


MONDAY = "2026-10-19"


@pytest.mark.parametrize(
    ("task", "hhmm", "slot", "latest"),
    [
        (T_0935, "10:00", "0935", "10:30"),
        (T_1030, "10:50", "1030", "11:30"),
        (T_1130, "11:50", "1130", "12:30"),
        (T_1230, "12:50", "1230", "13:30"),
        (T_1530, "15:45", "1530", "16:00"),
    ],
)
def test_a_single_fire_task_proceeds_at_its_fire_time(task, hhmm, slot, latest):
    result = gate(task, MONDAY, hhmm)
    assert result["status"] == "PROCEED"
    assert result["slot"] == slot and result["latest_placement_et"] == latest


@pytest.mark.parametrize(
    ("task", "hhmm", "expected"),
    [
        (T_0935, "09:46", "skipped_outside_window"),  # 2026-10-26: a manual 09:46 run placed 25 buys before its fire
        (T_0935, "09:54", "skipped_outside_window"),  # the fire time is the earliest start, so Claude's slot goes first
        (T_0935, "10:00", "PROCEED"),
        (T_0935, "10:29", "PROCEED"),
        (T_0935, "10:30", "skipped_outside_window"),  # never at or after the latest placement time
        (T_1530, "15:44", "skipped_outside_window"),
        (T_1530, "15:45", "PROCEED"),
        (T_1530, "16:00", "skipped_outside_window"),
    ],
)
def test_single_fire_window_edges(task, hhmm, expected):
    assert gate(task, MONDAY, hhmm)["status"] == expected


@pytest.mark.parametrize(
    ("hhmm", "expected", "slot", "latest"),
    [
        ("13:39", "skipped_outside_window", None, None),
        ("13:40", "PROCEED", "1330", "14:30"),
        ("13:50", "PROCEED", "1330", "14:30"),
        ("14:20", "PROCEED", "1330", "14:30"),
        ("14:21", "skipped_outside_window", None, None),
        ("14:39", "skipped_outside_window", None, None),
        ("14:40", "PROCEED", "1430", "15:30"),
        ("14:50", "PROCEED", "1430", "15:30"),
        ("15:20", "PROCEED", "1430", "15:30"),
        ("15:21", "skipped_outside_window", None, None),
    ],
)
def test_the_two_fire_task_lets_the_clock_pick_the_slot(hhmm, expected, slot, latest):
    result = gate(T_TWO_FIRE, MONDAY, hhmm)
    assert result["status"] == expected
    if slot:
        assert result["slot"] == slot and result["latest_placement_et"] == latest


@pytest.mark.parametrize("day", ["2026-10-17", "2026-10-18"])  # Saturday, Sunday
def test_weekends_are_market_closed(day):
    assert gate(T_0935, day, "10:00")["status"] == "skipped_market_closed"


@pytest.mark.parametrize("day", CALENDAR["holidays_closed"][:6] + ["2026-11-26", "2027-12-24"])
def test_listed_holidays_are_market_closed(day):
    assert gate(T_1030, day, "10:50")["status"] == "skipped_market_closed"


def test_market_closed_wins_over_outside_window():
    assert gate(T_0935, "2026-11-26", "03:00")["status"] == "skipped_market_closed"


def test_an_early_close_day_stops_a_fire_at_or_after_the_early_close_time():
    day = CALENDAR["early_close_1300"][0]
    assert gate(T_0935, day, "10:00")["status"] == "PROCEED"
    assert gate(T_1230, day, "12:50")["status"] == "PROCEED"
    assert gate(T_TWO_FIRE, day, "13:50")["status"] == "skipped_market_closed"
    assert gate(T_1530, day, "15:45")["status"] == "skipped_market_closed"


def test_the_clock_is_converted_to_eastern_time_across_the_dst_change():
    # 2026-11-02 is the first Monday after clocks went back; 15:00 UTC is 10:00 EST, not EDT.
    utc = datetime.fromisoformat("2026-11-02T15:00:00+00:00")
    assert backup_gates.evaluate(T_0935, utc, CALENDAR)["status"] == "PROCEED"
    # The same UTC clock reading in October (EDT) is 11:00 ET: outside the 0935 backup's window.
    october = datetime.fromisoformat("2026-10-19T15:00:00+00:00")
    assert backup_gates.evaluate(T_0935, october, CALENDAR)["status"] == "skipped_outside_window"


def test_a_naive_clock_value_is_refused_not_guessed():
    with pytest.raises(ValueError, match="timezone"):
        backup_gates.evaluate(T_0935, datetime(2026, 10, 19, 9, 55), CALENDAR)


def test_an_unknown_task_is_refused():
    with pytest.raises(KeyError):
        backup_gates.evaluate("deadbeef", at(MONDAY, "10:00"), CALENDAR)


def test_a_date_past_the_calendar_coverage_fails_closed():
    # The calendar file says: extend it before 2029 or the executors abort rather than guess.
    with pytest.raises(ValueError, match="calendar"):
        gate(T_0935, "2029-03-05", "10:00")


def _run(*args):
    return subprocess.run([sys.executable, "-m", "scripts.backup_planner", *args], cwd=REPO,
                          capture_output=True, text=True, check=False)


def test_gates_command_prints_one_json_status_and_exits_zero_for_a_normal_skip():
    proceed = _run("gates", "--task", T_0935, "--now", "2026-10-19T10:00:00-04:00")
    assert proceed.returncode == 0 and json.loads(proceed.stdout)["status"] == "PROCEED"
    skipped = _run("gates", "--task", T_0935, "--now", "2026-10-17T10:00:00-04:00")
    assert skipped.returncode == 0 and json.loads(skipped.stdout)["status"] == "skipped_market_closed"


def test_gates_command_fails_closed_on_bad_input():
    for args in (("--task", "deadbeef"), ("--task", T_0935, "--now", "not-a-time"),
                 ("--task", T_0935, "--now", "2026-10-19T10:00:00")):
        done = _run("gates", *args)
        assert done.returncode == 2, args
        assert json.loads(done.stdout)["status"] == "FAILED"


def test_gates_command_without_now_reads_the_real_clock():
    done = _run("gates", "--task", T_0935)
    assert done.returncode == 0
    report = json.loads(done.stdout)
    assert report["status"] in {"PROCEED", "skipped_market_closed", "skipped_outside_window"}
    assert report["now_et"].endswith(("-04:00", "-05:00"))
