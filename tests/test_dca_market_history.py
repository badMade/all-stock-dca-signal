"""Hermetic tests for the Claude courier's raw-file contract."""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dca_market_history import MarketHistoryStore
from dca_market_sources import SourceUnavailable

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 15, 30, tzinfo=ET)
ACCOUNT = "acct-test-xyz"


def _mcp_text(body):
    return {"content": [{"type": "text", "text": json.dumps(body)}]}


def _structured(body):
    return {"structuredContent": body}


def _historical(close="98.0"):
    return {"results": [{"symbol": "SPY", "interval": "5minute", "bounds": "regular", "bars": [
        {"begins_at": "2026-10-07T15:20:00Z", "open_price": "99", "high_price": "100", "low_price": "98",
         "close_price": close, "volume": 10, "session": "reg", "interpolated": False},
        {"begins_at": "2026-10-07T15:25:00Z", "open_price": "99", "high_price": "100", "low_price": "98",
         "close_price": "99", "volume": 10, "session": "reg", "interpolated": False},
    ]}]}


def _quote(price="99"):
    return {"results": [{"quote": {"symbol": "SPY", "last_trade_price": price, "previous_close": "100",
                                    "previous_close_date": "2026-10-06",
                                    "venue_last_trade_time": "2026-10-07T19:29:00Z",
                                    "bid_price": "98.99", "ask_price": "99.01"},
                         "close": {"price": "100", "date": "2026-10-06"}}]}


def _daily():
    return {"results": [{"symbol": "SPY", "interval": "day", "bars": [
        {"begins_at": "2026-10-06T20:00:00Z", "open_price": "100", "high_price": "101", "low_price": "99",
         "close_price": "100", "volume": 100, "session": "reg", "interpolated": False},
    ]}]}


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


def _run(store, name, *, bar=None, quote=None, daily=None, orders=None, portfolio=None, wrappers=False):
    run_dir = store.raw_dir / "incoming" / name
    _write(run_dir / "bars5m_0.json", (_mcp_text if wrappers else lambda value: value)(bar or _historical()))
    _write(run_dir / "quotes_0.json", (_structured if wrappers else lambda value: value)(quote or _quote()))
    if daily is not None:
        _write(run_dir / "daily_0.json", (_mcp_text if wrappers else lambda value: value)(daily))
    _write(run_dir / "orders_0.json", orders or {"orders": []})
    _write(run_dir / "portfolio.json", portfolio or {"buying_power": "25.50"})
    return run_dir


def test_ingest_merges_replaces_bars_keeps_first_daily_and_current_quotes(tmp_path):
    store = MarketHistoryStore(tmp_path / DAY.isoformat())
    first = _run(store, "15h00m00s", daily=_daily(), wrappers=True)
    snapshot1, broker1 = store.ingest_run(first, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)
    assert snapshot1.source == "robinhood_agent_fetch"
    assert broker1.buying_power_usd == 25.5
    assert len(json.loads((store.raw_dir / "day_bars.json").read_text())[0]["results"][0]["bars"]) == 2

    replacement = _historical("97.5")
    replacement["results"][0]["bars"] = replacement["results"][0]["bars"][:1]
    second = _run(store, "15h05m00s", bar=replacement, quote=_quote("98.5"), daily={"results": []})
    snapshot2, _ = store.ingest_run(second, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)
    saved = json.loads((store.raw_dir / "day_bars.json").read_text())[0]["results"][0]["bars"]
    assert len(saved) == 2
    assert saved[0]["close_price"] == "97.5"
    assert len(json.loads((store.raw_dir / "daily.json").read_text())) == 1
    assert snapshot2.quote_sides["SPY"] == [98.99, 99.01]
    assert snapshot2.payload["sources"]["robinhood"]["SPY"]["quote"]["price"] == 98.5
    assert ACCOUNT not in " ".join(path.read_text(errors="ignore") for path in store.raw_dir.rglob("*.*"))


