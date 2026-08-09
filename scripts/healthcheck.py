#!/usr/bin/env python3
"""Self-heal healthcheck: run the same checks ``.github/workflows/ci.yml`` runs.

``scripts/self_heal.py`` calls :func:`main` before and after each repair
attempt to decide whether a fix actually worked. The check list runs the
same commands as ci.yml's three jobs (lint, coverage-gated tests, the demo
smoke test, the production-safe-agent-loop lint/test pair, and pip-audit)
rather than inventing a shortcut, so "healthcheck passes" and "CI would
pass" stay the same claim -- though not a byte-for-byte copy of ci.yml,
since these are parameterized by interpreter and split across isolated
environments rather than separate CI jobs. There is no mypy/type-stub check
here -- this repo has no mypy step to mirror (CLAUDE.md: ruff only, stdlib
runtime).

ci.yml's "test" job runs its lint/coverage-tests/demo-smoke checks across a
Python 3.11/3.12 matrix; the other two jobs (production-safe-agent-loop,
audit) are each pinned to a single interpreter in ci.yml itself. ``main``'s
``matrix_pythons`` mirrors that structure -- a repair that only passes under
one interpreter must not be reported as having verified the whole matrix
(Codex review, PR #379: "verification needs to cover both supported
interpreters before declaring the repair successful").

``psal_python``/``audit_python`` matter for more than which interpreter runs
them: ci.yml's "test" job installs only requirements-dev.txt, while its
production-safe-agent-loop job additionally installs
production-safe-agent-loop/requirements.txt into the *same* environment. If
the caller points ``matrix_pythons`` at an interpreter that also has the
production-safe-agent-loop extras installed, an undeclared core import that
only those extras happen to provide would pass here while ci.yml's real
"test" job -- which never installs them -- would fail on it for real (Codex
review, PR #379). The caller (self-heal.yml) is expected to give each
argument an interpreter from its own isolated virtualenv for this reason;
this module has no way to enforce that itself since it only receives paths.

The runner is injected (default ``subprocess.run``) rather than called
directly, the same pattern ``dca_live_adapters`` uses for broker calls, so
tests can substitute a fake process invocation instead of actually shelling
out to pytest-inside-pytest.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
PSAL = ROOT / "production-safe-agent-loop"

# A near-close weekday timestamp already proven to pass the demo smoke test
# in ci.yml -- reusing it here means a healthcheck failure reflects a real
# repair-relevant problem, not an unrelated market-calendar edge case.
DEFAULT_DEMO_NOW = "2026-07-06T15:45"

Runner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[bytes]"]


class CheckSpec(NamedTuple):
    name: str
    cmd: List[str]
    cwd: Path


def default_runner(cmd: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[bytes]":
    """The real ``subprocess.run`` invocation. Public so sibling scripts (e.g.
    ``scripts/self_heal.py``) can reuse it as their own default runner instead
    of each redefining an identical wrapper."""
    return subprocess.run(list(cmd), cwd=cwd)


def build_checks(
    *,
    now: str = DEFAULT_DEMO_NOW,
    demo_dir: Optional[Path] = None,
    matrix_pythons: Sequence[str] = (sys.executable,),
    psal_python: Optional[str] = None,
    audit_python: Optional[str] = None,
) -> List[CheckSpec]:
    """The ordered checks. Each ``cmd``/``cwd`` pair is copied from ci.yml.

    ``matrix_pythons`` runs ci.yml's "test" job checks once per interpreter
    given, same as that job's own matrix. ``psal_python``/``audit_python``
    default to the first/last matrix interpreter, matching ci.yml pinning its
    production-safe-agent-loop job to 3.11 and its audit job to 3.12 when
    ``matrix_pythons`` is ``("3.11", "3.12")`` in that order.
    """
    psal_python = psal_python or matrix_pythons[0]
    audit_python = audit_python or matrix_pythons[-1]

    checks: List[CheckSpec] = []
    for python in matrix_pythons:
        this_demo_dir = demo_dir or Path(tempfile.mkdtemp(prefix="dca-selfheal-demo-"))
        checks.append(CheckSpec(f"Lint (ruff, core modules) [{python}]", [python, "-m", "ruff", "check", "."], ROOT))
        checks.append(
            CheckSpec(
                f"Tests + coverage (core suite) [{python}]",
                [python, "-m", "pytest", "--cov=.", "--cov-report=term-missing:skip-covered", "--cov-fail-under=90"],
                ROOT,
            )
        )
        checks.append(
            CheckSpec(
                f"Dry-run smoke test [{python}]",
                [python, "run_dca.py", "--demo", "--report", "--now", now, "--artifacts-dir", str(this_demo_dir)],
                ROOT,
            )
        )

    checks.append(
        CheckSpec(
            "Lint (ruff, production-safe-agent-loop)",
            [psal_python, "-m", "ruff", "check", "--isolated", "--select=E,F,B", "--line-length=120", str(PSAL)],
            ROOT,
        )
    )
    checks.append(CheckSpec("Tests (production-safe-agent-loop)", [psal_python, "-m", "pytest", "tests/"], PSAL))
    # Mirrors the "audit" CI job. self_heal.py has no repair step that can
    # fix this (bumping a pinned dependency version is out of scope --
    # CLAUDE.md: "bump deliberately"), but it must still show up here so a
    # pip-audit-only CI failure is reported as unfixed rather than silently
    # ignored.
    checks.append(
        CheckSpec(
            "Dependency audit (pip-audit)",
            [audit_python, "-m", "pip_audit", "--requirement", "requirements-dev.txt", "--strict"],
            ROOT,
        )
    )
    return checks


def run_check(spec: CheckSpec, *, runner: Runner = default_runner) -> bool:
    print(f"\n==> {spec.name}: {' '.join(spec.cmd)}")
    result = runner(spec.cmd, spec.cwd)
    ok = result.returncode == 0
    print(f"[{'OK' if ok else 'FAIL'}] {spec.name}" + ("" if ok else f" (exit {result.returncode})"))
    return ok


def main(
    *,
    matrix_pythons: Optional[Sequence[str]] = None,
    psal_python: Optional[str] = None,
    audit_python: Optional[str] = None,
    runner: Runner = default_runner,
) -> int:
    checks = build_checks(
        matrix_pythons=tuple(matrix_pythons) if matrix_pythons else (sys.executable,),
        psal_python=psal_python,
        audit_python=audit_python,
    )
    results = [run_check(spec, runner=runner) for spec in checks]
    return 0 if all(results) else 1


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        action="append",
        dest="matrix_pythons",
        metavar="PATH",
        help="Interpreter to run ci.yml's matrixed 'test'-job checks under (repeatable, "
        "in ci.yml's matrix order). Default: this interpreter only.",
    )
    parser.add_argument(
        "--psal-python",
        dest="psal_python",
        metavar="PATH",
        help="Interpreter (with production-safe-agent-loop's own requirements installed "
        "alongside requirements-dev.txt, in its own environment) to run its lint/tests "
        "under. Default: the first --python given.",
    )
    parser.add_argument(
        "--audit-python",
        dest="audit_python",
        metavar="PATH",
        help="Interpreter with pip-audit installed to run the dependency audit under. "
        "Default: the last --python given.",
    )
    args = parser.parse_args(argv)
    return main(matrix_pythons=args.matrix_pythons, psal_python=args.psal_python, audit_python=args.audit_python)


if __name__ == "__main__":
    raise SystemExit(_cli())
