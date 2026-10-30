"""Contract tests for the Python-free bridge abort writer (Key bridge_python_alert_channels)."""

import json
import os
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_alerts
import dca_env
import dca_paths
from scripts import alert_digest, verify_schedule

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/bridge_abort_marker.sh"
SHELLS = ["sh"] + (["dash"] if shutil.which("dash") else [])
SLOTS = {
    "0935": ("market_open", "market_open_dca"),
    "1030": ("morning", "morning_dca"),
    "1130": ("intraday", "intraday_dca"),
    "1230": ("midday", "midday_dca"),
    "1330": ("intraday", "intraday_dca"),
    "1430": ("intraday", "intraday_dca"),
    "1530": ("afternoon", "ce16ba6b"),
}
STAMP = "2026-10-29T11:30:12-04:00"


def _run(shell: str, *args: str, root: Path, script: Path = SCRIPT, extra_env: dict | None = None):
    env = os.environ.copy()
    for key in ("DCA_CRON_TRACKING_ROOT", "DCA_ATTEMPT_LOG_ROOT", "DCA_DISABLE_DOTENV"):
        env.pop(key, None)
    if not extra_env or "DCA_CRON_TRACKING_ROOT" not in extra_env:
        env["DCA_CRON_TRACKING_ROOT"] = str(root)
    if extra_env and extra_env.get("DCA_DISABLE_DOTENV") == "__UNSET__":
        env.pop("DCA_DISABLE_DOTENV", None)
    elif not extra_env or "DCA_DISABLE_DOTENV" not in extra_env:
        env["DCA_DISABLE_DOTENV"] = "1"
    if extra_env:
        env.update({key: value for key, value in extra_env.items() if value != "__UNSET__"})
    return subprocess.run([shell, str(script), *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)


def _good_args(*, slot: str = "1130", stamp: str = STAMP, reason: str = "bridge shell has no Python 3.11") -> tuple[str, ...]:
    return ("--slot", slot, "--gate", "2", "--reason", reason, "--started-at", stamp)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("slot,expected", SLOTS.items())
def test_slot_mappings_match_python_sources(shell: str, slot: str, expected: tuple[str, str], tmp_path: Path) -> None:
    assert verify_schedule.SLOT_EVIDENCE_LAYOUT[slot][0] == expected[0]
    assert dca_paths.stream_artifact_dir(expected[0]) == expected[1]
    assert dca_alerts.stream_artifact_dir(expected[0]) == expected[1]
    # Exercise the SCRIPT's case table, not just the Python constants: --print-roots exits
    # before the slot is mapped, so an earlier version of this test passed even with a
    # deliberately wrong 1230 -> intraday mapping (Claude review, Rock B mutation check).
    result = _run(shell, *_good_args(slot=slot), root=tmp_path)
    assert result.returncode == 0, result.stderr
    day_dir = STAMP.split("T", 1)[0].replace("-", "")
    assert (tmp_path / "attempt_log" / day_dir / f"{expected[0]}.attempts.jsonl").is_file()
    assert list((tmp_path / expected[1] / "alerts").glob(f"*-{slot}-gate2.txt"))


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("off_window", [False, True])
def test_marker_writes_files_and_schedule_reader_classifies_attempt(shell: str, off_window: bool, tmp_path: Path) -> None:
    root = tmp_path / "cron"
    args = list(_good_args()) + (["--off-window"] if off_window else [])
    result = _run(shell, *args, root=root)
    assert result.returncode == 0, result.stderr
    attempt = root / "attempt_log/20261029/intraday.attempts.jsonl"
    marker = json.loads(attempt.read_text(encoding="utf-8").splitlines()[0])
    assert marker == {
        "stream": "intraday",
        "error_class": "BridgeAbort",
        "error_message": "bridge shell has no Python 3.11",
        "failure_phase": "off_window" if off_window else "before_run_artifact",
        "triggered_at_utc": STAMP,
        "gate": "Gate 2",
        "slot": "1130",
        "source": "bridge_abort_marker",
    }
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("DCA_ATTEMPT_LOG_ROOT", str(root / "attempt_log"))
    try:
        now = datetime(2026, 10, 29, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        report = verify_schedule.verify_day(date(2026, 10, 29), now, root=root)
        row = next(item for item in report["slots"] if item["slot_id"] == "1130")
        assert row["status"] == "no_evidence"
        assert row["failure_key"] == ("slot_did_not_fire" if off_window else None)
    finally:
        monkeypatch_env.undo()
    alert_paths = list((root / "intraday_dca/alerts").glob("*.txt"))
    assert len(alert_paths) == 1
    body = alert_paths[0].read_text(encoding="utf-8")
    assert "Failing gate: Gate 2" in body and "Slot: 1130" in body
    records = alert_digest.load_alert_files(alert_paths[0].parent)
    assert len(records) == 1 and records[0].slot == "1130" and records[0].gate == "Gate 2"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("stamp", [None, "bad", "2026-10-29T11:30:12+00:00", "2026-10-29T11:30Z"])
def test_missing_malformed_or_utc_timestamp_writes_nothing(shell: str, stamp: str | None, tmp_path: Path) -> None:
    args = ["--slot", "1130", "--gate", "2", "--reason", "bad"]
    if stamp is not None:
        args.extend(("--started-at", stamp))
    result = _run(shell, *args, root=tmp_path / "cron")
    assert result.returncode == 2
    assert not (tmp_path / "cron").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_unknown_slot_writes_nothing(shell: str, tmp_path: Path) -> None:
    result = _run(shell, *_good_args(slot="9999"), root=tmp_path / "cron")
    assert result.returncode == 2
    assert not (tmp_path / "cron").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_json_escaping_control_stripping_and_digit_redaction(shell: str, tmp_path: Path) -> None:
    secret_digits = "".join(str(n) for n in range(1, 10))
    reason = f'quote " slash \\ newline\n control\x01 number {secret_digits}'
    result = _run(shell, *_good_args(reason=reason), root=tmp_path / "cron")
    assert result.returncode == 0, result.stderr
    marker = json.loads((tmp_path / "cron/attempt_log/20261029/intraday.attempts.jsonl").read_text())
    assert marker["error_message"] == 'quote " slash \\ newline control number [REDACTED]'
    body = next((tmp_path / "cron/intraday_dca/alerts").glob("*.txt")).read_text()
    assert secret_digits not in body and "[REDACTED]" in body


@pytest.mark.parametrize("shell", SHELLS)
def test_alert_collision_uses_numeric_suffix(shell: str, tmp_path: Path) -> None:
    root = tmp_path / "cron"
    directory = root / "intraday_dca/alerts"
    directory.mkdir(parents=True)
    (directory / "2026-10-29-1130-gate2.txt").write_text("old")
    first = _run(shell, *_good_args(), root=root)
    second = _run(shell, *_good_args(), root=root)
    assert first.returncode == second.returncode == 0
    assert (directory / "2026-10-29-1130-gate2.2.txt").exists()
    assert (directory / "2026-10-29-1130-gate2.3.txt").exists()


@pytest.mark.parametrize("shell", SHELLS)
def test_roots_env_override_and_trailing_slashes(shell: str, tmp_path: Path) -> None:
    result = _run(shell, "--print-roots", root=tmp_path / "ignored", extra_env={"DCA_CRON_TRACKING_ROOT": str(tmp_path / "cron///"), "DCA_ATTEMPT_LOG_ROOT": str(tmp_path / "attempts//")})
    assert result.returncode == 0
    assert result.stdout == f"CRON_ROOT:{tmp_path}/cron\nATTEMPT_ROOT:{tmp_path}/attempts\n"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("env_text,expected", [
    ("# note\nexport DCA_CRON_TRACKING_ROOT = \"first\"\nDCA_CRON_TRACKING_ROOT='second/path'\nDCA_CRON_TRACKING_ROOT=\n", "second/path"),
    ("DCA_CRON_TRACKING_ROOT=\"\"\n", None),
])
def test_checked_dotenv_root_resolution(shell: str, env_text: str, expected: str | None, tmp_path: Path) -> None:
    copied_root = tmp_path / "checkout"
    scripts_dir = copied_root / "scripts"
    scripts_dir.mkdir(parents=True)
    copied_script = scripts_dir / "bridge_abort_marker.sh"
    shutil.copyfile(SCRIPT, copied_script)
    env_file = copied_root / ".env"
    env_file.write_text(env_text, encoding="utf-8")
    env_file.chmod(0o600)
    result = _run(shell, "--print-roots", root=tmp_path / "unused", script=copied_script, extra_env={"DCA_CRON_TRACKING_ROOT": "", "DCA_DISABLE_DOTENV": "__UNSET__"})
    expected_root = expected or str(copied_root / "cron_tracking")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"CRON_ROOT:{expected_root}\nATTEMPT_ROOT:{expected_root}/attempt_log\n"
    # Compare the parsed fixture with dca_env's actual safe parser, while restoring
    # its module path immediately so this test cannot affect another case.
    previous = dca_env.REPO_ENV_PATH
    try:
        dca_env.REPO_ENV_PATH = env_file
        parsed = dca_env._parse(dca_env._read_checked(env_file) or "")
        assert parsed.get("DCA_CRON_TRACKING_ROOT") == expected
    finally:
        dca_env.REPO_ENV_PATH = previous


@pytest.mark.parametrize("shell", SHELLS)
def test_dotenv_permissions_symlink_disable_and_export_precedence(shell: str, tmp_path: Path) -> None:
    copied_root = tmp_path / "repo"
    scripts_dir = copied_root / "scripts"
    scripts_dir.mkdir(parents=True)
    copied_script = scripts_dir / "bridge_abort_marker.sh"
    shutil.copyfile(SCRIPT, copied_script)
    env_file = copied_root / ".env"
    env_file.write_text("DCA_CRON_TRACKING_ROOT=/from/file\n", encoding="utf-8")
    env_file.chmod(0o644)
    result = _run(shell, "--print-roots", root=tmp_path / "x", script=copied_script, extra_env={"DCA_CRON_TRACKING_ROOT": "", "DCA_DISABLE_DOTENV": "__UNSET__"})
    assert f"CRON_ROOT:{copied_root}/cron_tracking" in result.stdout
    env_file.chmod(0o600)
    result = _run(shell, "--print-roots", root=tmp_path / "x", script=copied_script, extra_env={"DCA_CRON_TRACKING_ROOT": "/from/environment", "DCA_DISABLE_DOTENV": ""})
    assert "CRON_ROOT:/from/environment\n" in result.stdout
    result = _run(shell, "--print-roots", root=tmp_path / "x", script=copied_script, extra_env={"DCA_CRON_TRACKING_ROOT": "", "DCA_DISABLE_DOTENV": "1"})
    assert f"CRON_ROOT:{copied_root}/cron_tracking" in result.stdout
    env_file.unlink()
    env_file.symlink_to(tmp_path / "elsewhere")
    result = _run(shell, "--print-roots", root=tmp_path / "x", script=copied_script, extra_env={"DCA_CRON_TRACKING_ROOT": "", "DCA_DISABLE_DOTENV": "__UNSET__"})
    assert f"CRON_ROOT:{copied_root}/cron_tracking" in result.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_print_roots_matches_python_default_and_attempt_override(shell: str, tmp_path: Path) -> None:
    result = _run(shell, "--print-roots", root=tmp_path / "unused", extra_env={"DCA_CRON_TRACKING_ROOT": "", "DCA_DISABLE_DOTENV": "1", "DCA_ATTEMPT_LOG_ROOT": str(tmp_path / "custom-attempt")})
    assert result.returncode == 0
    assert result.stdout.splitlines()[1] == f"ATTEMPT_ROOT:{tmp_path}/custom-attempt"
    assert dca_paths.DEFAULT_CRON_TRACKING_ROOT.rstrip("/") in result.stdout.splitlines()[0]


@pytest.mark.parametrize("shell", SHELLS)
def test_shell_syntax_and_script_contains_no_python(shell: str) -> None:
    for flag in ("-n",):
        result = subprocess.run([shell, flag, str(SCRIPT)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
    # The shell contains explanatory references to the missing interpreter, but
    # has no interpreter launch or embedded-language path; syntax checks above
    # exercise it directly under both POSIX shells.