def test_ingest_accepts_raw_and_rejects_all_error_marked_universe(tmp_path, monkeypatch):
    import dca_market_history

    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s")
    monkeypatch.setattr(dca_market_history.assemble_market_data, "assemble", lambda *_: {
        "sources": {}, "source_errors": [], "ticker_errors": {"robinhood": ["SPY"]}})
    with pytest.raises(SourceUnavailable, match="no usable"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)


@pytest.mark.parametrize("bad", ['{"results":[],"results":[]}', '{"results":[NaN]}'])
def test_duplicate_keys_and_non_finite_raw_files_are_refused(tmp_path, bad):
    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s")
    _write(run / "quotes_0.json", bad)
    with pytest.raises(SourceUnavailable, match="invalid"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)


def test_multiple_text_blocks_are_refused(tmp_path):
    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s")
    _write(run / "quotes_0.json", {"content": [
        {"type": "text", "text": json.dumps(_quote())},
        {"type": "text", "text": json.dumps(_quote())},
    ]})
    with pytest.raises(SourceUnavailable, match="invalid"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)


def test_incomplete_order_page_chain_refuses_and_buying_power_can_be_unknown(tmp_path):
    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s", orders={"orders": [], "next": "cursor"})
    with pytest.raises(SourceUnavailable, match="order history incomplete"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)
    run = _run(store, "15h05m00s", portfolio={"data": {"cash": "bad"}}, orders={"orders": []})
    _snapshot, broker = store.ingest_run(run, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)
    assert broker.buying_power_usd is None


@pytest.mark.parametrize("portfolio,expected", [
    ({"buying_power": "12.75"}, 12.75), ({"data": {"cash": 4}}, 4.0), ({}, None),
    ({"buying_power": True}, None), ({"buying_power": "bad"}, None), ({"buying_power": float("inf")}, None),
    ({"success": False, "buying_power": "12"}, None),
])
def test_buying_power_aliases_and_invalid_values(tmp_path, portfolio, expected):
    store = MarketHistoryStore(tmp_path / "day")
    assert store._buying_power(portfolio) == expected


def test_fetch_plan_chunks_calls_and_advances_start_time(tmp_path):
    store = MarketHistoryStore(tmp_path / DAY.isoformat())
    symbols = [f"T{index}" for index in range(60)]
    plan = store.fetch_plan(trading_day=DAY, now=NOW, universe=symbols, account_number=ACCOUNT)
    quotes = [call for call in plan["calls"] if call["tool"] == "get_equity_quotes"]
    hist = [call for call in plan["calls"] if call["tool"] == "get_equity_historicals" and call["args"]["interval"] == "5minute"]
    assert [len(call["args"]["symbols"]) for call in quotes] == [25, 25, 10]
    assert all(call["args"]["start_time"] == "2026-10-07T09:30:00-04:00" for call in hist)
    assert any(call["save_as"].startswith("daily_") for call in plan["calls"])
    _write(store.raw_dir / "daily.json", [])
    _write(store.raw_dir / "day_bars.json", [{"results": [{"symbol": "SPY", "bars": [
        {"begins_at": "2026-10-07T15:00:00Z"},
    ]}]}, {"results": [{"symbol": "QQQ", "bars": [
        {"begins_at": "2026-10-07T12:00:00-05:00"},
    ]}]}])
    next_plan = store.fetch_plan(trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)
    bars_call = next(call for call in next_plan["calls"] if call["save_as"] == "bars5m_0.json")
    assert bars_call["args"]["start_time"] == "2026-10-07T12:50:00-04:00"
    assert not any(call["save_as"].startswith("daily_") for call in next_plan["calls"])
    orders = next(call for call in next_plan["calls"] if call["tool"] == "get_equity_orders")
    # Wednesday 2026-10-07: seven days back (Wed 09-30) precedes Monday 10-05, so the pull covers the
    # kill switch's trailing seven days as well as the ISO week (PR #1667 review).
    assert orders["paginate"] is True and orders["args"]["created_at_gte"] == "2026-09-30T00:00:00-04:00"


