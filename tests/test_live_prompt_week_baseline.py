"""Post-9:35 prompts must reuse a safe ISO-week baseline across missed slots.

2026-11-05 15:30 ET: the run reached Gate 6 with 81 BUY candidates and aborted
``ABORT_GATE6_HISTORY_INCOMPLETE`` / ``output_cap_exceeded`` with 0 orders, because the
multi-page week-scoped ``get_equity_orders`` history "cannot be saved into the repo checkout
from this session". By Thursday afternoon the full week is ~150+ orders (the 9:35 run's saved
week envelope that morning was 184 KB), which a session cannot re-emit into a file.

The 9:35 run already saves a complete, script-assembled week envelope every morning
(``market_open_dca/<day>_0935ET/orders_pages_week_<Monday>_0935.json``, with ``snapshot_at``),
and ``scripts.merge_order_pages`` already merges an overlapping incremental fetch into such an
envelope, failing closed on any gap. Replaying the real 2026-11-05 data through it (9:35
baseline of 149 orders + the 25-order same-day page) gives 157 orders -- the same count the
14:30 run reported from a full fetch. So the 3:30 slot only has to save TODAY's orders.

2026-11-13: the 9:35 slot did not fire, so the 10:30 session had no same-day week envelope and
fell back to a 135,748-character full-week fetch that could not be saved. The week finder now
checks envelopes from earlier trading days of the same ISO week; merge_order_pages revalidates
the base rows and overlap, so these are safe incremental bases.

These tests pin the WEEK BASELINE REUSE rule in the 10:30, 11:30, 12:30, 1:30, 2:30 and 3:30 prompts so it
cannot silently drift out of one of them. The 12:30 prompt reshapes the week's rows into
``orders_week.json`` through the scripted ``reshape_order_pages`` tool.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
SLOTS = {
    "1030": "1030ET-1030-dca-recurring-buy.txt",
    "1130": "1130ET-1130-mornig-buying-run.txt",
    "1230": "1230ET-1230-pm-midday-buying-run.txt",
    "1330": "1330ET-130-pm-afternoon-buyiing-run.txt",
    "1430": "1430ET-230-pm-afternoon-buying-run.txt",
    "1530": "1530ET-330-final-buying-run.txt",
}
RULE = "WEEK BASELINE REUSE"
GATE4_FETCH = "Gate 4's own broker fetch (for orders_week.json above)"

pytestmark = pytest.mark.parametrize("slot", sorted(SLOTS))


def _text(slot: str) -> str:
    return (MIRRORS / SLOTS[slot]).read_text(encoding="utf-8")


def _rule(slot: str) -> str:
    text = _text(slot)
    start = text.index(RULE)
    end = text.find("\n", start)
    return text[start : end if end != -1 else len(text)]


def test_rule_sits_where_the_slot_first_fetches_the_week(slot):
    text = _text(slot)
    assert RULE in text
    if slot == "1230":
        # 12:30's week fetch is Gate 4's own broker fetch; the rule sits directly before it.
        assert text.index("GATE 2 - REPO REACHABLE") < text.index(RULE) < text.index(GATE4_FETCH)
        assert text.index(GATE4_FETCH) < text.index("GATE 5 - BUY LIST")
    else:
        assert text.index("GATE 6 - BUDGET") < text.index(RULE) < text.index("GATE 6.5 - SAME-DAY TOP-UP GUARD")


def test_rule_names_the_9_35_week_baseline_and_the_merge_script(slot):
    rule = _rule(slot)
    for needle in (
        "orders_pages_week_",
        "_0935.json",
        "market_open_dca",
        "scripts.merge_order_pages",
        "--created-at-gte",
        "--snapshot-at",
        "--overlap-seconds 120",
    ):
        assert needle in rule, needle


def test_rule_never_overwrites_the_9_35_baseline(slot):
    rule = _rule(slot)
    # The merged envelope is written under this run's own $ROOT; the 9:35 file is read-only here.
    assert "$ROOT/orders_pages_week_" in rule
    assert f"orders_pages_week_<Monday YYYY-MM-DD>_{slot}.json" in rule
    other_slots = [s for s in SLOTS if s != slot]
    assert not any(f"_{s}.json" in rule for s in other_slots)
    assert "never overwrite" in rule


def test_rule_only_narrows_by_created_at_gte(slot):
    rule = _rule(slot)
    assert "snapshot_at" in rule and "minus 120 seconds" in rule
    assert "symbol, state or placed_agent" in rule


def test_rule_falls_back_to_the_full_fetch_when_no_baseline_or_merge_refuses(slot):
    rule = _rule(slot)
    assert "exit 2" in rule or "exits 2" in rule
    assert "full" in rule and "OVERSIZED ORDER PAGES" in rule


def test_missed_935_uses_prior_week_baseline_before_full_fetch(slot):
    rule = _rule(slot)
    if slot in {"1030", "1130", "1230"}:
        assert "scripts.find_order_baseline --kind week" in rule
        assert "prior_baseline_merge" in rule
        assert "full ISO" in rule and "OVERSIZED ORDER PAGES" in rule


def test_rule_covers_the_gate_7_per_order_week_refetch(slot):
    rule = _rule(slot)
    assert "Gate 7" in rule
    assert "previous merged" in rule


def test_rule_keeps_broker_truth_and_does_not_relax_caps(slot):
    rule = _rule(slot)
    assert "raw get_equity_orders rows" in rule
    assert "does NOT relax" in rule


def test_1230_builds_orders_week_with_the_reshape_script_never_by_hand(slot):
    if slot != "1230":
        pytest.skip("only 12:30's gates read the reshaped orders_week.json list")
    rule = _rule(slot)
    assert "scripts.reshape_order_pages" in rule
    assert "--envelope $ROOT/orders_pages_week_<Monday YYYY-MM-DD>_1230.json" in rule
    assert "--out $ROOT/orders_week.json" in rule
    assert "NEVER reshape a row by hand" in rule
    # The merged envelope replaces the re-emitted raw copies, and a reshape refusal falls back too.
    assert "orders_week_raw.json" in rule and "orders_week_raw_final.json" in rule
    assert "reshape" in rule and "refuses" in rule


def test_1230_gate_4_and_gate_6_5_point_back_at_the_rule(slot):
    if slot != "1230":
        pytest.skip("12:30-specific cross-references")
    text = _text(slot)
    # One rule paragraph; the other mentions are these cross-references back at it.
    assert text.count(f"\n{RULE} (") == 1
    assert f"{GATE4_FETCH}, the FALLBACK used only when WEEK BASELINE REUSE just above does not apply" in text
    assert "through WEEK BASELINE REUSE step (4) when Gate 4 used it" in text
    # 12:30 re-fetches the week at Gate 6.5's SECOND refresh too, not only at Gate 7.
    rule = _rule(slot)
    assert "Gate 6.5's SECOND refresh" in rule
    assert "orders_fetched_at" in rule
