"""The launchd agent that runs ``scripts.planner_stamp_check --notify`` once a morning, before the first slot.

Pinned to sit beside ``com.all-stock-dca.schedule-watchdog.plist.template``: same placeholder convention, same
venv-Python launch (macOS privacy protection grants ~/Documents access per binary), logs under /tmp. Unlike the
watchdog it runs BEFORE the day's first slot, because the point is to learn the planner is stale before a slot
fires on it, not after.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

from dca_runner import SLOTS

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "deploy/launchd/com.all-stock-dca.planner-stamp-check.plist.template"
)


def _load() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_REPO_PATH", "/repo")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_label_and_command_run_the_check_with_notification_from_the_checkout():
    agent = _load()
    assert agent["Label"] == "com.all-stock-dca.planner-stamp-check"
    assert agent["ProgramArguments"] == [
        "/repo/.venv/bin/python", "-m", "scripts.planner_stamp_check", "--planner", "/repo/backup_planner.pyz", "--notify",
    ]
    assert agent["WorkingDirectory"] == "/repo"  # `-m scripts...` resolves from the checkout root


def test_fires_every_weekday_before_the_first_slot_opens():
    entries = _load()["StartCalendarInterval"]
    entries = entries if isinstance(entries, list) else [entries]
    assert sorted(e["Weekday"] for e in entries) == [1, 2, 3, 4, 5]
    assert len({(e["Hour"], e["Minute"]) for e in entries}) == 1

    first_open = min(spec.window_start_et for spec in SLOTS.values())
    fire_minutes = entries[0]["Hour"] * 60 + entries[0]["Minute"]
    # Leaves at least an hour to rebuild, refill, verify and repaste before the earliest slot window opens.
    assert fire_minutes <= first_open[0] * 60 + first_open[1] - 60


def test_template_holds_no_secret_or_account_shaped_literal():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
    assert "REPLACE_WITH_CRON_TRACKING_ROOT" not in text  # it reads no cron tree, so it needs no such variable


def test_logs_go_to_tmp_like_the_other_agents():
    agent = _load()
    assert agent["StandardOutPath"].startswith("/tmp/")
    assert agent["StandardErrorPath"] == agent["StandardOutPath"]
