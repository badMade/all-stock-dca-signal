import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.self_heal as self_heal


def test_run_cmd(monkeypatch):
    called = []

    def fake_run(cmd, check=False, shell=False):
        called.append(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    self_heal.run_cmd(["echo", "1"])
    assert called == [["echo", "1"]]


def test_run_cmd_file_not_found(monkeypatch):
    def fake_run(cmd, check=False, shell=False):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Should not raise
    self_heal.run_cmd(["missing"])


def test_check_health(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0),
    )
    assert self_heal.check_health() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=1),
    )
    assert self_heal.check_health() is False


def test_has_diff_and_status(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout=" M file.py\n"),
    )
    assert self_heal._status() == "M file.py"
    assert self_heal.has_diff() is True

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout=""),
    )
    assert self_heal._status() == ""
    assert self_heal.has_diff() is False


def test_steps(monkeypatch):
    called = []
    monkeypatch.setattr(self_heal, "run_cmd", lambda cmd: called.append(cmd))
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    self_heal.step_1()
    self_heal.step_2()
    self_heal.step_3()
    self_heal.step_4()
    self_heal.step_5()

    assert len(called) >= 5


def test_main_success_on_step(monkeypatch):
    statuses = ["", "M file.py", "M file.py"]
    call_count = [0]

    def fake_status():
        idx = min(call_count[0], len(statuses) - 1)
        call_count[0] += 1
        return statuses[idx]

    monkeypatch.setattr(self_heal, "_status", fake_status)
    monkeypatch.setattr(self_heal, "check_health", lambda: True)
    monkeypatch.setattr(self_heal, "step_1", lambda: None)

    with pytest.raises(SystemExit) as exc:
        self_heal.main()
    assert exc.value.code == 0


def test_main_failure_when_no_step_succeeds(monkeypatch):
    monkeypatch.setattr(self_heal, "_status", lambda: "")
    monkeypatch.setattr(self_heal, "check_health", lambda: False)

    for i in range(1, 6):
        monkeypatch.setattr(self_heal, f"step_{i}", lambda: None)

    with pytest.raises(SystemExit) as exc:
        self_heal.main()
    assert exc.value.code == 1
