"""Keep scheduled prompt payload examples compatible with their Gate 5 producers."""

import re
from pathlib import Path

import pytest

from scripts.generate_market_open_candidates import MARKET_OPEN_SPEC
from scripts.generate_morning_candidates import MORNING_SPEC


@pytest.mark.parametrize(
    ("filename", "spec"),
    [
        ("0935ET-935-am-dca-morning-buy.txt", MARKET_OPEN_SPEC),
        ("1030ET-1030-dca-recurring-buy.txt", MORNING_SPEC),
    ],
)
def test_prompt_primary_payload_matches_candidate_source(filename, spec):
    """A prompt must not tell an external executor to build a rejected payload."""
    mirrors = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
    text = (mirrors / filename).read_text(encoding="utf-8")
    gate_five = text.split("GATE 5 - BUY LIST", 1)[1].split("GATE 6 -", 1)[0]
    payload_source = re.search(r'"sources":\s*\{\s*"([^\"]+)"', gate_five)
    assert payload_source is not None, "Gate 5 must document its market-data payload"
    assert payload_source.group(1) == spec.source.value
