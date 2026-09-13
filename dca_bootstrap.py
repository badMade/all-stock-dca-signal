"""Automated environment bootstrap and virtual environment trampoline.

Ensures that executions invoked via the system Python (e.g. ``python3 from.py``
or ``python3 wondrous-octopus/run_walkforward.py``) automatically locate or create
the repository's ``.venv``, install any missing dependencies on first run, and
seamlessly re-execute the target command inside that virtual environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_VENV_DIR = REPO_ROOT / ".venv"


def get_venv_python(venv_dir: Path = DEFAULT_VENV_DIR) -> Path:
    """Return the expected python executable path within the given virtualenv."""
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _is_running_in_venv(venv_python: Path) -> bool:
    """Check if the current process is already executing inside the target venv."""
    try:
        return Path(sys.executable).resolve() == venv_python.resolve()
    except Exception:
        return False


def _check_packages_installed(python_bin: Path, packages: Sequence[str]) -> bool:
    """Check if all given package names can be imported using the specified python."""
    if not packages:
        return True
    code = "; ".join(f"import {pkg}" for pkg in packages)
    result = subprocess.run([str(python_bin), "-c", code], capture_output=True)
    return result.returncode == 0


def ensure_venv(
    *,
    packages: Sequence[str] = (),
    requirements_files: Sequence[Path | str] = (),
    venv_dir: Path = DEFAULT_VENV_DIR,
) -> None:
    """Ensure the target virtual environment exists and is running.

    If the current interpreter is not ``venv_dir``'s python:
    1. Creates ``venv_dir`` if missing.
    2. Checks if requested ``packages`` are importable in ``.venv``; if not,
       installs dependencies using pip.
    3. Re-executes the current script inside ``venv_dir`` using ``os.execv``.

    If the current interpreter is already ``venv_dir``'s python:
    1. Verifies in-process whether all requested ``packages`` are importable.
    2. If any are missing, runs ``pip install`` to complete first-time setup.
    """
    venv_python = get_venv_python(venv_dir)

    # 1. Already executing within the target venv
    if _is_running_in_venv(venv_python):
        missing = []
        for pkg in packages:
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pkg)

        if not missing:
            return

        print(f"Installing missing dependencies ({', '.join(missing)}) into {venv_dir.name}...", flush=True)
        install_cmd = [str(venv_python), "-m", "pip", "install"]
        has_file = False
        for req in requirements_files:
            req_path = Path(req)
            if req_path.is_file():
                install_cmd.extend(["-r", str(req_path)])
                has_file = True
        if not has_file:
            install_cmd.extend(missing)
        subprocess.run(install_cmd, check=True)
        return

    # 2. Executing outside the target venv: create if missing
    if not venv_python.is_file():
        print(f"Creating virtual environment at {venv_dir}...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)

    # Install requirements if packages are missing
    if packages and not _check_packages_installed(venv_python, packages):
        print(f"Installing required dependencies into {venv_dir.name} (first-time setup)...", flush=True)
        install_cmd = [str(venv_python), "-m", "pip", "install"]
        has_file = False
        for req in requirements_files:
            req_path = Path(req)
            if req_path.is_file():
                install_cmd.extend(["-r", str(req_path)])
                has_file = True
        if not has_file:
            install_cmd.extend(packages)
        subprocess.run(install_cmd, check=True)

    # 3. Seamlessly re-execute this process using venv_python
    try:
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)
    except Exception:
        # Fallback if os.execv is unavailable
        sys.exit(subprocess.call([str(venv_python)] + sys.argv))
