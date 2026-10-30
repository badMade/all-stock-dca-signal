"""Regression guard for Key ``bridge_python_alert_channels`` (PR #2032).

The 2026-10-28 shared-cache Gate 2 abort showed why every buying prompt must
keep its per-user uv paths, Python floor, and durable Gate 9 abort evidence.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_GLOBS = (
    REPO_ROOT / "claude/dca-executor-prompts/drafts/*.bridge-shell.draft.txt",
    REPO_ROOT / "claude/dca-executor-prompts/live-mirror/*.txt",
)
INVARIANTS = (
    "UV_CACHE_DIR=/tmp/uv-$(id -u)/cache",
    "UV_PYTHON_INSTALL_DIR=/tmp/uv-$(id -u)/python",
    "TMPDIR=/tmp/uv-$(id -u)/tmp",
    "uv python find 3.11",
    "durable alert to the destination",
    "any abort",
    "scripts.finalize_run_evidence",
)
DRAFT_INVARIANTS = (
    "durable alert to the destination",
    "any abort",
    "scripts.finalize_run_evidence",
)


def _discover(pattern: Path) -> list[Path]:
    # Hidden files are skipped because live-mirror/ holds a `.template.txt`
    # scaffold that is not a scheduled prompt and carries none of these lines.
    return sorted(
        path for path in REPO_ROOT.glob(str(pattern.relative_to(REPO_ROOT))) if not path.name.startswith(".")
    )


# Assert EACH directory is non-empty, not just their union: a rename that
# empties drafts/ alone would otherwise halve the coverage and still pass.
_DISCOVERED = {pattern: _discover(pattern) for pattern in PROMPT_GLOBS}
for _pattern, _paths in _DISCOVERED.items():
    assert _paths, f"no prompt files matched {_pattern.relative_to(REPO_ROOT)}"
PROMPT_FILES = [path for paths in _DISCOVERED.values() for path in paths]


@pytest.mark.parametrize("prompt_path", _DISCOVERED[PROMPT_GLOBS[1]], ids=lambda path: path.name)
@pytest.mark.parametrize("invariant", INVARIANTS)
def test_live_mirror_retains_existing_bridge_and_abort_alert_contract(prompt_path: Path, invariant: str) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    assert invariant in prompt, f"{prompt_path.relative_to(REPO_ROOT)} is missing invariant: {invariant}"


@pytest.mark.parametrize("prompt_path", _DISCOVERED[PROMPT_GLOBS[0]], ids=lambda path: path.name)
@pytest.mark.parametrize("invariant", DRAFT_INVARIANTS)
def test_bridge_shell_draft_retains_non_opener_alert_contract(prompt_path: Path, invariant: str) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    assert invariant in prompt, f"{prompt_path.relative_to(REPO_ROOT)} is missing invariant: {invariant}"


@pytest.mark.parametrize("prompt_path", _DISCOVERED[PROMPT_GLOBS[0]], ids=lambda path: path.name)
def test_bridge_shell_draft_uses_bootstrap_and_records_early_aborts(prompt_path: Path) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    assert ". scripts/bridge_bootstrap.sh" in prompt
    assert "scripts/bridge_abort_marker.sh" in prompt
    assert "--started-at" in prompt
    assert "&& { PY=$(uv python find 3.11" not in prompt
    assert "GATE 7 - PLACE THE ORDERS" in prompt
    assert "ISOLATE ONLY A FAILURE THAT PROVES NO ORDER EXISTS" in prompt
