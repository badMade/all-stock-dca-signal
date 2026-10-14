"""Tests for scripts.build_robinhood_market_data (TEST ONLY, written before the implementation).

The builder turns raw Robinhood MCP responses (get_equity_historicals 5minute and day,
get_equity_quotes) into the market-data payload documented in docs/intraday_signal_entry.md,
with no hand transcription. ASSUMPTION to confirm: a day bar's begins_at falls on the
session's calendar date in ET or UTC.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.build_robinhood_market_data import build_payload

ET = ZoneInfo("America/New_York")


def bars5(symbol, rows):
    return {"data": {"results": [{"symbol": symbol, "interval": "5minute", "bars": rows}]}}


def bar(ts, o, h, low, c, v=100, **extra):
    return {"begins_at": ts, "open_price": str(o), "high_price": str(h),
            "low_price": str(low), "close_price": str(c), "volume": v, "session": "reg", **extra}


def day(symbol, rows):
    return {"data": {"results": [{"symbol": symbol, "interval": "day", "bars": rows}]}}


def quote(symbol, price, ts):
    return {"data": {"results": [{"quote": {"symbol": symbol, "last_trade_price": str(price),
                                            "venue_last_trade_time": ts}}]}}


TRADING_DAY = "2026-10-13"
ARGS = dict(trading_day=TRADING_DAY)


def test_intraday_bars_become_tz_aware_et_with_numeric_fields():
    p = build_payload(
        intraday=[bars5("QQQ", [bar("2026-10-13T16:30:00Z", 743.16, 744.01, 743.16, 743.74, 18143)])],
        daily=[day("QQQ", [bar("2026-10-12T04:00:00Z", 740, 747, 739, 746.04)])],
        quotes=[quote("QQQ", 742.26, "2026-10-13T17:48:41Z")], **ARGS)
    b = p["sources"]["robinhood"]["QQQ"]["intraday_bars"][0]
    assert datetime.fromisoformat(b["timestamp"]) == datetime(2026, 10, 13, 12, 30, tzinfo=ET)
    assert b["timestamp"].endswith("-04:00")
    assert (b["open"], b["high"], b["low"], b["close"], b["volume"]) == (743.16, 744.01, 743.16, 743.74, 18143)


def test_previous_close_bar_is_last_completed_session_before_trading_day():
    p = build_payload(
        intraday=[bars5("QQQ", [bar("2026-10-13T16:30:00Z", 1, 1, 1, 1)])],
        daily=[day("QQQ", [bar("2026-10-09T04:00:00Z", 1, 2, 1, 2),
                           bar("2026-10-12T04:00:00Z", 3, 5, 3, 4),
                           bar("2026-10-13T04:00:00Z", 9, 9, 9, 9)])],
        quotes=[quote("QQQ", 4, "2026-10-13T17:48:41Z")], **ARGS)
    prev = p["sources"]["robinhood"]["QQQ"]["previous_close_bar"]
    assert (prev["close"], prev["low"]) == (4.0, 3.0)  # not the in-progress 10-13 bar


def test_quote_price_and_as_of_are_carried_through():
    p = build_payload(
        intraday=[bars5("QQQ", [bar("2026-10-13T16:30:00Z", 1, 1, 1, 1)])],
        daily=[day("QQQ", [bar("2026-10-12T04:00:00Z", 3, 5, 3, 4)])],
        quotes=[quote("QQQ", 742.26, "2026-10-13T17:48:41Z")], **ARGS)
    q = p["sources"]["robinhood"]["QQQ"]["quote"]
    assert q["price"] == 742.26
    assert datetime.fromisoformat(q["as_of"]) == datetime(2026, 10, 13, 13, 48, 41, tzinfo=ET)


def test_interpolated_bars_are_dropped():
    p = build_payload(
        intraday=[bars5("QQQ", [bar("2026-10-13T16:30:00Z", 1, 1, 1, 1),
                                bar("2026-10-13T16:35:00Z", 1, 1, 1, 1, interpolated=True)])],
        daily=[day("QQQ", [bar("2026-10-12T04:00:00Z", 3, 5, 3, 4)])],
        quotes=[quote("QQQ", 1, "2026-10-13T17:48:41Z")], **ARGS)
    assert len(p["sources"]["robinhood"]["QQQ"]["intraday_bars"]) == 1


def test_ticker_missing_data_is_recorded_as_error_not_omitted():
    p = build_payload(intraday=[], daily=[], quotes=[], requested=["ZZZZ"], **ARGS)
    assert "ZZZZ" in p["ticker_errors"]["robinhood"]
    assert "ZZZZ" not in p["sources"]["robinhood"]


def test_nothing_fetched_at_all_marks_whole_source_failed():
    p = build_payload(intraday=[], daily=[], quotes=[], requested=[], **ARGS)
    assert "robinhood" in p["source_errors"]


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf"])
def test_non_positive_or_non_finite_quote_is_a_ticker_error(bad):
    p = build_payload(
        intraday=[bars5("QQQ", [bar("2026-10-13T16:30:00Z", 1, 1, 1, 1)])],
        daily=[day("QQQ", [bar("2026-10-12T04:00:00Z", 3, 5, 3, 4)])],
        quotes=[quote("QQQ", bad, "2026-10-13T17:48:41Z")], requested=["QQQ"], **ARGS)
    assert "QQQ" in p["ticker_errors"]["robinhood"]
