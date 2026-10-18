"""Transport and payload failures fail closed, for both vendors.

Expected behaviour (PLAN-price-source.md §4): timeouts, connection errors, 5xx and a 429
that repeats are ProviderErrors; a single 429 honours Retry-After once; bodies are parsed
strictly (truncation, NaN and duplicate keys are rejected); nothing is retried silently
and nothing comes back as an empty "success".
"""

from __future__ import annotations

from datetime import date

import pytest
import requests

from market_data_ingest.prices.alpaca import AlpacaProvider
from market_data_ingest.prices.errors import IncompleteBatchError, ProviderError
from market_data_ingest.prices.massive import MassiveProvider

from .conftest import (
    TEST_ALPACA_KEY_ID,
    TEST_ALPACA_SECRET,
    TEST_MASSIVE_KEY,
    FakeResponse,
    FakeSession,
    FixedCalendar,
    load_fixture,
    weekdays,
)

SESSION = date(2024, 5, 31)
GOOD_GROUPED = load_fixture("massive_grouped_2024-05-31.json")
GOOD_TRADES = load_fixture("alpaca_latest_trades_iex.json")


def massive_call(responses, clock):
    session = FakeSession(responses)
    provider = MassiveProvider(
        TEST_MASSIVE_KEY,
        session=session,
        calendar=FixedCalendar(weekdays(date(2024, 1, 1), date(2024, 12, 31))),
        trailing_counts=lambda _s: [3] * 20,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return (lambda: provider.daily_bars(SESSION)), session, GOOD_GROUPED


def alpaca_call(responses, clock):
    session = FakeSession(responses)
    provider = AlpacaProvider(
        TEST_ALPACA_KEY_ID, TEST_ALPACA_SECRET, session=session, clock=clock.now, sleep=clock.sleep,
        universe=lambda: ["NVDA"],
    )
    return (lambda: provider.last_trade(["NVDA", "AAPL"], feed="iex")), session, GOOD_TRADES


VENDORS = pytest.mark.parametrize("build", [massive_call, alpaca_call], ids=["massive", "alpaca"])


@VENDORS
@pytest.mark.parametrize(
    ("exc", "code"),
    [(requests.exceptions.Timeout("read timed out"), "timeout"),
     (requests.exceptions.ConnectionError("connection reset"), "connection")],
)
def test_transport_failure_is_a_provider_error_and_not_retried(build, clock, exc, code):
    call, session, _ = build([exc], clock)

    with pytest.raises(ProviderError) as excinfo:
        call()
    assert excinfo.value.code == code
    assert excinfo.value.__cause__ is exc, "the original exception is chained, not swallowed"
    assert len(session.calls) == 1


@VENDORS
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_a_provider_error(build, clock, status):
    call, session, _ = build([FakeResponse(status_code=status, text="upstream error")], clock)

    with pytest.raises(ProviderError) as excinfo:
        call()
    assert excinfo.value.code == "http_5xx"
    assert len(session.calls) == 1


@VENDORS
def test_single_429_honours_retry_after_once_then_succeeds(build, clock):
    call, session, good = build(
        [FakeResponse(status_code=429, text="slow down", headers={"Retry-After": "7"}), FakeResponse.json_body(good)],
        clock,
    )

    assert call()  # non-empty result
    assert len(session.calls) == 2
    assert any(s >= 7 for s in clock.sleeps), "waited at least Retry-After"


@VENDORS
def test_repeated_429_raises_rate_limited(build, clock):
    call, session, _ = build(
        [FakeResponse(status_code=429, text="slow down", headers={"Retry-After": "1"}),
         FakeResponse(status_code=429, text="slow down", headers={"Retry-After": "1"})],
        clock,
    )

    with pytest.raises(ProviderError) as excinfo:
        call()
    assert excinfo.value.code == "rate_limited"
    assert len(session.calls) == 2, "exactly one retry"


@VENDORS
@pytest.mark.parametrize(
    "body",
    [
        '{"status": "OK", "results": [',  # truncated
        "",  # empty body
        "<html>gateway</html>",  # not JSON
        '{"status": "OK", "status": "OK", "results": []}',  # duplicate key
        '{"status": "OK", "trades": {"NVDA": {"p": NaN}}}',  # NaN is not JSON
        "[]",  # valid JSON that is not an object
    ],
    ids=["truncated", "empty", "html", "duplicate-key", "nan", "not-an-object"],
)
def test_malformed_body_never_becomes_an_empty_success(build, clock, body):
    call, _, _ = build([FakeResponse(status_code=200, text=body)], clock)

    with pytest.raises(ProviderError):
        call()


def test_massive_truncation_is_specifically_an_incomplete_batch(clock):
    call, _, _ = massive_call([FakeResponse(status_code=200, text='{"status": "OK", "results": [')], clock)
    with pytest.raises(IncompleteBatchError):
        call()


@VENDORS
def test_4xx_other_than_429_is_not_retried(build, clock):
    call, session, _ = build([FakeResponse(status_code=400, text='{"message": "bad request"}')], clock)

    with pytest.raises(ProviderError) as excinfo:
        call()
    assert excinfo.value.code in {"http_4xx", "not_authorized"}
    assert len(session.calls) == 1
