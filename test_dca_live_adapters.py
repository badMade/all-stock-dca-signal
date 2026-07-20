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


def test_parse_rfc3339_unparseable_timestamp_string_returns_none():
    assert _parse_rfc3339("2026-13-01T25:00:00Z") is None


def test_parse_rfc3339_naive_timestamp_gets_eastern_tz():
    parsed = _parse_rfc3339("2026-07-06T18:31:29")
    assert parsed is not None
    assert parsed.tzinfo == EASTERN


def test_to_float_defaults():
    assert _to_float("3.14") == pytest.approx(3.14)
    assert _to_float(None, default=-1.0) == -1.0
    assert _to_float("garbage", default=0.0) == 0.0


def test_to_float_rejects_non_finite():
    # NaN/inf must collapse to the default so they can't bypass a numeric gate.
    assert _to_float("NaN", default=0.0) == 0.0
    assert _to_float("inf", default=0.0) == 0.0
    assert _to_float(float("nan"), default=-1.0) == -1.0


def test_rh_interval_mapping_and_rejection():
    assert _rh_interval(5) == "5minute"
    assert _rh_interval(30) == "30minute"
    with pytest.raises(ValueError) as exc_info:
        _rh_interval(15)

    assert "Robinhood has no 15-minute bar; supported minutes:" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, KeyError)


# -- RobinhoodDataProvider -------------------------------------------------------


def test_provider_parses_bars_and_skips_interpolated():
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: QUOTES)
    start = datetime.combine(datetime(2026, 7, 2).date(), time(9, 30), tzinfo=EASTERN)
    bars = prov.get_intraday_bars("AAPL", start, _decision_dt(), 30)
    assert len(bars) == 2  # interpolated bar dropped
    assert bars[0].close == pytest.approx(302.81)
    assert bars[-1].close == pytest.approx(308.24)


def test_provider_drops_bars_with_invalid_close():
    # A missing/non-numeric close would become 0.0 and read as a ~100% drop -> spurious BUY.
    bad = {"data": {"results": [{"symbol": "AAPL", "bars": [
        {"begins_at": "2026-07-02T13:30:00Z", "open_price": "300", "close_price": "301", "high_price": "302", "low_price": "299", "volume": 1},
        {"begins_at": "2026-07-02T14:00:00Z", "open_price": "300", "close_price": None, "high_price": "302", "low_price": "299", "volume": 1},
        {"begins_at": "2026-07-02T14:30:00Z", "open_price": "300", "close_price": "0", "high_price": "302", "low_price": "299", "volume": 1},
    ]}]}}
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: bad)
    start = datetime.combine(datetime(2026, 7, 2).date(), time(9, 30), tzinfo=EASTERN)
    bars = prov.get_intraday_bars("AAPL", start, _decision_dt(), 30)
    assert len(bars) == 1  # only the valid bar survives
    assert bars[0].close == pytest.approx(301.0)


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


def test_provider_bid_ask_returns_spread_inputs():
    quotes = {
        "data": {
            "results": [
                {
                    "quote": {
                        "symbol": "AAPL",
                        "last_trade_price": "313.330100",
                        "bid_price": "313.10",
                        "ask_price": "313.40",
                        "has_traded": True,
                        "state": "active",
                    }
                }
            ]
        }
    }
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: quotes)
    assert prov.get_bid_ask("AAPL", _decision_dt()) == pytest.approx((313.10, 313.40))


def test_provider_bid_ask_raises_on_missing_fields():
    # QUOTES fixture has no bid_price/ask_price — fail closed rather than silently bypassing.
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: QUOTES)
    with pytest.raises(ValueError, match="no usable bid/ask"):
        prov.get_bid_ask("AAPL", _decision_dt())


def test_provider_bid_ask_raises_on_inactive_quote():
    halted = {"data": {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "1", "state": "halted", "has_traded": True}, "close": {}}]}}
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: halted)
    with pytest.raises(ValueError, match="inactive quote"):
        prov.get_bid_ask("AAPL", _decision_dt())