@pytest.mark.parametrize("day,expected", [
    ("2026-10-05", "2026-09-28"),  # Monday: seven days back is the previous Monday
    ("2026-10-07", "2026-09-30"),  # Wednesday: seven days back precedes this Monday
    ("2026-10-09", "2026-10-02"),  # Friday
])
def test_orders_window_start_covers_week_and_trailing_seven_days(day, expected):
    from datetime import date as _date

    from dca_market_history import orders_window_start
    start = orders_window_start(_date.fromisoformat(day))
    assert start.date().isoformat() == expected and start.hour == 0


def test_fetch_plan_and_ingest_reject_bad_arguments_and_missing_files(tmp_path):
    store = MarketHistoryStore(tmp_path / "day")
    with pytest.raises(ValueError, match="timezone-aware"):
        store.fetch_plan(trading_day=DAY, now=datetime(2026, 10, 7), universe=[], account_number=ACCOUNT)
    with pytest.raises(ValueError, match="account_number"):
        store.fetch_plan(trading_day=DAY, now=NOW, universe=[], account_number=" ")
    run = store.raw_dir / "incoming" / "empty"
    with pytest.raises(SourceUnavailable, match="incomplete"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.ingest_run(run, trading_day=DAY, now=datetime(2026, 10, 7), universe=[], account_number=ACCOUNT)


@pytest.mark.parametrize("saved", ["not json", "{}", "[{}]"])
def test_corrupt_persisted_day_bars_fail_closed(tmp_path, saved):
    store = MarketHistoryStore(tmp_path / "day")
    _write(store.raw_dir / "day_bars.json", saved)
    with pytest.raises(SourceUnavailable, match="stored market history"):
        store.fetch_plan(trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)


def test_invalid_bar_runs_daily_store_and_response_sequences_fail_closed(tmp_path):
    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s")
    _write(run / "bars5m_0.json", {"results": {}})
    with pytest.raises(SourceUnavailable, match="market history"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)

    run = _run(store, "15h05m00s")
    _write(run / "bars5m_0.json", {"results": [{"symbol": "SPY", "bars": [None]}]})
    with pytest.raises(SourceUnavailable, match="market history"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)

    run = _run(store, "15h10m00s")
    (run / "quotes_0.json").unlink()
    _write(run / "quotes_1.json", _quote())
    with pytest.raises(SourceUnavailable, match="sequence"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)


def test_optional_and_corrupt_daily_and_missing_portfolio(tmp_path):
    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s")
    assert store._daily(run) == []
    _write(run / "daily_0.json", _daily())
    first = store._daily(run)
    assert len(first) == 1
    _write(run / "daily_1.json", _daily())
    assert store._daily(run) == first
    _write(store.raw_dir / "daily.json", "bad")
    with pytest.raises(SourceUnavailable, match="daily history"):
        store._daily(run)

    store = MarketHistoryStore(tmp_path / "other-day")
    run = _run(store, "15h00m00s")
    (run / "portfolio.json").unlink()
    _, broker = store.ingest_run(run, trading_day=DAY, now=NOW, universe=["SPY"], account_number=ACCOUNT)
    assert broker.buying_power_usd is None


def test_index_gaps_and_invalid_latest_bar_are_refused(tmp_path):
    store = MarketHistoryStore(tmp_path / "day")
    run = _run(store, "15h00m00s")
    _write(run / "orders_bad.json", {"orders": []})
    with pytest.raises(SourceUnavailable, match="sequence"):
        store.ingest_run(run, trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)
    _write(store.raw_dir / "day_bars.json", [{"results": [{"symbol": "SPY", "bars": [
        {"begins_at": "bad-stamp"},
    ]}]}])
    with pytest.raises(SourceUnavailable, match="stored market history"):
        store.fetch_plan(trading_day=DAY, now=NOW, universe=[], account_number=ACCOUNT)
