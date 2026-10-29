"""The 9:35 live prompt must allow incremental history, forbid silent skips, and audit itself.

2026-10-28 and 2026-10-29: the run re-read the whole order history before every order (about
360,000 tokens of tool output for 18 orders, so the session was compacted mid-batch), narrowed
its own fetches to cope, batched the tradability check, and skipped the weekly-history refetch
because "budget headroom is wide". Each was a deviation from text the prompt makes mandatory,
reported only in the closing summary. These tests pin the rules that replace that text so the
behaviour is either allowed in a checkable form or forbidden outright:

  * PER-ORDER INCREMENTAL HISTORY: one full day-and-week fetch at batch start, then an
    overlapping fetch per order merged by ``scripts.merge_order_pages``;
  * BATCHED TRADABILITY: one up-front check, valid for 300 seconds;
  * NO SILENT SKIPS: a wide cap is not a reason to skip a check; a check that cannot run
    means stop and alert;
  * a per-order record that ``scripts.check_per_order_record`` audits at Gate 9.
"""

from pathlib import Path

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt"
)
INCREMENTAL = "PER-ORDER INCREMENTAL HISTORY"
BATCHED = "BATCHED TRADABILITY"
NO_SKIP = "NO SILENT SKIPS"


def _text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def _section(marker: str, span: int = 3500) -> str:
    text = _text()
    start = text.index(marker)
    return text[start : start + span]


def test_incremental_history_rule_sits_inside_gate_7():
    text = _text()
    assert INCREMENTAL in text
    assert text.index("GATE 7 - PLACE THE ORDERS") < text.index(INCREMENTAL) < text.index("GATE 8 - CONFIRM")


def test_incremental_rule_names_the_merge_script_and_its_arguments():
    rule = _section(INCREMENTAL)
    for needle in ("scripts.merge_order_pages", "--created-at-gte", "--snapshot-at", "--overlap-seconds 120"):
        assert needle in rule


def test_incremental_rule_says_it_supersedes_the_full_refetch_and_keeps_the_week():
    rule = _section(INCREMENTAL)
    assert "supersedes" in rule
    assert "week" in rule and "baseline" in rule


def test_incremental_rule_falls_back_to_a_full_fetch_when_the_merge_refuses():
    rule = _section(INCREMENTAL)
    assert "exit 2" in rule and "full" in rule


def test_batched_tradability_is_allowed_for_300_seconds():
    rule = _section(BATCHED, 1500)
    assert "300 seconds" in rule
    assert "get_equity_tradability" in rule


def test_no_silent_skips_rule_rejects_headroom_as_a_reason_and_says_stop_and_alert():
    rule = _section(NO_SKIP, 1800)
    assert "headroom" in rule
    assert "stop and alert" in rule


def test_per_order_record_is_written_and_audited_before_notifying():
    text = _text()
    assert "per_order_checks.jsonl" in text
    assert "scripts.check_per_order_record" in text
    gate9 = text[text.index("GATE 9 - REPORT AND PERSIST EVIDENCE") :]
    assert "scripts.check_per_order_record" in gate9


def test_the_old_every_order_full_refetch_sentence_is_gone():
    # "Fetch all pages again before every order" contradicted the incremental rule.
    assert "Fetch all pages again before every order" not in _text()
