"""The schedule manifest holds the registered row for the 07:31 ET goal snapshot task (Phase 2).

Written first with the placeholder ``TBD-task-id`` and status ``pending``. The operator approved
registering the task on 2026-11-17 (local Claude Desktop scheduled task ``goal-snapshot-0731``),
so the row now carries that ID and status ``active``, and ``config/scheduled_tasks.json`` lists
it as a non-trading analytics task. The "Planned, not yet registered" note is gone.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "schedule_manifest.md"
TASKS = ROOT / "config" / "scheduled_tasks.json"
TASK_ID = "goal-snapshot-0731"


def _snapshot_rows() -> list[str]:
    return [line for line in MANIFEST.read_text(encoding="utf-8").splitlines()
            if line.startswith("|") and "Goal snapshot" in line]


def test_manifest_has_exactly_one_goal_snapshot_row():
    assert len(_snapshot_rows()) == 1


def test_row_is_at_0731_et_on_trading_days_with_the_registered_id():
    (row,) = _snapshot_rows()
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[0] == "7:31 AM"
    assert "previous trading day" in row.lower()
    assert f"`{TASK_ID}`" in cells
    assert "TBD-task-id" not in row
    assert "`31 7 * * 1-5`" in cells
    assert cells[-2] == "no"  # Trades column: read tools only
    assert cells[-1] == "active"


def test_planned_note_is_removed_once_registered():
    assert "Planned, not yet registered: 07:31" not in MANIFEST.read_text(encoding="utf-8")


def test_live_task_config_lists_it_as_a_non_trading_analytics_task():
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    matches = [t for t in tasks if t.get("id") == TASK_ID]
    assert len(matches) == 1
    (task,) = matches
    assert task["trades"] is False
    assert task["type"] == "analytics"
    assert task["status"] == "active"
    assert "claude/goal-snapshot/0731ET-goal-snapshot.md" in task["notes"]
