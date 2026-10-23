"""Every live executor prompt must treat unproven order-history completeness as log-and-continue.

2026-10-22 9:35 ET: Gates 0-5 passed with 65 candidates, then Gate 6 aborted and nothing was bought
because get_equity_orders returned 81 orders with no pagination cursor (oldest Monday 09:59 ET) and
completeness could not be confirmed. The operator ruled that this is never an abort: log a warning
and size from the history as returned.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("*.txt") if not p.name.startswith("."))
HEADING = "HISTORY COMPLETENESS IS LOG-AND-CONTINUE"


def _rule(name: str) -> str:
    text = (MIRRORS / name).read_text(encoding="utf-8")
    assert text.count(HEADING) == 1
    return text.split(HEADING, 1)[1].split("\n", 1)[0]


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7


@pytest.mark.parametrize("name", PROMPTS)
def test_unproven_completeness_continues_and_logs(name):
    rule = _rule(name)
    assert "NOT an abort condition" in rule
    assert "history completeness unproven" in rule
    assert "EXACTLY AS RETURNED" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_rule_does_not_relax_real_failures(name):
    rule = _rule(name)
    for kept in ("tool error", "cursor that exists but cannot be followed", "malformed row", "rogue or colliding"):
        assert kept in rule
