"""``schedule_watchdog --notify``: put a failing daily audit in front of the operator.

2026-10-21: the Oct 13-16 aborts (nine of ten failing slot-days) each wrote a Gate 1/2 alert file and
nobody read one. ``scripts/schedule_watchdog.py`` already audits independently of the executor sessions and
writes idempotent per-slot alert files, but nothing ever scheduled it and it cannot interrupt anyone.
``--notify`` adds a macOS notification on top of those files; it deliberately does NOT add a second
alert-file writer next to the watchdog's (``dca_alerts.write_alert_file`` numbers a new file per re-run,
which the watchdog docs reject for a recurring check). Tests pinned before any of it exists.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import schedule_watchdog, verify_schedule  # noqa: E402

FAILING_DAY = "2026-10-16"  # a past Friday with no evidence under an empty root
CLEAN_DAY = "2026-10-17"  # a Saturday: every slot is market_closed, so the audit is OK


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule_watchdog, "notify_macos", lambda title, message: calls.append((title, message)))
    return calls


def alert_files(root):
    directory = root / "schedule_watchdog" / "alerts"
    return sorted(directory.glob("*.txt")) if directory.exists() else []


def test_notify_raises_one_notification_naming_the_date_and_failing_slots(tmp_path, notified):
    code = schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path), "--notify"])

    assert code == 1  # the watchdog's verdict is unchanged
    assert len(notified) == 1
    _, message = notified[0]
    assert FAILING_DAY in message
    assert "0935" in message and "1530" in message


def test_notify_keeps_the_watchdogs_own_per_slot_alert_files(tmp_path, notified):
    schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path), "--notify"])

    names = [f.name for f in alert_files(tmp_path)]
    assert len(names) == 7  # one per failing slot, from the existing writer
    assert all(n.endswith("-SCHEDULE_WATCHDOG.txt") for n in names)
    assert not (tmp_path / "schedule_audit").exists()  # no second alert-file path


def test_a_rerun_stays_idempotent_with_notify(tmp_path, notified):
    for _ in range(2):
        schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path), "--notify"])

    assert len(alert_files(tmp_path)) == 7  # overwritten in place, never numbered
    assert len(notified) == 2  # each invocation still notifies; the daily agent runs once


def test_notification_carries_no_run_state_contents(tmp_path, notified):
    schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path), "--notify"])

    _, message = notified[0]
    # Reasons quote run_state values, which are file contents: keep them out of a desktop banner.
    assert "run_state" not in message
    assert len(message) < 300


def test_a_clean_day_is_silent(tmp_path, notified):
    code = schedule_watchdog.main(["--date", CLEAN_DAY, "--root", str(tmp_path), "--notify"])

    assert code == 0
    assert notified == []
    assert alert_files(tmp_path) == []


def test_without_the_flag_nothing_is_notified(tmp_path, notified):
    code = schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path)])

    assert code == 1
    assert notified == []


def test_notify_is_independent_of_the_alert_file_switch(tmp_path, notified):
    schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path), "--notify", "--no-alert-file"])

    assert len(notified) == 1
    assert alert_files(tmp_path) == []


def test_a_failing_notifier_keeps_the_exit_code_and_the_alert_files(tmp_path, monkeypatch, capsys):
    def broken(title, message):
        raise OSError("osascript unavailable")

    monkeypatch.setattr(schedule_watchdog, "notify_macos", broken)

    code = schedule_watchdog.main(["--date", FAILING_DAY, "--root", str(tmp_path), "--notify"])

    assert code == 1
    assert len(alert_files(tmp_path)) == 7
    assert "notification" in capsys.readouterr().err.lower()  # a warning, not a crash


def test_notify_macos_passes_text_as_arguments_never_inside_the_script(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(schedule_watchdog.subprocess, "run", fake_run)
    hostile = 'x" & (do shell script "touch /tmp/pwned") & "'

    schedule_watchdog.notify_macos("Schedule watchdog", hostile)

    cmd = seen["cmd"]
    assert cmd[0] == "osascript"
    scripts = [cmd[i + 1] for i, part in enumerate(cmd[:-1]) if part == "-e"]
    assert scripts and all("pwned" not in s for s in scripts)  # text is not interpolated into the script
    assert hostile in cmd  # it travels as its own argv item, which AppleScript reads as data
    assert seen["kwargs"].get("timeout")  # a hung notifier must not hang the audit


def test_verify_schedule_has_no_second_alerting_path():
    with pytest.raises(SystemExit) as exc:
        verify_schedule.main(["--date", FAILING_DAY, "--alert"])
    assert exc.value.code == 2  # argparse rejects the unknown flag
    assert not hasattr(verify_schedule, "notify_macos")
