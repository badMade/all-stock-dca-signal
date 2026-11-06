"""The 3:30 PM live prompt must build its ISO-week history from the 9:35 week baseline.

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

These tests pin the WEEK BASELINE REUSE rule so it cannot silently drift out of the prompt.
"""

from pathlib import Path

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "claude/dca-executor-prompts/live-mirror/1530ET-330-final-buying-run.txt"
)
RULE = "WEEK BASELINE REUSE"


def _text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _rule() -> str:
    text = _text()
    start = text.index(RULE)
    end = text.find("\n", start)
    return text[start : end if end != -1 else len(text)]


def test_rule_sits_inside_gate_6():
    text = _text()
    assert RULE in text
    assert text.index("GATE 6 - BUDGET") < text.index(RULE) < text.index("GATE 6.5 - SAME-DAY TOP-UP GUARD")


def test_rule_names_the_9_35_week_baseline_and_the_merge_script():
    rule = _rule()
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


def test_rule_never_overwrites_the_9_35_baseline():
    rule = _rule()
    # The merged envelope is written under this run's own $ROOT; the 9:35 file is read-only here.
    assert "$ROOT/orders_pages_week_" in rule
    assert "_1530.json" in rule
    assert "never overwrite" in rule


def test_rule_only_narrows_by_created_at_gte():
    rule = _rule()
    assert "snapshot_at" in rule and "minus 120 seconds" in rule
    assert "symbol, state or placed_agent" in rule


def test_rule_falls_back_to_the_full_fetch_when_no_baseline_or_merge_refuses():
    rule = _rule()
    assert "exit 2" in rule
    assert "full" in rule and "OVERSIZED ORDER PAGES" in rule


def test_rule_covers_the_gate_7_per_order_week_refetch():
    rule = _rule()
    assert "Gate 7" in rule
    assert "previous merged" in rule


def test_rule_keeps_broker_truth_and_does_not_relax_caps():
    rule = _rule()
    assert "raw get_equity_orders rows" in rule
    assert "does NOT relax" in rule
