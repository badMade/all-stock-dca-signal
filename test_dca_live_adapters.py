"""Tests for the live Robinhood/finance adapters (dca_live_adapters)."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from dca_live_adapters import (
    FinanceDataProvider,
    RobinhoodBroker,
    RobinhoodDataProvider,
    _interpret_review,
    _parse_rfc3339,
    _rh_interval,
    _to_float,
)
from near_close_dca import (
    EASTERN,
    BrokerName,
    DryRunBroker,
    OrderFill,
    StrategyConfig,
    build_broker,
)

# Real MCP response shapes captured from the Robinhood tools.
HISTORICALS = {
    "data": {
        "results": [
            {
                "symbol": "AAPL",
                "interval": "30minute",
                "bounds": "regular",
                "bars": [
                    {"begins_at": "2026-07-02T13:30:00Z", "open_price": "294.05", "close_price": "302.81", "high_price": "302.81", "low_price": "293.68", "volume": 4184502, "session": "reg"},
                    {"begins_at": "2026-07-02T14:00:00Z", "open_price": "302.85", "close_price": "305.05", "high_price": "305.32", "low_price": "302.61", "volume": 3483065, "session": "reg", "interpolated": True},
                    {"begins_at": "2026-07-02T19:30:00Z", "open_price": "307.44", "close_price": "308.24", "high_price": "309.42", "low_price": "307.21", "volume": 5151032, "session": "reg"},
                ],
            }
        ]
    }
}

QUOTES = {
    "data": {
        "results": [
            {
                "quote": {"symbol": "AAPL", "last_trade_price": "313.330100", "venue_last_trade_time": "2026-07-06T18:31:29.201412148Z", "previous_close": "308.63", "previous_close_date": "2026-07-02", "has_traded": True, "state": "active"},
                "close": {"symbol": "AAPL", "date": "2026-07-02", "price": "308.63"},
            }
        ]
    }
}


def _decision_dt() -> datetime:
    return datetime.combine(datetime(2026, 7, 2).date(), time(15, 45), tzinfo=EASTERN)


# -- helpers ---------------------------------------------------------------------


def test_parse_rfc3339_handles_nanoseconds_and_z():
    parsed = _parse_rfc3339("2026-07-06T18:31:29.201412148Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.microsecond == 201412
    assert parsed.utcoffset().total_seconds() == 0


def test_parse_rfc3339_bad_values_return_none():
    assert _parse_rfc3339(None) is None
    assert _parse_rfc3339("not-a-date") is None
    assert _parse_rfc3339(12345) is None


def test_to_float_defaults():
    assert _to_float("3.14") == pytest.approx(3.14)
    assert _to_float(None, default=-1.0) == -1.0
    assert _to_float("garbage", default=0.0) == 0.0


def test_rh_interval_mapping_and_rejection():
    assert _rh_interval(5) == "5minute"
    assert _rh_interval(30) == "30minute"
    with pytest.raises(ValueError):
        _rh_interval(15)


# -- RobinhoodDataProvider -------------------------------------------------------


def test_provider_parses_bars_and_skips_interpolated():
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: QUOTES)
    start = datetime.combine(datetime(2026, 7, 2).date(), time(9, 30), tzinfo=EASTERN)
    bars = prov.get_intraday_bars("AAPL", start, _decision_dt(), 30)
    assert len(bars) == 2  # interpolated bar dropped
    assert bars[0].close == pytest.approx(302.81)
    assert bars[-1].close == pytest.approx(308.24)


def test_provider_previous_close_from_quote():
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: QUOTES)
    bar = prov.get_previous_close_bar("AAPL", _decision_dt())
    assert bar is not None
    assert bar.close == pytest.approx(308.63)
    assert bar.timestamp.astimezone(EASTERN).hour == 16


def test_provider_quote_returns_price_and_time():
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: QUOTES)
    quote = prov.get_quote("AAPL", _decision_dt())
    assert quote is not None
    price, when = quote
    assert price == pytest.approx(313.3301)
    assert when.year == 2026


def test_provider_quote_none_without_quotes_fn():
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS)
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_provider_skips_non_active_quote():
    halted = {"data": {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "1", "state": "halted", "has_traded": True}, "close": {}}]}}
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: halted)
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_provider_previous_close_fallback_to_daily_bars():
    daily = {"data": {"results": [{"symbol": "AAPL", "bars": [
        {"begins_at": "2026-06-30T13:30:00Z", "open_price": "300", "close_price": "301", "high_price": "302", "low_price": "299", "volume": 1},
        {"begins_at": "2026-07-01T13:30:00Z", "open_price": "305", "close_price": "306", "high_price": "307", "low_price": "304", "volume": 1},
    ]}]}}
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: daily)  # no quotes_fn -> fallback
    bar = prov.get_previous_close_bar("AAPL", _decision_dt())
    assert bar is not None
    assert bar.close == pytest.approx(306.0)  # last day strictly before 2026-07-02


# -- FinanceDataProvider ---------------------------------------------------------


def test_finance_provider_coerces_various_key_names():
    raw = [
        {"timestamp": "2026-07-02T13:30:00Z", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
        {"t": "2026-07-02T13:45:00Z", "o": 10.5, "h": 11, "low": 10, "c": 10.2, "v": 50},
    ]
    prov = FinanceDataProvider(bars_fn=lambda *a: raw, previous_close_fn=lambda *a: 100.0)
    bars = prov.get_intraday_bars("AAPL", _decision_dt(), _decision_dt(), 15)
    assert len(bars) == 2
    assert bars[0].close == pytest.approx(10.5)
    assert bars[1].close == pytest.approx(10.2)


def test_finance_provider_previous_close_scalar():
    prov = FinanceDataProvider(bars_fn=lambda *a: [], previous_close_fn=lambda *a: 42.0)
    bar = prov.get_previous_close_bar("AAPL", _decision_dt())
    assert bar is not None and bar.close == pytest.approx(42.0)


def test_finance_provider_coerces_date_objects():
    # yfinance/pandas often yield a bare datetime.date; it must not be dropped.
    from datetime import date as _date

    raw = [{"date": _date(2026, 7, 2), "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100}]
    prov = FinanceDataProvider(bars_fn=lambda *a: raw, previous_close_fn=lambda *a: 100.0)
    bars = prov.get_intraday_bars("AAPL", _decision_dt(), _decision_dt(), 15)
    assert len(bars) == 1
    assert bars[0].close == pytest.approx(10.5)
    assert bars[0].timestamp.astimezone(EASTERN).hour == 16


# -- RobinhoodBroker -------------------------------------------------------------


def test_broker_place_is_dry_run_without_live_gate():
    calls = []
    broker = RobinhoodBroker(account="562057042", place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "X"}}})
    order_id = broker.place_market_buy("AAPL", 1.0, "ref1")
    assert order_id.startswith("RH-DRYRUN-")
    assert calls == []  # place_fn never invoked while disabled


def test_broker_place_live_when_gated_on():
    broker = RobinhoodBroker(
        account="562057042",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-42"}}},
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "ORD-42"


def test_broker_place_live_raises_on_missing_order_id():
    # A live placement that returns no id must fail fast, not report false success —
    # otherwise run_once books the notional against budget with no order to reconcile.
    broker = RobinhoodBroker(
        account="562057042",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"status": "rejected"}}},
    )
    with pytest.raises(RuntimeError, match="no order id"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_review_fails_closed_when_live_without_review_fn():
    # enable_live_orders + no review_fn must refuse, so a real order is never placed unreviewed.
    live = RobinhoodBroker(enable_live_orders=True)
    ok, reason = live.review_market_buy("AAPL", 1.0)
    assert ok is False and "no review callable" in reason
    # Dry-run without a review_fn stays permissive.
    assert RobinhoodBroker(enable_live_orders=False).review_market_buy("AAPL", 1.0)[0] is True


def test_broker_review_blocks_on_error_alert():
    resp = {"data": {"alerts": [{"severity": "error", "message": "insufficient buying power"}]}}
    broker = RobinhoodBroker(review_fn=lambda **k: resp)
    ok, reason = broker.review_market_buy("AAPL", 1.0)
    assert ok is False and "insufficient" in reason


def test_broker_review_passes_on_warning_only():
    resp = {"data": {"alerts": [{"severity": "warning", "message": "wide spread"}]}}
    broker = RobinhoodBroker(review_fn=lambda **k: resp)
    ok, _ = broker.review_market_buy("AAPL", 1.0)
    assert ok is True


def test_broker_review_permissive_without_callable():
    broker = RobinhoodBroker()
    ok, _ = broker.review_market_buy("AAPL", 1.0)
    assert ok is True


def test_broker_tradability_parsing():
    tradable = {"data": {"results": [{"symbol": "AAPL", "tradeable": True, "state": "active", "fractional_tradability": "tradable"}]}}
    untradable = {"data": {"results": [{"symbol": "AAPL", "tradeable": False, "state": "active", "fractional_tradability": "tradable"}]}}
    assert RobinhoodBroker(tradability_fn=lambda **k: tradable).is_tradable_for_fractional_buy("AAPL") is True
    assert RobinhoodBroker(tradability_fn=lambda **k: untradable).is_tradable_for_fractional_buy("AAPL") is False


def test_broker_same_day_order_detection():
    live = {"data": {"orders": [{"state": "filled"}]}}
    cancelled = {"data": {"orders": [{"state": "cancelled"}]}}
    day = datetime(2026, 7, 6).date()
    assert RobinhoodBroker(orders_fn=lambda **k: live).same_day_strategy_order_exists("AAPL", day) is True
    assert RobinhoodBroker(orders_fn=lambda **k: cancelled).same_day_strategy_order_exists("AAPL", day) is False
    assert RobinhoodBroker().same_day_strategy_order_exists("AAPL", day) is False


def test_broker_buying_power_keyword_call_and_failure():
    assert RobinhoodBroker(simulated_buying_power_usd=250.0).available_buying_power() == 250.0

    # The wrapper is invoked by keyword (account_number=...), consistent with the other
    # Robinhood callables — a keyword-only wrapper must work, not raise.
    seen = {}

    def kw_only(*, account_number):
        seen["acct"] = account_number
        return 137.0

    assert RobinhoodBroker(account="562057042", buying_power_fn=kw_only).available_buying_power() == 137.0
    assert seen["acct"] == "562057042"

    def boom(*, account_number):
        raise RuntimeError("network down")

    assert RobinhoodBroker(buying_power_fn=boom).available_buying_power() == 0.0


def test_broker_order_fill_parsing():
    resp = {"data": {"orders": [{"id": "ORD-1", "cumulative_quantity": "0.5", "average_price": "100.00", "created_at": "2026-07-06T19:30:00Z", "last_transaction_at": "2026-07-06T19:30:05Z"}]}}
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-1")
    assert isinstance(fill, OrderFill)
    assert fill.avg_fill_price == pytest.approx(100.0)
    assert fill.quantity_filled == pytest.approx(0.5)


def test_interpret_review_empty_and_state_rejection():
    assert _interpret_review({})[0] is True
    ok, reason = _interpret_review({"data": {"state": "rejected"}})
    assert ok is False and "rejected" in reason


# -- build_broker routing --------------------------------------------------------


def test_build_broker_dry_run_without_callables():
    broker = build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD))
    assert isinstance(broker, DryRunBroker)


def test_build_broker_robinhood_when_wired():
    broker = build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD), review_fn=lambda **k: {})
    assert isinstance(broker, RobinhoodBroker)
    assert broker.account_number() == StrategyConfig().target_account_number
    assert broker.enable_live_orders is False
