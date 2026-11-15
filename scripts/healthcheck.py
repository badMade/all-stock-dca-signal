#!/usr/bin/env python3
"""
Deterministic gate script that verifies codebase health.
It runs linting, tests, smoke test, and pip-audit.
Exits 0 if healthy, 1 if not. Silent unless error.
"""

import os
import subprocess
import sys
import tempfile


def run_cmd(cmd: list[str]) -> bool:
    """Executes a command and returns True if successful.

    Args:
        cmd (list[str]): The command to execute.

    Returns:
        bool: True if the command succeeds, False otherwise.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, shell=False)  # nosec B603 - explicit shell=False
        if result.returncode != 0:
            print(f"Command '{' '.join(cmd)}' failed with exit code {result.returncode}")
            if result.stdout.strip():
                print(result.stdout)
            if result.stderr.strip():
                print(result.stderr)
            return False
        return True
    except FileNotFoundError:
        print(f"Command '{cmd[0]}' not found")
        return False


def main() -> None:
    """Main entrypoint for the healthcheck gate."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo_root)

    success = True

    # Linting
    if not run_cmd(["ruff", "check", "."]):
        success = False

    # No Pyright gate (2026-11-15): CI runs it as advisory only while repo-wide diagnostics remain
    # (CLAUDE.md), so gating on it here meant this check could never pass, before or after a repair.

    # Testing
    # -n auto matches ci.yml's core step. self-heal.yml runs this up to ~5 times in one job (pre, once per
    # changed repair step in self_heal.py, post), so a serial 20-30 min suite could never fit (2026-11-15).
    if not run_cmd(["pytest", "-n", "auto", "--cov=.", "--cov-report=term-missing:skip-covered", "--cov-fail-under=90"]):
        success = False

    # Smoke test (Dry run)
    # Use a fixed, known market-day near-close timestamp so this check is deterministic across environments.
    # Write demo artifacts to a tempdir outside the worktree, not a repo path -- otherwise a clean hosted
    # runner's `git add --all` in self-heal.yml's guard step stages them, and the allowed-scope check then
    # rejects that unignored path and aborts every successful repair before PR creation (Codex review, PR #507).
    near_close = "2026-08-03T15:45"
    with tempfile.TemporaryDirectory(prefix="dca-demo-artifacts-") as artifacts_dir:
        if not run_cmd([
            sys.executable,
            "run_dca.py",
            "--demo",
            "--report",
            "--now",
            near_close,
            "--artifacts-dir",
            artifacts_dir,
        ]):
            success = False

    # pip-audit
    if not run_cmd(["pip-audit", "--requirement", "requirements-dev.txt", "--strict"]):
        success = False

    if not success:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
