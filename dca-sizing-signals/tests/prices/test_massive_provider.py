"""MassiveProvider: grouped daily bars, splits/dividends, and its fail-closed rules.

Expected behaviour (PLAN-price-source.md §4):
- one grouped call per session, unadjusted, OTC excluded, key in a header only;
- an empty result is accepted only when the calendar says the market was closed;
- a batch under 90% of the trailing-20-session median row count is incomplete;
- NOT_AUTHORIZED (beyond the 2-year plan window) is a ProviderError, not an empty day;
- splits arrive as forward_split / reverse_split / stock_dividend and are stored raw;
- Massive Basic has no real-time trades, so last_trade always raises.
"""

from __future__ import annotations

from datetime import date

import pytest
from market_data_ingest.prices.errors import IncompleteBatchError, ProviderError
from market_data_ingest.prices.massive import MassiveProvider
from market_data_ingest.prices.models import CorporateAction, DailyBar
from market_data_ingest.prices.provider import PriceProvider

from .conftest import (
    TEST_MASSIVE_KEY,
    FakeResponse,
    FakeSession,
    FixedCalendar,
    grouped_payload,
    load_fixture,
    weekdays,
)

# Every 2024 weekday except the 2024-07-04 holiday, so the calendar says "closed" for it.
OPEN = FixedCalendar(d for d in weekdays(date(2024, 1, 1), date(2024, 12, 31)) if d != date(2024, 7, 4))

BASELINE_3 = lambda _session: [3] * 20  # noqa: E731 - trailing 20 sessions of 3 rows each


