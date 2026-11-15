"""The 11:30 live prompt must script its gates and batch its independent per-order reads (written before the edit).

2026-11-13: the 11:30 primary started at 11:39:25 ET, spent about five minutes on gates before its first order
(11:44:49) and about two minutes per order after that. The script-driven backup placed about 12 orders a minute,
reached the same tickers first, and CAT was bought twice. Operator decisions, 2026-11-13:

  * SCRIPT THE GATES: Gates 0, 1 and 3, the account pin and the Gate 6 cap arithmetic run as one command each
    (``scripts.primary_gates``) instead of one model turn per check;
  * PARALLEL PER-ORDER READS: the independent reads before an order go out in ONE tool-call turn;
  * KEEP per-order revalidation. Nothing here may narrow, skip or reorder a check, and the audit stays.
"""

from pathlib import Path

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "claude/dca-executor-prompts/live-mirror/1130ET-1130-mornig-buying-run.txt"
)
PARALLEL = "PARALLEL PER-ORDER READS"


def _text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    text = _text()
    return text[text.index(start) : text.index(end)]


# ---- scripted gates -------------------------------------------------------------------------------------------


def test_gates_0_1_3_run_as_one_scripted_command_for_this_slot():
    gates = _between("GATE 0 - MARKET DAY", "GATE 4 - SOLE-EXECUTOR INTERLOCK")
    assert "python3 -m scripts.primary_gates clock --slot 1130" in gates


def test_scripted_clock_verdict_is_authoritative_and_maps_to_the_three_outcomes():
    gates = _between("GATE 0 - MARKET DAY", "GATE 4 - SOLE-EXECUTOR INTERLOCK")
    for outcome in ("PROCEED", "END_SILENTLY", "ABORT"):
        assert outcome in gates


def test_scripted_clock_samples_an_offset_aware_instant_not_the_host_wall_clock():
    gates = _between("GATE 1 - TIME SANITY", "GATE 2 - REPO REACHABLE")
    assert "--now" in gates and "offset" in gates


def test_gate_6_cap_arithmetic_runs_through_the_script():
    budget = _between("GATE 6 - BUDGET", "GATE 6.5 - SAME-DAY TOP-UP GUARD")
    assert "scripts.primary_gates budget" in budget


def test_gate_4_rogue_classification_stays_in_the_prompt():
    # Still true after Gate 4 was scripted (tests/test_live_prompt_scripted_gate4.py): the prose rules remain as the
    # manual fallback beside the command.
    gate4 = _between("GATE 4 - SOLE-EXECUTOR INTERLOCK", "GATE 5 - BUY LIST")
    assert "rogue" in gate4 and "placed_agent" in gate4


# ---- parallel per-order reads ---------------------------------------------------------------------------------


def test_parallel_read_rule_sits_inside_gate_7():
    text = _text()
    assert PARALLEL in text
    assert text.index("GATE 7 - PLACE THE ORDERS") < text.index(PARALLEL) < text.index("GATE 8 - CONFIRM")


def test_parallel_read_rule_names_the_independent_reads_and_one_turn():
    rule = _text()[_text().index(PARALLEL) :][:3000]
    for needle in ("get_equity_orders", "get_equity_quotes", "ONE tool-call turn"):
        assert needle in rule


def test_parallel_read_rule_does_not_parallelise_placement_or_the_decision():
    rule = _text()[_text().index(PARALLEL) :][:3000]
    assert "place_equity_order" in rule
    assert "never" in rule.lower() and "same turn" in rule


def test_parallel_read_rule_states_it_removes_no_check():
    rule = _text()[_text().index(PARALLEL) :][:3000]
    assert "does not skip" in rule or "skips nothing" in rule


# ---- operator decision: per-order revalidation is kept --------------------------------------------------------


def test_revalidate_before_every_order_is_unchanged():
    text = _text()
    assert "REVALIDATE BEFORE EVERY ORDER, not once per batch" in text
    assert "PER-ORDER INCREMENTAL HISTORY" in text
    assert "NO SILENT SKIPS" in text


def test_per_order_record_and_audit_are_unchanged():
    text = _text()
    assert "PER-ORDER RECORD (operator, 2026-10-29)" in text
    assert "python3 -m scripts.check_per_order_record --run-dir $ROOT" in text
