"""The 12:30 live prompt must build orders_week.json from the 9:35 week baseline, by script.

2026-11-03 12:30 ET and 2026-11-05 15:30 ET: both slots aborted ``output_cap_exceeded`` with 0
orders because the multi-page same-ISO-week ``get_equity_orders`` history (~150+ orders, ~184 KB by
Thursday) could not be saved into the checkout. The other post-9:35 slots now reuse the 9:35 run's
saved, script-assembled week envelope (``market_open_dca/<day>_0935ET/orders_pages_week_<Monday>_0935.json``)
and merge only the orders since its ``snapshot_at`` with ``scripts.merge_order_pages``; their rule
is pinned in ``tests/test_live_prompt_week_baseline.py`` (the 3:30 copy started as
``tests/test_live_prompt_1530_week_baseline.py``).

12:30 differs in two ways, which is why it has its own file: its week history is fetched at Gate 4,
not Gate 6, and its gate scripts read ``orders_week.json`` -- a flat list the session used to reshape
from the raw rows BY HAND -- so the merged envelope is turned into that list by
``scripts.reshape_order_pages`` (tested in ``scripts/tests/test_reshape_order_pages.py``). These tests
mirror the shared rule's assertions and add the reshape-specific ones. When the shared file is
committed this one can be folded into it with ``"1230"`` added to its ``SLOTS``.
"""

from pathlib import Path

MIRROR = (
    Path(__file__).resolve().parent.parent
    / "claude/dca-executor-prompts/live-mirror/1230ET-1230-pm-midday-buying-run.txt"
)
RULE = "WEEK BASELINE REUSE"
GATE4_FETCH = "Gate 4's own broker fetch (for orders_week.json above)"
# 0935 is absent on purpose: the rule names the 9:35 file as the read-only --base.
OTHER_SLOTS = ("1030", "1130", "1330", "1430", "1530")


def _text() -> str:
    return MIRROR.read_text(encoding="utf-8")


def _rule() -> str:
    text = _text()
    start = text.index(f"\n{RULE} (") + 1
    end = text.find("\n", start)
    return text[start : end if end != -1 else len(text)]


def test_rule_sits_directly_before_gate_4s_own_week_fetch():
    text = _text()
    # One rule paragraph; Gate 4 and Gate 6.5 also mention it by name.
    assert text.count(f"\n{RULE} (") == 1
    start = text.index(f"\n{RULE} (")
    assert text.index("GATE 2 - REPO REACHABLE") < start < text.index(GATE4_FETCH)
    assert text.index(GATE4_FETCH) < text.index("GATE 5 - BUY LIST")


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
    assert "--out $ROOT/orders_pages_week_<Monday YYYY-MM-DD>_1230.json" in rule
    assert not any(f"orders_pages_week_<Monday YYYY-MM-DD>_{s}.json" in rule for s in OTHER_SLOTS)
    assert "never overwrite" in rule


def test_rule_only_narrows_by_created_at_gte():
    rule = _rule()
    assert "snapshot_at" in rule and "minus 120 seconds" in rule
    assert "symbol, state or placed_agent" in rule


def test_rule_builds_orders_week_with_the_reshape_script_never_by_hand():
    rule = _rule()
    assert "scripts.reshape_order_pages" in rule
    assert "--envelope $ROOT/orders_pages_week_<Monday YYYY-MM-DD>_1230.json" in rule
    assert "--out $ROOT/orders_week.json" in rule
    assert "NEVER reshape a row by hand" in rule
    # The merged envelope stands in for the raw copies the prompt used to re-emit.
    assert "orders_week_raw.json" in rule and "orders_week_raw_final.json" in rule


def test_rule_falls_back_to_the_full_fetch_when_no_baseline_or_either_script_refuses():
    rule = _rule()
    assert "exit 2" in rule
    assert "merge_order_pages or reshape_order_pages ends with exit 2" in rule
    assert "full" in rule and "OVERSIZED ORDER PAGES" in rule


def test_rule_covers_the_gate_6_5_and_gate_7_week_refetches():
    rule = _rule()
    assert "Gate 6.5's SECOND refresh" in rule
    assert "Gate 7" in rule
    assert "previous merged" in rule
    assert "orders_fetched_at" in rule


def test_rule_keeps_broker_truth_and_does_not_relax_caps():
    rule = _rule()
    assert "raw get_equity_orders rows" in rule
    assert "does NOT relax" in rule


def test_gate_4_and_gate_6_5_point_back_at_the_rule():
    text = _text()
    assert f"{GATE4_FETCH}, the FALLBACK used only when WEEK BASELINE REUSE just above does not apply" in text
    assert "through WEEK BASELINE REUSE step (4) when Gate 4 used it" in text
