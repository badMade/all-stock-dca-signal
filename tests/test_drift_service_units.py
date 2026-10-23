"""The drift-check systemd units must survive a ``REPO_ROOT`` that contains a space.

PR #1870 review (Copilot): every unit ran ``/bin/sh -c 'exec ${REPO_ROOT}/deploy/run-drift-check.sh ...'``.
systemd substitutes ``${REPO_ROOT}`` into that command line *before* the shell sees it, so a checkout at
``/srv/DCA checkout`` becomes ``exec /srv/DCA checkout/deploy/...`` and ``/bin/sh`` tries to execute
``/srv/DCA``: the drift check silently never runs. This test emulates that systemd expansion rather than
asserting any particular quoting, so any fix that keeps the path as one word passes.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

UNITS = sorted((Path(__file__).resolve().parents[1] / "deploy" / "systemd").glob("dca-drift-*.service"))


def _exec_start_script(unit: Path) -> str:
    line = next(raw for raw in unit.read_text(encoding="utf-8").splitlines() if raw.startswith("ExecStart="))
    argv = shlex.split(line.removeprefix("ExecStart="))
    assert argv[:2] == ["/bin/sh", "-c"], f"{unit.name}: expected a /bin/sh -c ExecStart, got {argv[:2]}"
    return argv[2]


def _systemd_expand(script: str, env: dict) -> str:
    """systemd's own pass over a command line: ``$$`` is a literal ``$``, ``${NAME}`` is replaced textually."""
    protected = script.replace("$$", "\0")
    substituted = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), protected)
    return substituted.replace("\0", "$")


def test_there_are_drift_units_to_check():
    assert len(UNITS) == 7


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_drift_unit_runs_the_check_when_repo_root_contains_a_space(unit, tmp_path):
    repo_root = tmp_path / "DCA checkout"
    (repo_root / "deploy").mkdir(parents=True)
    runner = repo_root / "deploy" / "run-drift-check.sh"
    runner.write_text("#!/bin/sh\necho drift-check-ran\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR)

    env = {"REPO_ROOT": str(repo_root)}
    script = _systemd_expand(_exec_start_script(unit), env)
    result = subprocess.run(
        ["/bin/sh", "-c", script], env={**os.environ, **env}, capture_output=True, text=True, timeout=20
    )

    assert "drift-check-ran" in result.stdout, f"{unit.name}: the drift check never ran ({result.stderr.strip()})"
