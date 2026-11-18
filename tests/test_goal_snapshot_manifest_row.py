"""The schedule manifest holds the active row for the 07:31 ET goal snapshot task (Phase 2).

The operator registered a Desktop scheduled task on 2026-11-16 (``07:31 ET goal progress
snapshot``), but its cron fired at 7:31 PM daily; on 2026-11-17 the operator paused it and kept
``goal-snapshot-0731`` (cron ``31 7 * * 1-5`` on the Mac), so the row names that ID and is ``active``. It still must not appear in
``config/scheduled_tasks.json``: this is a read-only snapshot, not a trading slot.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "schedule_manifest.md"
TASKS = ROOT / "config" / "scheduled_tasks.json"


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
    assert "`goal-snapshot-0731`" in cells
    assert "`31 7 * * 1-5`" in cells
    assert cells[-1] == "active"


def test_row_is_not_in_the_live_task_config():
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]
    assert not any("goal" in str(t.get("name", "")).lower() and "snapshot" in str(t.get("name", "")).lower()
                   for t in tasks)
