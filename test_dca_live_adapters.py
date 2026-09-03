"""Tests for the live Robinhood/finance adapters (dca_live_adapters)."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from dca_live_adapters import (
    FinanceDataProvider,
    RobinhoodBroker,
    RobinhoodDataProvider,
    _interpret_review,
    _is_error_response,
    _parse_rfc3339,
    _response_failure,
    _rh_interval,
    _to_float,
    _unwrap,
)
from near_close_dca import (
    EASTERN,
    AlpacaBroker,
    BrokerAdapter,
    BrokerName,
    DryRunBroker,
    OrderFill,
    OrderPlacementAmbiguousError,
    StrategyConfig,
    WholeSharePlacementTerminalError,
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
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_parse_rfc3339_bad_values_return_none():
    assert _parse_rfc3339(None) is None
    assert _parse_rfc3339("not-a-date") is None
    assert _parse_rfc3339("2023-13-45T99:99:99Z") is None
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


@pytest.mark.parametrize("interval, expected", [
    (1, "minute"),
    (5, "5minute"),
    (10, "10minute"),
    (30, "30minute"),
    (60, "hour")
])
def test_rh_interval_mapping_supported(interval, expected):
    assert _rh_interval(interval) == expected


@pytest.mark.parametrize("interval", [2, 15, 45, 120])
def test_rh_interval_rejection_unsupported(interval):
    with pytest.raises(ValueError) as exc_info:
        _rh_interval(interval)

    assert f"Robinhood has no {interval}-minute bar; supported minutes:" in str(exc_info.value)
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
    # A quote-derived bar has no real prior-session low -- it must NOT fabricate
    # low=close, or select_first_allocation_base_usd would treat every below-close
    # price as also below the low and always pick the $2 base (Codex review:
    # "Obtain the prior-session low before selecting the base").
    assert bar.low == 0.0


def test_provider_quote_returns_price_and_time():
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=lambda **k: QUOTES)
    quote = prov.get_quote("AAPL", _decision_dt())
    assert quote is not None
    price, when = quote
    assert price == pytest.approx(313.3301)
    assert when is not None
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


# -- signal-time quote dedupe (⚡ Bolt) ------------------------------------------
# One quote payload carrying everything the three consumers read: previous-close
# (get_previous_close_bar), last trade (get_quote), and a two-sided NBBO (get_bid_ask).
QUOTES_WITH_SPREAD = {
    "data": {
        "results": [
            {
                "quote": {
                    "symbol": "AAPL",
                    "last_trade_price": "313.330100",
                    "venue_last_trade_time": "2026-07-06T18:31:29Z",
                    "previous_close": "308.63",
                    "previous_close_date": "2026-07-02",
                    "bid_price": "313.10",
                    "ask_price": "313.40",
                    "has_traded": True,
                    "state": "active",
                },
                "close": {"symbol": "AAPL", "date": "2026-07-02", "price": "308.63"},
            }
        ]
    }
}


class _CountingQuotesFn:
    """Test spy that records each batch of requested symbols to assert the
    number of underlying quote fetches (each is a live MCP/network round-trip)."""

    def __init__(self, payload=QUOTES_WITH_SPREAD):
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, symbols=None, **_kw):
        self.calls.append(tuple(symbols or []))
        return self.payload


def _counting_quotes_fn(payload=QUOTES_WITH_SPREAD) -> _CountingQuotesFn:
    return _CountingQuotesFn(payload)


def test_signal_time_quote_fetched_once_for_prev_close_and_quote():
    # ⚡ Bolt: get_previous_close_bar and get_quote for the same ticker at the same
    # decision moment must share a single quotes_fn fetch, not issue two.
    qf = _counting_quotes_fn()
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=qf)
    when = _decision_dt()
    bar = prov.get_previous_close_bar("AAPL", when)
    quote = prov.get_quote("AAPL", when)
    assert bar is not None and quote is not None
    assert quote[0] == pytest.approx(313.3301)
    assert bar.close == pytest.approx(308.63)
    assert qf.calls == [("AAPL",)]  # exactly one fetch, deduped


def test_get_bid_ask_never_served_from_signal_quote_cache():
    # get_bid_ask is the pre-placement spread breaker: it MUST read a fresh quote
    # even after the signal-time quote for that ticker was already memoized, so a
    # future refactor cannot silently route it through the cache.
    qf = _counting_quotes_fn()
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=qf)
    when = _decision_dt()
    prov.get_previous_close_bar("AAPL", when)
    prov.get_quote("AAPL", when)
    assert qf.calls == [("AAPL",)]  # signal-time fetch cached
    assert prov.get_bid_ask("AAPL", when) == pytest.approx((313.10, 313.40))
    assert qf.calls == [("AAPL",), ("AAPL",)]  # breaker fetched fresh, not cached


def test_signal_quote_cache_refetches_on_new_decision_moment():
    # The memo is bounded to one decision moment: a later as_of drops the prior
    # snapshot (so the cache can't grow without bound) and re-fetches.
    qf = _counting_quotes_fn()
    prov = RobinhoodDataProvider(historicals_fn=lambda **k: HISTORICALS, quotes_fn=qf)
    first = _decision_dt()
    second = datetime.combine(datetime(2026, 7, 6).date(), time(15, 45), tzinfo=EASTERN)
    prov.get_quote("AAPL", first)
    prov.get_quote("AAPL", first)  # same moment -> cached
    assert qf.calls == [("AAPL",)]
    prov.get_quote("AAPL", second)  # new moment -> cleared + re-fetched
    assert qf.calls == [("AAPL",), ("AAPL",)]
    assert len(prov._signal_quote_cache) == 1  # bounded: prior moment evicted


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
    # A scalar previous-close carries no real prior-session low -- must not
    # fabricate low=close (Codex review: "Obtain the prior-session low before
    # selecting the base"); see the matching RobinhoodDataProvider assertion above.
    assert bar.low == 0.0


def test_finance_provider_preserves_low_from_timestamp_less_mapping():
    # Codex review ("Preserve an available low in price-only finance mappings"):
    # a mapping with a real low but no bar timestamp falls through to the scalar
    # path (since _coerce_bar requires a timestamp) -- it must not discard the
    # supplied low the way a genuinely low-less scalar (e.g. a bare float) does.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [], previous_close_fn=lambda *a: {"close": 100.0, "low": 95.0}
    )
    bar = prov.get_previous_close_bar("AAPL", _decision_dt())
    assert bar is not None
    assert bar.close == pytest.approx(100.0)
    assert bar.low == pytest.approx(95.0)


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
    broker = RobinhoodBroker(account="999000111", place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "X"}}})
    with pytest.raises(RuntimeError, match="live gate disabled"):
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert calls == []  # place_fn never invoked while disabled


def test_broker_place_live_when_gated_on():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-42"}}},
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "ORD-42"


def test_broker_place_wraps_connector_failure_as_ambiguous():
    # Codex review, PR #1300 discussion_r3920155064 ("Emit the new
    # ambiguous-placement status"): when the underlying connector call
    # itself fails (network timeout, connection reset, ...) before any
    # broker response was observed, place_market_buy must re-raise as
    # OrderPlacementAmbiguousError -- distinct from every RuntimeError below,
    # which reacts to a DEFINITIVE response the broker DID return.
    def _raising_place_fn(**kwargs):
        raise TimeoutError("connection reset")

    broker = RobinhoodBroker(account="999000111", enable_live_orders=True, place_fn=_raising_place_fn)
    with pytest.raises(OrderPlacementAmbiguousError, match="no broker response"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_raises_on_outer_failure_envelope():
    # A failure envelope carrying an inner order id must not be read as a successful submit.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "data": {"order": {"id": "ORD-9"}}},
    )
    with pytest.raises(RuntimeError, match="failed"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_live_raises_on_terminal_state():
    # An id in a terminal-failure state means no live order exists -> must not report success.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-7", "state": "rejected"}}},
    )
    with pytest.raises(RuntimeError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_live_raises_on_missing_order_id():
    # A live placement that returns no id must fail fast, not report false success —
    # otherwise run_once books the notional against budget with no order to reconcile.
    broker = RobinhoodBroker(
        account="999000111",
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


# -- RobinhoodBroker whole-share (quantity-based) orders -----------------------
# See dca_whole_share_accumulator.py / docs/whole_share_accumulation.md -- these
# mirror place_market_buy/review_market_buy's tests exactly, but through the
# quantity= argument path instead of dollar_amount=.


def test_broker_whole_share_place_raises_when_live_gate_disabled():
    calls = []
    broker = RobinhoodBroker(
        account="999000111", place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "X"}}}
    )
    with pytest.raises(RuntimeError, match="live gate disabled"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert calls == []


def test_broker_whole_share_place_live_when_gated_on():
    calls = []
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "ORD-42"}}},
    )
    assert broker.place_whole_share_buy("BYDDY", 1, "ref1") == "ORD-42"
    assert calls[0]["quantity"] == "1"
    assert "dollar_amount" not in calls[0]


def test_broker_whole_share_place_live_multiple_shares_passes_quantity():
    calls = []
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "ORD-43"}}},
    )
    assert broker.place_whole_share_buy("BYDDY", 3, "ref2") == "ORD-43"
    assert calls[0]["quantity"] == "3"


def test_broker_whole_share_place_wraps_connector_failure_as_ambiguous():
    # Codex review, PR #1300 discussion_r3920546926 ("Preserve ambiguity for
    # whole-share placement timeouts"): the notional path's identical fix
    # (test_broker_place_wraps_connector_failure_as_ambiguous) previously
    # wrapped only place_market_buy's connector call, leaving this quantity-
    # based path's own connector call to surface a plain, unclassified
    # exception on a response timeout.
    def _raising_place_fn(**kwargs):
        raise TimeoutError("connection reset")

    broker = RobinhoodBroker(account="999000111", enable_live_orders=True, place_fn=_raising_place_fn)
    with pytest.raises(OrderPlacementAmbiguousError, match="no broker response"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


def test_broker_whole_share_place_raises_terminal_error_when_outer_failure_has_an_inner_order_id():
    """Codex review ("Preserve accepted nonterminal orders in failure
    envelopes"): an inner order with a usable id is real evidence of a
    broker attempt regardless of its (possibly absent) state -- it must be
    preserved via WholeSharePlacementTerminalError, not discarded as a bare
    RuntimeError, so the caller's own terminal-vs-live discrimination can
    see it.
    """
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "data": {"order": {"id": "ORD-9"}}},
    )
    with pytest.raises(WholeSharePlacementTerminalError, match="real broker attempt") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.order == {"id": "ORD-9"}


def test_broker_whole_share_place_raises_plain_error_on_outer_failure_with_no_inner_order():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "error": "insufficient buying power"},
    )
    with pytest.raises(RuntimeError, match="failed"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


def test_broker_whole_share_place_live_raises_on_terminal_state():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-7", "state": "rejected"}}},
    )
    with pytest.raises(RuntimeError, match="terminal state"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


def test_broker_whole_share_place_live_raises_on_expired_state():
    """Codex review ("Reject expired whole-share placement responses"):
    "expired" is a terminal, non-live state exactly like rejected/failed/
    cancelled/voided (see _LIVE_ORDER_STATES's own comment, which already
    excludes it from the live-order set). Without rejecting it here too,
    the caller would treat this response as a successful full submission
    and settle the claim for the entire requested quantity even if no
    shares actually executed.
    """
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-8", "state": "expired"}}},
    )
    with pytest.raises(RuntimeError, match="terminal state"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


def test_broker_whole_share_place_live_terminal_error_carries_the_order_payload():
    """Codex review ("Reconcile terminal fills from the placement
    response"): a terminal placement response is already authoritative
    evidence of what executed (state + cumulative_quantity + average_price)
    -- discarding it in favor of a bare RuntimeError forces the caller to
    depend exclusively on a SEPARATE, later order-history lookup that can
    itself be eventually consistent and report a clean "no match" for an
    order this very response already proved landed, silently restoring
    credit for shares that actually executed. The raised exception must
    carry the raw order payload so the caller can settle directly from it.
    """
    order_payload = {
        "id": "ORD-CANCELED-WITH-FILL",
        "state": "cancelled",
        "cumulative_quantity": "1",
        "average_price": "10.25",
    }
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": order_payload}},
    )
    with pytest.raises(WholeSharePlacementTerminalError, match="terminal state") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.order == order_payload


def test_broker_whole_share_place_live_preserves_an_inner_order_from_an_outer_failure():
    """Codex review ("Preserve inner orders from failure envelopes"): an
    outer {"success": false, ...} envelope can still carry an inner order
    that reached the broker and reports its own terminal state and
    cumulative_quantity -- e.g. a partially filled order that was then
    cancelled, with the cancellation surfaced as the outer failure. The old
    behavior raised a bare RuntimeError as soon as the outer envelope
    failed, discarding that inner order entirely: if the separate
    order-history endpoint had not yet indexed the order, the caller's
    placement-failure reconciliation found nothing and restored the full
    reservation, making the already-executed share's credit reusable. The
    inner order must be surfaced via WholeSharePlacementTerminalError, just
    like a terminal order found in a SUCCESSFUL outer envelope already is.
    """
    order_payload = {
        "id": "ORD-OUTER-FAILURE-WITH-FILL",
        "state": "cancelled",
        "cumulative_quantity": "1",
        "average_price": "10.25",
    }
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "error": "order rejected downstream", "data": {"order": order_payload}},
    )
    with pytest.raises(WholeSharePlacementTerminalError, match="real broker attempt") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.order == order_payload


def test_broker_whole_share_place_live_preserves_a_bare_order_from_an_outer_failure():
    """Codex review ("Preserve bare orders in outer failure envelopes"): the
    unwrapped response's "data" can itself BE the bare order object -- no
    nested "order" key at all -- exactly the shape the normal success path
    already handles via `data.get("order") or data`. Checking only for a
    nested "order" key missed this shape entirely: an outer failure like
    {"success": false, "data": {"id": ..., "state": "cancelled",
    "cumulative_quantity": ...}} still fell through to a bare RuntimeError,
    discarding the terminal fill evidence.
    """
    order_payload = {
        "id": "ORD-BARE-OUTER-FAILURE",
        "state": "cancelled",
        "cumulative_quantity": "1",
        "average_price": "10.25",
    }
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "error": "order rejected downstream", "data": order_payload},
    )
    with pytest.raises(WholeSharePlacementTerminalError, match="real broker attempt") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.order == order_payload


def test_broker_whole_share_place_live_preserves_a_still_live_inner_order_from_an_outer_failure():
    """Codex review ("Preserve accepted nonterminal orders in failure
    envelopes"): an inner order that is still LIVE/working (e.g. "new" or
    "confirmed"), not yet terminal, is just as real a broker acceptance as
    a terminal one -- discarding it (an earlier version of this fix did,
    only preserving TERMINAL inner states) forced the caller to rely
    exclusively on its own separate order-history lookups, which can BOTH
    independently miss a just-accepted, not-yet-indexed order and restore
    the full reservation while it is still live at the broker.
    """
    order_payload = {"id": "ORD-STILL-LIVE", "state": "confirmed"}
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "error": "connector timeout", "data": {"order": order_payload}},
    )
    with pytest.raises(WholeSharePlacementTerminalError, match="real broker attempt") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.order == order_payload


def test_broker_whole_share_place_live_raises_plain_error_when_outer_failure_has_no_inner_order():
    """Companion to the preservation test above: an outer failure with NO
    usable inner order (e.g. a bare validation error, no order ever
    attempted) must still raise a plain RuntimeError -- there is nothing to
    preserve.
    """
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "error": "invalid symbol"},
    )
    with pytest.raises(RuntimeError, match="invalid symbol") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert not isinstance(exc_info.value, WholeSharePlacementTerminalError)


def test_broker_whole_share_place_live_raises_on_missing_order_id():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"status": "rejected"}}},
    )
    with pytest.raises(RuntimeError, match="no order id"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


@pytest.mark.parametrize("bad_quantity", [0, -1])
def test_broker_whole_share_place_rejects_nonpositive_quantity(bad_quantity):
    broker = RobinhoodBroker(account="999000111", enable_live_orders=True, place_fn=lambda **k: {})
    with pytest.raises(ValueError):
        broker.place_whole_share_buy("BYDDY", bad_quantity, "ref1")


def test_broker_whole_share_review_fails_closed_when_live_without_review_fn():
    live = RobinhoodBroker(enable_live_orders=True)
    ok, reason = live.review_whole_share_buy("BYDDY", 1)
    assert ok is False and "no review callable" in reason
    assert RobinhoodBroker(enable_live_orders=False).review_whole_share_buy("BYDDY", 1)[0] is True


def test_broker_whole_share_review_blocks_on_error_alert():
    resp = {"data": {"alerts": [{"severity": "error", "message": "insufficient buying power"}]}}
    broker = RobinhoodBroker(review_fn=lambda **k: resp)
    ok, reason = broker.review_whole_share_buy("BYDDY", 1)
    assert ok is False and "insufficient" in reason


def test_broker_whole_share_review_passes_on_warning_only():
    resp = {"data": {"alerts": [{"severity": "warning", "message": "wide spread"}]}}
    broker = RobinhoodBroker(review_fn=lambda **k: resp)
    ok, _ = broker.review_whole_share_buy("BYDDY", 1)
    assert ok is True


def test_broker_whole_share_review_permissive_without_callable():
    broker = RobinhoodBroker()
    ok, _ = broker.review_whole_share_buy("BYDDY", 1)
    assert ok is True


def test_broker_whole_share_review_passes_quantity_not_dollar_amount():
    calls = []
    broker = RobinhoodBroker(review_fn=lambda **k: calls.append(k) or {"data": {}})
    broker.review_whole_share_buy("BYDDY", 2)
    assert calls[0]["quantity"] == "2"
    assert "dollar_amount" not in calls[0]


@pytest.mark.parametrize("bad_quantity", [0, -1])
def test_broker_whole_share_review_rejects_nonpositive_quantity(bad_quantity):
    broker = RobinhoodBroker()
    with pytest.raises(ValueError):
        broker.review_whole_share_buy("BYDDY", bad_quantity)


def test_broker_tradability_parsing():
    tradable = {"data": {"results": [{"symbol": "AAPL", "tradeable": True, "state": "active", "fractional_tradability": "tradable"}]}}
    untradable = {"data": {"results": [{"symbol": "AAPL", "tradeable": False, "state": "active", "fractional_tradability": "tradable"}]}}
    assert RobinhoodBroker(tradability_fn=lambda **k: tradable).is_tradable_for_fractional_buy("AAPL") is True
    assert RobinhoodBroker(tradability_fn=lambda **k: untradable).is_tradable_for_fractional_buy("AAPL") is False


def test_broker_whole_share_tradability_ignores_fractional_tradability():
    """Codex review ("Verify whole-share tradability before scheduled
    placement"): a whole-share ticker (e.g. BYDDY) is in that category
    PRECISELY because it fails fractional_tradability, so
    is_tradable_for_whole_share_buy must not require it the way
    is_tradable_for_fractional_buy does -- only tradeable + active.
    """
    not_fractional_but_tradeable = {
        "data": {
            "results": [
                {"symbol": "BYDDY", "tradeable": True, "state": "active", "fractional_tradability": "untradable"}
            ]
        }
    }
    assert (
        RobinhoodBroker(tradability_fn=lambda **k: not_fractional_but_tradeable).is_tradable_for_fractional_buy(
            "BYDDY"
        )
        is False
    )
    assert (
        RobinhoodBroker(
            tradability_fn=lambda **k: not_fractional_but_tradeable
        ).is_tradable_for_whole_share_buy("BYDDY")
        is True
    )


def test_broker_whole_share_tradability_still_requires_tradeable_and_active():
    halted = {"data": {"results": [{"symbol": "BYDDY", "tradeable": False, "state": "halted"}]}}
    assert RobinhoodBroker(tradability_fn=lambda **k: halted).is_tradable_for_whole_share_buy("BYDDY") is False
    assert RobinhoodBroker().is_tradable_for_whole_share_buy("BYDDY") is True  # no callable wired -> permissive


def test_broker_whole_share_tradability_matches_the_response_symbol_case_insensitively():
    """Codex review ("Require canonical symbols for whole-share tickers"):
    unlike "active", "whole_share" had no schema-enforced uppercase pattern,
    so a config-authored lowercase entry (e.g. "byddy") reaches this call
    verbatim, while the broker's real API response uses its own canonical
    (uppercase) casing regardless of what was requested. An exact-match
    comparison would silently treat every real, tradeable response as
    "symbol not found" -> not tradable, restoring every fire's deduction
    instead of ever placing an order.
    """
    uppercase_response = {"data": {"results": [{"symbol": "BYDDY", "tradeable": True, "state": "active"}]}}
    assert (
        RobinhoodBroker(tradability_fn=lambda **k: uppercase_response).is_tradable_for_whole_share_buy("byddy")
        is True
    )


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

    assert RobinhoodBroker(account="999000111", buying_power_fn=kw_only).available_buying_power() == 137.0
    assert seen["acct"] == "999000111"

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


def test_broker_defaults_to_no_concurrent_fill_lookup():
    # order_fill_fn is typically an MCP-backed callable of unverified thread-safety;
    # reporting must not run it concurrently unless a caller explicitly opts in.
    assert RobinhoodBroker().supports_concurrent_fill_lookup is False
    assert RobinhoodBroker(supports_concurrent_fill_lookup=True).supports_concurrent_fill_lookup is True


def test_broker_buying_power_parses_dict_response():
    # A raw MCP dict (per the module callable contract) must be unwrapped, not cast to 0.0.
    resp = {"data": {"buying_power": "137.00"}}
    assert RobinhoodBroker(buying_power_fn=lambda **k: resp).available_buying_power() == pytest.approx(137.0)
    # A dict with no recognizable amount field degrades to 0.0 (blocked), not a crash.
    assert RobinhoodBroker(buying_power_fn=lambda **k: {"data": {}}).available_buying_power() == 0.0


# --- BrokerAdapter conformance --------------------------------------------------
#
# RobinhoodBroker's own docstring claims to be "a BrokerAdapter for Robinhood",
# but nothing had ever checked that against near_close_dca.BrokerAdapter (the
# Protocol every gate/reporting function is typed against) -- strategy logic is
# exercised in tests almost entirely against DryRunBroker and ad-hoc test-local
# fakes, so a RobinhoodBroker method silently drifting from the Protocol's
# shape (renamed, dropped, or given an incompatible signature) would have no
# test catching it; it would only surface against a real account.
#
# Two Protocol members carry their own default body in BrokerAdapter itself
# (supports_concurrent_fill_lookup -> False, get_raw_placement_responses ->
# []) rather than being abstract -- every real call site (run_dca.py's
# `getattr(broker, "supports_concurrent_fill_lookup", False)`) already
# treats them as optional-with-a-fallback, so an implementer omitting them
# is not a conformance defect and must not be flagged as one.
_BROKER_ADAPTER_OPTIONAL_MEMBERS = frozenset(
    {"supports_concurrent_fill_lookup", "get_raw_placement_responses"}
)


@pytest.mark.parametrize(
    "broker_cls",
    [
        RobinhoodBroker,
        DryRunBroker,
        AlpacaBroker,
    ],
)
def test_broker_implementations_satisfy_the_broker_adapter_protocol(broker_cls):
    import inspect

    instance = broker_cls()
    for name in dir(BrokerAdapter):
        if name.startswith("_"):
            continue
        protocol_member = inspect.getattr_static(BrokerAdapter, name)
        if not callable(protocol_member) and not isinstance(protocol_member, property):
            continue
        if name in _BROKER_ADAPTER_OPTIONAL_MEMBERS and not hasattr(instance, name):
            continue
        assert hasattr(instance, name), f"{broker_cls.__name__} is missing BrokerAdapter.{name}"
        if isinstance(protocol_member, property):
            continue  # signature check below does not apply to a property
        expected_params = list(inspect.signature(protocol_member).parameters)[1:]  # drop self
        actual_params = list(inspect.signature(getattr(broker_cls, name)).parameters)[1:]
        assert actual_params == expected_params, (
            f"{broker_cls.__name__}.{name} parameters {actual_params} do not match "
            f"BrokerAdapter.{name} parameters {expected_params}"
        )


def test_robinhood_broker_ref_id_prefix_finds_a_live_matching_order():
    resp = {"data": {"orders": [{"state": "filled", "ref_id": "dca-runner-0935-2026-07-28-AAPL"}]}}
    order = RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists_with_ref_id_prefix(
        "AAPL", datetime(2026, 7, 28).date(), ("dca-runner-0935-2026-07-28-AAPL",)
    )
    assert order is not None
    assert order["ref_id"] == "dca-runner-0935-2026-07-28-AAPL"


def test_robinhood_broker_ref_id_prefix_ignores_non_live_states():
    # A matching ref_id on a cancelled order is not a live duplicate.
    resp = {"data": {"orders": [{"state": "cancelled", "ref_id": "dca-runner-0935-2026-07-28-AAPL"}]}}
    order = RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists_with_ref_id_prefix(
        "AAPL", datetime(2026, 7, 28).date(), ("dca-runner-0935-2026-07-28-AAPL",)
    )
    assert order is None


def test_robinhood_broker_history_ref_id_includes_terminal_states():
    resp = {"data": {"orders": [{"state": "cancelled", "ref_id": "crashed-fire"}]}}
    order = RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order == resp["data"]["orders"][0]


def test_robinhood_broker_history_ref_id_follows_pagination():
    """Regression test for "Page broker history before releasing the fire
    lease". ``get_equity_orders`` is paginated; reading only page one made
    every same-day lookup fail OPEN -- the crash-recovery path would report a
    claimed fire order as never submitted and release its lease for a second
    real purchase, purely because the order sat on page two.
    """
    pages = [
        {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": "PAGE2"}},
        {"data": {"orders": [{"state": "cancelled", "ref_id": "crashed-fire"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "PAGE2"]
    assert order == pages[1]["data"]["orders"][0]


def test_robinhood_broker_pagination_deduplicates_a_repeated_order():
    """Regression test for "Deduplicate orders across pagination boundaries".
    When Robinhood repeats an order at a page boundary (an insert ahead of the
    cursor shifts every row behind it by one), appending every page verbatim
    returned the same order more than once, so ``same_day_ticker_notional_usd``
    summed it twice and overstated the broker-reported prior allocation --
    letting a guarded top-up be reduced or skipped incorrectly.
    """
    pages = [
        {
            "data": {
                "orders": [{"id": "order-1", "state": "filled", "dollar_amount": "50.00"}],
                "next_cursor": "PAGE2",
            }
        },
        {
            "data": {
                "orders": [
                    # Same order repeated at the page boundary, plus one genuinely new one.
                    {"id": "order-1", "state": "filled", "dollar_amount": "50.00"},
                    {"id": "order-2", "state": "filled", "dollar_amount": "25.00"},
                ]
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    total = RobinhoodBroker(orders_fn=orders_fn).same_day_ticker_notional_usd(
        "AAPL", datetime(2026, 7, 28).date()
    )
    assert seen_cursors == [None, "PAGE2"]
    assert total == pytest.approx(75.00)


def test_robinhood_broker_pagination_deduplicates_a_whitespace_only_id_via_order_id():
    """Regression test for "Normalize order IDs before pagination deduplication".
    A whitespace-only ``id`` field with a valid ``order_id`` must fall back to
    ``order_id`` for identity, and any leading/trailing whitespace on the
    identifier used must be stripped before it is compared/recorded -- a page-1
    row with a whitespace ``id`` and a page-2 repeat of the SAME order_id must
    still collapse to one row, not be double-counted as if they were two
    distinct orders.
    """
    pages = [
        {
            "data": {
                "orders": [
                    {"id": "   ", "order_id": "order-1", "state": "filled", "dollar_amount": "50.00"}
                ],
                "next_cursor": "PAGE2",
            }
        },
        {
            "data": {
                "orders": [
                    # Same order repeated at the page boundary, keyed only by order_id
                    # since id is blank again, plus one genuinely new order.
                    {"id": "", "order_id": "order-1", "state": "filled", "dollar_amount": "50.00"},
                    {"id": "order-2", "state": "filled", "dollar_amount": "25.00"},
                ]
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    total = RobinhoodBroker(orders_fn=orders_fn).same_day_ticker_notional_usd(
        "AAPL", datetime(2026, 7, 28).date()
    )
    assert seen_cursors == [None, "PAGE2"]
    assert total == pytest.approx(75.00)


def test_robinhood_broker_pagination_handles_a_numeric_order_id():
    """Regression test for "Convert non-string order IDs before stripping".
    _is_blank_broker_field() correctly treats a numeric id as present (not
    blank), but the dedup code then called .strip() directly on it, raising
    AttributeError for any broker payload shape using numeric ids -- failing
    every caller of _iter_same_day_orders(), not just this one page.
    """
    pages = [
        {
            "data": {
                "orders": [{"id": 12345, "state": "filled", "dollar_amount": "50.00"}],
                "next_cursor": "PAGE2",
            }
        },
        {
            "data": {
                "orders": [
                    # Same numeric id repeated at the page boundary, plus a new one.
                    {"id": 12345, "state": "filled", "dollar_amount": "50.00"},
                    {"id": 67890, "state": "filled", "dollar_amount": "25.00"},
                ]
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    total = RobinhoodBroker(orders_fn=orders_fn).same_day_ticker_notional_usd(
        "AAPL", datetime(2026, 7, 28).date()
    )
    assert seen_cursors == [None, "PAGE2"]
    assert total == pytest.approx(75.00)


def test_robinhood_broker_pagination_reads_the_cursor_from_a_next_url():
    pages = [
        {"data": {"orders": [], "next": "https://api.robinhood.com/orders/?cursor=ABC&page_size=10"}},
        {"data": {"orders": [{"state": "filled", "ref_id": "crashed-fire"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "ABC"]
    assert order == pages[1]["data"]["orders"][0]


@pytest.mark.parametrize("blank_cursor", ["", "%20", "+"])
def test_robinhood_broker_pagination_fails_closed_on_a_blank_next_url_cursor(blank_cursor):
    """Regression test for a Copilot review comment on PR #464: `_next_order_cursor`
    read `cursor_values[0]` from a `next` URL's query string without stripping or
    validating it, unlike the `next_cursor`/bare-`cursor` fields it checks first
    (both require `.strip()` truth). A `next` URL whose `cursor` param is empty or
    whitespace-only (`.../orders/?cursor=`) would be treated as a real, usable
    continuation instead of the same end-of-list convention used everywhere else --
    and a blank cursor sent back to the broker on retry risks a repeated-cursor
    error or bogus pagination rather than a clean stop.

    The fix normalizes it the same way: a blank cursor extracted from a `next` URL
    yields no usable cursor, so `_has_continuation_pointer` (which still sees the
    non-empty `next` URL string itself) reports "there is more we cannot reach" and
    the lookup fails closed rather than silently following a hollow cursor.
    """
    page = {
        "data": {
            "orders": [{"state": "filled", "ref_id": "other"}],
            "next": f"https://api.robinhood.com/orders/?cursor={blank_cursor}&page_size=10",
        }
    }
    with pytest.raises(RuntimeError, match="unusable pagination pointer"):
        RobinhoodBroker(orders_fn=lambda **k: page).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_ref_lookup_stops_at_the_match_before_the_page_ceiling():
    """Regression test for "Bound historical recovery queries to the claim day".
    ``get_equity_orders`` has no upper time bound, so a lease recovered several
    trading days after its claim scans everything since. Materializing the whole
    result first meant a long history tripped the page ceiling and raised even
    when the wanted ref had already gone past — which recovery reads as "cannot
    reconcile" and preserves the lease indefinitely. The lookup now returns on
    the match, so a found ref never depends on how much history sits behind it.
    """
    pages_fetched = []

    def orders_fn(**kwargs):
        pages_fetched.append(kwargs.get("cursor"))
        if len(pages_fetched) == 2:
            return {"data": {"orders": [{"state": "filled", "ref_id": "crashed-fire"}], "next_cursor": "MORE"}}
        # Every other page advertises another page forever: consuming this
        # history to the end would hit the ceiling and raise.
        return {"data": {"orders": [], "next_cursor": f"P{len(pages_fetched)}"}}

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order is not None
    assert len(pages_fetched) == 2  # stopped at the match, not at the ceiling


def test_robinhood_broker_ref_lookup_raises_when_history_is_unwired():
    """Regression test for "Preserve leases when order history is unavailable".
    An adapter with no orders_fn yields nothing, which is indistinguishable from
    a scanned-and-empty history. Crash recovery reads that emptiness as proof the
    claimed order was never submitted and releases the balance for a duplicate
    purchase, so the recovery lookup must raise instead.
    """
    with pytest.raises(RuntimeError, match="no orders_fn wired"):
        RobinhoodBroker().strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


@pytest.mark.parametrize("page", [{}, {"data": {}}, {"data": {"orders": "nope"}}, []])
def test_robinhood_broker_ref_lookup_raises_on_an_unrecognizable_page(page):
    # A response carrying no orders list and no pagination pointer is
    # unparseable, not a valid empty history. (A bare list is rejected one step
    # earlier, by the error-envelope check -- either way it raises rather than
    # reporting an absence the caller would act on.)
    with pytest.raises(RuntimeError, match="unrecognizable page|not a valid JSON object"):
        RobinhoodBroker(orders_fn=lambda **k: page).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


@pytest.mark.parametrize(
    "page",
    [
        {"data": {"order": {"state": "filled", "ref_id": "crashed-fire"}}},
        {"data": {"id": "abc123", "state": "filled", "ref_id": "crashed-fire"}},
    ],
)
def test_robinhood_broker_ref_lookup_rejects_a_single_order_envelope(page):
    """Regression test for "Reject single-order envelopes when proving history".
    _iter_same_day_orders only yields data["orders"], so a single-order envelope
    would pass a laxer well-formedness check while yielding nothing -- and crash
    recovery would read that as "never submitted" and release the balance, even
    when the discarded order IS the claimed one.
    """
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: page).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_any_state_ref_lookup_raises_when_history_is_unwired():
    """Codex review ("Require provable order history before releasing
    claims"): same_day_order_with_ref_id_prefix_any_state used to iterate
    the eager _same_day_orders (require_provable_history=False), so an
    unwired orders_fn returned a clean empty list -- indistinguishable from
    a scanned-and-empty history. Whole-share reconciliation reads a clean
    None from this method as permission to restore a stale/placement-
    failure claim, so an unprovable history must raise here too, exactly
    like strategy_order_with_ref_id already does for the same purpose.
    """
    with pytest.raises(RuntimeError, match="no orders_fn wired"):
        RobinhoodBroker().same_day_order_with_ref_id_prefix_any_state(
            "AAPL", datetime(2026, 7, 28).date(), ("crashed-fire",)
        )


def test_robinhood_broker_any_state_ref_lookup_raises_on_an_unrecognizable_page():
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: {"data": {}}).same_day_order_with_ref_id_prefix_any_state(
            "AAPL", datetime(2026, 7, 28).date(), ("crashed-fire",)
        )


def test_robinhood_broker_any_state_ref_lookup_finds_a_cancelled_order():
    page = {"data": {"orders": [{"state": "cancelled", "ref_id": "crashed-fire"}]}}
    order = RobinhoodBroker(orders_fn=lambda **k: page).same_day_order_with_ref_id_prefix_any_state(
        "AAPL", datetime(2026, 7, 28).date(), ("crashed-fire",)
    )
    assert order is not None
    assert order["state"] == "cancelled"


def test_robinhood_broker_pagination_stops_on_an_echoed_cursor():
    """Regression test for "Treat an echoed cursor as the end of pagination".
    A final page that echoes the cursor it was REQUESTED with is the end of the
    list, not a continuation. Reading it as one tripped the repeated-cursor
    guard and raised, failing an absence check closed on a perfectly good scan.
    """
    calls = []

    def orders_fn(**kwargs):
        calls.append(kwargs.get("cursor"))
        if len(calls) == 1:
            return {"data": {"orders": [], "next_cursor": "PAGE2"}}
        # Last page: no next/next_cursor, but echoes its own request cursor.
        return {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "cursor": "PAGE2"}}

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert calls == [None, "PAGE2"]
    assert order is None  # a completed scan that genuinely found nothing


def test_robinhood_broker_pagination_stops_on_a_whitespace_padded_echoed_cursor():
    """Regression test for "Normalize the cursor before checking for an
    echo". A page that echoes the requested cursor with incidental
    surrounding whitespace (" PAGE2 " for a page requested with "PAGE2") is
    still the SAME echo -- comparing the raw, unstripped value made it look
    like a fresh cursor, which the caller's repeated-cursor guard then
    rejected instead of ending pagination cleanly.
    """
    calls = []

    def orders_fn(**kwargs):
        calls.append(kwargs.get("cursor"))
        if len(calls) == 1:
            return {"data": {"orders": [], "next_cursor": "PAGE2"}}
        return {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "cursor": " PAGE2 "}}

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert calls == [None, "PAGE2"]
    assert order is None  # a completed scan that genuinely found nothing


@pytest.mark.parametrize("pointer", [0, [], {}, False, 0.0])
def test_robinhood_broker_fails_closed_on_a_falsey_nonstring_continuation(pointer):
    """Regression test for "Reject falsey non-string continuation pointers".

    `_next_order_cursor` only understands STRING pointers, so a present but
    falsey non-string one (`next_cursor: 0`, `next: []`) yields no cursor. The
    old truthiness-based `_has_continuation_pointer` then reported "no
    continuation", and the iterator declared a truncated history complete --
    which crash recovery reads as proof the claimed order was never submitted,
    releasing the lease and authorizing a duplicate purchase.
    """
    page = {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": pointer}}
    with pytest.raises(RuntimeError):
        RobinhoodBroker(orders_fn=lambda **k: page).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


@pytest.mark.parametrize("pointer", ["", "   "])
def test_robinhood_broker_treats_an_empty_string_pointer_as_the_end(pointer):
    """The complement: an empty or whitespace string is the conventional
    end-of-list, not an unusable continuation, and must still complete the scan
    so a genuine absence can be proven."""
    page = {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": pointer}}
    order = RobinhoodBroker(orders_fn=lambda **k: page).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order is None


def test_robinhood_broker_pagination_still_follows_a_new_bare_cursor():
    # A bare cursor DIFFERENT from the request's is still a real continuation.
    calls = []

    def orders_fn(**kwargs):
        calls.append(kwargs.get("cursor"))
        if len(calls) == 1:
            return {"data": {"orders": [], "cursor": "NEXT"}}
        return {"data": {"orders": [{"state": "filled", "ref_id": "crashed-fire"}]}}

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert calls == [None, "NEXT"]
    assert order is not None


def test_robinhood_broker_ref_lookup_accepts_a_genuine_empty_page():
    # An explicit {"orders": []} IS well-formed: a real, provable empty history.
    order = RobinhoodBroker(orders_fn=lambda **k: {"data": {"orders": []}}).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order is None


def test_robinhood_broker_duplicate_gate_still_tolerates_an_unwired_lookup():
    # The ordinary duplicate gate keeps its established contract -- morning_dca.py
    # uses it as its sole duplicate check, and an unwired adapter reports "no
    # order today" rather than raising. Only the recovery lookup is stricter.
    assert RobinhoodBroker().same_day_strategy_order_exists("AAPL", datetime(2026, 7, 28).date()) is False


def test_robinhood_broker_absence_still_enforces_the_page_ceiling():
    # Proving ABSENCE requires the whole history, so an endless one must raise
    # rather than report "no such order" from a truncated scan.
    def orders_fn(**kwargs):
        return {"data": {"orders": [], "next_cursor": str(kwargs.get("cursor", "")) + "x"}}

    with pytest.raises(RuntimeError, match="exceeded"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "never-submitted"
        )


def test_robinhood_broker_pagination_fails_closed_on_an_unusable_pointer():
    # A "there is more" pointer we cannot follow must never be reported as a
    # complete history: callers read absence as proof and would authorize a
    # duplicate buy (or release a fire lease) on a truncated page set.
    resp = {"data": {"orders": [], "next": "https://api.robinhood.com/orders/?page_size=10"}}
    with pytest.raises(RuntimeError, match="unusable pagination pointer"):
        RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
            "AAPL", datetime(2026, 7, 28).date()
        )


def test_robinhood_broker_pagination_fails_closed_on_a_cursor_loop():
    resp = {"data": {"orders": [], "next_cursor": "SAME"}}
    with pytest.raises(RuntimeError, match="repeated a pagination cursor"):
        RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
            "AAPL", datetime(2026, 7, 28).date()
        )


def test_robinhood_broker_ref_id_prefix_requires_an_exact_match():
    # A live order whose ref_id is not among the supplied prefixes is a
    # different stream's order, not this stream's retry.
    resp = {"data": {"orders": [{"state": "filled", "ref_id": "dca-runner-1030-2026-07-28-AAPL"}]}}
    order = RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists_with_ref_id_prefix(
        "AAPL", datetime(2026, 7, 28).date(), ("dca-runner-0935-2026-07-28-AAPL",)
    )
    assert order is None


def test_robinhood_broker_ref_id_prefix_falls_back_across_client_id_field_names():
    for field in ("client_order_id", "client_order_uuid", "client_id"):
        resp = {"data": {"orders": [{"state": "filled", field: "manual-retry-1"}]}}
        order = RobinhoodBroker(
            orders_fn=lambda resp=resp, **k: resp
        ).same_day_strategy_order_exists_with_ref_id_prefix(
            "AAPL", datetime(2026, 7, 28).date(), ("manual-retry-1",)
        )
        assert order is not None, f"expected a match via {field!r}"


def test_broker_tradability_defaults_to_true_without_a_callable():
    """No tradability_fn wired is the permissive/simulated default, matching
    every other RobinhoodBroker callable's degrade-safely behavior."""
    assert RobinhoodBroker().is_tradable_for_fractional_buy("AAPL") is True


def test_broker_tradability_skips_a_non_matching_symbol_before_the_real_one():
    # A multi-symbol tradability response with an unrelated entry first must
    # not be mistaken for AAPL's own result.
    resp = {
        "data": {
            "results": [
                {"symbol": "MSFT", "tradeable": False, "state": "inactive", "fractional_tradability": "untradable"},
                {"symbol": "AAPL", "tradeable": True, "state": "active", "fractional_tradability": "tradable"},
            ]
        }
    }
    assert RobinhoodBroker(tradability_fn=lambda **k: resp).is_tradable_for_fractional_buy("AAPL") is True


def test_broker_tradability_returns_false_when_the_symbol_is_absent_entirely():
    # The response has results, but none for the requested ticker -- an
    # unrecognized/unlisted symbol must fail closed (not tradable), not
    # silently pass through the loop with no verdict.
    resp = {"data": {"results": [{"symbol": "MSFT", "tradeable": True, "state": "active", "fractional_tradability": "tradable"}]}}
    assert RobinhoodBroker(tradability_fn=lambda **k: resp).is_tradable_for_fractional_buy("AAPL") is False


def test_broker_order_fill_defaults_to_none_without_a_callable():
    assert RobinhoodBroker().get_order_fill("ORD-1") is None


def test_broker_order_fill_defaults_to_none_for_an_empty_order_id():
    assert RobinhoodBroker(order_fill_fn=lambda **k: {}).get_order_fill("") is None


def test_broker_order_fill_accepts_a_bare_top_level_order_object():
    # A response with no "orders"/"order" wrapper at all -- the order fields
    # sit directly at the top level after _unwrap.
    resp = {"data": {"id": "ORD-7", "cumulative_quantity": "3", "average_price": "10.00"}}
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-7")
    assert isinstance(fill, OrderFill)
    assert fill.quantity_filled == pytest.approx(3.0)
    assert fill.avg_fill_price == pytest.approx(10.0)


def test_broker_order_fill_skips_non_matching_orders_in_a_multi_order_list():
    resp = {
        "data": {
            "orders": [
                {"id": "ORD-OTHER", "cumulative_quantity": "9", "average_price": "9.00"},
                {"id": "ORD-TARGET", "cumulative_quantity": "1", "average_price": "20.00"},
            ]
        }
    }
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-TARGET")
    assert isinstance(fill, OrderFill)
    assert fill.avg_fill_price == pytest.approx(20.0)


def test_broker_order_fill_returns_none_when_id_is_not_in_the_list():
    resp = {"data": {"orders": [{"id": "ORD-OTHER", "cumulative_quantity": "1", "average_price": "1.00"}]}}
    assert RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-MISSING") is None


def test_broker_order_fill_falls_back_to_quantity_when_cumulative_quantity_is_empty_string():
    """Regression test (Antigravity review: "Empty String Check for
    cumulative_quantity"). A payload with cumulative_quantity="" is not
    None, so the plain `is not None` check previously picked it as the
    quantity source, parsed it to nothing, and never fell back to the
    filled order's plain "quantity" -- valuing a real fill at $0.
    """
    resp = {
        "data": {
            "orders": [
                {
                    "id": "ORD-1",
                    "state": "filled",
                    "cumulative_quantity": "",
                    "quantity": "0.07",
                    "average_price": "100.00",
                }
            ]
        }
    }
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-1")
    assert isinstance(fill, OrderFill)
    assert fill.quantity_filled == pytest.approx(0.07)
    assert fill.avg_fill_price == pytest.approx(100.0)


def test_broker_order_fill_falls_back_to_quantity_when_cumulative_quantity_is_whitespace():
    """Regression test (Codex review: "Treat whitespace-only cumulative
    quantities as absent"). A payload with cumulative_quantity=" " is not
    caught by an empty-string check either -- it still gets selected as the
    quantity source and parses to nothing.
    """
    resp = {
        "data": {
            "orders": [
                {
                    "id": "ORD-1",
                    "state": "filled",
                    "cumulative_quantity": "  ",
                    "quantity": "0.07",
                    "average_price": "100.00",
                }
            ]
        }
    }
    fill = RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-1")
    assert isinstance(fill, OrderFill)
    assert fill.quantity_filled == pytest.approx(0.07)
    assert fill.avg_fill_price == pytest.approx(100.0)


@pytest.mark.parametrize(
    "qty,price",
    [("0", "10.00"), ("1", "0"), ("-1", "10.00"), ("1", "-5.00")],
)
def test_broker_order_fill_returns_none_for_a_non_positive_quantity_or_price(qty, price):
    # A matched order that has not actually filled (zero/negative quantity or
    # price) must report as unfilled, not a phantom zero-price fill.
    resp = {"data": {"orders": [{"id": "ORD-1", "cumulative_quantity": qty, "average_price": price}]}}
    assert RobinhoodBroker(order_fill_fn=lambda **k: resp).get_order_fill("ORD-1") is None


def test_broker_get_raw_placement_responses_drains_the_buffer():
    broker = RobinhoodBroker(
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "ORD-1", "state": "confirmed"}}},
    )
    broker.place_market_buy("AAPL", 2.0, "ref-1")
    first = broker.get_raw_placement_responses()
    assert len(first) == 1
    assert first[0]["order_id"] == "ORD-1"
    # Drained: a second call (e.g. a reused broker in a later pipeline call)
    # must not resurface the same response.
    assert broker.get_raw_placement_responses() == []


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

@pytest.mark.parametrize(
    "data, expected",
    [
        # The explicit isError envelope returned by mcp-bridge for network failures.
        ({"isError": True, "error": "network timeout"}, "network timeout"),
        ({"isError": True}, "Unknown network/bridge error"),
        ({"isError": True, "error": None}, "Unknown network/bridge error"),
        ({"isError": True, "error": ""}, "Unknown network/bridge error"),

        # A top-level "error" is always a failure signal on its own.
        ({"error": "invalid symbol"}, "error: invalid symbol"),
        # Empty/falsy error values must not be treated as errors.
        ({"error": ""}, None),
        # Truthy non-string error payloads (structured broker errors) must still
        # be detected, stringified only when building the reason.
        ({"error": {"code": "service_unavailable"}}, "error: {'code': 'service_unavailable'}"),
        ({"error": ["timeout", "retry_exhausted"]}, "error: ['timeout', 'retry_exhausted']"),

        # "message"/"detail" alone (no explicit failure flag) are informational,
        # not error indicators, and must not be treated as failures.
        ({"message": "rate limited"}, None),
        ({"detail": "not found"}, None),
        ({"message": ""}, None),
        ({"detail": ""}, None),

        # Original wrapper payloads from the codebase
        ({"error": "some network failure"}, "error: some network failure"),
        ({"success": False}, "success=false"),
        ({"ok": False}, "ok=false"),
        ({"success": False, "data": {}}, "success=false"),
        ({"ok": False, "data": {}}, "ok=false"),
        # Concrete error message takes priority over bare flag name.
        ({"success": False, "error": "permission denied"}, "error: permission denied"),
        ({"ok": False, "message": "quota exceeded"}, "quota exceeded"),
        # message/detail only count as a reason once a failure flag establishes
        # that the envelope actually represents a failure.
        ({"ok": False, "detail": "not found"}, "not found"),

        # Non-error dictionaries
        ({"success": True}, None),
        ({"ok": True}, None),
        ({"other": "value"}, None),
        ({}, None),
        # Explicit failure flag with no concrete message still resolves to a reason.
        ({"success": False, "message": ""}, "success=false"),

        # Error-ish dictionaries that should still be treated as errors
        ({"isError": False, "error": "msg"}, "error: msg"),

        # Non-dictionary inputs - should return None
        ([], None),
        (None, None),
        ("string", None),
        (42, None),
    ]
)
def test_is_error_response(data, expected):
    """Test that _is_error_response correctly identifies failure envelopes."""
    assert _is_error_response(data) == expected


@pytest.mark.parametrize("response, expected", [
    # Happy paths: no errors should return None
    ({"data": {"result": 42}}, None),
    ({"success": True}, None),
    ({"ok": True, "data": {"things": []}}, None),

    # Non-dictionary inputs (handled by explicit string return in the new implementation)
    (None, "Response is not a valid JSON object"),
    ("string", "Response is not a valid JSON object"),
    ([1, 2, 3], "Response is not a valid JSON object"),

    # Outer envelope errors
    ({"success": False, "data": {"result": 42}}, "success=false"),
    ({"error": "server down"}, "error: server down"),
    ({"ok": False}, "ok=false"),

    # Inner errors (within "data")
    ({"data": {"error": "not found"}}, "error: not found"),
    ({"data": {"success": False}}, "success=false"),

    # Inner error takes precedence over outer in the new unwrapping logic
    ({"error": "outer error", "data": {"error": "inner error"}}, "error: inner error"),
])
def test_response_failure(response, expected):
    assert _response_failure(response) == expected

def test_unwrap_valid_dict_with_data():
    payload = {"data": {"key": "value"}}
    assert _unwrap(payload) == {"key": "value"}

def test_unwrap_valid_dict_without_data():
    payload = {"key": "value"}
    assert _unwrap(payload) == {"key": "value"}

def test_unwrap_non_dict_payload():
    assert _unwrap("not a dict") == {}
    assert _unwrap(None) == {}
    assert _unwrap(["a", "list"]) == {}

def test_unwrap_data_key_not_dict():
    payload = {"data": "not a dict"}
    assert _unwrap(payload) == {"data": "not a dict"}
