"""Every live executor prompt must forbid aborting Gate 7 on a PREDICTED inability to finish.

2026-10-21 10:30 ET: Gates 0-5 passed with 26 candidates, then the session judged the per-order
revalidation could not finish inside the window and aborted before the first order (and before the
order plan was emitted). The prompt already permits a partial batch (a closing window stops the
batch and reports what was placed), so the pre-judged abort was an unauthorized third outcome.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("*.txt") if not p.name.startswith("."))


def _gate_seven(name: str) -> str:
    text = (MIRRORS / name).read_text(encoding="utf-8")
    return text.split("GATE 7 - ", 1)[1]


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7


@pytest.mark.parametrize("name", PROMPTS)
def test_gate7_forbids_prejudged_abort(name):
    gate = _gate_seven(name)
    assert "NO PRE-JUDGED ABORT" in gate


@pytest.mark.parametrize("name", PROMPTS)
def test_rule_orders_plan_emit_before_placement_and_priority_order(name):
    rule = _gate_seven(name).split("NO PRE-JUDGED ABORT", 1)[1][:3000]
    assert "emit_order_plan" in rule
    assert "allocation-priority order" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_rule_stops_only_on_window_close_and_lists_unplaced_unresized(name):
    rule = _gate_seven(name).split("NO PRE-JUDGED ABORT", 1)[1][:3000]
    assert "window closes" in rule
    assert "UNPLACED TICKERS" in rule
    assert "must NOT be resized" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_rule_does_not_loosen_per_order_revalidation(name):
    gate = _gate_seven(name)
    assert "REVALIDATE BEFORE EVERY ORDER" in gate
    rule = gate.split("NO PRE-JUDGED ABORT", 1)[1][:3000]
    assert "never skips, shortens or batches" in rule
