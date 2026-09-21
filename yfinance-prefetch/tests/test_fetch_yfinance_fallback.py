"""Tests for fetch_yfinance_fallback.py -- no real network calls.

Every yfinance-touching function is exercised via a monkeypatched
`yfinance` module (sys.modules substitution, mirroring
ticker-market-breadth/tests/test_fetch_tick.py's own pattern) so these tests
never require network access or a real `yfinance` install to construct their
inputs -- only to run, since `yfinance`/`pandas` are this subproject's own
declared dependencies.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from fetch_yfinance_fallback import (
    SOURCE_KEY,
    build_fallback_document,
    fetch_intraday_bars,
    fetch_previous_close_bar,
    fetch_quote,
    fetch_ticker_payload,
    main,
)

ET = ZoneInfo("America/New_York")
DAY = date(2026, 8, 5)


def _daily_frame(rows: dict) -> pd.DataFrame:
    """rows: {date: (open, high, low, close, volume)}"""
    index = pd.DatetimeIndex(sorted(rows), name="Date")
    ordered = [rows[d] for d in sorted(rows)]
    return pd.DataFrame(ordered, index=index, columns=["Open", "High", "Low", "Close", "Volume"])


def _intraday_frame(rows: dict) -> pd.DataFrame:
    """rows: {tz-aware datetime: (open, high, low, close, volume)}"""
    index = pd.DatetimeIndex(sorted(rows), name="Datetime")
    ordered = [rows[t] for t in sorted(rows)]
    return pd.DataFrame(ordered, index=index, columns=["Open", "High", "Low", "Close", "Volume"])


class _FakeTicker:
    def __init__(self, symbol: str, *, daily=None, intraday=None, fast_info=None, raise_on=()):
        self.symbol = symbol
        self._daily = daily
        self._intraday = intraday
        self.fast_info = fast_info or {}
        self._raise_on = set(raise_on)

    def history(self, *, interval, **_kwargs):
        if self.symbol in self._raise_on:
            raise RuntimeError(f"synthetic fetch failure for {self.symbol}")
        if interval == "1d":
            return self._daily if self._daily is not None else pd.DataFrame()
        return self._intraday if self._intraday is not None else pd.DataFrame()


def _install_fake_yfinance(monkeypatch, tickers: dict) -> None:
    """tickers: {symbol: _FakeTicker instance}"""
    fake_module = types.ModuleType("yfinance")
    fake_module.Ticker = lambda symbol: tickers[symbol]  # noqa: E731
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)


# --- fetch_previous_close_bar ------------------------------------------------


def test_fetch_previous_close_bar_returns_the_latest_row_strictly_before_trading_day(monkeypatch):
    daily = _daily_frame(
        {
            date(2026, 8, 3): (10.0, 11.0, 9.0, 10.5, 1000.0),
            date(2026, 8, 4): (10.5, 11.5, 9.5, 11.0, 1200.0),
        }
    )
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", daily=daily)})
    bar = fetch_previous_close_bar("TEST", DAY)
    assert bar["close"] == pytest.approx(11.0)
    assert bar["low"] == pytest.approx(9.5)
    assert bar["timestamp"] == datetime(2026, 8, 4, 16, 0, tzinfo=ET).isoformat()


def test_fetch_previous_close_bar_ignores_same_day_or_future_rows(monkeypatch):
    """A row dated on-or-after trading_day is never "the prior completed
    session" -- scripts/generate_buy_candidates.py's own _StaticMarketData
    rejects such a bar outright, so this fetch must never hand one out."""
    daily = _daily_frame({DAY: (10.0, 11.0, 9.0, 10.5, 1000.0)})
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", daily=daily)})
    assert fetch_previous_close_bar("TEST", DAY) is None


def test_fetch_previous_close_bar_returns_none_on_empty_history(monkeypatch):
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", daily=pd.DataFrame())})
    assert fetch_previous_close_bar("TEST", DAY) is None


# --- fetch_intraday_bars ------------------------------------------------


def test_fetch_intraday_bars_omits_bars_after_the_decision_moment(monkeypatch):
    intraday = _intraday_frame(
        {
            datetime(2026, 8, 5, 12, 0, tzinfo=ET): (10.0, 10.2, 9.9, 10.1, 500.0),
            datetime(2026, 8, 5, 12, 15, tzinfo=ET): (10.1, 10.3, 10.0, 10.2, 500.0),
            # Still-forming/future relative to a 12:15 decision anchor.
            datetime(2026, 8, 5, 12, 30, tzinfo=ET): (10.2, 10.4, 10.1, 10.3, 500.0),
        }
    )
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", intraday=intraday)})
    bars = fetch_intraday_bars("TEST", DAY, dtime(12, 15), 15)
    assert [b["timestamp"] for b in bars] == [
        datetime(2026, 8, 5, 12, 0, tzinfo=ET).isoformat(),
        datetime(2026, 8, 5, 12, 15, tzinfo=ET).isoformat(),
    ]


def test_fetch_intraday_bars_returns_empty_list_on_empty_history(monkeypatch):
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", intraday=pd.DataFrame())})
    assert fetch_intraday_bars("TEST", DAY, dtime(15, 30), 15) == []


# --- fetch_quote ------------------------------------------------


def test_fetch_quote_reads_last_price_from_fast_info(monkeypatch):
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", fast_info={"last_price": 42.5})})
    quote = fetch_quote("TEST")
    assert quote["price"] == pytest.approx(42.5)
    assert quote["as_of"] is not None


def test_fetch_quote_returns_none_when_no_price_is_available(monkeypatch):
    _install_fake_yfinance(monkeypatch, {"TEST": _FakeTicker("TEST", fast_info={})})
    assert fetch_quote("TEST") is None


# --- fetch_ticker_payload / build_fallback_document ------------------------------------------------


def test_fetch_ticker_payload_omits_missing_previous_close_and_quote(monkeypatch):
    """An absent previous_close_bar/quote is left OUT of the payload, never
    stamped as a literal null -- matching the optional-key shape the
    candidate generators already expect."""
    _install_fake_yfinance(
        monkeypatch,
        {"TEST": _FakeTicker("TEST", daily=pd.DataFrame(), intraday=pd.DataFrame(), fast_info={})},
    )
    payload = fetch_ticker_payload("TEST", DAY, dtime(15, 30), 15)
    assert payload == {"intraday_bars": []}


def test_build_fallback_document_records_a_ticker_whose_fetch_raised(monkeypatch):
    """A per-ticker fetch exception must land in ticker_errors and the
    ticker must be entirely absent from sources.yahoo_finance_yfinance --
    never a partial/garbled entry that could be mistaken for real data."""
    daily = _daily_frame({date(2026, 8, 4): (10.0, 11.0, 9.0, 10.5, 1000.0)})
    _install_fake_yfinance(
        monkeypatch,
        {
            "GOOD": _FakeTicker("GOOD", daily=daily, intraday=pd.DataFrame(), fast_info={}),
            "BAD": _FakeTicker("BAD", raise_on={"BAD"}),
        },
    )
    document = build_fallback_document(["GOOD", "BAD"], DAY, dtime(15, 30), 15)
    assert set(document["sources"][SOURCE_KEY]) == {"GOOD"}
    assert document["ticker_errors"] == {SOURCE_KEY: ["BAD"]}
    assert document["source_errors"] == []


def test_build_fallback_document_omits_ticker_errors_key_when_nothing_failed(monkeypatch):
    _install_fake_yfinance(
        monkeypatch,
        {"GOOD": _FakeTicker("GOOD", daily=pd.DataFrame(), intraday=pd.DataFrame(), fast_info={})},
    )
    document = build_fallback_document(["GOOD"], DAY, dtime(15, 30), 15)
    assert document["ticker_errors"] == {}


# --- main() (CLI) ------------------------------------------------


def test_main_writes_the_documented_shape_to_out(tmp_path, monkeypatch):
    daily = _daily_frame({date(2026, 8, 4): (10.0, 11.0, 9.0, 10.5, 1000.0)})
    intraday = _intraday_frame({datetime(2026, 8, 5, 12, 15, tzinfo=ET): (10.1, 10.3, 10.0, 10.2, 500.0)})
    _install_fake_yfinance(
        monkeypatch,
        {"TEST": _FakeTicker("TEST", daily=daily, intraday=intraday, fast_info={"last_price": 10.2})},
    )
    out_path = tmp_path / "fallback.json"
    exit_code = main(
        [
            "--tickers", "test",
            "--trading-day", DAY.isoformat(),
            "--decision-time-et", "12:30",
            "--interval-minutes", "15",
            "--out", str(out_path),
        ]
    )
    assert exit_code == 0
    document = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(document["sources"][SOURCE_KEY]) == {"TEST"}
    assert document["sources"][SOURCE_KEY]["TEST"]["previous_close_bar"]["close"] == pytest.approx(10.5)
    assert document["sources"][SOURCE_KEY]["TEST"]["quote"]["price"] == pytest.approx(10.2)


def test_main_rejects_an_empty_tickers_argument():
    with pytest.raises(SystemExit):
        main(
            [
                "--tickers", "  , ",
                "--trading-day", DAY.isoformat(),
                "--decision-time-et", "12:30",
                "--out", "/dev/null",
            ]
        )


def test_main_rejects_a_malformed_decision_time():
    with pytest.raises(SystemExit):
        main(
            [
                "--tickers", "TEST",
                "--trading-day", DAY.isoformat(),
                "--decision-time-et", "not-a-time",
                "--out", "/dev/null",
            ]
        )
