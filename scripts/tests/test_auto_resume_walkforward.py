"""Tests for scripts/auto_resume_walkforward.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import auto_resume_walkforward


def test_build_walkforward_cmd() -> None:
    run_dir = Path("artifacts/test_run")
    cmd = auto_resume_walkforward.build_walkforward_cmd(run_dir, python_bin="/usr/bin/python3")
    assert cmd == [
        "/usr/bin/python3",
        "wondrous-octopus/run_walkforward.py",
        "--resume",
        "artifacts/test_run",
        "--retrain",
    ]


def test_build_export_cmd() -> None:
    run_dir = Path("artifacts/test_run")
    cmd = auto_resume_walkforward.build_export_cmd(run_dir, python_bin="/usr/bin/python3")
    assert cmd == [
        "/usr/bin/python3",
        "scripts/export_octopus_ledger.py",
        "--run-dir",
        "artifacts/test_run",
        "--all-scheduled",
        "--csv-out",
        "artifacts/test_run/predictions.csv",
        "--report-md",
        "artifacts/octopus_evaluation_report.md",
        "--json-out",
        "artifacts/octopus_evaluation_metrics.json",
    ]


def test_dry_run(capsys) -> None:
    code = auto_resume_walkforward.main(["--dry-run", "--run-dir", "artifacts/some_dir"])
    assert code == 0
    captured = capsys.readouterr().out
    assert "[Supervisor DRY RUN]" in captured
    assert "run_walkforward.py" in captured


@patch("scripts.auto_resume_walkforward.time.sleep")
@patch("scripts.auto_resume_walkforward.subprocess.Popen")
@patch("scripts.auto_resume_walkforward.subprocess.run")
def test_supervisor_recovers_after_one_failure(mock_run, mock_popen, mock_sleep, tmp_path) -> None:
    # First attempt fails with code 1, second attempt succeeds with code 0
    proc1 = MagicMock()
    proc1.wait.return_value = 1
    proc2 = MagicMock()
    proc2.wait.return_value = 0
    mock_popen.side_effect = [proc1, proc2]

    export_proc = MagicMock()
    export_proc.returncode = 0
    mock_run.return_value = export_proc

    code = auto_resume_walkforward.run_supervisor(
        run_dir=tmp_path,
        delay_seconds=0.1,
        max_retries=3,
        auto_export=True,
    )

    assert code == 0
    assert mock_popen.call_count == 2
    mock_sleep.assert_called_once_with(0.1)
    mock_run.assert_called_once()
