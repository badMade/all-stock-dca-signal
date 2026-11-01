"""Failing-first tests: the operator's NO SHORTCUTS rule is captured in all seven live-mirror prompts.

On 2026-10-29 the operator added a rule straight to the seven live Cowork tasks after the 9:35 run skipped the
per-order full-week history re-fetch, batched tradability lookups and used filtered history fetches because headroom
"looked wide enough". It was never copied into ``claude/dca-executor-prompts/live-mirror/``, so
``scripts/check_live_prompt_drift`` reports ``7 of 7 slots need a repaste`` (one changed line each) and
``scripts/sync_live_prompts --write`` would overwrite the live tasks and delete the rule. Capturing live into the mirror,
byte for byte, makes the drift check read ``0 of 7`` and a sync a no-op.

Pinned here: the exact sentence, appended to the ``place_equity_order`` submission line it governs, once, in every
slot's prompt, identical across the seven.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIRROR = Path(__file__).resolve().parents[1] / "claude" / "dca-executor-prompts" / "live-mirror"
SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")
#: The line the rule is appended to, in every prompt (the per-order submission instruction it constrains).
ANCHOR = "Load the schema with ToolSearch for place_equity_order, then submit one order at a time, sequentially, never in parallel."
RULE = (
    ' NO SHORTCUTS (operator, 2026-10-29): the 10/29 9:35 run skipped the per-order full-week history re-fetch, '
    'batched tradability lookups across tickers, and used filtered history fetches, each because headroom '
    '"looked wide enough". That is not allowed. Every per-order step above runs for EVERY order, in full, one ticker '
    'at a time, however much cap headroom remains; if time runs short, stop the batch and alert -- never thin the '
    'checks to fit the window. Gate 9 likewise means all three channels (message center, alert file, email); a push '
    "notification does not replace any of them."
)


def _lines(slot: str) -> list[str]:
    (path,) = MIRROR.glob(f"{slot}ET-*.txt")
    return path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("slot", SLOTS)
def test_the_rule_is_appended_to_the_per_order_submission_line(slot):
    matches = [line for line in _lines(slot) if line.startswith(ANCHOR)]
    assert len(matches) == 1, f"{slot}: expected exactly one place_equity_order submission line, got {len(matches)}"
    assert matches[0] == ANCHOR + RULE, f"{slot}: the line must be the anchor sentence followed by the operator's rule"


@pytest.mark.parametrize("slot", SLOTS)
def test_the_rule_appears_once(slot):
    text = "\n".join(_lines(slot))
    assert text.count("NO SHORTCUTS (operator, 2026-10-29)") == 1, slot
