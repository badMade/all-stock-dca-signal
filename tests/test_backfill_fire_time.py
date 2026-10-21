"""A backfilled afternoon fire must carry the FIRE's time, not the time the backfill happened to run.

2026-10-21: ``tests/test_backfill_afternoon_evidence.py`` failed in CI between 00:00 and 14:29 ET and
passed in the afternoon. ``finalize_run_evidence`` stamps ``generated_at`` with the wall clock, and
``dca_risk.is_afternoon_scheduled_evidence`` compares that time of day with the 14:30 ET floor, so a
record backfilled in the morning is rejected as afternoon evidence. That is the tool's whole purpose
(restore the missing 14:30/15:30 records so Gate 5's 7-day pair check passes), and the operator will
usually run it the morning after an abort. These tests pin the clock instead of depending on it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_backfill_afternoon_evidence import _run, _setup

import dca_risk
from scripts import backfill_afternoon_evidence as bf
from scripts import finalize_run_evidence as fre

ET = ZoneInfo("America/New_York")

FIRES = {
    "1430": ("2026-10-13:1430", "intraday_dca/20261013_1430ET", "intraday_dca/alerts/2026-10-13-1430-gate2.txt"),
    "1530": ("2026-10-13:1530", "ce16ba6b/scheduled_runs/20261013_1530ET",
             "ce16ba6b/scheduled_runs/alerts/2026-10-13-1530-gate2.txt"),
}
# Wall-clock times the backfill might run at, in ET: before the 14:30 floor, just after it, late evening.
RUN_CLOCKS = {"09:00": (9, 0), "14:29": (14, 29), "16:00": (16, 0), "23:59": (23, 59)}


def _freeze(monkeypatch, hour: int, minute: int) -> datetime:
    frozen = datetime(2026, 10, 21, hour, minute, tzinfo=ET)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz is not None else frozen

    monkeypatch.setattr(fre, "datetime", FrozenDateTime)
    monkeypatch.setattr(bf, "datetime", FrozenDateTime)
    return frozen


def _backfill(tmp_path: Path, slot: str) -> dict:
    fire, run_dir, alert = FIRES[slot]
    assert _run(tmp_path, _setup(tmp_path, alerts=(alert,)), fire=fire) == 0
    return json.loads((tmp_path / run_dir / "run_state.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("clock", RUN_CLOCKS, ids=list(RUN_CLOCKS))
@pytest.mark.parametrize("slot", FIRES)
def test_backfilled_state_is_afternoon_evidence_whatever_time_it_is_written(slot, clock, tmp_path, monkeypatch):
    _freeze(monkeypatch, *RUN_CLOCKS[clock])

    state = _backfill(tmp_path, slot)

    assert dca_risk.is_afternoon_scheduled_evidence(state)


@pytest.mark.parametrize("slot", FIRES)
def test_the_evidence_timestamp_is_the_fires_own_nominal_time(slot, tmp_path, monkeypatch):
    _freeze(monkeypatch, 9, 0)

    state = _backfill(tmp_path, slot)

    expected = datetime(2026, 10, 13, int(slot[:2]), int(slot[2:]), tzinfo=ET)
    assert dca_risk.run_timestamp_et(state) == expected


def test_the_real_write_time_is_not_erased(tmp_path, monkeypatch):
    """The write timestamps and audit trail say when the backfill actually ran."""
    frozen = _freeze(monkeypatch, 9, 0)

    state = _backfill(tmp_path, "1430")

    reconstruction = state["gate9_report"]["reconstruction"]
    assert reconstruction["backfilled"] is True
    assert datetime.fromisoformat(reconstruction["backfilled_at"]) == frozen
    assert datetime.fromisoformat(state["generated_at"]) == frozen
    assert datetime.fromisoformat(state["finalized_at"]) == frozen
