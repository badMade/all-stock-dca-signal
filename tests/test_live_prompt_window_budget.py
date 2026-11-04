"""No live slot may abort a whole batch up front because the remaining window "looks too short" (2026-11-03).

The 3:30 run had 34 Gate 5 BUY rows and about 14 minutes left, judged that the per-order checks the NO SHORTCUTS rule
requires could not fit, and aborted at Gate 6.5 without running it -- although about 25 of the 34 had already been
bought by earlier slots, so Gate 6.5 would have cut the list to a handful. The rule this pins: always run Gate 6.5
first, then place its resolved list in priority order with every per-order check, and stop cleanly when the window
closes, recording the rest. Thinning the checks stays forbidden; giving up before trying is what changes.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("*.txt") if not p.name.startswith("."))
HEADING = "WINDOW BUDGET (2026-11-03)"


def _rule(name: str) -> str:
    text = (MIRRORS / name).read_text(encoding="utf-8")
    assert text.count(HEADING) == 1, name
    return text.split(HEADING, 1)[1].split("\n", 1)[0]


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7


@pytest.mark.parametrize("name", PROMPTS)
def test_gate_6_5_runs_before_any_time_judgment(name):
    rule = _rule(name)
    assert "never abort a batch up front because the remaining window looks too short" in rule
    assert "run Gate 6.5 first" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_orders_go_in_priority_order_until_the_window_closes(name):
    rule = _rule(name)
    assert "in the order Gate 6.5 resolved them" in rule
    assert "stop placing when this slot's window closes" in rule
    assert "skipped_window_closed" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_the_no_shortcuts_rule_is_not_relaxed(name):
    rule = _rule(name)
    assert "every per-order check still runs in full" in rule
    assert "NO SHORTCUTS (operator, 2026-10-29)" in (MIRRORS / name).read_text(encoding="utf-8")
