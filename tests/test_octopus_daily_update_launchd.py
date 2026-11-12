"""The launchd agent that runs wondrous-octopus/daily_update.py once each weekday evening."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy/launchd/com.all-stock-dca.octopus-daily-update.plist.template"


def _load() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("REPLACE_WITH_REPO_PATH", "/repo").replace("REPLACE_WITH_OCTOPUS_RUN_DIR", "/run")
    text = text.replace("REPLACE_WITH_TICKER_ARCHIVE_DIR", "/archive")
    assert "REPLACE_WITH_" not in text
    return plistlib.loads(text.encode("utf-8"))


def test_runs_daily_update_through_the_checkout_venv():
    agent = _load()
    assert agent["Label"] == "com.all-stock-dca.octopus-daily-update"
    assert agent["ProgramArguments"] == [
        "/repo/.venv/bin/python", "/repo/wondrous-octopus/daily_update.py", "--run-dir", "/run",
        "--history-dir", "/archive",
    ]
    assert agent["StandardOutPath"].startswith("/tmp/")


def test_path_includes_sbin_so_joblib_finds_sysctl():
    path = _load()["EnvironmentVariables"]["PATH"].split(":")
    assert "/usr/sbin" in path and "/sbin" in path


def test_fires_each_weekday_after_the_close():
    entries = _load()["StartCalendarInterval"]
    assert sorted(e["Weekday"] for e in entries) == [1, 2, 3, 4, 5]
    assert all(e["Hour"] * 60 + e["Minute"] > 16 * 60 for e in entries)


def test_holds_no_secret_literal():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
