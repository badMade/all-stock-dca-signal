"""AlpacaProvider (latest trades, raw daily bars, corporate actions) and AlpacaCalendar.

Expected behaviour (PLAN-price-source.md §4):
- latest trades come from the feed asked for (iex or delayed_sip), timestamped by the
  trade itself, with both keys sent as headers only;
- daily bars are raw (adjustment=raw), cover the given universe, follow next_page_token,
  and refuse to run with no universe to ask for;
- the calendar is authoritative for open/closed, including ad-hoc closures, and a
  calendar outage is an error, never "closed".
"""

from __future__ import annotations

from datetime import date

import pytest
from market_data_ingest.prices.alpaca import AlpacaCalendar, AlpacaProvider
from market_data_ingest.prices.errors import IncompleteBatchError, ProviderError
from market_data_ingest.prices.models import Quote
from market_data_ingest.prices.provider import PriceProvider

from .conftest import (
    TEST_ALPACA_KEY_ID,
    TEST_ALPACA_SECRET,
    FakeResponse,
    FakeSession,
    load_fixture,
    utc,
)


def make(responses, *, clock, universe=lambda: ["NVDA", "AAPL"]):
    session = FakeSession(responses)
    provider = AlpacaProvider(
        TEST_ALPACA_KEY_ID,
        TEST_ALPACA_SECRET,
        session=session,
        clock=clock.now,
        sleep=clock.sleep,
        universe=universe,
    )
    return provider, session


def assert_header_auth(call):
    assert call.headers.get("APCA-API-KEY-ID") == TEST_ALPACA_KEY_ID
    assert call.headers.get("APCA-API-SECRET-KEY") == TEST_ALPACA_SECRET
    flat = call.url + repr(call.params)
    assert TEST_ALPACA_KEY_ID not in flat and TEST_ALPACA_SECRET not in flat
    assert call.timeout is not None


def test_satisfies_price_provider_protocol(clock):
    provider, _ = make([], clock=clock)
    assert isinstance(provider, PriceProvider)


def test_latest_trade_iex_returns_trade_timestamped_quotes(clock):
    provider, session = make([FakeResponse.json_body(load_fixture("alpaca_latest_trades_iex.json"))], clock=clock)

    quotes = provider.last_trade(["NVDA", "AAPL", "MSFT"], feed="iex")

    call = session.calls[0]
    assert call.url.endswith("/v2/stocks/trades/latest")
    assert call.params.get("feed") == "iex"
    assert set(str(call.params.get("symbols")).split(",")) == {"NVDA", "AAPL", "MSFT"}
    assert_header_auth(call)

    assert set(quotes) == {"NVDA", "AAPL"}, "a symbol with no print is absent, never invented"
    nvda = quotes["NVDA"]
    assert isinstance(nvda, Quote)
    assert nvda.price == pytest.approx(121.5)
    assert nvda.ts == utc(2024, 6, 11, 14, 34, 58, 500000)
    assert nvda.feed == "iex"
    assert nvda.as_of == clock.now(), "as_of is when we fetched it, ts is when it traded"


def test_latest_trade_delayed_sip_is_labelled_delayed_sip(clock):
    provider, session = make([FakeResponse.json_body(load_fixture("alpaca_latest_trades_delayed.json"))], clock=clock)

    quotes = provider.last_trade(["AAPL", "MSFT"], feed="delayed_sip")

    assert session.calls[0].params.get("feed") == "delayed_sip"
    assert {q.feed for q in quotes.values()} == {"delayed_sip"}


def test_latest_trade_rejects_an_unknown_feed(clock):
    provider, session = make([], clock=clock)
    with pytest.raises(ValueError):
        provider.last_trade(["NVDA"], feed="sip")  # real-time SIP is not in the Basic plan
    assert session.calls == []


def _bars_pages(url, params):
    assert url.endswith("/v2/stocks/bars")
    if params.get("page_token") == "TOKEN2":
        return FakeResponse.json_body(load_fixture("alpaca_bars_page2.json"))
    return FakeResponse.json_body(load_fixture("alpaca_bars_page1.json"))


def test_daily_bars_are_raw_and_follow_pagination(clock):
    provider, session = make(_bars_pages, clock=clock)

    bars = provider.daily_bars(date(2024, 6, 10))

    assert {(b.symbol, b.session, b.source) for b in bars} == {
        ("NVDA", date(2024, 6, 10), "alpaca"),
        ("AAPL", date(2024, 6, 10), "alpaca"),
    }
    nvda = next(b for b in bars if b.symbol == "NVDA")
    assert nvda.close == pytest.approx(121.79)
    first = session.calls[0]
    assert first.params.get("timeframe") == "1Day"
    assert first.params.get("adjustment") == "raw"
    assert set(str(first.params.get("symbols")).split(",")) == {"NVDA", "AAPL"}
    assert len(session.calls) == 2
    for c in session.calls:
        assert_header_auth(c)


