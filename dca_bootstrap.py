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


def _missing_packages(packages: Sequence[str]) -> list[str]:
    """Return the subset of ``packages`` not importable in THIS interpreter."""
    missing = []
    for pkg in packages:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


# PyPI's default `torch` wheel bundles the full CUDA runtime (multi-GB, e.g.
# nvidia-cudnn/cuda-toolkit); every caller that requests "torch" here only
# needs CPU (wondrous-octopus's model is ~1-2M parameters -- see its
# requirements.txt and .github/workflows/ci.yml's identical CPU-first-install
# comment). Installing this wheel before the requirements-file install
# satisfies the unpinned `torch` line so pip never reaches for the default
# CUDA bundle, mirroring CI's own install order.
_TORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"


def _install_dependencies(
    venv_python: Path,
    to_install: Sequence[str],
    requirements_files: Sequence[Path | str],
) -> None:
    """Install ``to_install`` (or ``requirements_files``) into ``venv_python``."""
    if "torch" in to_install:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "torch", "--index-url", _TORCH_CPU_INDEX_URL],
            check=True,
        )

    install_cmd = [str(venv_python), "-m", "pip", "install"]
    has_file = False
    for req in requirements_files:
        req_path = Path(req)
        if req_path.is_file():
            install_cmd.extend(["-r", str(req_path)])
            has_file = True
    if not has_file:
        install_cmd.extend(to_install)
    subprocess.run(install_cmd, check=True)


def ensure_venv(
    *,
    packages: Sequence[str] = (),
    requirements_files: Sequence[Path | str] = (),
    venv_dir: Path = DEFAULT_VENV_DIR,
) -> None:
    """Ensure the target virtual environment exists and is running.

    0. If every requested ``package`` is already importable in the CURRENTLY
       RUNNING interpreter -- whatever it is: the caller's own virtualenv, or
       a CI job that already installed everything into the system
       interpreter -- this is a true no-op. Re-execing into a separate
       ``venv_dir`` from here would install a second, independent copy of
       every dependency (an unpinned package can resolve to a much heavier
       default build than whatever is already satisfied, e.g. torch's full
       CUDA wheel) and then replace the running process outright, which is
       destructive when the caller is a test process rather than a
       standalone script.

    Otherwise, if the current interpreter is not ``venv_dir``'s python:
    1. Creates ``venv_dir`` if missing.
    2. Checks if requested ``packages`` are importable in ``.venv``; if not,
       installs dependencies using pip.
    3. Re-executes the current script inside ``venv_dir`` using ``os.execv``.

    If the current interpreter is already ``venv_dir``'s python:
    1. Verifies in-process whether all requested ``packages`` are importable.
    2. If any are missing, runs ``pip install`` to complete first-time setup.

    """
    if packages and not _missing_packages(packages):
        return

    venv_python = get_venv_python(venv_dir)

    # 1. Already executing within the target venv
    if _is_running_in_venv(venv_python):
        missing = _missing_packages(packages)
        if not missing:
            return

        print(f"Installing missing dependencies ({', '.join(missing)}) into {venv_dir.name}...", flush=True)
        _install_dependencies(venv_python, missing, requirements_files)
        return

    # 2. Executing outside the target venv: create if missing
    if not venv_python.is_file():
        print(f"Creating virtual environment at {venv_dir}...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)

    # Install requirements if packages are missing
    if packages and not _check_packages_installed(venv_python, packages):
        print(f"Installing required dependencies into {venv_dir.name} (first-time setup)...", flush=True)
        _install_dependencies(venv_python, packages, requirements_files)

    # 3. Seamlessly re-execute this process using venv_python
    try:
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)
    except Exception:
        # Fallback if os.execv is unavailable
        sys.exit(subprocess.call([str(venv_python)] + sys.argv))
