"""The 1:30, 2:30 and 3:30 live prompts must chain today's order history from an earlier slot.

2026-11-12 14:30 ET: the 2:30 slot reached Gate 6 with 57 BUY candidates and aborted
``ABORT_GATE6_HISTORY_NOT_PERSISTABLE`` / ``output_cap_exceeded`` with 0 orders. The WEEK
BASELINE REUSE rule said "the day-scoped fetch ... is unchanged (it is small)", but by
mid-afternoon today's page alone is ~35 KB (37 orders) and the session could not save it
verbatim; its week increment since 9:35 was that same page. The 3:30 slot survived the same
afternoon only by improvising a merge onto the 1:30 run's day envelope.

These tests pin the SAME-DAY BASELINE CHAIN rule that makes that improvisation the procedure:
``scripts.find_order_baseline`` picks the newest script-built envelope an earlier slot saved,
ONE increment is fetched, and ``scripts.merge_order_pages`` (which fails closed on any gap)
extends both the day and the week envelope into this run's own $ROOT, with the full fetch as
the per-kind fallback.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
SLOTS = {
    "1330": "1330ET-130-pm-afternoon-buyiing-run.txt",
    "1430": "1430ET-230-pm-afternoon-buying-run.txt",
    "1530": "1530ET-330-final-buying-run.txt",
}
RULE = "SAME-DAY BASELINE CHAIN"
FETCH = "Fetch today's order history: call get_equity_orders"

pytestmark = pytest.mark.parametrize("slot", sorted(SLOTS))


def _text(slot: str) -> str:
    return (MIRRORS / SLOTS[slot]).read_text(encoding="utf-8")


def _rule(slot: str) -> str:
    text = _text(slot)
    start = text.index(RULE + " (")
    end = text.find("\n", start)
    return text[start : end if end != -1 else len(text)]


def test_rule_sits_in_gate_6_right_after_week_baseline_reuse(slot):
    text = _text(slot)
    assert text.count(RULE + " (") == 1
    week = text.index("WEEK BASELINE REUSE (")
    rule = text.index(RULE + " (")
    assert text.index("GATE 6 - BUDGET") < week < rule < text.index("GATE 6.5 - SAME-DAY TOP-UP GUARD")
    # Nothing between the two rules: the chain is read as the week rule's amendment.
    assert text[week:rule].count("\n") == 1


def test_rule_supersedes_the_day_fetch_is_small_sentence(slot):
    rule = _rule(slot)
    assert "SUPERSEDES the sentence in WEEK BASELINE REUSE that leaves the day-scoped fetch unchanged" in rule
    assert "ABORT_GATE6_HISTORY_NOT_PERSISTABLE" in rule


def test_rule_runs_the_finder_for_this_slot_for_both_kinds(slot):
    rule = _rule(slot)
    assert f"scripts.find_order_baseline --kind day --slot {slot} --trading-day <YYYY-MM-DD>" in rule
    assert "`--kind week`" in rule
    assert "never by transcription" in rule


def test_rule_fetches_once_and_merges_both_envelopes_into_this_runs_root(slot):
    rule = _rule(slot)
    assert "ONE incremental fetch" in rule
    assert "EARLIER of the two printed created_at_gte values" in rule
    assert "twice over those SAME page files" in rule
    for needle in (
        f"--out $ROOT/orders_pages_<YYYY-MM-DD>_{slot}.json",
        f"--out $ROOT/orders_pages_week_<Monday YYYY-MM-DD>_{slot}.json",
        "--overlap-seconds 120",
        "--created-at-gte <the gte you fetched with>",
        "--snapshot-at <offset-aware instant right after the last page>",
    ):
        assert needle in rule, needle


def test_rule_never_writes_another_slots_file(slot):
    rule = _rule(slot)
    assert "--base is read only and --out is always this run's own $ROOT" in rule


def test_rule_falls_back_per_kind_to_the_full_fetch_and_keeps_every_bound(slot):
    rule = _rule(slot)
    assert "exit 2 or any other non-zero exit, do NOT proceed on that kind's merge" in rule
    assert "do not retype anything" in rule
    assert "same output_cap_exceeded abort" in rule
    for bound in (
        "does NOT relax any cap",
        "order-count ceiling",
        "buying power",
        "Gate 4",
        "dca_runner.MAX_ORDERS_FETCH_AGE_SECONDS",
        "2026-10-22",
    ):
        assert bound in rule, bound


def test_rule_records_the_modes_in_gate_9(slot):
    rule = _rule(slot)
    assert "day_history_mode baseline_merge" in rule
    assert "day_history_sha256" in rule
    assert "week_history_mode baseline_merge" in rule


def test_gate_6_5_full_day_fetch_points_back_to_the_chain(slot):
    text = _text(slot)
    line_start = text.rindex("\n", 0, text.index(FETCH)) + 1
    line = text[line_start : text.index("\n", line_start)]
    assert line.startswith("(SAME-DAY BASELINE CHAIN in Gate 6 comes first")
    assert "they are its fallback" in line
