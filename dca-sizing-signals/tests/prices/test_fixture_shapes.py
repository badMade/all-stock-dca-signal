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


# Pagination cursors appear only when another page exists, so a single-page live response
# legitimately lacks them (the 2026-10-18 PoC's splits response had no next_url).
OPTIONAL_TOP_LEVEL = frozenset({"_provenance", "next_url", "next_page_token"})


def _rows(body: dict) -> list[dict]:
    for key in ("results", "trades", "bars"):
        value = body.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            rows: list[dict] = []
            for item in value.values():
                rows.extend(r for r in (item if isinstance(item, list) else [item]) if isinstance(r, dict))
            return rows
    return []


def _row_keys(body: dict) -> set[str]:
    """Union of keys over every row: some fields (Massive's historical_adjustment_factor on
    splits) are present on only some live rows, so the first row alone under-reports."""
    return set().union(*(set(r) for r in _rows(body))) if _rows(body) else set()


@pytest.mark.parametrize(("synthetic", "pattern"), sorted(COUNTERPARTS.items()))
def test_synthetic_fixture_keys_are_a_subset_of_the_live_recording(synthetic, pattern):
    matches = sorted(RECORDED.glob(pattern)) if RECORDED.exists() else []
    if not matches:
        pytest.skip(f"no live recording for {synthetic} yet (run the opt-in PoC)")
    fake = json.loads((FIXTURES / synthetic).read_text())
    real = json.loads(matches[0].read_text())
    assert set(fake) - OPTIONAL_TOP_LEVEL <= set(real)
    assert _row_keys(fake) <= _row_keys(real)
