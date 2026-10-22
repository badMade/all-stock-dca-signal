"""The launchd agent that runs ``scripts.schedule_watchdog --notify`` once a day after the last slot is due.

Pins the template next to ``com.all-stock-dca.robinhood-payload.plist.template``: same placeholder
convention, same venv-Python launch (macOS privacy protection grants ~/Documents access per binary),
and a fire time that cannot land while a slot's evidence is still "awaiting".
"""

from __future__ import annotations

import plistlib
import re
from datetime import time
from pathlib import Path

from dca_runner import SLOTS
from scripts.verify_schedule import GRACE_MINUTES

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "deploy/launchd/com.all-stock-dca.schedule-watchdog.plist.template"
)


def _load() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_REPO_PATH", "/repo").replace("REPLACE_WITH_CRON_TRACKING_ROOT", "/cron")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_label_and_command_run_the_watchdog_with_notification_from_the_checkout():
    agent = _load()
    assert agent["Label"] == "com.all-stock-dca.schedule-watchdog"
    assert agent["ProgramArguments"] == ["/repo/.venv/bin/python", "-m", "scripts.schedule_watchdog", "--notify"]
    assert agent["WorkingDirectory"] == "/repo"  # `-m scripts...` resolves from the checkout root
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == "/cron"  # must match the live sessions


def test_fires_once_each_weekday_after_the_last_slots_grace_deadline():
    entries = _load()["StartCalendarInterval"]
    entries = entries if isinstance(entries, list) else [entries]
    assert sorted(e["Weekday"] for e in entries) == [1, 2, 3, 4, 5]
    assert len({(e["Hour"], e["Minute"]) for e in entries}) == 1

    last_end = max(spec.window_end_et for spec in SLOTS.values())
    deadline_minutes = last_end[0] * 60 + last_end[1] + GRACE_MINUTES
    fire = time(entries[0]["Hour"], entries[0]["Minute"])
    assert fire.hour * 60 + fire.minute > deadline_minutes


def test_template_holds_no_secret_or_account_shaped_literal():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text


def test_logs_go_to_tmp_like_the_other_agent():
    agent = _load()
    assert agent["StandardOutPath"].startswith("/tmp/")
    assert agent["StandardErrorPath"] == agent["StandardOutPath"]


def test_the_superseded_verify_schedule_template_is_gone():
    old = TEMPLATE.parent / "com.all-stock-dca.verify-schedule.plist.template"
    assert not old.exists()
