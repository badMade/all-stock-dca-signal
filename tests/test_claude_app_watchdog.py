"""The Claude app watchdog (operator request 2026-11-09).

2026-11-09: the Claude desktop app was not running from 22:25 ET Sunday until 19:12 ET Monday while the Mac stayed
awake. The seven live DCA slots are Claude scheduled tasks, so none fired in its window; when the app came back all
seven fired at once (00:24 UTC) and aborted GATE1_OFF_WINDOW. Nothing was bought.

``deploy/launchd/claude_app_watchdog.sh`` runs every 5 minutes and, only Mon-Fri 08:30-16:00 America/New_York, opens
Claude when no Claude process exists. It never quits or restarts a running app and does nothing on a weekend, so it
cannot fight the Saturday ``claude_vm_reset.sh``.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy/launchd/claude_app_watchdog.sh"
TEMPLATE = REPO / "deploy/launchd/com.all-stock-dca.claude-app-watchdog.plist.template"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def mac(tmp_path):
    """A fake Mac: stubs for ps/open/osascript/date that record calls and read a fake ET clock."""
    calls = tmp_path / "calls.log"
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # ps lists Claude's helpers always, and Claude itself only while state/running exists.
    _stub(
        bin_dir,
        "ps",
        f'echo "ps $*" >> "{calls}"; echo "Claude Helper"; echo "Claude Helper (Renderer)"; '
        f'[ -e "{state}/running" ] && echo Claude; exit 0',
    )
    # `open` starts Claude unless state/broken exists.
    _stub(bin_dir, "open", f'echo "open $*" >> "{calls}"; [ -e "{state}/broken" ] || touch "{state}/running"')
    _stub(bin_dir, "osascript", f'echo "osascript $*" >> "{calls}"')
    _stub(
        bin_dir,
        "date",
        f'echo "date TZ=$TZ $*" >> "{calls}"; '
        f'case "$1" in +%u) cat "{state}/weekday";; +%H%M) cat "{state}/hhmm";; *) /bin/date "$@";; esac',
    )

    def run(*args: str, weekday: int = 1, hhmm: str = "0925", running: bool = False, broken: bool = False):
        (state / "weekday").write_text(f"{weekday}\n", encoding="utf-8")
        (state / "hhmm").write_text(f"{hhmm}\n", encoding="utf-8")
        for name, on in (("running", running), ("broken", broken)):
            if on:
                (state / name).touch()
            else:
                (state / name).unlink(missing_ok=True)
        calls.unlink(missing_ok=True)
        env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "CLAUDE_APP_START_WAIT": "2"}
        done = subprocess.run(["/bin/bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
        log = calls.read_text(encoding="utf-8") if calls.exists() else ""
        return done, log

    return run


def test_in_market_hours_with_claude_down_it_opens_claude_and_notifies(mac):
    done, log = mac(weekday=1, hhmm="0925")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("relaunched:")
    assert "open -a Claude" in log
    assert "reopened it" in log  # the notification


def test_a_running_claude_is_left_alone(mac):
    done, log = mac(running=True)
    assert done.returncode == 0
    assert done.stdout.startswith("ok: running")
    assert "open -a" not in log
    assert "osascript" not in log


def test_helpers_alone_do_not_count_as_a_running_claude(mac):
    # Only the "Claude Helper ..." processes are listed: the app itself is gone, so it must be opened.
    done, log = mac(running=False)
    assert done.stdout.startswith("relaunched:")
    assert "open -a Claude" in log


@pytest.mark.parametrize("hhmm", ["0000", "0829", "1600", "1912", "2359"])
def test_outside_the_window_it_never_opens_claude(mac, hhmm):
    done, log = mac(hhmm=hhmm)
    assert done.returncode == 0
    assert done.stdout.startswith("skip: outside_window")
    assert "open -a" not in log


@pytest.mark.parametrize("hhmm", ["0830", "0835", "0935", "1530", "1559"])
def test_inside_the_window_including_leading_zero_times(mac, hhmm):
    # 0835 / 0935 would be invalid octal; the script must compare them as decimal.
    done, log = mac(hhmm=hhmm)
    assert done.stdout.startswith("relaunched:"), done.stdout + done.stderr
    assert "open -a Claude" in log


@pytest.mark.parametrize("weekday", [6, 7])
def test_weekends_are_skipped_so_it_never_fights_the_saturday_vm_reset(mac, weekday):
    done, log = mac(weekday=weekday, hhmm="1000")
    assert done.returncode == 0
    assert done.stdout.startswith("skip: weekend")
    assert "open -a" not in log


def test_force_ignores_the_window(mac):
    done, log = mac("--force", weekday=7, hhmm="2300")
    assert done.stdout.startswith("relaunched:")
    assert "open -a Claude" in log


def test_a_launch_that_never_comes_up_exits_1_with_a_loud_notification(mac):
    done, log = mac(broken=True)
    assert done.returncode == 1
    assert done.stdout.startswith("failed: did_not_start")
    assert "did not start" in log


def test_the_clock_is_read_in_new_york_time():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "TZ=America/New_York date +%u" in text
    assert "TZ=America/New_York date +%H%M" in text


def test_the_script_never_quits_or_kills_claude():
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "quit app" not in code
    assert "kill" not in code
    assert "rm " not in code


# -- the launchd agent -----------------------------------------------------------------------------------------------


def _agent() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8").replace("REPLACE_WITH_REPO_PATH", "/repo")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_agent_runs_the_script_every_five_minutes():
    agent = _agent()
    assert agent["Label"] == "com.all-stock-dca.claude-app-watchdog"
    # Same launch path as claude-vm-reset (TCC, 2026-11-07/08): venv Python, then /bin/bash, never /bin/sh.
    assert agent["ProgramArguments"] == [
        "/repo/.venv/bin/python", "-c",
        'import subprocess, sys; sys.exit(subprocess.call(["/bin/bash", sys.argv[1]]))',
        "/repo/deploy/launchd/claude_app_watchdog.sh",
    ]
    assert agent["StartInterval"] == 300
    assert "KeepAlive" not in agent  # launchd must not own Claude's lifecycle


def test_agent_logs_to_tmp_and_holds_no_secret():
    agent = _agent()
    assert agent["StandardOutPath"].startswith("/tmp/") and agent["StandardErrorPath"].startswith("/tmp/")
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
