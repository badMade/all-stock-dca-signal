"""The core coverage-gated pytest run is parallel (pytest-xdist) and uses coverage.py's sysmon core on 3.12.

Why (2026-11-14): the core suite reached 14,568 tests and took 24m05s on the operator's Mac at 100% of ONE
core (971.86s user + 483.64s system; 91.05% coverage). CI's 3.12 leg took 25-29 minutes (see ci.yml's
timeout-minutes comment), and Actions minutes are what the 2026-10-31 billing block was about. Two changes:

* ``-n auto`` (pytest-xdist) spreads the suite over every core the runner has. pytest-cov combines the
  per-worker data itself, so ``--cov-fail-under=90`` still gates the combined figure.
* ``COVERAGE_CORE=sysmon`` uses Python 3.12's sys.monitoring for line coverage, which the coverage.py
  maintainer expects to cost "often lower than 5%". It is set ONLY on the 3.12 leg: 3.11 has no
  sys.monitoring, and the suite does not measure branches (sysmon branch support needs 3.14).

These tests pin the wiring in all three places that must agree, so a later edit cannot quietly drop the
parallelism (back to a 25-minute serial run) or the coverage gate (a fast run that proves nothing):
requirements-dev.txt and pyproject's ``dev`` extra (the two documented install paths, CLAUDE.md), ci.yml's
core test step, and scripts/local_ci.sh's core step (the CI stand-in since the billing block).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
PYPROJECT = ROOT / "pyproject.toml"
LOCAL_CI = ROOT / "scripts" / "local_ci.sh"

# Exact pin, same policy as every other dev tool: "Dev tool versions are pinned on purpose -- bump
# deliberately" (CLAUDE.md). 3.8.0 is the version measured on the operator's Mac on 2026-11-14.
XDIST_PIN = "pytest-xdist==3.8.0"


def _core_coverage_step() -> dict:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    matches = [step for step in steps if step.get("name") == "Run tests with coverage"]
    assert len(matches) == 1, "ci.yml's core test job must have exactly one 'Run tests with coverage' step"
    return matches[0]


def _dev_extra() -> str:
    match = re.search(r"^dev = \[(.*?)^\]", PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert match, "pyproject.toml lost its [project.optional-dependencies] dev extra"
    return match.group(1)


def test_requirements_dev_pins_xdist_exactly():
    lines = [line.split("#", 1)[0].strip() for line in REQUIREMENTS_DEV.read_text(encoding="utf-8").splitlines()]
    assert XDIST_PIN in lines


def test_pyproject_dev_extra_pins_the_same_xdist():
    # `pip install -e '.[dev]'` must install the same tools as requirements-dev.txt, or `-n auto` is an
    # "unrecognized arguments" error on that path (the exact failure the operator hit on 2026-11-14 when
    # xdist went into a different interpreter than .venv).
    assert f'"{XDIST_PIN}"' in _dev_extra()


def test_ci_core_coverage_step_runs_in_parallel_and_keeps_the_gate():
    command = " ".join(_core_coverage_step()["run"].split())
    assert command.startswith("pytest ")
    assert "-n auto" in command
    assert "--cov=." in command
    assert "--cov-fail-under=90" in command


def test_ci_sets_sysmon_only_on_the_312_leg():
    env = _core_coverage_step().get("env", {})
    assert "COVERAGE_CORE" in env, "the coverage step must set COVERAGE_CORE"
    value = " ".join(str(env["COVERAGE_CORE"]).split())
    # An expression keyed on the matrix leg, not a bare "sysmon" that 3.11 cannot honour.
    assert value.startswith("${{") and value.endswith("}}"), value
    assert "matrix.python-version == '3.12'" in value
    assert "'sysmon'" in value


def test_local_ci_core_step_matches_ci():
    done = subprocess.run(
        ["bash", str(LOCAL_CI), "--dry-run"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    coverage_lines = [line for line in done.stdout.splitlines() if "--cov-fail-under=90" in line]
    assert coverage_lines, "local_ci.sh's default run lost the coverage gate"
    assert all("-n auto" in line for line in coverage_lines), coverage_lines
