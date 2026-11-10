from __future__ import annotations

import json
import plistlib
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import claude_liveness as liveness

ET = ZoneInfo("America/New_York")
TEMPLATE = Path(__file__).resolve().parent.parent / "deploy/launchd/com.all-stock-dca.claude-liveness.plist.template"


def _now(day="2026-11-10", hm="09:15"):
    return datetime.fromisoformat(f"{day}T{hm}:00-05:00").astimezone(ET)


def test_running_records_ok_without_launch_or_alert(tmp_path, capsys):
    result = liveness.check(_now(), root=tmp_path, is_running=lambda: True,
                            launch=lambda: pytest.fail("launched"), notify=lambda *_: pytest.fail("notified"))
    assert result["status"] == "claude_running" and result["exit_code"] == 0
    assert "ok: claude_running" in capsys.readouterr().out
    assert not list(tmp_path.rglob("*alerts*"))
    assert len((tmp_path / "claude_liveness/checks_2026-11-10.jsonl").read_text().splitlines()) == 1


def test_down_then_reopened_writes_alert_and_notifies(tmp_path):
    states = iter([False, True])
    launches, notices = [], []
    result = liveness.check(_now(), root=tmp_path, is_running=lambda: next(states), launch=lambda: launches.append(1),
                            sleep=lambda _: None, wait_s=3, notify=lambda *args: notices.append(args))
    assert result["status"] == "reopened" and result["exit_code"] == 0
    assert launches == [1] and len(notices) == 1
    alert = next((tmp_path / "claude_liveness/alerts").glob("*.txt")).read_text()
    assert "Outcome: reopened" in alert and "10:30 ET" in alert and "must not be placed late" in alert
    assert "check Robinhood order history" in alert and "Perplexity backup may have bought" in alert


def test_still_down_exits_one_and_alerts(tmp_path):
    result = liveness.check(_now(), root=tmp_path, is_running=lambda: False, launch=lambda: None,
                            sleep=lambda _: None, wait_s=2)
    assert result["status"] == "still_down" and result["exit_code"] == 1
    assert "Outcome: still_down" in next((tmp_path / "claude_liveness/alerts").glob("*.txt")).read_text()


@pytest.mark.parametrize("day", ["2026-11-14", "2026-11-26"])
def test_closed_days_skip_without_touching_files(tmp_path, day):
    result = liveness.check(_now(day), root=tmp_path, is_running=lambda: pytest.fail("checked"),
                            launch=lambda: pytest.fail("launched"))
    assert result["status"] == "market_closed" and result["exit_code"] == 0
    assert not list(tmp_path.iterdir())


def test_alert_is_idempotent_for_same_minute(tmp_path):
    for _ in range(2):
        liveness.check(_now(), root=tmp_path, is_running=lambda: False, launch=lambda: None,
                       sleep=lambda _: None, wait_s=0)
    files = list((tmp_path / "claude_liveness/alerts").glob("*"))
    assert len(files) == 1


def test_notification_error_does_not_change_result_or_prevent_alert(tmp_path):
    def fail(*_):
        raise RuntimeError("notification unavailable")
    result = liveness.check(_now(), root=tmp_path, is_running=lambda: False, launch=lambda: None,
                            sleep=lambda _: None, wait_s=0, notify=fail)
    assert result["exit_code"] == 1
    assert list((tmp_path / "claude_liveness/alerts").glob("*.txt"))


def test_check_log_appends_each_non_closed_run(tmp_path):
    for running in (True, False):
        liveness.check(_now(), root=tmp_path, is_running=lambda r=running: r, launch=lambda: None,
                       sleep=lambda _: None, wait_s=0)
    rows = [json.loads(line) for line in (tmp_path / "claude_liveness/checks_2026-11-10.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["claude_running", "still_down"]
    assert rows[0]["waited_s"] == 0


def test_source_never_stops_or_deletes_claude():
    source = Path(liveness.__file__).read_text()
    assert not re.search(r"\b(?:quit|kill|pkill)\b|\brm\s", source, re.I)


def test_launchd_template_runs_checkout_venv_and_has_35_local_fires():
    text = TEMPLATE.read_text()
    rendered = text.replace("REPLACE_WITH_REPO_PATH", "/repo").replace("REPLACE_WITH_CRON_TRACKING_ROOT", "/cron")
    assert "REPLACE_WITH_" not in rendered
    agent = plistlib.loads(rendered.encode())
    assert agent["Label"] == "com.all-stock-dca.claude-liveness"
    assert agent["ProgramArguments"] == ["/repo/.venv/bin/python", "-m", "scripts.claude_liveness", "--notify"]
    assert agent["WorkingDirectory"] == "/repo"
    assert agent["EnvironmentVariables"]["DCA_CRON_TRACKING_ROOT"] == "/cron"
    entries = agent["StartCalendarInterval"]
    assert len(entries) == 35
    assert {(e["Weekday"], e["Hour"], e["Minute"]) for e in entries} == {
        (day, hour, 15) for day in range(1, 6) for hour in range(9, 16)
    }
    assert agent["StandardOutPath"] == agent["StandardErrorPath"] == "/tmp/dca-claude-liveness.log"
    assert "America/New_York" in text and "09:35" in text and "09:15" in text
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text


def test_process_check_includes_ancestors_so_a_claude_session_run_sees_claude():
    # 2026-11-09 review: without -a, macOS pgrep skips its ancestors, so run from a Claude session the check reported
    # the open app as down.
    import inspect

    from scripts import claude_liveness

    assert '"pgrep", "-a", "-x", "Claude"' in inspect.getsource(claude_liveness._pgrep_running)
