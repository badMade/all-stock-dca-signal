"""Regression guard for Key ``bridge_python_alert_channels`` (PR #2032).

The 2026-10-28 shared-cache Gate 2 abort showed why every buying prompt must
keep its per-user uv paths, Python floor, and durable Gate 9 abort evidence.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIRROR_DIR = REPO_ROOT / "claude/dca-executor-prompts/live-mirror"
INVARIANTS = (
    "UV_CACHE_DIR=/tmp/uv-$(id -u)/cache",
    "UV_PYTHON_INSTALL_DIR=/tmp/uv-$(id -u)/python",
    "TMPDIR=/tmp/uv-$(id -u)/tmp",
    "uv python find 3.11",
    "durable alert to the destination",
    "any abort",
    "scripts.finalize_run_evidence",
)


# Hidden files are skipped because live-mirror/ holds a `.template.txt`
# scaffold that is not a scheduled prompt and carries none of these lines.
# The seven drafts/*.bridge-shell.draft.txt this file also used to scan were
# removed on 2026-10-30 once the mirrors were re-captured from live (#2052).
MIRROR_FILES = sorted(path for path in MIRROR_DIR.glob("*.txt") if not path.name.startswith("."))
assert MIRROR_FILES, f"no prompt files matched {MIRROR_DIR.relative_to(REPO_ROOT)}/*.txt"


@pytest.mark.parametrize("prompt_path", MIRROR_FILES, ids=lambda path: path.name)
@pytest.mark.parametrize("invariant", INVARIANTS)
def test_live_mirror_retains_existing_bridge_and_abort_alert_contract(prompt_path: Path, invariant: str) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    assert invariant in prompt, f"{prompt_path.relative_to(REPO_ROOT)} is missing invariant: {invariant}"


@pytest.mark.parametrize("prompt_path", MIRROR_FILES, ids=lambda path: path.name)
def test_live_mirror_uses_checked_in_bridge_scripts(prompt_path: Path) -> None:
    # The 2026-10-30 repaste moved every slot onto the #2052 scripts; a mirror
    # re-captured from a stale live body would bring back the inline opener
    # whose shared uv cache caused the 2026-10-28 Gate 2 aborts.
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "scripts/bridge_python.sh" in prompt
    assert "scripts/record_evidence_free_abort.sh" in prompt
    assert "&& { PY=$(uv python find 3.11" not in prompt
