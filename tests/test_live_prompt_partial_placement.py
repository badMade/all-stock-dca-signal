"""Every live prompt must say a short window means PARTIAL placement, never an up-front abort.

Two scheduled sessions placed nothing at all because they judged, before the first order, that the
remaining window was too short for the whole batch:

  * 2026-10-30, 9:35: the run fired at 10:11 ET with about 6 minutes left, cited the NO SILENT SKIPS
    rule and ended ``Gate 7, window insufficient`` with a buy list it could have started on;
  * 2026-11-03, 15:30: the payload was published at 15:40 ET, and the run ended ``window_exhausted``
    ("insufficient window for full per-order Gate 7 sequence on 34 candidates; nothing placed").

The prompts already treat a window that closes MID-batch as a STOP-THE-BATCH condition that halts the
remaining orders while "everything already placed is still reported", so partial placement is the
specified behaviour. What no prompt says is that an ESTIMATE of too little time, made before the first
order, is not a reason to place nothing; the 9:35 prompt's "place nothing further" was read as "place
nothing". The fix is wording only: the per-order checks stay mandatory and unshortened, fewer orders
are placed, and the rest are reported as skipped for the window.

These tests pin that wording in all seven mirrors. They are derived from the behaviour wanted (start
placing in priority order, run every check for every order, stop when the window closes, report the
rest), not from any implementation, and they fail until each mirror carries a PARTIAL PLACEMENT
paragraph. The mirrors are copies of the live Claude tasks, so the operator repastes them after merge.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("*.txt") if not p.name.startswith("."))
MARKER = "PARTIAL PLACEMENT"
# One paragraph, not the whole prompt: the phrases must sit together in the rule, not be satisfied by
# unrelated wording elsewhere in a 130 KB prompt.
SPAN = 3000

# (why it is required, lower-cased phrase the paragraph must contain)
REQUIRED = [
    ("an estimate is not a reason to place nothing", "estimate"),
    ("the abort the sessions chose is forbidden before the first order", "before the first order"),
    ("orders go in the existing allocation priority order", "allocation priority"),
    ("no check is shortened to fit the window", "never shorten"),
    ("every order still runs the whole per-order sequence", "full per-order sequence"),
    ("the window closing mid-batch still stops the batch", "stop the batch"),
    ("the unplaced tickers are reported with a window reason", "skipped_window"),
]


def _rule(name: str) -> str:
    text = (MIRRORS / name).read_text(encoding="utf-8")
    assert MARKER in text, f"{name} has no {MARKER} paragraph"
    start = text.index(MARKER)
    return text[start : start + SPAN].lower()


def test_there_are_live_prompt_mirrors_to_check():
    # An empty glob would make every parametrized test below vacuous.
    assert PROMPTS


@pytest.mark.parametrize("name", PROMPTS)
def test_every_live_prompt_has_a_partial_placement_paragraph(name):
    assert MARKER in (MIRRORS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PROMPTS)
@pytest.mark.parametrize(("why", "phrase"), REQUIRED, ids=[phrase for _, phrase in REQUIRED])
def test_partial_placement_paragraph_carries_each_required_rule(name, why, phrase):
    assert phrase in _rule(name), f"{name}: {why}"
