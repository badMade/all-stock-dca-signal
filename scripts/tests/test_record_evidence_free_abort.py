"""Tests for scripts/record_evidence_free_abort.sh and scripts/abort_markers.py, and the watchdog wiring.

2026-10-29: a slot that aborted off-window (Gate 1) or with no Python 3.11 (Gate 2) reached only the
push channel -- no durable alert file -- and the watchdog called it ``slot_did_not_fire``. These tests
run the real shell script, then feed what it wrote through ``scripts.schedule_watchdog``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.abort_markers import (  # noqa: E402
    FAILURE_KEY_FIRED_ABORTED,
    marker_path,
    read_abort_markers,
)
from scripts.alert_digest import _ALERT_FILENAME  # noqa: E402
from scripts.schedule_watchdog import alert_body, audit_day, failing_rows, main  # noqa: E402
from scripts.verify_schedule import (  # noqa: E402
    FAILURE_KEY_SLOT_DID_NOT_FIRE,
    SLOT_EVIDENCE_LAYOUT,
    verify_day,
)

SCRIPT = REPO / "scripts" / "record_evidence_free_abort.sh"
ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 28)
AFTER_CLOSE = datetime(2026, 10, 28, 18, 0, tzinfo=ET)
# 11:31 ET on 2026-10-28 (EDT, -04:00): inside the 11:30 slot's window.
IN_WINDOW_UTC = "2026-10-28T15:31:00Z"
# 23:10 ET the same evening: a manual night-time "Run now".
NIGHT_UTC = "2026-10-29T03:10:00Z"


def _record(root: Path, *args: str, body: str = "Slot aborted.\n", path: str | None = None,
            python: bool = True) -> subprocess.CompletedProcess:
    env = {"DCA_CRON_TRACKING_ROOT": str(root), "DCA_DISABLE_DOTENV": "1", "HOME": str(root)}
    if path is not None:
        env["PATH"] = path
    elif python:
        env["PATH"] = f"{Path(sys.executable).parent}:/usr/bin:/bin"
    return subprocess.run(["/bin/sh", str(SCRIPT), *args], input=body, env=env,
                          capture_output=True, text=True, timeout=30, check=False)


def _no_python_path(tmp_path: Path) -> str:
    """A PATH with the POSIX tools the script uses and no python3 anywhere on it."""
    bin_dir = tmp_path / "nopy-bin"
    bin_dir.mkdir()
    for tool in ("sed", "grep", "date", "mkdir", "dirname", "tr", "cat", "rm"):
        found = shutil.which(tool, path="/usr/bin:/bin")
        assert found, tool
        os.symlink(found, bin_dir / tool)
    return str(bin_dir)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "cron"
    path.mkdir()
    return path


def _prompt_alert_dirs() -> dict[str, str]:
    """slot -> the channel-2 alert dir each live prompt's own Gate 9 resolves through dca_paths."""
    found = {}
    for prompt in (REPO / "claude/dca-executor-prompts/live-mirror").glob("[0-9][0-9][0-9][0-9]ET-*.txt"):
        match = re.search(r"stream_root\(((?:'[a-z0-9_]+', )+)'alerts'", prompt.read_text(encoding="utf-8"))
        assert match, prompt.name
        found[prompt.name[:4]] = "/".join(re.findall(r"'([a-z0-9_]+)'", match.group(1)))
    return found


def test_slot_to_alert_dir_mapping_matches_every_live_prompt_and_verify_schedule(root):
    prompt_dirs = _prompt_alert_dirs()
    assert set(prompt_dirs) == set(SLOT_EVIDENCE_LAYOUT)
    for slot, (_, parent) in SLOT_EVIDENCE_LAYOUT.items():
        done = _record(root, "--slot", slot, "--gate", "gate1", "--reason", "GATE1_OFF_WINDOW",
                       "--fired-at-utc", IN_WINDOW_UTC)
        assert done.returncode == 0, done.stderr
        alert = Path(done.stdout.split("ALERT_FILE=", 1)[1].splitlines()[0])
        assert alert.parent == root / parent / "alerts"
        assert alert.parent == root / prompt_dirs[slot] / "alerts"


def test_writes_the_channel_2_alert_file_with_a_digest_parseable_name(root):
    done = _record(root, "--slot", "1130", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   "--fired-at-utc", IN_WINDOW_UTC, body="11:30 aborted at Gate 2: no Python 3.11.\n")
    assert done.returncode == 0, done.stderr
    alert = root / "intraday_dca" / "alerts" / "2026-10-28-1130-gate2.txt"
    assert alert.read_text() == "11:30 aborted at Gate 2: no Python 3.11.\n"
    match = _ALERT_FILENAME.match(alert.name)
    assert match and match["slot"] == "1130" and match["gate"] == "gate2"
    assert f"ALERT_FILE={alert}" in done.stdout


