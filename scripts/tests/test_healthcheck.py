import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.healthcheck as healthcheck


def test_run_cmd_success(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout="ok", stderr=""),
    )
    assert healthcheck.run_cmd(["echo", "hello"]) is True


def test_run_cmd_failure_output(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=1, stdout="out msg", stderr="err msg"
        ),
    )
    assert healthcheck.run_cmd(["false"]) is False
    captured = capsys.readouterr()
    assert "failed with exit code 1" in captured.out
    assert "out msg" in captured.out
    assert "err msg" in captured.out


def test_run_cmd_file_not_found(monkeypatch, capsys):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert healthcheck.run_cmd(["missing-binary"]) is False
    captured = capsys.readouterr()
    assert "Command 'missing-binary' not found" in captured.out


def test_main_all_success(monkeypatch):
    monkeypatch.setattr(healthcheck, "run_cmd", lambda cmd: True)
    with pytest.raises(SystemExit) as exc:
        healthcheck.main()
    assert exc.value.code == 0


def test_main_ruff_failure(monkeypatch):
    def fake_run_cmd(cmd):
        if cmd[0] == "ruff":
            return False
        return True

    monkeypatch.setattr(healthcheck, "run_cmd", fake_run_cmd)
    with pytest.raises(SystemExit) as exc:
        healthcheck.main()
    assert exc.value.code == 1


def test_main_pytest_failure(monkeypatch):
    def fake_run_cmd(cmd):
        if cmd[0] == "pytest":
            return False
        return True

    monkeypatch.setattr(healthcheck, "run_cmd", fake_run_cmd)
    with pytest.raises(SystemExit) as exc:
        healthcheck.main()
    assert exc.value.code == 1


def test_main_smoke_test_failure(monkeypatch):
    def fake_run_cmd(cmd):
        if "run_dca.py" in cmd:
            return False
        return True

    monkeypatch.setattr(healthcheck, "run_cmd", fake_run_cmd)
    with pytest.raises(SystemExit) as exc:
        healthcheck.main()
    assert exc.value.code == 1


def test_main_pip_audit_failure(monkeypatch):
    def fake_run_cmd(cmd):
        if cmd[0] == "pip-audit":
            return False
        return True

    monkeypatch.setattr(healthcheck, "run_cmd", fake_run_cmd)
    with pytest.raises(SystemExit) as exc:
        healthcheck.main()
    assert exc.value.code == 1
