"""PriceRouter: primary, then fallback, then raise. Nothing swallowed.

Expected behaviour (PLAN-price-source.md §4): a ProviderError (including StaleDataError and
IncompleteBatchError) from the primary sends the call to the fallback; if that also fails,
RouterExhaustedError carries both causes. A bug in a provider (any non-ProviderError) is
not a reason to fail over and propagates unchanged.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from market_data_ingest.prices.errors import (
    IncompleteBatchError,
    ProviderError,
    RouterExhaustedError,
    StaleDataError,
)
from market_data_ingest.prices.provider import PriceProvider
from market_data_ingest.prices.router import PriceRouter

SESSION = date(2024, 6, 10)


class StubProvider:
    def __init__(self, name, *, bars=None, actions=None, trades=None, error=None):
        self.name, self._bars, self._actions, self._trades, self._error = name, bars, actions, trades, error
        self.calls: list[str] = []

    def _maybe_fail(self, method):
        self.calls.append(method)
        if self._error is not None:
            raise self._error

    def daily_bars(self, session):
        self._maybe_fail("daily_bars")
        return self._bars

    def corporate_actions(self, since):
        self._maybe_fail("corporate_actions")
        return self._actions

    def last_trade(self, symbols):
        self._maybe_fail("last_trade")
        return self._trades


def test_router_is_itself_a_price_provider():
    assert isinstance(PriceRouter(StubProvider("p"), StubProvider("f")), PriceProvider)


def test_primary_success_never_touches_the_fallback():
    primary, fallback = StubProvider("p", bars=["p-bar"]), StubProvider("f", bars=["f-bar"])

    assert PriceRouter(primary, fallback).daily_bars(SESSION) == ["p-bar"]
    assert fallback.calls == []


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("down", code="http_5xx"),
        IncompleteBatchError("short", code="below_median"),
        StaleDataError("old", code="stale"),
    ],
    ids=["provider", "incomplete", "stale"],
)
def test_primary_provider_error_falls_back(error, caplog):
    primary, fallback = StubProvider("p", error=error), StubProvider("f", bars=["f-bar"])

    with caplog.at_level(logging.WARNING):
        assert PriceRouter(primary, fallback).daily_bars(SESSION) == ["f-bar"]
    assert type(error).__name__ in caplog.text, "the failover is logged, not silent"


def test_both_failing_raises_router_exhausted_with_both_causes():
    p_err, f_err = ProviderError("primary down", code="http_5xx"), ProviderError("fallback down", code="timeout")
    router = PriceRouter(StubProvider("p", error=p_err), StubProvider("f", error=f_err))

    with pytest.raises(RouterExhaustedError) as excinfo:
        router.daily_bars(SESSION)
    assert isinstance(excinfo.value, ProviderError)
    assert excinfo.value.primary_error is p_err
    assert excinfo.value.fallback_error is f_err


@pytest.mark.parametrize("method,arg", [("corporate_actions", date(2024, 5, 11)), ("last_trade", ["NVDA"])])
def test_every_protocol_method_routes_the_same_way(method, arg):
    primary = StubProvider("p", error=ProviderError("down", code="http_5xx"))
    fallback = StubProvider("f", actions=["f-action"], trades={"NVDA": "f-quote"})

    result = getattr(PriceRouter(primary, fallback), method)(arg)
    assert result in (["f-action"], {"NVDA": "f-quote"})


def test_a_bug_in_the_primary_is_not_masked_by_failover():
    primary, fallback = StubProvider("p", error=KeyError("results")), StubProvider("f", bars=["f-bar"])

    with pytest.raises(KeyError):
        PriceRouter(primary, fallback).daily_bars(SESSION)
    assert fallback.calls == []
