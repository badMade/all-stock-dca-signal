"""The weekly Cowork VM reset (operator request 2026-11-04).

2026-11-04: every Claude slot aborted at Gate 2 (GATE2_ABORT_DISK_LOW / GATE2_ABORT_NO_PYTHON311). The Cowork bridge
VM's 9.6 GB root disk was full: a 2.7 GB /tmp/out left by an earlier session, owned by ``nobody`` and undeletable
from inside (no sudo). The VM disk is a file on the Mac,
``~/Library/Application Support/Claude/vm_bundles/claudevm.bundle/rootfs.img``, and it persists across app restarts.
Deleting it with Claude quit made the app build a fresh VM on the next start (verified 2026-11-04 20:23 ET: 4.8 GB
used, 4.8 GB free, /tmp empty).

``deploy/launchd/claude_vm_reset.sh`` does that every Saturday at 03:00 (no slot runs on a weekend): quit Claude, wait
until it has exited, delete rootfs.img only, reopen Claude. It never deletes the disk while Claude is running, never
touches sessiondata.img, and refuses to run on a weekday unless forced.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy/launchd/claude_vm_reset.sh"
TEMPLATE = REPO / "deploy/launchd/com.all-stock-dca.claude-vm-reset.plist.template"


# -- the script, with osascript / pgrep / open / date stubbed on PATH --------------------------------------------------


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def mac(tmp_path):
    """A fake Mac: a bundle folder with both images, and stubs that record what the script asked for."""
    bundle = tmp_path / "claudevm.bundle"
    bundle.mkdir()
    (bundle / "rootfs.img").write_bytes(b"old root disk")
    (bundle / "sessiondata.img").write_bytes(b"session data")
    calls = tmp_path / "calls.log"
    state = tmp_path / "state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Claude "runs" while state/running exists; quitting removes it unless state/stuck exists.
    _stub(bin_dir, "osascript", f'echo "osascript $*" >> "{calls}"; '
          f'if [ -e "{state}/denied" ]; then echo "execution error: Not authorized to send Apple events to Claude. (-1743)" >&2; exit 1; fi; '
          f'if [ -e "{state}/late" ]; then (sleep 3; rm -f "{state}/running"; touch "{state}/late_quit") & '
          f'elif [ ! -e "{state}/stuck" ]; then rm -f "{state}/running"; fi')
    _stub(bin_dir, "pgrep", f'echo "pgrep $*" >> "{calls}"; [ -e "{state}/running" ]')
    _stub(bin_dir, "open", f'echo "open $*" >> "{calls}"; '
          f'if [ -e "{state}/late" ]; then while [ ! -e "{state}/late_quit" ]; do sleep 0.1; done; fi; '
          f'touch "{state}/running"')
    _stub(bin_dir, "date", f'if [ "$1" = "+%u" ]; then cat "{state}/weekday"; else /bin/date "$@"; fi')
    (state / "running").touch()
    (state / "weekday").write_text("6\n", encoding="utf-8")  # Saturday

    def run(*args: str, weekday: int = 6, stuck: bool = False, running: bool = True, denied: bool = False,
            late: bool = False):
        (state / "weekday").write_text(f"{weekday}\n", encoding="utf-8")
        if denied:
            (state / "denied").touch()
        if late:
            (state / "late").touch()
        if stuck:
            (state / "stuck").touch()
        if not running:
            (state / "running").unlink(missing_ok=True)
        env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "CLAUDE_VM_DISK": str(bundle / "rootfs.img"),
               "CLAUDE_VM_QUIT_WAIT": "2"}
        done = subprocess.run(["/bin/bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
        log = calls.read_text(encoding="utf-8") if calls.exists() else ""
        return done, log

    run.bundle = bundle
    run.state = state
    return run


def test_on_saturday_it_quits_claude_deletes_only_the_root_disk_and_reopens_claude(mac):
    done, log = mac()
    assert done.returncode == 0, done.stderr
    assert "reset: deleted" in done.stdout
    assert not (mac.bundle / "rootfs.img").exists()
    assert (mac.bundle / "sessiondata.img").read_bytes() == b"session data"  # never touched
    assert log.index("osascript") < log.index("open")  # quit first, reopen last
    assert 'quit app "Claude"' in log
    assert "open -a Claude" in log


def test_it_never_deletes_the_disk_while_claude_is_still_running(mac):
    done, log = mac(stuck=True)
    assert done.returncode == 1
    assert "refused: claude_still_running" in done.stdout
    assert (mac.bundle / "rootfs.img").exists()
    assert "open -a Claude" in log
    assert log.index("osascript") < log.index("open -a Claude")


def test_late_quit_reopens_claude_without_deleting_the_disk(mac):
    done, log = mac(late=True)
    assert done.returncode == 1
    assert "refused: claude_still_running" in done.stdout
    assert "open -a Claude" in log
    assert (mac.state / "running").exists()
    assert (mac.bundle / "rootfs.img").exists()


def test_it_refuses_on_a_weekday_so_it_can_never_interrupt_a_slot(mac):
    for weekday in (1, 2, 3, 4, 5):
        done, log = mac(weekday=weekday)
        assert done.returncode == 2
        assert "refused: weekday" in done.stdout
        assert (mac.bundle / "rootfs.img").exists()
        assert "osascript" not in log  # did not even quit Claude


def test_refusal_status_lines_include_timestamps(mac):
    timestamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    weekday, _ = mac(weekday=3)
    still_running, _ = mac(stuck=True)
    for result, status in ((weekday, "refused: weekday"),
                           (still_running, "refused: claude_still_running")):
        line = next(line for line in result.stdout.splitlines() if status in line)
        assert timestamp.search(line), line


def test_force_allows_a_weekday_run_for_a_manual_reset(mac):
    done, _ = mac("--force", weekday=3)
    assert done.returncode == 0, done.stderr
    assert not (mac.bundle / "rootfs.img").exists()


def test_a_missing_disk_is_not_an_error_and_claude_still_reopens(mac):
    (mac.bundle / "rootfs.img").unlink()
    done, log = mac()
    assert done.returncode == 0, done.stderr
    assert "reset: no_disk" in done.stdout
    assert "open -a Claude" in log


def test_when_claude_is_not_running_it_still_deletes_and_starts_claude(mac):
    done, log = mac(running=False)
    assert done.returncode == 0, done.stderr
    assert not (mac.bundle / "rootfs.img").exists()
    assert "open -a Claude" in log


def test_the_script_only_ever_removes_one_file_named_rootfs_img():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "sessiondata" not in text
    assert "rm -rf" not in text  # one file, never a folder
    assert "rootfs.img" in text


# -- the launchd agent -----------------------------------------------------------------------------------------------


def _agent() -> dict:
    text = TEMPLATE.read_text(encoding="utf-8").replace("REPLACE_WITH_REPO_PATH", "/repo")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_agent_runs_the_script_every_saturday_at_three_am():
    agent = _agent()
    assert agent["Label"] == "com.all-stock-dca.claude-vm-reset"
    # 2026-11-07: the first Saturday run failed with "/bin/sh: ...claude_vm_reset.sh: Operation not permitted".
    # macOS privacy protection (TCC) grants ~/Documents access per binary; /bin/sh run straight from launchd has none.
    # Start it through the checkout's venv Python like the payload agent does: a child inherits the parent's access.
    assert agent["ProgramArguments"] == [
        "/repo/.venv/bin/python", "-c",
        'import subprocess, sys; sys.exit(subprocess.call(["/bin/bash", sys.argv[1]]))',
        "/repo/deploy/launchd/claude_vm_reset.sh",
    ]
    assert agent["StartCalendarInterval"] == {"Weekday": 6, "Hour": 3, "Minute": 0}
    assert "StartInterval" not in agent and "RunAtLoad" not in agent  # never on install, never on a timer


def test_agent_logs_to_tmp_and_holds_no_secret():
    agent = _agent()
    assert agent["StandardOutPath"].startswith("/tmp/") and agent["StandardErrorPath"].startswith("/tmp/")
    text = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\d{8,}", text)
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" not in text
    assert "CLAUDE_VM_DISK" not in text  # the agent uses the script's default path, not an override


def test_every_launchd_agent_starts_through_the_checkout_venv_python():
    # The same TCC failure must not come back with the next agent: none may launch a shell (or anything else) on a
    # file under ~/Documents directly. Every template's first program is the checkout's venv Python.
    for template in sorted((REPO / "deploy/launchd").glob("*.plist.template")):
        text = template.read_text(encoding="utf-8")
        for placeholder in ("REPLACE_WITH_REPO_PATH", "REPLACE_WITH_CRON_TRACKING_ROOT", "REPLACE_WITH_CODEX_PATH"):
            text = text.replace(placeholder, "/repo")
        program = plistlib.loads(text.encode("utf-8"))["ProgramArguments"][0]
        assert program == "/repo/.venv/bin/python", (template.name, program)


# -- 2026-11-08: what the Saturday runs taught us ----------------------------------------------------------------------
# With the venv-Python wrapper the script was still refused ("Operation not permitted") while it ran under /bin/sh,
# and ran once it ran under /bin/bash, the shell the payload agents use: /bin/sh on macOS is a launcher that re-execs
# another shell, and that hop loses the ~/Documents grant. Then it stopped at "refused: claude_still_running" with no
# reason, because the script threw away osascript's output. A refused quit must say why in the agent's log.


def test_the_agent_runs_the_script_with_bash_not_sh():
    args = _agent()["ProgramArguments"]
    assert '"/bin/bash"' in args[2] and '"/bin/sh"' not in args[2]


def test_a_refused_quit_writes_osascripts_reason_to_the_log(mac):
    done, _ = mac(denied=True)
    assert done.returncode == 1
    out = done.stdout + done.stderr  # launchd sends both to /tmp/dca-claude-vm-reset.log
    assert "refused: claude_still_running" in out
    assert "Not authorized to send Apple events" in out  # the reason, not just the outcome
    assert (mac.bundle / "rootfs.img").exists()


def test_a_successful_quit_logs_nothing_extra_from_osascript(mac):
    done, _ = mac()
    assert done.returncode == 0
    assert "osascript" not in done.stdout + done.stderr
