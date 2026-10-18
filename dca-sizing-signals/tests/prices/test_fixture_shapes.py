"""Fixture honesty: every fixture says where it came from, and synthetic fixtures match the
shape of live recordings once the opt-in PoC has produced them.

Until fixtures/recorded/ exists the shape check skips (it cannot pass or fail honestly
without a live payload to compare against).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED = FIXTURES / "recorded"

# synthetic fixture -> glob of the recording that should share its shape
COUNTERPARTS = {
    "massive_grouped_2024-05-31.json": "massive_grouped_*.json",
    "massive_splits_page1.json": "massive_splits*.json",
    "massive_dividends.json": "massive_dividends*.json",
    "alpaca_latest_trades_iex.json": "alpaca_latest_trades_iex*.json",
    "alpaca_bars_page1.json": "alpaca_bars*.json",
}


def synthetic_fixtures():
    return sorted(FIXTURES.glob("*.json"))


@pytest.mark.parametrize("path", synthetic_fixtures(), ids=lambda p: p.name)
def test_every_fixture_declares_its_provenance(path):
    body = json.loads(path.read_text())
    assert isinstance(body, dict) and body.get("_provenance"), f"{path.name} has no _provenance"


def _row_keys(body: dict) -> set[str]:
    for key in ("results", "trades", "bars"):
        value = body.get(key)
        if isinstance(value, list) and value:
            return set(value[0])
        if isinstance(value, dict) and value:
            first = next(iter(value.values()))
            first = first[0] if isinstance(first, list) and first else first
            return set(first) if isinstance(first, dict) else set()
    return set()


@pytest.mark.parametrize(("synthetic", "pattern"), sorted(COUNTERPARTS.items()))
def test_synthetic_fixture_keys_are_a_subset_of_the_live_recording(synthetic, pattern):
    matches = sorted(RECORDED.glob(pattern)) if RECORDED.exists() else []
    if not matches:
        pytest.skip(f"no live recording for {synthetic} yet (run the opt-in PoC)")
    fake = json.loads((FIXTURES / synthetic).read_text())
    real = json.loads(matches[0].read_text())
    assert set(fake) - {"_provenance"} <= set(real) | {"_provenance"}
    assert _row_keys(fake) <= _row_keys(real)