def make(responses, *, clock, calendar=OPEN, trailing_counts=BASELINE_3, bootstrap_floor=None):
    session = FakeSession(responses)
    provider = MassiveProvider(
        TEST_MASSIVE_KEY,
        session=session,
        calendar=calendar,
        trailing_counts=trailing_counts,
        bootstrap_floor=bootstrap_floor,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return provider, session


def test_satisfies_price_provider_protocol(clock):
    provider, _ = make([], clock=clock)
    assert isinstance(provider, PriceProvider)


def test_grouped_daily_parses_raw_bars_for_the_requested_session(clock):
    provider, session = make([FakeResponse.json_body(load_fixture("massive_grouped_2024-05-31.json"))], clock=clock)

    bars = provider.daily_bars(date(2024, 5, 31))

    assert {b.symbol for b in bars} == {"NVDA", "AAPL", "MSFT"}
    nvda = next(b for b in bars if b.symbol == "NVDA")
    assert isinstance(nvda, DailyBar)
    assert nvda.source == "massive"
    assert nvda.session == date(2024, 5, 31)
    # Raw, pre-split price: 1096.33, not the split-adjusted ~109.63.
    assert nvda.close == pytest.approx(1096.33)
    assert (nvda.open, nvda.high, nvda.low) == pytest.approx((1130.0, 1135.0, 1060.0))
    assert nvda.volume == pytest.approx(67_000_000)
    assert nvda.vwap == pytest.approx(1095.1)
    assert {b.symbol: b.trades for b in bars} == {"NVDA": 9000, "AAPL": 7000, "MSFT": 6000}


def test_grouped_daily_is_one_unadjusted_call_without_otc_and_key_in_header(clock):
    provider, session = make([FakeResponse.json_body(load_fixture("massive_grouped_2024-05-31.json"))], clock=clock)

    provider.daily_bars(date(2024, 5, 31))

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call.url.endswith("/v2/aggs/grouped/locale/us/market/stocks/2024-05-31")
    assert str(call.params.get("adjusted")).lower() == "false"
    assert str(call.params.get("include_otc")).lower() == "false"
    assert call.headers.get("Authorization") == f"Bearer {TEST_MASSIVE_KEY}"
    assert "apiKey" not in call.params and TEST_MASSIVE_KEY not in call.url
    assert call.timeout is not None, "every request needs an explicit timeout"


def test_empty_result_on_a_closed_day_is_accepted(clock):
    provider, _ = make([FakeResponse.json_body(load_fixture("massive_grouped_holiday_2024-07-04.json"))], clock=clock)

    assert provider.daily_bars(date(2024, 7, 4)) == []


def test_empty_result_on_an_open_day_is_an_incomplete_batch(clock):
    holiday_shape = load_fixture("massive_grouped_holiday_2024-07-04.json")
    provider, _ = make([FakeResponse.json_body(holiday_shape)], clock=clock)

    with pytest.raises(IncompleteBatchError):
        provider.daily_bars(date(2024, 7, 5))  # a regular session


def test_results_count_mismatch_is_an_incomplete_batch(clock):
    provider, _ = make([FakeResponse.json_body(load_fixture("massive_grouped_count_mismatch.json"))], clock=clock)

    with pytest.raises(IncompleteBatchError):
        provider.daily_bars(date(2024, 5, 31))


def test_truncated_body_is_an_incomplete_batch(clock):
    body = FakeResponse.json_body(load_fixture("massive_grouped_2024-05-31.json")).text
    provider, _ = make([FakeResponse(status_code=200, text=body[: len(body) // 2])], clock=clock)

    with pytest.raises(IncompleteBatchError):
        provider.daily_bars(date(2024, 5, 31))


@pytest.mark.parametrize(
    ("rows", "should_pass"),
    [
        (89, False),  # under 90% of the median (100) -> incomplete
        (90, True),  # exactly 90% is not "under 90%"
        (150, True),
    ],
)
def test_row_count_threshold_is_90_percent_of_trailing_20_session_median(clock, rows, should_pass):
    counts = [100] * 19 + [5]  # median 100; one bad day must not drag the baseline down
    provider, _ = make(
        [FakeResponse.json_body(grouped_payload(date(2024, 6, 3), rows))],
        trailing_counts=lambda _s: counts,
        clock=clock,
    )

    if should_pass:
        assert len(provider.daily_bars(date(2024, 6, 3))) == rows
    else:
        with pytest.raises(IncompleteBatchError):
            provider.daily_bars(date(2024, 6, 3))


def test_fewer_than_20_baseline_sessions_refuses_without_bootstrap(clock):
    provider, _ = make(
        [FakeResponse.json_body(grouped_payload(date(2024, 6, 3), 10_000))],
        trailing_counts=lambda _s: [10_000] * 19,
        clock=clock,
    )

    with pytest.raises(IncompleteBatchError):
        provider.daily_bars(date(2024, 6, 3))


def test_bootstrap_floor_replaces_the_missing_baseline(clock):
    ok, _ = make(
        [FakeResponse.json_body(grouped_payload(date(2024, 6, 3), 50))],
        trailing_counts=lambda _s: [],
        bootstrap_floor=50,
        clock=clock,
    )
    assert len(ok.daily_bars(date(2024, 6, 3))) == 50

    short, _ = make(
        [FakeResponse.json_body(grouped_payload(date(2024, 6, 3), 49))],
        trailing_counts=lambda _s: [],
        bootstrap_floor=50,
        clock=clock,
    )
    with pytest.raises(IncompleteBatchError):
        short.daily_bars(date(2024, 6, 3))


def test_not_authorized_beyond_plan_window_is_a_provider_error_not_an_empty_day(clock):
    provider, session = make(
        [FakeResponse.json_body(load_fixture("massive_not_authorized.json"), status_code=403)],
        clock=clock,
    )

    with pytest.raises(ProviderError) as excinfo:
        provider.daily_bars(date(2021, 3, 1))

    assert not isinstance(excinfo.value, IncompleteBatchError)
    assert excinfo.value.code == "not_authorized"
    assert len(session.calls) == 1, "an entitlement refusal is not retried"


def test_not_authorized_status_with_http_200_is_still_refused(clock):
    body = load_fixture("massive_not_authorized.json")
    provider, _ = make([FakeResponse.json_body(body, status_code=200)], clock=clock)

    with pytest.raises(ProviderError) as excinfo:
        provider.daily_bars(date(2021, 3, 1))
    assert excinfo.value.code == "not_authorized"


def _corporate_action_responses(url, params):
    if "/stocks/v1/splits" in url:
        if "PAGE2CURSOR" in url or params.get("cursor") == "PAGE2CURSOR":
            return FakeResponse.json_body(load_fixture("massive_splits_page2.json"))
        return FakeResponse.json_body(load_fixture("massive_splits_page1.json"))
    if "/stocks/v1/dividends" in url:
        return FakeResponse.json_body(load_fixture("massive_dividends.json"))
    raise AssertionError(f"unexpected URL {url}")


def test_corporate_actions_cover_forward_reverse_stock_dividend_and_cash(clock):
    provider, session = make(_corporate_action_responses, clock=clock)

    actions = provider.corporate_actions(since=date(2024, 5, 12))

    assert all(isinstance(a, CorporateAction) and a.source == "massive" for a in actions)
    by_symbol = {a.symbol: a for a in actions}
    assert set(by_symbol) == {"NVDA", "RVRS", "STKD", "KO"}, "both split pages and dividends are read"

    nvda = by_symbol["NVDA"]
    assert (nvda.action_type, nvda.ex_date, nvda.split_from, nvda.split_to) == (
        "forward_split",
        date(2024, 6, 10),
        1,
        10,
    )
    assert nvda.historical_adjustment_factor == pytest.approx(0.1), "stored as delivered"

    rvrs = by_symbol["RVRS"]
    assert (rvrs.action_type, rvrs.split_from, rvrs.split_to) == ("reverse_split", 20, 1)

    stkd = by_symbol["STKD"]
    assert (stkd.action_type, stkd.split_from, stkd.split_to) == ("stock_dividend", 100, 105)

    ko = by_symbol["KO"]
    assert (ko.action_type, ko.ex_date, ko.cash_amount, ko.currency) == (
        "cash_dividend",
        date(2024, 6, 14),
        pytest.approx(0.485),
        "USD",
    )
    assert ko.pay_date == date(2024, 7, 1)

    # The "since" bound is sent to the vendor rather than filtered client-side after the fact.
    assert any("2024-05-12" in str(v) for c in session.calls for v in c.params.values())


def test_pagination_keeps_the_key_out_of_next_url(clock):
    provider, session = make(_corporate_action_responses, clock=clock)

    provider.corporate_actions(since=date(2024, 5, 12))

    page2 = [c for c in session.calls if "PAGE2CURSOR" in c.url or c.params.get("cursor") == "PAGE2CURSOR"]
    assert page2, "next_url was followed"
    assert all(TEST_MASSIVE_KEY not in c.url and "apiKey" not in c.params for c in session.calls)
    assert all(c.headers.get("Authorization") == f"Bearer {TEST_MASSIVE_KEY}" for c in session.calls)


def test_free_tier_rate_limit_spaces_calls_at_least_12_seconds(clock):
    provider, session = make(_corporate_action_responses, clock=clock)

    provider.corporate_actions(since=date(2024, 5, 12))

    assert len(session.calls) == 3  # splits p1, splits p2, dividends
    # 5 calls/minute: every call after the first waits until 12 s have passed since the last.
    assert sum(clock.sleeps) >= 24.0


def test_last_trade_is_not_in_the_basic_plan(clock):
    provider, session = make([], clock=clock)

    with pytest.raises(ProviderError) as excinfo:
        provider.last_trade(["NVDA"])
    assert excinfo.value.code == "not_in_plan"
    assert session.calls == []


def test_from_env_requires_the_key_and_never_echoes_it():
    with pytest.raises(ProviderError) as excinfo:
        MassiveProvider.from_env(env={}, session=FakeSession([]), calendar=OPEN, trailing_counts=BASELINE_3)
    assert excinfo.value.code == "missing_credentials"
    assert "MASSIVE_API_KEY" in str(excinfo.value)

    provider = MassiveProvider.from_env(
        env={"MASSIVE_API_KEY": TEST_MASSIVE_KEY}, session=FakeSession([]), calendar=OPEN, trailing_counts=BASELINE_3
    )
    assert TEST_MASSIVE_KEY not in repr(provider)
