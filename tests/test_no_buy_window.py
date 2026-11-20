"""Tests for the dated ``no_buy`` block in ``config/tickers.json`` (slice 1: the pure rule).

Written BEFORE the implementation (TDD). Expectations come from the operator's chat request of
2026-11-20 and ``docs/adr-tax-loss-sale.md``, not from any code:

- Seven tickers sold at a realized loss on 2026-11-20 (DFTX, RGTI, LITE, CBRS, VCX, INTC, SNDK)
  must not be bought for the wash-sale window. The ADR's pause is 31 calendar days after the sale
  date, so the last blocked day is 2026-12-21 and buying resumes 2026-12-22.
- The tickers stay in ``active`` (still evaluated and tracked); only the BUY is blocked. That is
  why this is a separate ``no_buy`` block and not the permanent ``excluded`` list.
- The operator can release a ticker early. A release is recorded as ``released_on`` on the entry,
  never by deleting the entry, so the history of what was blocked and when stays in the file.
- Fail closed: a malformed block is a hard error, never silently treated as "nothing blocked".

Later slices (each gets its own approved test first): enforcement in ``near_close_dca``,
``morning_dca`` (and its three subclasses), ``dca_runner`` and ``scripts/generate_buy_candidates``;
the early-release helper script; and the blocked-buy tracking record in run artifacts.

Every ticker below is SYNTHETIC (``AAA``, ``BBB``, ...) except in the one test that reads the real
checkout file on purpose.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

import dca_config

# Test-first gate: the function does not exist until the operator approves this test and the
# implementation lands. Skipping at module level (not failing) keeps the rest of the suite green
# in the meantime. REMOVE THIS GATE in the implementation commit so the tests become binding.
if not hasattr(dca_config, "no_buy_blocked_tickers"):
    pytest.skip("no_buy_blocked_tickers not implemented yet (TDD, awaiting approval)", allow_module_level=True)

no_buy_blocked_tickers = dca_config.no_buy_blocked_tickers

REAL_TICKERS_JSON = Path(__file__).resolve().parent.parent / "config" / "tickers.json"


def _cfg(**no_buy_entries: dict) -> dict:
    """A minimal synthetic tickers.json body with the given no_buy entries."""
    return {
        "active": ["AAA", "BBB", "CCC"],
        "excluded": ["XXX"],
        "whole_share": ["WWW"],
        "no_buy": no_buy_entries,
    }


def _entry(**overrides) -> dict:
    base = {
        "blocked_on": "2026-11-20",
        "until": "2026-12-21",
        "reason": "synthetic tax-loss harvest",
        "released_on": None,
    }
    base.update(overrides)
    return base


# --- the window: blocked_on and until are both inclusive -----------------------------------


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        (date(2026, 11, 19), frozenset()),  # the day before the sale: not yet blocked
        (date(2026, 11, 20), frozenset({"AAA"})),  # first blocked day
        (date(2026, 12, 1), frozenset({"AAA"})),  # mid-window
        (date(2026, 12, 21), frozenset({"AAA"})),  # last blocked day (31 days after the sale)
        (date(2026, 12, 22), frozenset()),  # buying resumes
    ],
)
def test_window_bounds_are_inclusive(on, expected):
    assert no_buy_blocked_tickers(_cfg(AAA=_entry()), on) == expected


def test_only_listed_tickers_are_blocked():
    blocked = no_buy_blocked_tickers(_cfg(AAA=_entry()), date(2026, 12, 1))
    assert "BBB" not in blocked
    assert "CCC" not in blocked


def test_several_entries_are_evaluated_independently():
    cfg = _cfg(AAA=_entry(), BBB=_entry(blocked_on="2026-11-10", until="2026-11-30"))
    assert no_buy_blocked_tickers(cfg, date(2026, 11, 25)) == {"AAA", "BBB"}
    assert no_buy_blocked_tickers(cfg, date(2026, 12, 1)) == {"AAA"}


# --- early release: recorded on the entry, effective on that date -----------------------------


def test_release_takes_effect_on_the_release_date():
    cfg = _cfg(AAA=_entry(released_on="2026-11-25"))
    assert no_buy_blocked_tickers(cfg, date(2026, 11, 24)) == {"AAA"}  # still blocked the day before
    assert no_buy_blocked_tickers(cfg, date(2026, 11, 25)) == frozenset()  # released that day
    assert no_buy_blocked_tickers(cfg, date(2026, 12, 1)) == frozenset()


def test_null_release_means_not_released():
    assert no_buy_blocked_tickers(_cfg(AAA=_entry(released_on=None)), date(2026, 12, 1)) == {"AAA"}


def test_released_entry_still_exists_for_tracking():
    # The rule only reports what is blocked; the entry (the audit trail) is the caller's data and
    # must not be mutated by evaluating it.
    cfg = _cfg(AAA=_entry(released_on="2026-11-25"))
    before = copy.deepcopy(cfg)
    no_buy_blocked_tickers(cfg, date(2026, 12, 1))
    assert cfg == before


# --- absent block ------------------------------------------------------------------------


def test_missing_no_buy_key_blocks_nothing():
    cfg = _cfg()
    del cfg["no_buy"]
    assert no_buy_blocked_tickers(cfg, date(2026, 12, 1)) == frozenset()


def test_empty_no_buy_block_blocks_nothing():
    assert no_buy_blocked_tickers(_cfg(), date(2026, 12, 1)) == frozenset()


# --- fail closed on a malformed block ---------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "no_buy"),
    [
        ("block is a list", ["AAA"]),
        ("block is a string", "AAA"),
        ("entry is not an object", {"AAA": "2026-12-21"}),
        ("blocked_on missing", {"AAA": {k: v for k, v in _entry().items() if k != "blocked_on"}}),
        ("until missing", {"AAA": {k: v for k, v in _entry().items() if k != "until"}}),
        ("reason missing", {"AAA": {k: v for k, v in _entry().items() if k != "reason"}}),
        ("reason empty", {"AAA": _entry(reason="  ")}),
        ("date not ISO", {"AAA": _entry(until="12/21/2026")}),
        ("date not a string", {"AAA": _entry(until=20261221)}),
        ("impossible date", {"AAA": _entry(until="2026-02-30")}),
        ("until before blocked_on", {"AAA": _entry(blocked_on="2026-12-01", until="2026-11-01")}),
        ("released before blocked_on", {"AAA": _entry(released_on="2026-11-01")}),
        ("lowercase symbol", {"aaa": _entry()}),
        ("symbol not in active", {"ZZZ": _entry()}),
        ("symbol is permanently excluded", {"XXX": _entry()}),
    ],
)
def test_malformed_block_is_a_hard_error(label, no_buy):
    cfg = {"active": ["AAA", "BBB", "CCC"], "excluded": ["XXX"], "whole_share": ["WWW"], "no_buy": no_buy}
    with pytest.raises(ValueError, match="no_buy"):
        no_buy_blocked_tickers(cfg, date(2026, 12, 1))


def test_a_bad_entry_is_not_skipped_while_good_ones_are_honoured():
    # One bad entry must fail the whole load: silently dropping it would un-block a ticker the
    # operator believes is blocked.
    cfg = _cfg(AAA=_entry(), BBB=_entry(until="not-a-date"))
    with pytest.raises(ValueError, match="no_buy"):
        no_buy_blocked_tickers(cfg, date(2026, 12, 1))


# --- the real checkout file -----------------------------------------------------------------

HARVEST_TICKERS = {"DFTX", "RGTI", "LITE", "CBRS", "VCX", "INTC", "SNDK"}


def _real() -> dict:
    return json.loads(REAL_TICKERS_JSON.read_bytes())


def test_real_config_blocks_the_seven_harvest_tickers_through_2026_12_21():
    cfg = _real()
    assert set(cfg["no_buy"]) == HARVEST_TICKERS
    assert no_buy_blocked_tickers(cfg, date(2026, 11, 20)) == HARVEST_TICKERS
    assert no_buy_blocked_tickers(cfg, date(2026, 12, 21)) == HARVEST_TICKERS
    assert no_buy_blocked_tickers(cfg, date(2026, 12, 22)) == frozenset()


def test_real_config_keeps_the_harvest_tickers_active_for_tracking():
    cfg = _real()
    assert HARVEST_TICKERS <= set(cfg["active"]), "a no_buy ticker must stay in 'active' so it is still tracked"
    assert not (HARVEST_TICKERS & set(cfg["excluded"])), "no_buy is not the permanent excluded list"


def test_real_config_entries_start_unreleased_and_carry_a_reason():
    for symbol, entry in _real()["no_buy"].items():
        assert entry["released_on"] is None, f"{symbol} should start unreleased"
        assert entry["reason"].strip(), f"{symbol} needs a reason"
        assert entry["blocked_on"] == "2026-11-20"
