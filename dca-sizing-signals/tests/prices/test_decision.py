"""price_for_suggestion: a fresh, recorded price, or an explicit "no suggestion". Never stale.

Expected behaviour (PLAN-price-source.md §4): IEX first; delayed SIP only where the slot
allows it, flagged delayed; on total failure a NoSuggestion with the reason and error
class; every price returned is persisted to intraday_quotes before it is returned, and a
price that cannot be recorded is not used.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from market_data_ingest.prices.decision import price_for_suggestion
from market_data_ingest.prices.errors import ProviderError
from market_data_ingest.prices.models import NoSuggestion, PriceOK, Quote

from .conftest import FakeClock, utc

T_0935 = utc(2024, 6, 11, 13, 35)
T_1030 = utc(2024, 6, 11, 14, 30)


class StubAlpaca:
    """``feeds`` maps feed -> {symbol: age_seconds} or an exception to raise."""

    def __init__(self, now, feeds):
        self.now, self.feeds, self.requested = now, feeds, []

    def last_trade(self, symbols, *, feed="iex"):
        self.requested.append(feed)
        spec = self.feeds.get(feed, {})
        if isinstance(spec, BaseException):
            raise spec
        return {
            s: Quote(
                symbol=s, price=100.0 + age / 1000, ts=self.now - timedelta(seconds=age), feed=feed, as_of=self.now
            )
            for s, age in spec.items()
            if s in symbols
        }


def decide(store, slot, now, feeds, symbol="NVDA"):
    clock = FakeClock(now)
    provider = StubAlpaca(now, feeds)
    result = price_for_suggestion(symbol, slot=slot, provider=provider, store=store, clock=clock.now, run_id="rq-1")
    return result, provider


def test_fresh_iex_quote_is_used_and_recorded(store):
    result, provider = decide(store, "09:35", T_0935, {"iex": {"NVDA": 30}})

    assert isinstance(result, PriceOK)
    assert (result.symbol, result.feed, result.delayed) == ("NVDA", "iex", False)
    assert provider.requested == ["iex"]
    rows = store.intraday_quotes(run_id="rq-1")
    assert len(rows) == 1
    assert (rows[0]["symbol"], rows[0]["feed"], rows[0]["delayed"], rows[0]["slot"]) == ("NVDA", "iex", False, "09:35")
    assert rows[0]["price"] == pytest.approx(result.price)
    assert rows[0]["ts"] == result.ts


def test_0935_never_falls_back_to_delayed_data(store):
    result, provider = decide(store, "09:35", T_0935, {"iex": {"NVDA": 300}, "delayed_sip": {"NVDA": 905}})

    assert isinstance(result, NoSuggestion)
    assert provider.requested == ["iex"], "the delayed feed is not even asked"
    assert result.error_class == "StaleDataError"
    assert store.intraday_quotes(run_id="rq-1") == []


@pytest.mark.parametrize("slot", ["10:30", "12:30", "13:30"])
def test_later_slots_fall_back_to_flagged_delayed_sip(store, slot):
    result, provider = decide(store, slot, T_1030, {"iex": {"NVDA": 300}, "delayed_sip": {"NVDA": 905}})

    assert isinstance(result, PriceOK)
    assert (result.feed, result.delayed) == ("delayed_sip", True)
    assert provider.requested == ["iex", "delayed_sip"]
    [row] = store.intraday_quotes(run_id="rq-1")
    assert (row["feed"], row["delayed"]) == ("delayed_sip", True)


def test_symbol_with_no_iex_print_uses_delayed_where_allowed(store):
    result, _ = decide(store, "12:30", T_1030, {"iex": {}, "delayed_sip": {"NVDA": 920}})
    assert isinstance(result, PriceOK) and result.delayed


def test_stale_delayed_quote_is_no_suggestion(store):
    result, _ = decide(store, "10:30", T_1030, {"iex": {"NVDA": 300}, "delayed_sip": {"NVDA": 1100}})

    assert isinstance(result, NoSuggestion)
    assert not hasattr(result, "price"), "a NoSuggestion never carries a price"
    assert store.intraday_quotes(run_id="rq-1") == []


@pytest.mark.parametrize(
    ("slot", "feeds", "error_class"),
    [
        ("09:35", {"iex": ProviderError("down", code="http_5xx")}, "ProviderError"),
        (
            "13:30",
            {"iex": ProviderError("down", code="http_5xx"), "delayed_sip": ProviderError("down", code="timeout")},
            "RouterExhaustedError",
        ),
    ],
)
def test_provider_failure_is_an_explicit_no_suggestion(store, slot, feeds, error_class):
    result, _ = decide(store, slot, T_1030, feeds)

    assert isinstance(result, NoSuggestion)
    assert result.symbol == "NVDA"
    assert result.error_class == error_class
    assert result.reason


def test_unknown_slot_is_a_programming_error_not_a_no_suggestion(store):
    with pytest.raises(ValueError):
        decide(store, "11:30", T_1030, {"iex": {"NVDA": 10}})


def test_a_price_that_cannot_be_recorded_is_not_used(store):
    class BrokenStore:
        def insert_intraday_quote(self, *args, **kwargs):
            raise OSError("disk full")

    clock = FakeClock(T_0935)
    result = price_for_suggestion(
        "NVDA",
        slot="09:35",
        provider=StubAlpaca(T_0935, {"iex": {"NVDA": 5}}),
        store=BrokenStore(),
        clock=clock.now,
        run_id="rq-1",
    )

    assert isinstance(result, NoSuggestion)
    assert "record" in result.reason.lower() or "persist" in result.reason.lower()
    assert result.error_class == "OSError"