def test_provider_bid_ask_reads_top_level_mcp_sibling_fields():
    # PR #48 / Codex review: some Robinhood MCP payloads expose bid/ask as
    # siblings of the nested "quote" object rather than inside it. The
    # nested quote here has no bid/ask at all, so the only usable spread
    # data is on the top-level result.
    quotes = {
        "data": {
            "results": [
                {
                    "quote": {
                        "symbol": "AAPL",
                        "last_trade_price": "313.330100",
                        "has_traded": True,
                        "state": "active",
                    },
                    "bid_price": "313.10",
                    "ask_price": "313.40",
                }
            ]
        }
    }
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: quotes)
    assert prov.get_bid_ask("AAPL", _decision_dt()) == pytest.approx((313.10, 313.40))


def test_provider_bid_ask_prefers_sibling_over_empty_nested_placeholder():
    # Codex review on PR #48: when the nested quote object carries an empty
    # or zero bid/ask placeholder alongside a usable top-level sibling, the
    # placeholder must not win just because it is nested. The genuinely
    # positive value (wherever it lives) should be used.
    quotes = {
        "data": {
            "results": [
                {
                    "quote": {
                        "symbol": "AAPL",
                        "last_trade_price": "313.330100",
                        "bid_price": "0",
                        "ask_price": "",
                        "has_traded": True,
                        "state": "active",
                    },
                    "bid_price": "313.10",
                    "ask_price": "313.40",
                }
            ]
        }
    }
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: quotes)
    assert prov.get_bid_ask("AAPL", _decision_dt()) == pytest.approx((313.10, 313.40))


def test_provider_bid_ask_recognizes_alternate_key_names():
    # Codex/Copilot review on PR #48: some MCP shapes name the fields
    # best_bid/best_ask instead of bid_price/ask_price.
    quotes = {
        "data": {
            "results": [
                {
                    "quote": {
                        "symbol": "AAPL",
                        "last_trade_price": "313.330100",
                        "best_bid": "313.10",
                        "best_ask": "313.40",
                        "has_traded": True,
                        "state": "active",
                    }
                }
            ]
        }
    }
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: quotes)
    assert prov.get_bid_ask("AAPL", _decision_dt()) == pytest.approx((313.10, 313.40))


def test_provider_bid_ask_raises_on_partial_nbbo_even_with_siblings():
    # Codex review on PR #48 explicitly warned against a trade-price fallback
    # that fires when only one side of the NBBO is present -- that would let
    # the spread breaker see a false 0% spread. We deliberately did not add
    # any last-trade-price fallback, so a one-sided quote (bid present, ask
    # missing everywhere) must still fail closed.
    quotes = {
        "data": {
            "results": [
                {
                    "quote": {
                        "symbol": "AAPL",
                        "last_trade_price": "313.330100",
                        "bid_price": "313.10",
                        "has_traded": True,
                        "state": "active",
                    }
                }
            ]
        }
    }
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: quotes)
    with pytest.raises(ValueError, match="no usable bid/ask"):
        prov.get_bid_ask("AAPL", _decision_dt())


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


def test_finance_provider_drops_nonnumeric_close():
    # A placeholder close ("N/A") must be dropped, not zeroed into a ~100%-drop bar.
    raw = [
        {"timestamp": "2026-07-02T13:30:00Z", "open": 10, "high": 11, "low": 9, "close": "N/A", "volume": 1},
        {"timestamp": "2026-07-02T13:45:00Z", "open": 10, "high": 11, "low": 9, "close": 10.2, "volume": 1},
    ]
    prov = FinanceDataProvider(bars_fn=lambda *a: raw, previous_close_fn=lambda *a: 100.0)
    bars = prov.get_intraday_bars("AAPL", _decision_dt(), _decision_dt(), 15)
    assert len(bars) == 1
    assert bars[0].close == pytest.approx(10.2)


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


