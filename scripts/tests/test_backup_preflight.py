"""``backup_planner preflight``: the build stamp and the step 1 gates in ONE call (2026-11-09).

Why: the 2026-11-09 final backup reached its gates command at 16:03 ET, 18 minutes after its 15:45 fire, and skipped
with skipped_outside_window. Setup was five or six sequential model calls (build-info three times, a whole-disk find,
part 0, universe, gates). A skipped fire needs none of the later ones, so ``preflight`` prints the build and the gates
result together and a skip ends the run before part 0 is ever requested.

The planner reports its build; it never compares it. The comparison stays in the pasted prompt (an expected hash cannot
live inside the build it names), so these tests pin what preflight prints, not a verdict on the stamp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import backup_planner

REPO = Path(__file__).resolve().parents[2]
T_1530 = "f83b8d62"
BUILD = {"content_sha256": "a" * 64, "source_commit": "b" * 40, "schema_version": 1}


@pytest.fixture(autouse=True)
def _stamped_build(monkeypatch):
    monkeypatch.setattr(backup_planner, "_read_build_info", lambda: dict(BUILD))


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = backup_planner.main(["preflight", "--task", T_1530, *argv])
    return code, json.loads(capsys.readouterr().out)


def test_a_fire_inside_the_window_proceeds_and_reports_build_and_gates_together(capsys):
    code, out = run(capsys, "--now", "2026-11-09T15:40:00-05:00")
    assert code == 0
    assert out["status"] == "PROCEED"
    assert out["task"] == T_1530 and out["slot"] == "1530" and out["latest_placement_et"] == "16:00"
    assert out["build"]["content_sha256"] == BUILD["content_sha256"]
    assert out["build"]["source_commit"] == BUILD["source_commit"]


def test_the_report_carries_the_time_it_was_taken_for_the_state_record(capsys):
    _, out = run(capsys, "--now", "2026-11-09T15:41:07-05:00")
    assert out["now_et"] == "2026-11-09T15:41:07-05:00"  # becomes preflight_at_et in the state record


def test_the_2026_11_09_start_is_a_skip_and_still_reports_the_build(capsys):
    code, out = run(capsys, "--now", "2026-11-09T16:03:00-05:00")
    assert code == 0  # a skip is a recorded status, not an error
    assert out["status"] == "skipped_outside_window"
    assert out["build"]["content_sha256"] == BUILD["content_sha256"]  # the skip record names the build that ran


def test_a_holiday_is_a_market_closed_skip(capsys):
    code, out = run(capsys, "--now", "2026-11-26T15:40:00-05:00")
    assert code == 0 and out["status"] == "skipped_market_closed"


@pytest.mark.parametrize("now", ["2026-11-09T15:39:00-05:00", "2026-11-09T16:00:00-05:00"])
def test_the_window_edges_match_gates(capsys, now):
    _, out = run(capsys, "--now", now)
    assert out["status"] == "skipped_outside_window"


def test_an_unreadable_build_fails_closed_with_exit_2_and_never_proceeds(monkeypatch, capsys):
    monkeypatch.setattr(backup_planner, "_read_build_info", lambda: None)
    code, out = run(capsys, "--now", "2026-11-09T15:40:00-05:00")
    assert code == 2
    assert out["status"] == "FAILED"
    assert out["errors"][0]["code"] == "build_info_unavailable"


def test_a_build_without_a_sha_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(backup_planner, "_read_build_info", lambda: {"source_commit": "b" * 40})
    code, out = run(capsys, "--now", "2026-11-09T15:40:00-05:00")
    assert code == 2 and out["status"] == "FAILED"


def test_an_unknown_task_fails_closed(capsys):
    code = backup_planner.main(["preflight", "--task", "deadbeef", "--now", "2026-11-09T15:40:00-05:00"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["status"] == "FAILED"


def test_a_clock_without_an_offset_fails_closed(capsys):
    code = backup_planner.main(["preflight", "--task", T_1530, "--now", "2026-11-09T15:40:00"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["status"] == "FAILED"


def test_a_skip_never_reads_the_universe_or_the_broker(monkeypatch, capsys):
    # The skip path must cost one call: no universe load, no config beyond the market calendar.
    def boom(*_a, **_k):
        raise AssertionError("preflight must not load the universe")

    monkeypatch.setattr(backup_planner, "_cmd_universe", boom)
    code, out = run(capsys, "--now", "2026-11-09T16:03:00-05:00")
    assert code == 0 and out["status"] == "skipped_outside_window"
