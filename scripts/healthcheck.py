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


def run_cmd(cmd):
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


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo_root)

    success = True

    # Linting
    if not run_cmd(["ruff", "check", "."]):
        success = False

    # Types
    if not run_cmd(["pyright"]):
        success = False

    # Testing
    if not run_cmd(["pytest", "--cov=.", "--cov-report=term-missing:skip-covered", "--cov-fail-under=90"]):
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
