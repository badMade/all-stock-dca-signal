"""Step 5 of the 2026-10-30 credit plan: fewer scheduled Perplexity backup fires (operator decision 2026-10-31).

The backups cost about 2,500 credits a trading day against a target under 200 (docs/cost_tracking.md). Steps 1-4
made each fire cheaper; step 5 cuts how many fire. The operator kept three: 10:00 (backs up the 09:35 slot), 12:50
(12:30) and 15:45 (15:30). The 10:50 and 11:50 tasks and the two-fire 13:50/14:50 task are PAUSED, not retired: their
prompts stay generated and re-enabling one is a config change plus a paste.

This amends the 2026-09-08 standing rule ("Perplexity shouldn't be disabled anywhere permanently"). The policy
still never expires; it now says per slot whether its backup is active or paused, and why.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "scheduled_tasks.json"

ACTIVE = {"09:35": "4412c3c4", "12:30": "062605c6", "15:30": "f83b8d62"}
PAUSED = {"10:30": "e028361e", "11:30": "e3590042", "13:30": "54ab24d2", "14:30": "54ab24d2"}
PAUSED_TASKS = {"e028361e", "e3590042", "54ab24d2"}  # 54ab24d2 is paused for both of its fires


def _data() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def _tasks() -> dict:
    return {task["id"]: task for task in _data()["tasks"]}


def test_every_slot_says_whether_its_backup_is_active_or_paused():
    slots = _data()["standing_perplexity_backup_policy"]["authorized_slots_et"]
    assert set(slots) == set(ACTIVE) | set(PAUSED)
    for et, backup_id in ACTIVE.items():
        assert slots[et]["backup_status"] == "active", et
        assert slots[et]["backup_task_id"] == backup_id, et
    for et, backup_id in PAUSED.items():
        assert slots[et]["backup_status"] == "paused", et
        assert slots[et]["backup_task_id"] == backup_id, et


def test_the_policy_records_the_amendment_and_still_never_expires():
    policy = _data()["standing_perplexity_backup_policy"]
    assert policy["amended_on_et"] == "2026-10-31"
    amendment = policy["amendment_2026_10_31"]
    for needle in ("credit", "10:00", "12:50", "15:40", "paused", "2026-09-08"):
        assert needle in amendment, needle
    assert policy["all_backup_tasks_live"] is False
    assert "expires" not in json.dumps(policy).lower()


def test_paused_tasks_are_paused_not_disabled_or_removed():
    tasks = _tasks()
    for task_id in PAUSED_TASKS:
        task = tasks[task_id]
        assert task["status"] == "paused", task_id
        assert task["trades"] is True, task_id  # unchanged contract for when it is re-enabled
        assert "2026-10-31" in task["notes"], task_id


def test_kept_tasks_stay_active():
    tasks = _tasks()
    for task_id in ("4412c3c4", "062605c6", "f83b8d62"):
        assert tasks[task_id]["status"] == "active", task_id


def test_the_two_fire_task_is_paused_for_both_fires_and_keeps_its_schedule_on_record():
    task = _tasks()["54ab24d2"]
    assert task["status"] == "paused"
    assert task["fires_at_et"] == "13:50, 14:50"  # what re-enabling restores


def test_the_paused_prompts_are_still_generated_so_re_enabling_is_a_paste():
    prompts = REPO / "claude" / "dca-executor-prompts" / "perplexity"
    names = {path.name for path in prompts.glob("*-backup.txt")}
    assert len(names) == 6
    for prefix in ("1050ET", "1150ET", "1350-1450ET"):
        assert any(name.startswith(prefix) for name in names), prefix


def test_the_docs_show_which_backups_are_paused():
    manifest = (REPO / "docs" / "schedule_manifest.md").read_text(encoding="utf-8")
    standing = manifest[manifest.index("Standing Perplexity backup policy"):]
    standing = standing[:standing.index("Sep 8-11, 2026 (SUPERSEDED")]
    assert "PAUSED 2026-10-31" in standing
    for task_id in ("e028361e", "e3590042", "54ab24d2"):
        assert task_id in standing

    todos = (REPO / "docs" / "dca_operational_todos.md").read_text(encoding="utf-8")
    assert "Handoff - 2026-10-31 Perplexity backup fires reduced" in todos

    cost = (REPO / "docs" / "cost_tracking.md").read_text(encoding="utf-8")
    assert "10:00, 12:50 and 15:40" in cost
