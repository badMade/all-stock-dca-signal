"""Failing-first tests: every dry-run demo command writes into a private ``.../scheduled_runs/...`` directory.

``run_dca._default_prior_allocation_state_root`` derives the folder it scans (recursively, for ``run_state*.json``)
for same-day prior allocations from ``--artifacts-dir``: the parent of the ``scheduled_runs`` segment, or, when there
is none, the artifacts directory's PARENT. A demo given ``$(mktemp -d)``, ``$RUNNER_TEMP/dca-demo`` or ``/tmp/dca-demo``
therefore scans the whole temp directory, which on 2026-10-31 held the pytest folders of the run just before and
produced ``UnicodeDecodeError`` tracebacks in the demo log (the demo still passed, and it is a dry run). Live runs
always sit under ``scheduled_runs/`` and are unaffected. The fix is in the commands, not in the runtime: nesting the
demo's directory under its own ``scheduled_runs/`` makes the scan root a private directory.

Pinned here: the helper's contract, and that no documented or scripted demo command passes a bare directory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import run_dca

ROOT = Path(__file__).resolve().parents[1]
#: Every file that carries a runnable ``--demo ... --artifacts-dir <path>`` command today.
COMMAND_FILES = ("scripts/local_ci.sh", ".github/workflows/ci.yml", "CLAUDE.md", "README.md", "AGENTS.md")
_DEMO_LINE = re.compile(r"(?:run_dca\.py|run-dca|run_dca\b).*--demo.*--artifacts-dir\s+(\S+)")


def _demo_artifact_args(relative: str) -> list[str]:
    text = (ROOT / relative).read_text(encoding="utf-8")
    return [m.group(1).strip("\"'`") for m in _DEMO_LINE.finditer(text)]


def test_the_scan_root_is_the_parent_of_scheduled_runs_and_falls_back_to_the_parent_directory(tmp_path):
    nested = tmp_path / "scheduled_runs" / "demo"
    assert run_dca._default_prior_allocation_state_root(nested) == tmp_path
    # The fallback that made the demo scan the whole temp directory: documented so nobody relies on it by accident.
    assert run_dca._default_prior_allocation_state_root(tmp_path / "demo") == tmp_path


@pytest.mark.parametrize("relative", COMMAND_FILES)
def test_every_demo_command_nests_its_artifacts_directory_under_scheduled_runs(relative):
    args = _demo_artifact_args(relative)
    assert args, f"{relative}: expected at least one --demo --artifacts-dir command"
    for arg in args:
        assert "/scheduled_runs/" in arg, (
            f"{relative}: --artifacts-dir {arg} has no scheduled_runs segment, so the demo scans its parent directory"
        )


def test_the_demo_never_uses_a_shared_temp_directory_as_its_scan_root():
    # What the helper scans: the part before /scheduled_runs/, or the artifacts directory's parent when there is none.
    # It must be a private directory, never the shared temp directory itself.
    shared = {"/tmp", "$TMPDIR", "$RUNNER_TEMP", "${TMPDIR}", "${RUNNER_TEMP}"}
    for relative in COMMAND_FILES:
        for arg in _demo_artifact_args(relative):
            root = arg.split("/scheduled_runs/", 1)[0] if "/scheduled_runs/" in arg else arg.rsplit("/", 1)[0]
            assert root.rstrip("/") not in shared, f"{relative}: scan root {root!r} is a shared temp directory"
