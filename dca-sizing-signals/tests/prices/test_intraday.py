"""Intraday slot policy: which feeds a slot may use and how fresh a trade must be.

Expected behaviour (PLAN-price-source.md §4, operator answers Q2 and Q3):
- only the 09:35, 10:30, 12:30 and 13:30 ET slots are configured; any other slot raises;
- IEX last trade must be at most 120 s old;
- delayed SIP is allowed only for slots at or after 10:30 and never at 09:35; it must be at
  most 15 min + 120 s old and is always flagged delayed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from market_data_ingest.prices import intraday
from market_data_ingest.prices.errors import StaleDataError
from market_data_ingest.prices.models import Quote

from .conftest import utc

NOW = utc(2024, 6, 11, 14, 30)  # 10:30 ET


def quote(age_s: float, *, feed="iex", price=100.0, symbol="NVDA"):
    return Quote(symbol=symbol, price=price, ts=NOW - timedelta(seconds=age_s), feed=feed, as_of=NOW)


def test_only_the_four_briefed_slots_are_configured():
    assert set(intraday.SLOT_POLICY) == {"09:35", "10:30", "12:30", "13:30"}


@pytest.mark.parametrize(("slot", "allowed"), [("09:35", False), ("10:30", True), ("12:30", True), ("13:30", True)])
def test_delayed_feed_is_allowed_only_at_or_after_10_30(slot, allowed):
    assert intraday.allows_delayed(slot) is allowed


@pytest.mark.parametrize("slot", ["11:30", "14:30", "15:30", "9:35", "", "10:31"])
def test_unconfigured_slots_raise(slot):
    with pytest.raises(ValueError):
        intraday.allows_delayed(slot)


def test_freshness_limits():
    assert intraday.MAX_AGE_IEX_S == 120
    assert intraday.MAX_AGE_DELAYED_S == 15 * 60 + 120


@pytest.mark.parametrize(("age", "fresh"), [(0, True), (120, True), (120.001, False), (900, False)])
def test_iex_quote_freshness(age, fresh):
    q = quote(age)
    if fresh:
        intraday.check_fresh(q, now=NOW)
    else:
        with pytest.raises(StaleDataError):
            intraday.check_fresh(q, now=NOW)


@pytest.mark.parametrize(("age", "fresh"), [(900, True), (1020, True), (1020.001, False)])
def test_delayed_quote_freshness(age, fresh):
    q = quote(age, feed="delayed_sip")
    if fresh:
        intraday.check_fresh(q, now=NOW)
    else:
        with pytest.raises(StaleDataError):
            intraday.check_fresh(q, now=NOW)


@pytest.mark.parametrize("bad", [quote(-60), quote(10, price=0.0), quote(10, price=-1.0)],
                         ids=["future-timestamp", "zero-price", "negative-price"])
def test_implausible_quotes_are_stale_not_usable(bad):
    with pytest.raises(StaleDataError):
        intraday.check_fresh(bad, now=NOW)
