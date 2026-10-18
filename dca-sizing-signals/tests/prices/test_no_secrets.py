"""API keys never leave the process: not in logs, exception text, request URLs or
parameters, provider reprs, run records, or the database file.

The worst case is modelled on purpose: a vendor error body that echoes the key back.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest
from market_data_ingest.prices.alpaca import AlpacaProvider
from market_data_ingest.prices.daily_job import run_daily_job
from market_data_ingest.prices.errors import ProviderError
from market_data_ingest.prices.massive import MassiveProvider
from market_data_ingest.prices.router import PriceRouter
from market_data_ingest.prices.store import PriceStore

from .conftest import (
    TEST_ALPACA_KEY_ID,
    TEST_ALPACA_SECRET,
    TEST_MASSIVE_KEY,
    FakeClock,
    FakeResponse,
    FakeSession,
    FixedCalendar,
    utc,
    weekdays,
)

SECRETS = (TEST_MASSIVE_KEY, TEST_ALPACA_KEY_ID, TEST_ALPACA_SECRET)
CAL = FixedCalendar(weekdays(date(2024, 5, 1), date(2024, 6, 30)))
S = date(2024, 6, 11)


def echoing(status, secret):
    return FakeResponse(status_code=status, text=f'{{"error": "bad request for key {secret}", "apiKey": "{secret}"}}')


def providers(clock, massive_responses, alpaca_responses):
    massive = MassiveProvider(
        TEST_MASSIVE_KEY,
        session=FakeSession(massive_responses),
        calendar=CAL,
        trailing_counts=lambda _s: [3] * 20,
        clock=clock.now,
        sleep=clock.sleep,
    )
    alpaca = AlpacaProvider(
        TEST_ALPACA_KEY_ID,
        TEST_ALPACA_SECRET,
        session=FakeSession(alpaca_responses),
        clock=clock.now,
        sleep=clock.sleep,
        universe=lambda: ["NVDA"],
    )
    return massive, alpaca


def assert_clean(text: str):
    for secret in SECRETS:
        assert secret not in text


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_exception_text_is_redacted_even_when_the_vendor_echoes_the_key(clock, status):
    massive, alpaca = providers(clock, [echoing(status, TEST_MASSIVE_KEY)], [echoing(status, TEST_ALPACA_SECRET)])

    for call in (lambda: massive.daily_bars(S), lambda: alpaca.last_trade(["NVDA"], feed="iex")):
        with pytest.raises(ProviderError) as excinfo:
            call()
        assert_clean(str(excinfo.value))
        assert_clean(repr(excinfo.value))
        assert_clean(str(excinfo.value.__cause__ or ""))


def test_reprs_do_not_contain_keys(clock):
    massive, alpaca = providers(clock, [], [])
    assert_clean(repr(massive) + repr(alpaca))


def test_failed_job_leaves_no_key_in_logs_run_records_or_the_database(tmp_path, caplog):
    clock = FakeClock(utc(2024, 6, 12, 2))
    massive, alpaca = providers(
        clock, lambda u, p: echoing(500, TEST_MASSIVE_KEY), lambda u, p: echoing(500, TEST_ALPACA_SECRET)
    )
    db = tmp_path / "prices.duckdb"
    store = PriceStore(db)
    staging = tmp_path / "staging"
    staging.mkdir()

    with caplog.at_level(logging.DEBUG):
        result = run_daily_job(
            session=S,
            store=store,
            router=PriceRouter(massive, alpaca),
            calendar=CAL,
            clock=clock.now,
            staging_dir=staging,
            run_id_factory=lambda: "run-secret",
        )
    events = store.run_events("run-secret")
    store.close()

    assert result.exit_code == 1
    assert_clean(caplog.text)
    assert_clean(repr(events))
    assert_clean(db.read_bytes().decode("latin-1"))
    for f in staging.rglob("*"):
        if f.is_file():
            assert_clean(f.read_bytes().decode("latin-1"))
