"""Pasted live prompts carry instructions only, never repo reviewer notes.

2026-10-29: the 0935 mirror held a 41-line '#' change summary ("AHEAD OF LIVE as of this commit ...") below its
first instruction line. check_live_prompt_drift._mirror_body() strips only the LEADING '#' block, so that summary
was pasted into the live task and sent on every 9:35 run (~2,800 characters) while instructing nothing. The 1230
mirror is the one exception: its '#' lines are Python comments inside a code snippet the run executes.
"""

from pathlib import Path

import pytest

from scripts import check_live_prompt_drift as drift

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("[0-9]*.txt"))
CODE_COMMENT_SLOTS = {"1230ET"}  # '#' lines there are code comments inside a snippet, not notes


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7


@pytest.mark.parametrize("name", [n for n in PROMPTS if n[:6] not in CODE_COMMENT_SLOTS])
def test_pasted_body_has_no_hash_note_lines(name):
    body = drift._mirror_body((MIRRORS / name).read_text(encoding="utf-8"))
    notes = [line for line in body if line.startswith("#")]
    assert notes == [], f"{name}: {len(notes)} '#' note lines would be pasted into the live task"


def test_0935_keeps_its_instructions():
    body = "\n".join(drift._mirror_body((MIRRORS / "0935ET-935-am-dca-morning-buy.txt").read_text(encoding="utf-8")))
    for kept in ("STANDING ALERT DELIVERY OVERRIDE", "RESOLVED ACCOUNT FOR NATIVE CALLS", "OPERATOR RETRY AFTER A PLATFORM REFUSAL"):
        assert kept in body
