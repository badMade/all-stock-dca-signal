"""Courier source response normalization and freshness checks."""

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from dca_market_sources import AgentFetchSource, SourceUnavailable, _quote_sides, _raw_response

DAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def test_raw_mcp_response_shapes_and_refusals():
    assert _raw_response({"structuredContent": {"ok": 1}, "content": []}) == {"ok": 1}
    assert _raw_response({"content": [{"type": "text", "text": '{"ok":2}'}]}) == {"ok": 2}
    for value in (
        None, [], {"structuredContent": []}, {"content": [{"type": "image"}]},
        {"content": [{"type": "text", "text": "not json"}]},
        {"content": [{"type": "text", "text": "[]"}]},
        {"content": [{"type": "text", "text": '{"x":1,"x":2}'}]},
        {"content": [{"type": "text", "text": '{"x":NaN}'}]},
    ):
        with pytest.raises(ValueError):
            _raw_response(value)


def test_agent_fetch_validates_age_shape_and_data(monkeypatch, tmp_path):
    import dca_market_sources

    path = tmp_path / "raw.json"
    for age in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            AgentFetchSource(path, max_age=age)
    with pytest.raises(SourceUnavailable):
        AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    path.write_text("[]", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    with pytest.raises(SourceUnavailable, match="shape"):
        AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    for raw in (
        '{"historicals": [], "historicals": [], "quotes": []}',
        '{"historicals": [], "quotes": [], "extra": 1}',
        '{"historicals": {}, "quotes": []}',
        '{"historicals": [], "quotes": [], "daily": {}}',
        '{"historicals": [], "quotes": [], "x": NaN}',
    ):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(SourceUnavailable):
            AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    path.write_text('{"historicals": [], "quotes": []}', encoding="utf-8")
    os.utime(path, (NOW.timestamp() - 901, NOW.timestamp() - 901))
    with pytest.raises(SourceUnavailable, match="stale"):
        AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    os.utime(path, (NOW.timestamp() + 1, NOW.timestamp() + 1))
    with pytest.raises(SourceUnavailable, match="stale"):
        AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    monkeypatch.setattr(dca_market_sources.assemble_market_data, "assemble", lambda *args: {
        "sources": {}, "source_errors": [], "ticker_errors": {"robinhood": ["SPY"]}})
    with pytest.raises(SourceUnavailable, match="no usable"):
        AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    with pytest.raises(ValueError, match="timezone-aware"):
        AgentFetchSource(path).fetch(DAY, datetime(2026, 10, 13), ["SPY"])


def test_agent_fetch_assembles_and_marks_market_source(monkeypatch, tmp_path):
    import dca_market_sources

    path = tmp_path / "raw.json"
    path.write_text(json.dumps({"historicals": [], "quotes": [], "daily": []}), encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    monkeypatch.setattr(dca_market_sources.assemble_market_data, "assemble", lambda raw, day, universe: {
        "sources": {"robinhood": {}}, "source_errors": [], "ticker_errors": {"robinhood": []}})
    snapshot = AgentFetchSource(path).fetch(DAY, NOW, ["SPY"])
    assert snapshot.source == "robinhood_agent_fetch"


def test_quote_sides_accepts_aliases_and_rejects_bad_values():
    assert _quote_sides([{"results": [
        {"symbol": "spy", "quote": {"bid_price": "1", "ask_price": "2"}},
        {"symbol": "QQQ", "best_bid": 3, "best_ask": 4},
        {"symbol": "BAD", "bid_price": "no", "ask_price": 1},
        {"symbol": "NAN", "bid_price": "nan", "ask_price": 1},
        {"symbol": "CROSS", "bid_price": 2, "ask_price": 1},
        {"symbol": "ZERO", "bid_price": 0, "ask_price": 1},
        {"symbol": "BOOL", "bid_price": True, "ask_price": 1},
        None,
    ]}]) == {"SPY": [1.0, 2.0], "QQQ": [3.0, 4.0], "BAD": None, "NAN": None,
            "CROSS": None, "ZERO": None, "BOOL": None}