def test_broker_place_raises_when_live_gate_disabled():
    # run_once only reaches a live broker's place_market_buy on the non-dry-run path, so a
    # disabled gate here is a misconfiguration that must fail loudly, not book a phantom buy.
    calls = []
    broker = RobinhoodBroker(account="562057042", place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "X"}}})
    with pytest.raises(RuntimeError, match="live gate disabled"):
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert calls == []  # place_fn never invoked while disabled


def test_broker_place_live_when_gated_on():
    broker = RobinhoodBroker(
        account="562057042",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-42"}}},
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "ORD-42"


def test_broker_place_raises_on_outer_failure_envelope():
    # A failure envelope carrying an inner order id must not be read as a successful submit.
    broker = RobinhoodBroker(
        account="562057042",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "data": {"order": {"id": "ORD-9"}}},
    )
    with pytest.raises(RuntimeError, match="failed"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_live_raises_on_terminal_state():
    # An id in a terminal-failure state means no live order exists -> must not report success.
    broker = RobinhoodBroker(
        account="562057042",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-7", "state": "rejected"}}},
    )
    with pytest.raises(RuntimeError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


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


def test_broker_same_day_fails_closed_on_error_response():
    # An error-shaped lookup must not read as "no order exists" and allow a duplicate buy.
    day = datetime(2026, 7, 6).date()
    err = {"data": {"error": "orders service unavailable"}}
    with pytest.raises(RuntimeError, match="same-day order lookup"):
        RobinhoodBroker(orders_fn=lambda **k: err).same_day_strategy_order_exists("AAPL", day)
    with pytest.raises(RuntimeError, match="same-day order lookup"):
        RobinhoodBroker(orders_fn=lambda **k: {"data": {"success": False}}).same_day_strategy_order_exists("AAPL", day)


def test_broker_order_fill_swallows_lookup_failure():
    # A transient fill-lookup outage after placement must not crash the run.
    def boom(**k):
        raise RuntimeError("get_equity_orders timeout")

    assert RobinhoodBroker(order_fill_fn=boom).get_order_fill("ORD-1") is None


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


def test_broker_order_fill_matches_order_id_key():
    # A history wrapper keyed by "order_id" (not "id") must still match the fill.
    resp = {"data": {"orders": [{"order_id": "ORD-9", "cumulative_quantity": "1", "average_price": "50.0"}]}}
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-9")
    assert isinstance(fill, OrderFill)
    assert fill.avg_fill_price == pytest.approx(50.0)


def test_broker_order_fill_nested_single_order_envelope():
    # A single-order lookup nested as {"data": {"order": {...}}} must be matched, not missed.
    resp = {"data": {"order": {"id": "ORD-5", "cumulative_quantity": "2", "average_price": "25.0"}}}
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-5")
    assert isinstance(fill, OrderFill)
    assert fill.quantity_filled == pytest.approx(2.0)
    assert fill.avg_fill_price == pytest.approx(25.0)


def test_broker_buying_power_parses_dict_response():
    # A raw MCP dict (per the module callable contract) must be unwrapped, not cast to 0.0.
    resp = {"data": {"buying_power": "137.00"}}
    assert RobinhoodBroker(buying_power_fn=lambda **k: resp).available_buying_power() == pytest.approx(137.0)
    # A dict with no recognizable amount field degrades to 0.0 (blocked), not a crash.
    assert RobinhoodBroker(buying_power_fn=lambda **k: {"data": {}}).available_buying_power() == 0.0


def test_interpret_review_empty_and_state_rejection():
    assert _interpret_review({})[0] is True
    ok, reason = _interpret_review({"data": {"state": "rejected"}})
    assert ok is False and "rejected" in reason


def test_interpret_review_fails_closed_on_error_shaped_dict():
    # A wrapper reporting failure as data (not an exception) must block, not fall through.
    assert _interpret_review({"data": {"error": "server exploded"}})[0] is False
    assert _interpret_review({"data": {"success": False}})[0] is False
    assert _interpret_review({"data": {"approved": False}})[0] is False


def test_interpret_review_fails_closed_on_outer_failure_envelope():
    # Failure flag at the outer envelope with inner data must still block.
    assert _interpret_review({"success": False, "data": {"quote": {"ask": "1.0"}}})[0] is False


def test_interpret_review_plain_warning_string_does_not_block():
    # A harmless string under "warnings" must not reject an otherwise valid candidate.
    ok, _ = _interpret_review({"data": {"warnings": ["wide spread"], "quote": {"ask": "1.00"}}})
    assert ok is True
    # But a string in an explicitly error-named container does block.
    assert _interpret_review({"data": {"errors": ["hard stop"]}})[0] is False


def test_finance_previous_close_price_only_mapping():
    # A price-only mapping (no bar timestamp) must not degrade to no-data.
    prov = FinanceDataProvider(bars_fn=lambda *a: [], previous_close_fn=lambda *a: {"price": "123.45"})
    bar = prov.get_previous_close_bar("AAPL", _decision_dt())
    assert bar is not None and bar.close == pytest.approx(123.45)


# -- build_broker routing --------------------------------------------------------


def test_build_broker_dry_run_without_callables():
    broker = build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD))
    assert isinstance(broker, DryRunBroker)