def test_failover_bars_covering_under_90_percent_of_the_universe_are_incomplete(clock):
    # Regression (review 2026-10-18): an Alpaca failover that answers for only part of the
    # universe must not let the nightly job record "ok" with most of the market missing.
    provider, _ = make(_bars_pages, clock=clock, universe=lambda: ["NVDA", "AAPL", "MSFT"])

    with pytest.raises(IncompleteBatchError) as excinfo:
        provider.daily_bars(date(2024, 6, 10))
    assert excinfo.value.code == "below_universe"


def test_daily_bars_without_a_universe_fail_closed(clock):
    provider, session = make([], clock=clock, universe=lambda: [])

    with pytest.raises(ProviderError) as excinfo:
        provider.daily_bars(date(2024, 6, 10))
    assert excinfo.value.code == "empty_universe"
    assert session.calls == []


def test_corporate_actions_map_every_alpaca_type(clock):
    provider, session = make([FakeResponse.json_body(load_fixture("alpaca_corporate_actions.json"))], clock=clock)

    actions = {a.symbol: a for a in provider.corporate_actions(since=date(2024, 5, 12))}

    assert all(a.source == "alpaca" for a in actions.values())
    assert (actions["NVDA"].action_type, actions["NVDA"].split_from, actions["NVDA"].split_to) == (
        "forward_split",
        1,
        10,
    )
    assert (actions["RVRS"].action_type, actions["RVRS"].split_from, actions["RVRS"].split_to) == (
        "reverse_split",
        20,
        1,
    )
    # A 5% stock dividend is a 100 -> 105 share ratio.
    stkd = actions["STKD"]
    assert stkd.action_type == "stock_dividend"
    assert stkd.split_to / stkd.split_from == pytest.approx(1.05)
    assert (actions["KO"].action_type, actions["KO"].cash_amount) == ("cash_dividend", pytest.approx(0.485))
    assert_header_auth(session.calls[0])


def test_from_env_requires_both_keys():
    for env in ({}, {"ALPACA_KEY_ID": TEST_ALPACA_KEY_ID}, {"ALPACA_SECRET_KEY": TEST_ALPACA_SECRET}):
        with pytest.raises(ProviderError) as excinfo:
            AlpacaProvider.from_env(env=env, session=FakeSession([]), universe=lambda: ["NVDA"])
        assert excinfo.value.code == "missing_credentials"
        assert TEST_ALPACA_SECRET not in str(excinfo.value)


# --- calendar -------------------------------------------------------------------------


def _calendar_handler(url, params):
    assert url.endswith("/v2/calendar")
    start = date.fromisoformat(params["start"])
    end = date.fromisoformat(params["end"])
    days = [d for d in load_fixture("alpaca_calendar.json")["days"] if start <= date.fromisoformat(d["date"]) <= end]
    return FakeResponse.json_body(days)


def make_calendar(responses, *, clock):
    session = FakeSession(responses)
    cal = AlpacaCalendar(TEST_ALPACA_KEY_ID, TEST_ALPACA_SECRET, session=session, clock=clock.now, sleep=clock.sleep)
    return cal, session


@pytest.mark.parametrize(
    ("day", "is_open"),
    [
        (date(2024, 5, 31), True),
        (date(2024, 7, 3), True),  # early close is still a session
        (date(2024, 7, 4), False),  # Independence Day
        (date(2024, 5, 27), False),  # Memorial Day
        (date(2025, 1, 9), False),  # ad-hoc closure a computed calendar would miss
        (date(2024, 6, 8), False),  # Saturday
    ],
)
def test_calendar_is_authoritative_for_open_and_closed(clock, day, is_open):
    cal, session = make_calendar(_calendar_handler, clock=clock)
    assert cal.is_session(day) is is_open
    assert_header_auth(session.calls[0])


def test_previous_sessions_skip_holidays_most_recent_first(clock):
    cal, _ = make_calendar(_calendar_handler, clock=clock)

    assert cal.previous_sessions(date(2024, 6, 3), 5) == [
        date(2024, 5, 31),
        date(2024, 5, 30),
        date(2024, 5, 29),
        date(2024, 5, 28),
        date(2024, 5, 24),
    ]


@pytest.mark.parametrize(
    "failure", [FakeResponse(status_code=503, text="unavailable"), FakeResponse(status_code=401, text="{}")]
)
def test_calendar_outage_raises_instead_of_reporting_closed(clock, failure):
    cal, _ = make_calendar([failure], clock=clock)

    with pytest.raises(ProviderError):
        cal.is_session(date(2024, 7, 4))
