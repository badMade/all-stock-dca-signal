"""Rollout of the 11:30 scripted-gates change to the other slot prompts (written before the edit).

``tests/test_live_prompt_1130_scripted_gates.py`` pins the 11:30 prompt. This file pins the same rules for the
rest, with the differences the operator settled on 2026-11-14 ("do what is best", applied to the recommendations):

  * 9:35, 10:30, 1:30 and 2:30 take the full change: scripted clock (Gates 0, 1, 3), scripted Gate 6 cap
    arithmetic (they all use the NORMAL tier unconditionally) and the PARALLEL PER-ORDER READS rule;
  * 3:30 takes the clock, with ``--latest-start 15:55`` so the script does not loosen the prompt's 3:55 PM close
    (``dca_runner.SLOTS['1530']`` runs to 4:00 PM), and the parallel-reads rule. It keeps its own Gate 6, because it
    escalates through dynamic budget tiers and ``primary_gates budget`` is NORMAL-tier only;
  * 12:30 already scripts its own gates and Gate 6, so it takes only the parallel-reads rule.

Per-order revalidation, the per-order record and its audit stay exactly as they are everywhere (operator decision,
2026-11-13), and Gate 4's rogue classification stays in the prompt text.
"""

from pathlib import Path

import pytest

LIVE_MIRROR = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
PARALLEL = "PARALLEL PER-ORDER READS"
FULL = ("0935", "1030", "1330", "1430")
CLOCK_ONLY = ("1530",)
PARALLEL_ONLY = ("1230",)
ALL_ROLLED = FULL + CLOCK_ONLY + PARALLEL_ONLY


def _text(slot: str) -> str:
    (path,) = sorted(LIVE_MIRROR.glob(f"{slot}ET-*.txt"))
    return path.read_text(encoding="utf-8")


def _between(slot: str, start: str, end: str) -> str:
    text = _text(slot)
    return text[text.index(start) : text.index(end)]


def _parallel_rule(slot: str) -> str:
    text = _text(slot)
    return text[text.index(PARALLEL) :][:3200]


# ---- scripted clock: 9:35, 10:30, 1:30, 2:30, 3:30 -------------------------------------------------------------


@pytest.mark.parametrize("slot", FULL + CLOCK_ONLY)
def test_gates_0_1_3_run_as_one_scripted_command_for_this_slot(slot):
    gates = _between(slot, "GATE 0 - MARKET DAY", "GATE 4 - SOLE-EXECUTOR INTERLOCK")
    assert f"python3 -m scripts.primary_gates clock --slot {slot}" in gates
    for outcome in ("PROCEED", "END_SILENTLY", "ABORT"):
        assert outcome in gates


@pytest.mark.parametrize("slot", FULL + CLOCK_ONLY)
def test_scripted_clock_takes_an_offset_aware_now(slot):
    gate1 = _between(slot, "GATE 1 - TIME SANITY", "GATE 2 - REPO REACHABLE")
    assert "--now" in gate1 and "offset" in gate1


@pytest.mark.parametrize("slot", FULL + CLOCK_ONLY)
def test_gate_4_rogue_classification_stays_in_the_prompt(slot):
    gate4 = _between(slot, "GATE 4 - SOLE-EXECUTOR INTERLOCK", "GATE 5 - BUY LIST")
    assert "rogue" in gate4 and "placed_agent" in gate4
    assert "primary_gates" not in gate4


def test_the_3_30_clock_command_clamps_the_start_to_the_prompts_3_55_close():
    gates = _between("1530", "GATE 0 - MARKET DAY", "GATE 4 - SOLE-EXECUTOR INTERLOCK")
    assert "--latest-start 15:55" in gates


@pytest.mark.parametrize("slot", FULL)
def test_other_slots_do_not_pass_a_latest_start(slot):
    assert "--latest-start" not in _text(slot)


# ---- scripted Gate 6: only the four NORMAL-tier slots ----------------------------------------------------------


@pytest.mark.parametrize("slot", FULL)
def test_gate_6_cap_arithmetic_runs_through_the_script_for_normal_tier_slots(slot):
    budget = _between(slot, "GATE 6 - BUDGET", "GATE 6.5 - SAME-DAY TOP-UP GUARD")
    assert "scripts.primary_gates budget" in budget
    assert "--orders-pages" in budget and "--buying-power" in budget


@pytest.mark.parametrize("slot", ("1530", "1230"))
def test_3_30_and_12_30_keep_their_own_gate_6(slot):
    assert "primary_gates budget" not in _text(slot)


def test_the_3_30_dynamic_budget_tiers_are_untouched():
    budget = _between("1530", "GATE 6 - BUDGET", "GATE 6.5 - SAME-DAY TOP-UP GUARD")
    assert "Elevated (6-10)" in budget and "Stress (>17)" in budget


# ---- parallel per-order reads: every rolled-out slot ------------------------------------------------------------


@pytest.mark.parametrize("slot", ALL_ROLLED)
def test_parallel_read_rule_sits_inside_gate_7(slot):
    text = _text(slot)
    assert PARALLEL in text
    assert text.index("GATE 7 - PLACE THE ORDERS") < text.index(PARALLEL) < text.index("GATE 8 - CONFIRM")


@pytest.mark.parametrize("slot", ALL_ROLLED)
def test_parallel_read_rule_names_the_reads_and_one_turn(slot):
    rule = _parallel_rule(slot)
    for needle in ("get_equity_orders", "get_equity_quotes", "ONE tool-call turn"):
        assert needle in rule


@pytest.mark.parametrize("slot", ALL_ROLLED)
def test_parallel_read_rule_never_puts_placement_in_the_same_turn_and_skips_nothing(slot):
    rule = _parallel_rule(slot)
    assert "place_equity_order" in rule and "same turn" in rule and "never" in rule.lower()
    assert "does not skip" in rule or "skips nothing" in rule


@pytest.mark.parametrize("slot", FULL + CLOCK_ONLY)
def test_parallel_rule_clock_check_names_this_slot(slot):
    assert f"scripts.primary_gates clock --slot {slot}" in _parallel_rule(slot)


# ---- copy-paste hygiene -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("slot", FULL + CLOCK_ONLY)
def test_no_11_30_values_leak_into_other_slots_script_lines(slot):
    for line in _text(slot).splitlines():
        if "primary_gates" in line:
            assert "1130" not in line, f"{slot}: a primary_gates line still carries the 11:30 slot id"


@pytest.mark.parametrize("slot", ALL_ROLLED)
def test_parallel_rule_does_not_carry_the_11_30_slot_id(slot):
    assert "1130" not in _parallel_rule(slot)


# ---- operator decision: per-order revalidation, record and audit are kept ---------------------------------------


@pytest.mark.parametrize("slot", ALL_ROLLED)
def test_per_order_revalidation_and_incremental_history_are_unchanged(slot):
    text = _text(slot)
    assert "REVALIDATE BEFORE EVERY ORDER, not once per batch" in text
    assert "PER-ORDER INCREMENTAL HISTORY" in text
    assert "BATCHED TRADABILITY" in text