def test_build_broker_robinhood_when_wired():
    broker = build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD), review_fn=lambda **k: {})
    assert isinstance(broker, RobinhoodBroker)
    assert broker.account_number() == StrategyConfig().target_account_number
    assert broker.enable_live_orders is False


def test_build_broker_refuses_simulator_on_live_run():
    # dry_run=False without live callables must not silently hand back the DryRunBroker.
    with pytest.raises(RuntimeError, match="dry_run is False"):
        build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD, dry_run=False))
    # dry_run=True is fine (returns the simulator).
    assert isinstance(build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD, dry_run=True)), DryRunBroker)


def test_build_broker_refuses_live_run_with_unset_account_sentinel():
    # gemini-code-assist / chatgpt-codex-connector review on PR #54: run_once's
    # `broker.account_number() != config.target_account_number` safety check is a
    # tautology -- build_broker always constructs the broker with
    # `account=config.target_account_number`, so both sides of that comparison are
    # the same value by construction and the check can never fail. If the
    # ROBINHOOD_AGENTIC_ACCOUNT_NUMBER env var is unset, config.target_account_number
    # silently defaults to the "UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" placeholder,
    # and a live (dry_run=False) run would build a real RobinhoodBroker against that
    # placeholder instead of refusing to trade. build_broker must fail closed before
    # constructing any live broker when the account number is unset or the sentinel.
    with pytest.raises(RuntimeError, match="target_account_number"):
        build_broker(
            StrategyConfig(broker=BrokerName.ROBINHOOD, dry_run=False, target_account_number=""),
            review_fn=lambda **k: {},
        )
    with pytest.raises(RuntimeError, match="target_account_number"):
        build_broker(
            StrategyConfig(
                broker=BrokerName.ROBINHOOD,
                dry_run=False,
                target_account_number="UNSET_ROBINHOOD_AGENTIC_ACCOUNT_NUMBER",
            ),
            review_fn=lambda **k: {},
        )
    # Same guard applies to the ALPACA branch, which also skips the dry_run gate.
    with pytest.raises(RuntimeError, match="target_account_number"):
        build_broker(StrategyConfig(broker=BrokerName.ALPACA, dry_run=False, target_account_number=""))
    # A dry run with an unset account number is fine (no real order can be placed).
    assert isinstance(
        build_broker(StrategyConfig(broker=BrokerName.ROBINHOOD, dry_run=True, target_account_number="")),
        DryRunBroker,
    )
