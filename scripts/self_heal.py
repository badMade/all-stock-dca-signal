#!/usr/bin/env python3
"""Self-heal repair pass: one scoped, idempotent auto-fix, re-verified.

Every CI run in this repo starts from a fresh GitHub Actions runner (no
persistent local environment to drift), and ``requirements-dev.txt`` pins
exact tool versions on purpose (CLAUDE.md: "bump deliberately"). That rules
out the two repair classes a generic self-heal script would normally try
first -- reinstalling dependencies fixes nothing a fresh runner didn't already
do, and re-resolving a lockfile is exactly the deliberate-pinning policy this
repo enforces. With no mypy step and no snapshot tests to regenerate either,
the one repair that is both safe (never changes behavior) and meaningful
(actually produces a diff CI would accept) is a ruff lint auto-fix.

Exit 0 only when the auto-fix makes ``scripts.healthcheck`` pass AND leaves a
non-empty ``git status --porcelain`` -- a passing healthcheck with no diff
means the failure wasn't a lint issue at all, and this script has nothing
further it is allowed to try (CLAUDE.md draws the line at formatting/lint;
logic bugs, coverage drops, and dependency vulnerabilities need a human). A
tree that is *already* dirty before the repair even runs is refused outright
(Codex review, PR #379): otherwise a pre-existing, unrelated change would
satisfy the "non-empty diff" gate on its own and get misreported as "fixed by
ruff lint auto-fix" even if the auto-fix itself changed nothing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import healthcheck  # noqa: E402  local module; see sys.path insert above

ROOT = healthcheck.ROOT
PSAL = healthcheck.PSAL

Runner = healthcheck.Runner
CaptureRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]
HealthcheckRunner = Callable[[Optional[Sequence[str]], Optional[str], Optional[str]], int]


def default_capture_runner(cmd: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)


def _run_healthcheck(
    matrix_pythons: Optional[Sequence[str]], psal_python: Optional[str], audit_python: Optional[str]
) -> int:
    return healthcheck.main(matrix_pythons=matrix_pythons, psal_python=psal_python, audit_python=audit_python)


def lint_autofix(*, python: str = sys.executable, runner: Runner = healthcheck.default_runner) -> None:
    """Apply ruff's safe auto-fixes to both lint scopes CI checks separately.

    Ruff's output doesn't vary by interpreter version (it analyzes syntax,
    not runtime behavior), so unlike verification this needs no per-Python
    repeat -- ``python`` only needs to be *an* interpreter with ruff
    installed, invoked via ``-m`` rather than a bare ``ruff`` on PATH so this
    still works when the caller isolates ruff inside its own virtualenv
    (Codex review, PR #379, on healthcheck.py's matching PATH-ambiguity fix).
    """
    for spec in (
        healthcheck.CheckSpec("ruff --fix (core modules)", [python, "-m", "ruff", "check", "--fix", "."], ROOT),
        healthcheck.CheckSpec(
            "ruff --fix (production-safe-agent-loop)",
            [python, "-m", "ruff", "check", "--isolated", "--select=E,F,B", "--line-length=120", "--fix", str(PSAL)],
            ROOT,
        ),
    ):
        print(f"\n$ {' '.join(spec.cmd)}")
        runner(spec.cmd, spec.cwd)


def has_uncommitted_changes(*, runner: CaptureRunner = default_capture_runner) -> bool:
    result = runner(["git", "status", "--porcelain"], ROOT)
    return bool(result.stdout.strip())


def main(
    *,
    matrix_pythons: Optional[Sequence[str]] = None,
    psal_python: Optional[str] = None,
    audit_python: Optional[str] = None,
    ruff_python: str = sys.executable,
    runner: Runner = healthcheck.default_runner,
    capture_runner: CaptureRunner = default_capture_runner,
    healthcheck_main: HealthcheckRunner = _run_healthcheck,
) -> int:
    if has_uncommitted_changes(runner=capture_runner):
        print(
            "\n[self-heal] Working tree already has uncommitted changes before any repair "
            "ran; refusing to attempt one, since its diff couldn't be cleanly attributed to "
            "the repair. Start from a clean checkout."
        )
        return 1

    print("\n### Repair: ruff lint auto-fix")
    lint_autofix(python=ruff_python, runner=runner)

    if healthcheck_main(matrix_pythons, psal_python, audit_python) == 0 and has_uncommitted_changes(
        runner=capture_runner
    ):
        print("\n[self-heal] Fixed by ruff lint auto-fix.")
        return 0

    print("\n[self-heal] Lint auto-fix did not produce a passing, non-empty diff. No fix found.")
    return 1


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        action="append",
        dest="matrix_pythons",
        metavar="PATH",
        help="Interpreter to verify the repair under (repeatable, in ci.yml's matrix order). "
        "Forwarded to scripts/healthcheck.py. Default: this interpreter only.",
    )
    parser.add_argument("--psal-python", dest="psal_python", metavar="PATH", help="Forwarded to healthcheck.py.")
    parser.add_argument("--audit-python", dest="audit_python", metavar="PATH", help="Forwarded to healthcheck.py.")
    parser.add_argument(
        "--ruff-python",
        dest="ruff_python",
        metavar="PATH",
        default=sys.executable,
        help="Interpreter with ruff installed to run the repair's own ruff --fix under. "
        "Default: this interpreter.",
    )
    args = parser.parse_args(argv)
    return main(
        matrix_pythons=args.matrix_pythons,
        psal_python=args.psal_python,
        audit_python=args.audit_python,
        ruff_python=args.ruff_python,
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