def test_a_second_abort_numbers_its_alert_file_instead_of_overwriting(root):
    for body in ("first\n", "second\n"):
        assert _record(root, "--slot", "1230", "--gate", "gate1", "--reason", "GATE1_OFF_WINDOW",
                       "--fired-at-utc", IN_WINDOW_UTC, body=body).returncode == 0
    alerts = root / "midday_dca" / "alerts"
    assert (alerts / "2026-10-28-1230-gate1.txt").read_text() == "first\n"
    assert (alerts / "2026-10-28-1230-gate1.2.txt").read_text() == "second\n"
    assert len(read_abort_markers(root, DAY, "1230")) == 2


def test_account_shaped_digits_and_email_addresses_are_redacted(root):
    body = "Account 123456789 failed; mail ops.person@example.com; at 2026-10-28 11:31.\n"
    assert _record(root, "--slot", "1030", "--gate", "gate1", "--reason", "GATE1_OFF_WINDOW",
                   "--fired-at-utc", IN_WINDOW_UTC, body=body).returncode == 0
    text = (root / "morning_dca" / "alerts" / "2026-10-28-1030-gate1.txt").read_text()
    assert "123456789" not in text and "example.com" not in text
    assert "2026-10-28 11:31" in text


def test_the_marker_uses_the_et_trading_day_not_the_utc_date(root):
    """03:10Z on Oct 29 is 23:10 ET on Oct 28: both files belong to Oct 28."""
    assert _record(root, "--slot", "1530", "--gate", "gate1", "--reason", "GATE1_OFF_WINDOW",
                   "--fired-at-utc", NIGHT_UTC).returncode == 0
    assert (root / "ce16ba6b" / "scheduled_runs" / "alerts" / "2026-10-28-1530-gate1.txt").is_file()
    [marker] = read_abort_markers(root, DAY, "1530")
    assert marker["fired_at_et"] == datetime(2026, 10, 28, 23, 10, tzinfo=ET)
    assert marker["gate"] == "gate1" and marker["reason_code"] == "GATE1_OFF_WINDOW"
    assert marker_path(root, DAY, "1530").is_file()


