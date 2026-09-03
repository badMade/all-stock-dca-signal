#!/usr/bin/env python3
"""
Repair pipeline for self-healing workflow.
Idempotent steps:
1. Rebuild/reinstall
2. Lint/format auto-fix
3. Snapshot/generated updates
4. Type stubs/analyzer config
5. Dependency re-resolve
6. Static asset regeneration
Exits 0 only if a repair changed the repository AND it now passes healthcheck.
"""

import os
import subprocess
import sys


def run_cmd(cmd):
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        pass


def check_health():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    healthcheck_script = os.path.join(repo_root, "scripts", "healthcheck.py")
    result = subprocess.run([sys.executable, healthcheck_script])
    return result.returncode == 0


def has_diff():
    return bool(_status())


def _status():
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return result.stdout.strip()


def step_1():
    run_cmd([sys.executable, "-m", "pip", "install", "-e", "."])


def step_2():
    # claude/ holds prompt/handoff docs and agent scratch scripts that are
    # intentionally outside this workflow's repair scope -- the guard step's
    # allowed-scope check only accepts root files and a fixed set of
    # directories (src/, tests/, scripts/, ...) and does not include claude/,
    # so a repair that touches a file under it would pass post-healthcheck
    # and then get rejected by the guard, aborting an otherwise-valid repair
    # (Codex review, PR #477). Excluded here, on the repair commands
    # themselves, rather than in [tool.ruff] extend-exclude, so the ordinary
    # `ruff check .` CI lint gate still covers claude/'s tracked harness
    # scripts (Codex review, PR #507).
    run_cmd(["ruff", "check", "--fix", "--extend-exclude", "claude", "."])
    # `ruff format` has no --extend-exclude flag (only `ruff check` does) --
    # passing it is a hard CLI argument error, so `run_cmd`'s swallowed
    # nonzero exit silently skipped formatting entirely on every run. `ruff
    # format` does support plain --exclude, and passing it on the CLI adds to
    # (rather than replaces) [tool.ruff] extend-exclude's config-level
    # excludes when formatting a directory recursively (Codex review, PR #569).
    run_cmd(["ruff", "format", "--exclude", "claude", "."])


def step_3():
    run_cmd(["pytest", "--snapshot-update"])


def step_4():
    run_cmd([sys.executable, "-m", "pip", "install", "types-requests", "types-PyYAML", "types-beautifulsoup4"])


def step_5():
    if os.path.exists("requirements.in"):
        run_cmd(["pip-compile", "requirements.in", "-o", "requirements.txt"])


def step_6():
    if os.path.exists("scripts/update_docs.py"):
        run_cmd([sys.executable, "scripts/update_docs.py"])
    if os.path.exists("scripts/generate_badges.py"):
        run_cmd([sys.executable, "scripts/generate_badges.py"])


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo_root)

    steps = [step_1, step_2, step_3, step_4, step_5, step_6]

    for step in steps:
        before = _status()
        step()
        after = _status()
        # Only run the expensive lint/test/smoke/audit healthcheck when THIS
        # step actually changed something new. Comparing against has_diff()
        # alone is wrong once an earlier step has produced any diff: every
        # later no-op step would still see a nonempty `git status` left over
        # from that earlier step and re-run the full healthcheck for no
        # reason (Codex review, PR #477).
        if after and after != before and check_health():
            sys.exit(0)

    sys.exit(1)


if __name__ == "__main__":
    main()
