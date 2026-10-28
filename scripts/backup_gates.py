"""Step 1 of the Perplexity backup procedure (the gates) as code.

The agent used to derive "which slot am I, is the market open, am I inside the window" from a clock reading and a
pasted calendar. That is deterministic, so the planner does it: ``backup_planner gates --task <id>`` reads a real
clock, applies the SAME rules step 1's text states (``scripts/backup_procedure.py``), and prints one status for the
agent to act on. The rules are not new:

* the task's slot is chosen by the clock (a single-fire task has one slot; the two-fire task 13:50/14:50 picks
  1330 or 1430 from fixed ET ranges);
* market day: a weekday that is not a listed holiday; on an early-close day a fire at or after the early-close
  time stops too (``skipped_market_closed``);
* time: inside the slot's range (a single-fire task starts at its fire time, not before), else
  ``skipped_outside_window``;
* market-closed is decided before the window, as in the step 1 text (b before c).

Fail closed: a naive clock value, an unknown task, or a date the calendar does not cover is an error (the
calendar file says to extend it before 2029 rather than guess), never a PROCEED.

Standard library only; the calendar is passed in so the tests (and the zipapp's bundled config) own the data.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import dca_order_plan
from scripts import backup_procedure

ET = ZoneInfo("America/New_York")
# The two-fire task's two ET ranges are fixed in the step 1 text (they start ten minutes before each fire; left as is).
_TWO_FIRE_RANGES = {"1330": (time(13, 40), time(14, 20)), "1430": (time(14, 40), time(15, 20))}


def _hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _single_fire_range(backup: Any) -> tuple[str, time, time, bool]:
    """(slot, start, end, end_inclusive): from the fire time up to the latest placement time.

    The fire time is the earliest start, never earlier: a backup is a delayed second chance, and a run that began
    before it (a manual one at 09:46 on 2026-10-26, inside the old ten-minutes-early allowance) bought 25 orders
    before Claude's slot could be judged to have failed. Scheduled runs start at or after their fire time.
    """
    slot = backup.slots[0]
    return slot, _hhmm(backup.fires[0]), _hhmm(dca_order_plan.LATEST_PLACEMENT_ET[slot]), False


def _covered(calendar: dict, day: str) -> None:
    if int(day[:4]) not in set(calendar.get("years_covered", [])):
        raise ValueError(f"market calendar does not cover {day[:4]}; extend config/market_calendar.json")


def evaluate(task_id: str, now: datetime, calendar: dict) -> dict:
    """The step 1 decision for one task at one instant. ``KeyError``: unknown task. ``ValueError``: unusable input."""
    backup = backup_procedure.find_backup(task_id)
    if backup is None:
        raise KeyError(f"unknown task id {task_id!r}")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the clock value has no timezone; refusing to guess one")
    now_et = now.astimezone(ET)
    day = now_et.date().isoformat()
    _covered(calendar, day)
    clock = now_et.time().replace(second=0, microsecond=0)
    report: dict = {"status": "PROCEED", "task": task_id, "now_et": now_et.isoformat(timespec="seconds"),
                    "slot": None, "latest_placement_et": None, "reason": None}

    if now_et.weekday() >= 5:
        return {**report, "status": "skipped_market_closed", "reason": "weekend"}
    if day in set(calendar["holidays_closed"]):
        return {**report, "status": "skipped_market_closed", "reason": "holiday"}
    if day in set(calendar["early_close_1300"]) and clock >= _hhmm(calendar["early_close_time_et"]):
        return {**report, "status": "skipped_market_closed", "reason": "at or after the early close"}

    if len(backup.slots) == 1:
        slot, start, end, end_inclusive = _single_fire_range(backup)
        candidates = [(slot, start, end, end_inclusive)]
    else:
        candidates = [(slot, *_TWO_FIRE_RANGES[slot], True) for slot in backup.slots]
    for slot, start, end, end_inclusive in candidates:
        if start <= clock and (clock <= end if end_inclusive else clock < end):
            return {**report, "slot": slot, "latest_placement_et": dca_order_plan.LATEST_PLACEMENT_ET[slot]}
    return {**report, "status": "skipped_outside_window", "reason": "outside every window of this task"}
