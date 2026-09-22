"""Tests for dca_bootstrap module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dca_bootstrap import (
    DEFAULT_VENV_DIR,
    _check_packages_installed,
    _is_running_in_venv,
    _missing_packages,
    ensure_venv,
    get_venv_python,
)


def test_get_venv_python_default() -> None:
    venv_python = get_venv_python(DEFAULT_VENV_DIR)
    assert venv_python.name in ("python", "python.exe")
    assert venv_python.parent.name in ("bin", "Scripts")


def test_is_running_in_venv() -> None:
    current_python = Path(sys.executable)
    assert _is_running_in_venv(current_python) is True
    assert _is_running_in_venv(Path("/nonexistent/python")) is False


def test_check_packages_installed() -> None:
    current_python = Path(sys.executable)
    assert _check_packages_installed(current_python, ("sys", "os")) is True
    assert _check_packages_installed(current_python, ("nonexistent_dummy_package_12345",)) is False


def test_missing_packages() -> None:
    assert _missing_packages(()) == []
    assert _missing_packages(("sys", "os")) == []
    assert _missing_packages(("sys", "nonexistent_dummy_package_12345")) == ["nonexistent_dummy_package_12345"]


def test_ensure_venv_noop_when_already_satisfied(tmp_path, monkeypatch) -> None:
    # Regression: ensure_venv() used to unconditionally create/populate a
    # SEPARATE `venv_dir` and os.execv into it even when every requested
    # package was already importable in the CURRENTLY RUNNING interpreter --
    # destructive when the caller is a live process (like this test's own
    # pytest run) rather than a standalone script, and wasteful (a second,
    # independent dependency install, potentially resolving an unpinned
    # package like torch to a much heavier default build) even for a
    # standalone script whose ambient environment already has everything it
    # needs. Point venv_dir at a path that doesn't exist and assert neither
    # subprocess nor os.execv is ever touched: a genuine no-op.
    venv_dir = tmp_path / "should-never-be-created"

    def _forbidden_run(*args, **kwargs):
        raise AssertionError(f"subprocess.run must not be called when packages are already satisfied: {args}")

    def _forbidden_execv(*args, **kwargs):
        raise AssertionError(f"os.execv must not be called when packages are already satisfied: {args}")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)
    monkeypatch.setattr(os, "execv", _forbidden_execv)

    ensure_venv(packages=("sys", "os"), venv_dir=venv_dir)

    assert not venv_dir.exists()


def test_ensure_venv_bootstraps_when_packages_are_missing(tmp_path, monkeypatch) -> None:
    # The opposite path must still work: a standalone script invoked from a
    # bare interpreter that genuinely lacks the requested packages should
    # still create the venv, install into it, and re-exec.
    venv_dir = tmp_path / "venv"
    venv_python = get_venv_python(venv_dir)
    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "-m" in cmd and "venv" in cmd:
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.touch()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: 0)
    # Never actually replace this test process; just record the attempt.
    execv_calls: list[tuple] = []
    monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
    monkeypatch.setattr(
        "dca_bootstrap._check_packages_installed", lambda python_bin, packages: False
    )

    ensure_venv(packages=("nonexistent_dummy_package_12345",), venv_dir=venv_dir)

    assert venv_python.is_file()
    assert execv_calls  # a re-exec into the new venv was attempted
    assert any("pip" in cmd and "install" in cmd for cmd in calls)


def test_ensure_venv_ignores_ambient_packages_when_none_requested(tmp_path, monkeypatch) -> None:
    # packages=() (from.py's call shape) can't be checked for "already
    # satisfied" -- there is nothing to check -- so it must still fall
    # through to the create/exec dance rather than silently no-op'ing.
    venv_dir = tmp_path / "venv"
    venv_python = get_venv_python(venv_dir)

    def _fake_run(cmd, *args, **kwargs):
        if "-m" in cmd and "venv" in cmd:
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.touch()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    execv_calls: list[tuple] = []
    monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
    ensure_venv(venv_dir=venv_dir)
    ensure_venv(venv_dir=venv_dir)
    assert venv_python.is_file()
    assert execv_calls
    assert execv_calls


def test_ensure_venv_installs_torch_cpu_wheel_before_requirements_when_in_venv(tmp_path, monkeypatch) -> None:
    # Regression: PyPI's default `torch` wheel bundles the full CUDA runtime
    # (multi-GB -- nvidia-cudnn, cuda-toolkit, etc). wondrous-octopus's own
    # requirements.txt and .github/workflows/ci.yml both install the CPU-only
    # wheel from pytorch.org's cpu index FIRST, so the later requirements-file
    # install finds torch already satisfied and never reaches for the CUDA
    # default. ensure_venv's own install path used to skip that and pip
    # straight from requirements.txt, so running e.g.
    # `python wondrous-octopus/run_walkforward.py --help` on a fresh checkout
    # pulled the multi-GB CUDA bundle instead.
    venv_dir = tmp_path / "venv"
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("torch\n")

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr("dca_bootstrap._is_running_in_venv", lambda venv_python: True)
    monkeypatch.setattr("dca_bootstrap._missing_packages", lambda packages: ["torch"])

    ensure_venv(packages=("torch",), requirements_files=(req_file,), venv_dir=venv_dir)

    torch_cpu_calls = [c for c in calls if "torch" in c and "--index-url" in c]
    assert torch_cpu_calls, calls
    assert "https://download.pytorch.org/whl/cpu" in torch_cpu_calls[0]

    requirements_calls = [c for c in calls if "-r" in c]
    assert requirements_calls, calls
    assert calls.index(torch_cpu_calls[0]) < calls.index(requirements_calls[0])


def test_ensure_venv_installs_torch_cpu_wheel_before_requirements_on_first_setup(tmp_path, monkeypatch) -> None:
    # Same regression as above, for the OTHER install path: a fresh venv
    # that doesn't exist yet (first-time setup before the re-exec).
    venv_dir = tmp_path / "venv"
    venv_python = get_venv_python(venv_dir)
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("torch\n")

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "-m" in cmd and "venv" in cmd:
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.touch()
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(os, "execv", lambda *a: None)
    monkeypatch.setattr("dca_bootstrap._missing_packages", lambda packages: ["torch"])
    monkeypatch.setattr("dca_bootstrap._check_packages_installed", lambda python_bin, packages: False)

    ensure_venv(packages=("torch",), requirements_files=(req_file,), venv_dir=venv_dir)

    torch_cpu_calls = [c for c in calls if "torch" in c and "--index-url" in c]
    assert torch_cpu_calls, calls
    assert "https://download.pytorch.org/whl/cpu" in torch_cpu_calls[0]

    requirements_calls = [c for c in calls if "-r" in c]
    assert requirements_calls, calls
    assert calls.index(torch_cpu_calls[0]) < calls.index(requirements_calls[0])
