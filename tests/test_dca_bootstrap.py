"""Tests for dca_bootstrap module."""

from __future__ import annotations

import sys
from pathlib import Path

from dca_bootstrap import (
    DEFAULT_VENV_DIR,
    _check_packages_installed,
    _is_running_in_venv,
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


def test_ensure_venv_noop_when_already_satisfied() -> None:
    # Running inside our test environment with standard packages should complete cleanly
    ensure_venv(packages=("sys", "os"))
