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


# --- 2026-11-06: the same four rules in all seven live-mirror prompts. -----------------------------------------
# The 9:35 prompt ran the incremental design on 2026-11-05 with 18 orders (17 incremental merges, a clean
# check_per_order_record), the size at which the old full re-read compacted the session on 2026-10-29. The ADR
# (docs/adr-incremental-order-history.md) held the other six slots back until that happened. The rule text is
# identical across slots apart from the slot code in the baseline file names, so these tests pin both that each
# prompt carries it and that no prompt drifts from the 9:35 wording.

import re  # noqa: E402

import pytest  # noqa: E402

MIRROR_DIR = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
MIRRORS = sorted(p for p in MIRROR_DIR.glob("*.txt") if not p.name.startswith("."))
RULE_MARKERS = (INCREMENTAL, BATCHED, NO_SKIP, "PER-ORDER RECORD")
AUDIT = "PER-ORDER AUDIT"


def _slot(path: Path) -> str:
    return path.name[:4]


def _paragraph(text: str, marker: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(marker))


def _gate_text(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def test_the_mirror_set_is_the_seven_live_slots():
    assert [_slot(p) for p in MIRRORS] == ["0935", "1030", "1130", "1230", "1330", "1430", "1530"]


@pytest.mark.parametrize("mirror", MIRRORS, ids=_slot)
def test_every_mirror_carries_the_four_rules_inside_gate_7(mirror):
    gate7 = _gate_text(mirror.read_text(encoding="utf-8"), "GATE 7 - ", "GATE 8 - ")
    for marker in RULE_MARKERS:
        assert marker in gate7, f"{mirror.name}: {marker} is not inside Gate 7"


@pytest.mark.parametrize("mirror", MIRRORS, ids=_slot)
def test_the_rules_sit_before_per_order_timing(mirror):
    text = mirror.read_text(encoding="utf-8")
    assert text.index(INCREMENTAL) < text.index("PER-ORDER TIMING")


@pytest.mark.parametrize("mirror", MIRRORS, ids=_slot)
def test_the_incremental_rule_names_this_slots_baseline_files_and_no_other_slots(mirror):
    rule = _paragraph(mirror.read_text(encoding="utf-8"), INCREMENTAL)
    slot = _slot(mirror)
    assert f"orders_pages_<YYYY-MM-DD>_{slot}.json" in rule
    assert f"orders_pages_week_<Monday YYYY-MM-DD>_{slot}.json" in rule
    other_codes = set(re.findall(r"_(\d{4})\.json", rule)) - {slot}
    assert not other_codes, f"{mirror.name}: baseline names carry another slot's code {sorted(other_codes)}"


@pytest.mark.parametrize("mirror", MIRRORS, ids=_slot)
def test_the_incremental_rule_supersedes_the_full_refetch_and_names_the_merge_script(mirror):
    rule = _paragraph(mirror.read_text(encoding="utf-8"), INCREMENTAL)
    for needle in ("supersedes", "scripts.merge_order_pages", "--overlap-seconds 120", "exit 2"):
        assert needle in rule, f"{mirror.name}: {needle!r} missing from the incremental rule"


@pytest.mark.parametrize("mirror", MIRRORS, ids=_slot)
def test_gate_9_runs_the_per_order_audit_after_the_finalize_check(mirror):
    text = mirror.read_text(encoding="utf-8")
    gate9 = text[text.index("GATE 9 - ") :]
    assert AUDIT in gate9 and "scripts.check_per_order_record" in gate9
    assert gate9.index("Exit 0: verified") < gate9.index(AUDIT)


@pytest.mark.parametrize("mirror", MIRRORS, ids=_slot)
def test_the_rule_paragraphs_match_the_9_35_wording_apart_from_the_slot_code(mirror):
    reference = PROMPT.read_text(encoding="utf-8")
    text = mirror.read_text(encoding="utf-8")
    slot = _slot(mirror)
    for marker in (*RULE_MARKERS, AUDIT):
        assert _paragraph(text, marker).replace(slot, "0935") == _paragraph(reference, marker), (
            f"{mirror.name}: {marker} differs from the 9:35 paragraph"
        )