def test_without_any_python3_it_still_writes_both_files_and_warns(root, tmp_path):
    """No --fired-at-utc: the fire time is "now", which plain `date` converts on GNU and BSD alike."""
    done = _record(root, "--slot", "1430", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   path=_no_python_path(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "no python3 to resolve dca_paths" in done.stderr
    assert list((root / "intraday_dca" / "alerts").glob("*-1430-gate2.txt"))
    assert list((root / "abort_markers").glob("*/1430.jsonl"))


def test_without_python3_an_explicit_fire_time_is_never_replaced_by_now(root, tmp_path):
    """BSD date cannot parse -d; filing a 2026-10-28 abort under today's date would misfile it."""
    no_py = _no_python_path(tmp_path)
    done = _record(root, "--slot", "1430", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   "--fired-at-utc", "2026-10-28T18:31:00Z", path=no_py)
    gnu = subprocess.run(["date", "-d", "2026-10-28T18:31:00Z"], env={"PATH": no_py},
                         capture_output=True, check=False).returncode == 0
    if gnu:
        assert done.returncode == 0, done.stderr
        assert (root / "intraday_dca" / "alerts" / "2026-10-28-1430-gate2.txt").is_file()
    else:
        assert done.returncode == 1
        assert "could not convert" in done.stderr
        assert list(root.iterdir()) == []


@pytest.mark.parametrize("args", [
    ["--slot", "1145", "--gate", "gate1", "--reason", "X"],
    ["--slot", "1130", "--gate", "gate 1", "--reason", "X"],
    ["--slot", "1130", "--gate", "gate1", "--reason", "lower"],
    ["--slot", "1130", "--gate", "gate1", "--reason", "X", "--fired-at-utc", "11:31"],
    ["--slot", "1130", "--gate"],
    ["--bogus", "1"],
])
def test_bad_arguments_exit_2_and_write_nothing(root, args):
    done = _record(root, *args)
    assert done.returncode == 2
    assert list(root.iterdir()) == []


def test_an_unwritable_root_exits_1(root):
    root.chmod(0o555)
    try:
        done = _record(root, "--slot", "1130", "--gate", "gate1", "--reason", "GATE1_OFF_WINDOW",
                       "--fired-at-utc", IN_WINDOW_UTC)
    finally:
        root.chmod(0o755)
    assert done.returncode == 1
    assert "could not be written" in done.stderr


def _row(report, slot):
    return next(r for r in report["slots"] if r["slot_id"] == slot)


def test_watchdog_reports_an_in_window_abort_as_fired_not_as_did_not_fire(root):
    assert _record(root, "--slot", "1130", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   "--fired-at-utc", IN_WINDOW_UTC).returncode == 0
    report = audit_day(DAY, AFTER_CLOSE, root=root)
    row = _row(report, "1130")
    assert row["status"] == "no_evidence"  # still failing: a marker is never run evidence
    assert row in failing_rows(report)
    assert row["failure_key"] == FAILURE_KEY_FIRED_ABORTED
    assert any("GATE2_ABORT_NO_PYTHON311" in reason for reason in row["reasons"])
    assert f"Key: {FAILURE_KEY_FIRED_ABORTED}" in alert_body(report, row)
    # Every other slot is untouched.
    assert _row(report, "1330")["failure_key"] == FAILURE_KEY_SLOT_DID_NOT_FIRE


def test_watchdog_keeps_did_not_fire_for_an_off_window_marker_and_says_why(root):
    assert _record(root, "--slot", "1530", "--gate", "gate1", "--reason", "GATE1_OFF_WINDOW",
                   "--fired-at-utc", NIGHT_UTC).returncode == 0
    row = _row(audit_day(DAY, AFTER_CLOSE, root=root), "1530")
    assert row["failure_key"] == FAILURE_KEY_SLOT_DID_NOT_FIRE
    assert any("outside this slot's window (23:10 ET)" in reason for reason in row["reasons"])
    assert row["abort_markers"][0]["reason_code"] == "GATE1_OFF_WINDOW"


def test_verify_schedule_itself_is_unchanged_by_markers(root):
    """verify_schedule ships in the Perplexity backup planner; the retag lives in the watchdog only."""
    assert _record(root, "--slot", "1130", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   "--fired-at-utc", IN_WINDOW_UTC).returncode == 0
    assert _row(verify_day(DAY, AFTER_CLOSE, root=root), "1130")["failure_key"] == FAILURE_KEY_SLOT_DID_NOT_FIRE


def test_a_marker_never_touches_a_verified_or_incomplete_row(root):
    (root / "intraday_dca" / "20261028_1130ET").mkdir(parents=True)
    assert _record(root, "--slot", "1130", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   "--fired-at-utc", IN_WINDOW_UTC).returncode == 0
    row = _row(audit_day(DAY, AFTER_CLOSE, root=root), "1130")
    assert row["status"] == "evidence_incomplete"
    assert row["failure_key"] is None
    assert "abort_markers" not in row


def test_malformed_or_foreign_marker_lines_are_ignored(root):
    path = marker_path(root, DAY, "1230")
    path.parent.mkdir(parents=True)
    path.write_text("\n".join([
        "not json",
        json.dumps({"kind": "evidence_free_abort", "slot_id": "1330", "trading_day": "2026-10-28",
                    "fired_at_utc": "2026-10-28T16:31:00Z"}),
        json.dumps({"kind": "evidence_free_abort", "slot_id": "1230", "trading_day": "2026-10-27",
                    "fired_at_utc": "2026-10-28T16:31:00Z"}),
        json.dumps({"kind": "other", "slot_id": "1230", "trading_day": "2026-10-28",
                    "fired_at_utc": "2026-10-28T16:31:00Z"}),
        json.dumps({"kind": "evidence_free_abort", "slot_id": "1230", "trading_day": "2026-10-28",
                    "fired_at_utc": "yesterday"}),
    ]) + "\n")
    assert read_abort_markers(root, DAY, "1230") == []
    assert _row(audit_day(DAY, AFTER_CLOSE, root=root), "1230")["failure_key"] == FAILURE_KEY_SLOT_DID_NOT_FIRE


def test_watchdog_cli_json_carries_the_new_key(root, capsys):
    assert _record(root, "--slot", "0935", "--gate", "gate2", "--reason", "GATE2_ABORT_NO_PYTHON311",
                   "--fired-at-utc", "2026-10-28T13:36:00Z").returncode == 0
    code = main(["--date", DAY.isoformat(), "--root", str(root), "--json", "--no-alert-file"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert _row(report, "0935")["failure_key"] == FAILURE_KEY_FIRED_ABORTED
