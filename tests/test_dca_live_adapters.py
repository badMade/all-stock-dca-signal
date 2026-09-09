"""Tests for the live Robinhood/finance adapters (dca_live_adapters)."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from dca_live_adapters import (
    AmbiguousPlacementResponseError,
    FinanceDataProvider,
    FractionalPlacementRejectedError,
    RobinhoodBroker,
    RobinhoodDataProvider,
    _canonicalized_order_row,
    _envelope_layer_shares_order_identity,
    _envelope_layers_conflict_with_order,
    _failure_is_broker_authored,
    _fill_evidence_values_conflict,
    _has_unusable_direct_cursor_alias,
    _has_unusable_next_url_cursor,
    _interpret_review,
    _is_error_response,
    _is_provable_orders_page,
    _is_unusable_live_placement_order_id,
    _merge_envelope_layers_prefer_innermost_nonblank,
    _normalized_order_identifier,
    _order_collections_semantically_agree,
    _order_lifecycle_aliases_conflict,
    _order_rows_contradict,
    _parse_rfc3339,
    _placement_response_has_untrackable_positive_order_id,
    _response_failure,
    _rh_interval,
    _to_float,
    _unwrap,
    _unwrap_seeking_order_evidence,
    _usable_ref_id_aliases,
)
from near_close_dca import (
    EASTERN,
    AlpacaBroker,
    BrokerAdapter,
    BrokerName,
    DryRunBroker,
    OrderFill,
    OrderPlacementAmbiguousError,
    OrderPlacementRejectedError,
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


def test_untrackable_order_id_ignores_error_container_diagnostics():
    rejected = {"success": False, "error": {"id": 10**400, "state": "rejected", "filled_quantity": 0}}
    assert _placement_response_has_untrackable_positive_order_id(rejected) is False


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


def test_finance_provider_reads_splits_from_a_fresh_daily_query():
    calls = []

    def daily_rows(ticker, start, end, interval_minutes):
        calls.append((ticker, start.date(), end.date(), interval_minutes))
        return [
            {"date": "2026-08-17", "Stock Splits": 2.0, "Dividends": 0.0},
            {"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": 0.0},
        ]

    prov = FinanceDataProvider(bars_fn=daily_rows, previous_close_fn=lambda *a: 100.0)
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 16), date(2026, 8, 18)) == (
        2.0,
        date(2026, 8, 18),
    )
    assert calls == [("AAPL", date(2026, 8, 17), date(2026, 8, 18), 1440)]


def test_finance_provider_does_not_treat_ohlc_only_rows_as_split_evidence():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "close": 50.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_folds_a_dividend_into_the_factor():
    # close=98, dividend=2 -> (98 + 2) / 98 reconstructs the pre-drop scale,
    # exactly like archived_split_factor's dividend handling.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": 2.0, "Close": 98.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    factor, cursor = prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18))
    assert factor == pytest.approx(100.0 / 98.0)
    assert cursor == date(2026, 8, 18)


def test_finance_provider_rejects_boolean_dividend_value():
    # Codex review, PR #1299 (dca_live_adapters.py:4899): bool is a subclass
    # of int in Python, so float(True)/float(False) succeed as 1.0/0.0
    # instead of raising. "Dividends": true from a connector must not be
    # accepted as a fabricated $1 dividend -- rejected (unavailable) the
    # same way a non-numeric/empty-string dividend value already is,
    # instead of durably rebasing the inverse-Martingale recovery latch on
    # data the connector never actually reported.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": True, "Close": 98.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_rejects_boolean_split_ratio_value():
    # Same rejection, for the split-ratio field: "Stock Splits": true must
    # not be accepted as a real 1:1 split ratio.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": True, "Dividends": 0.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_combines_a_split_and_dividend_on_the_same_ex_date():
    """Split metadata must not mask a simultaneous cash distribution."""
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"date": "2026-08-18", "Stock Splits": 2.0, "Dividends": 2.0, "Close": 98.0}
        ],
        previous_close_fn=lambda *a: 100.0,
    )

    factor, cursor = prov.get_corporate_action_factor(
        "AAPL", date(2026, 8, 17), date(2026, 8, 18)
    )

    assert factor == pytest.approx(2.0 * (100.0 / 98.0))
    assert cursor == date(2026, 8, 18)


def test_finance_provider_rejects_a_dividend_row_with_no_usable_close():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": 2.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_rejects_a_blank_split_value_instead_of_treating_it_as_zero():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": None, "Dividends": 0.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_rejects_a_blank_dividend_value_instead_of_treating_it_as_zero():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": ""}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_interprets_a_naive_action_datetime_as_eastern():
    from datetime import datetime as _datetime

    # A naive midnight timestamp for the boundary day must not be shifted to
    # the prior day by the host's local timezone (e.g. UTC), which would drop
    # an effective-day split from the [since, as_of] window entirely.
    since = date(2026, 8, 17)
    as_of = date(2026, 8, 18)
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"timestamp": _datetime(2026, 8, 18, 0, 0), "Stock Splits": 2.0, "Dividends": 0.0}
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", since, as_of) == (2.0, as_of)


def test_finance_provider_dedupes_identical_duplicate_action_rows():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"date": "2026-08-18", "Stock Splits": 2.0, "Dividends": 0.0},
            {"date": "2026-08-18", "Stock Splits": 2.0, "Dividends": 0.0},
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) == (
        2.0,
        date(2026, 8, 18),
    )


def test_finance_provider_rejects_conflicting_duplicate_action_rows():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"date": "2026-08-18", "Stock Splits": 2.0, "Dividends": 0.0},
            {"date": "2026-08-18", "Stock Splits": 3.0, "Dividends": 0.0},
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_catches_a_failure_raised_while_iterating_rows():
    def _bad_rows(*_a):
        yield {"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": 0.0}
        raise RuntimeError("transient lookup failure mid-iteration")

    prov = FinanceDataProvider(bars_fn=_bad_rows, previous_close_fn=lambda *a: 100.0)
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


@pytest.mark.parametrize("failure", [KeyError("bad payload"), IndexError("missing field")])
def test_finance_provider_isolates_unexpected_connector_failures(failure):
    # An injected connector decoding a malformed third-party payload raises
    # exception types that are not part of its advertised API (KeyError,
    # IndexError, ...). The provider boundary must fail closed on ANY of them
    # -- the narrower (OSError, RuntimeError, TypeError, ValueError,
    # OverflowError) catch this method used to have let these escape and
    # abort the whole scheduled scan over one ticker's evidence lookup
    # (PR #1357: "Fix corporate action isolation and state update races").
    def _failed_lookup(*_args):
        raise failure

    prov = FinanceDataProvider(bars_fn=_failed_lookup, previous_close_fn=lambda *a: 100.0)
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_parses_an_offset_iso_action_timestamp_in_eastern():
    # 2026-08-18T02:00:00Z is 2026-08-17 22:00 Eastern -- the textual prefix
    # ("2026-08-18") must not be used directly, or the row misfiles onto the
    # wrong ET calendar day and either escapes or falls outside the window.
    since = date(2026, 8, 16)
    as_of = date(2026, 8, 17)
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"timestamp": "2026-08-18T02:00:00Z", "Stock Splits": 2.0, "Dividends": 0.0}
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", since, as_of) == (2.0, as_of)


def test_finance_provider_rejects_an_unparseable_action_timestamp():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "not-a-date", "Stock Splits": 0.0, "Dividends": 0.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


def test_finance_provider_short_circuits_when_since_already_covers_as_of():
    # since == as_of (a same-day later stream re-evaluating an already-
    # examined cursor) must not query bars_fn at all -- since + 1 day would
    # sit strictly after as_of, an inverted range some connector wrappers
    # reject outright -- and must not raise even at date.max, where
    # `since + timedelta(days=1)` would otherwise overflow.
    calls = []
    prov = FinanceDataProvider(
        bars_fn=lambda *a: (calls.append(a) or []), previous_close_fn=lambda *a: 100.0
    )
    assert prov.get_corporate_action_factor("AAPL", date.max, date.max) == (1.0, date.max)
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 18), date(2026, 8, 18)) == (
        1.0,
        date(2026, 8, 18),
    )
    assert calls == []


def test_finance_provider_rejects_a_cursor_later_than_as_of():
    # since > as_of (a restored ledger carrying a schema-valid but
    # nonsensical future cursor) must NOT be treated like the since == as_of
    # no-op -- a real action before as_of could still be unconfirmed.
    calls = []
    prov = FinanceDataProvider(
        bars_fn=lambda *a: (calls.append(a) or []), previous_close_fn=lambda *a: 100.0
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 20), date(2026, 8, 18)) is None
    assert calls == []


def test_finance_provider_requires_coverage_through_as_of():
    # The provider's newest published row stops one day short of as_of (an
    # in-progress session not yet published) -- a split effective ON as_of
    # could still be missing, so the whole call must report unavailable
    # rather than a confirmed factor from the incomplete window.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-17", "Stock Splits": 2.0, "Dividends": 0.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 16), date(2026, 8, 18)) is None


def test_finance_provider_rejects_a_window_with_a_missing_interior_session():
    # Monday and Wednesday are returned but Tuesday -- an ordinary NYSE
    # trading weekday strictly between since and as_of -- is transiently
    # absent. The endpoint (cursor == as_of) check alone would pass; the
    # interior-coverage check must still reject the whole window.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"date": "2026-08-17", "Stock Splits": 2.0, "Dividends": 0.0},  # Monday
            {"date": "2026-08-19", "Stock Splits": 0.0, "Dividends": 0.0},  # Wednesday
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 16), date(2026, 8, 19)) is None


def test_finance_provider_accepts_a_window_that_covers_every_trading_weekday():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"date": "2026-08-17", "Stock Splits": 2.0, "Dividends": 0.0},  # Monday
            {"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": 0.0},  # Tuesday
            {"date": "2026-08-19", "Stock Splits": 0.0, "Dividends": 0.0},  # Wednesday
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 16), date(2026, 8, 19)) == (
        2.0,
        date(2026, 8, 19),
    )


def test_finance_provider_does_not_require_rows_on_weekends_or_holidays():
    # 2026-07-03 is the observed Independence Day holiday and 2026-07-04/05
    # are a weekend -- none of the three need their own row.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [
            {"date": "2026-07-02", "Stock Splits": 0.0, "Dividends": 0.0},  # Thursday
            {"date": "2026-07-06", "Stock Splits": 2.0, "Dividends": 0.0},  # Monday
        ],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 7, 1), date(2026, 7, 6)) == (
        2.0,
        date(2026, 7, 6),
    )


def test_finance_provider_rejects_a_degenerate_multi_year_cursor_span():
    # An implausibly old `since` (e.g. a corrupted/legacy tracker state) must
    # degrade to "cannot confirm" rather than day-by-day scanning years of
    # calendar dates.
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-19", "Stock Splits": 0.0, "Dividends": 0.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2000, 1, 1), date(2026, 8, 19)) is None


def test_finance_provider_accepts_close_price_and_c_aliases_for_dividends():
    for key in ("close_price", "c"):
        prov = FinanceDataProvider(
            bars_fn=lambda *a, _key=key: [
                {"date": "2026-08-18", "Stock Splits": 0.0, "Dividends": 2.0, _key: 98.0}
            ],
            previous_close_fn=lambda *a: 100.0,
        )
        factor, cursor = prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18))
        assert factor == pytest.approx(100.0 / 98.0), key
        assert cursor == date(2026, 8, 18), key


def test_finance_provider_get_quote_returns_none_without_a_quote_fn():
    prov = FinanceDataProvider(bars_fn=lambda *a: [], previous_close_fn=lambda *a: 100.0)
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_finance_provider_get_quote_accepts_a_mapping():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [],
        previous_close_fn=lambda *a: 100.0,
        quote_fn=lambda *a: {"price": 95.5, "as_of": "2026-07-02T15:44:00-04:00"},
    )
    price, as_of = prov.get_quote("AAPL", _decision_dt())
    assert price == pytest.approx(95.5)
    assert as_of == datetime(2026, 7, 2, 15, 44, tzinfo=EASTERN)


def test_finance_provider_get_quote_accepts_a_bare_scalar():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [], previous_close_fn=lambda *a: 100.0, quote_fn=lambda *a: 95.5
    )
    price, as_of = prov.get_quote("AAPL", _decision_dt())
    assert price == pytest.approx(95.5)
    assert as_of == _decision_dt()


def test_finance_provider_get_quote_rejects_a_boolean_price():
    """Python makes bool a subclass of int, so float(True) == 1.0: an
    unguarded _to_float(raw_price) would accept a malformed connector
    response like {"price": true, ...} as an ordinary $1.00 quote, which
    could turn an ordinary stock into a maximum-drop BUY signal (Codex
    review: "Reject boolean Finance quote prices")."""
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [],
        previous_close_fn=lambda *a: 100.0,
        quote_fn=lambda *a: {"price": True, "as_of": "2026-07-02T15:44:00-04:00"},
    )
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_finance_provider_get_quote_rejects_a_boolean_bare_scalar():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [], previous_close_fn=lambda *a: 100.0, quote_fn=lambda *a: False
    )
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_finance_provider_get_quote_rejects_a_non_positive_price():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [],
        previous_close_fn=lambda *a: 100.0,
        quote_fn=lambda *a: {"price": 0.0, "as_of": "2026-07-02T15:44:00-04:00"},
    )
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_finance_provider_get_quote_returns_none_when_the_quote_fn_yields_nothing():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [], previous_close_fn=lambda *a: 100.0, quote_fn=lambda *a: None
    )
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_finance_provider_third_positional_argument_still_sets_supports_corporate_actions():
    """A caller wiring ``FinanceDataProvider(bars_fn, previous_close_fn, False)``
    predates ``quote_fn`` and must keep setting ``supports_corporate_actions``,
    not silently reinterpret that boolean as the quote callable (which would
    also leave ``supports_corporate_actions`` stuck at its True default and
    later crash ``get_quote()`` calling a bare ``False``) (Codex review:
    "Preserve the existing third positional argument")."""
    prov = FinanceDataProvider(lambda *a: [], lambda *a: 100.0, False)
    assert prov.supports_corporate_actions is False
    assert prov.get_quote("AAPL", _decision_dt()) is None


def test_finance_provider_quote_fn_is_keyword_only():
    with pytest.raises(TypeError):
        FinanceDataProvider(lambda *a: [], lambda *a: 100.0, True, lambda *a: 95.5)


def test_finance_provider_accepts_capitalized_date_alias():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"Date": "2026-08-18", "Stock Splits": 2.0, "Dividends": 0.0}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) == (
        2.0,
        date(2026, 8, 18),
    )


def test_finance_provider_rejects_whitespace_only_corporate_action_metadata():
    prov = FinanceDataProvider(
        bars_fn=lambda *a: [{"date": "2026-08-18", "Stock Splits": " ", "Dividends": "\t"}],
        previous_close_fn=lambda *a: 100.0,
    )
    assert prov.get_corporate_action_factor("AAPL", date(2026, 8, 17), date(2026, 8, 18)) is None


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


def test_broker_place_sees_past_a_domain_field_wrapper_on_success():
    # Codex review, PR #1164: "Keep placement descent past unrelated
    # domain fields". `_unwrap`'s ordinary descent stops on a recognized
    # domain payload key (`buying_power`) before reaching the nested
    # accepted `order` one layer deeper -- `place_market_buy` used to
    # discard the accepted order and raise `AmbiguousPlacementResponseError`
    # even though the broker had accepted the purchase.
    # `_unwrap_seeking_order_evidence` seeks the order-bearing layer
    # instead of stopping on the domain field.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"buying_power": "100", "data": {"order": {"id": "ORD", "state": "queued"}}}
        },
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "ORD"


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


def test_broker_place_classifies_explicit_no_order_rejection():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "message": "order refused"},
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_classifies_explicit_rejection_with_error_container_id():
    # Diagnostic error IDs in error containers must not be treated as order IDs.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "error": {"id": "ERR-123", "code": "BAD_REQ", "message": "order refused"},
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_classifies_terminal_state_inside_a_failure_envelope():
    # Codex review, PR #688 discussion_r3914393246 ("Classify terminal orders
    # inside failure envelopes"): a failure envelope naming an order whose OWN
    # state is already terminal (rejected/failed/cancelled/canceled/voided)
    # PROVES no live order exists -- it must not be treated as an unresolved,
    # batch-halting failure just because the outer envelope says
    # "success": false. terminal_broker_row_proves_no_order() reads an absent
    # fill figure on a declined state as proof, not as ambiguity, so this
    # raises the SAME OrderPlacementRejectedError (public alias for
    # FractionalPlacementRejectedError) a multi-ticker batch already catches
    # to continue past a definitive rejection -- not a bare RuntimeError.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "data": {"order": {"id": "ORD-DEAD", "state": "rejected"}},
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    # The id travels with the proven refusal -- an operator still needs it to
    # look the dead order up (Codex review, PR #859: "Preserve the terminal
    # order ID from proven rejections").
    assert exc_info.value.order_id == "ORD-DEAD"


def test_broker_place_keeps_bridge_failure_ambiguous_even_with_success_false():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"isError": True, "success": False, "error": "timeout"},
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_treats_an_oversized_order_id_as_unresolved_not_proven():
    # An astronomically large id (e.g. `10**400`) nulls out to
    # `envelope_order_id = None` the same way the `-1` sentinel a few lines
    # above does (Codex review, PR #859/#911 round 44: "Reject sentinel
    # order IDs on the failure-envelope path too"), but it is NOT the same
    # absence of evidence: the response DID name a positive order id, just
    # one too large to trust, so this is a malformed response rather than a
    # broker actively reporting "no order". Previously this fell into the
    # id-less "the envelope itself is the evidence" branch and was reported
    # as a PROVEN rejection -- silently dropping a possibly-real, if
    # unrepresentable, order from deployment and cap accounting. It must
    # stay unresolved (a plain `RuntimeError`, not
    # `FractionalPlacementRejectedError`) and go through reconciliation
    # instead (Codex review, PR #1168: "Preserve ambiguity for malformed
    # positive order IDs").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "id": 10**400, "message": "timeout"},
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_treats_an_oversized_negative_order_id_as_a_proven_rejection():
    # An oversized NEGATIVE id (`-(10**400)`) is not the same "malformed
    # response naming a real order" the oversized POSITIVE case above is --
    # every negative id, oversized or not, is already cleared as a plain
    # no-order sentinel by the `.startswith("-")` check a few lines above
    # `_envelope_order_id_unrepresentable`'s own computation. Flagging it
    # as unrepresentable too barred the id-less "the envelope itself is the
    # evidence" branch from an envelope that was never ambiguous to begin
    # with, forcing an unambiguous broker refusal through the unresolved-
    # halt path instead of the proven-rejection one it belongs on (Codex
    # review, PR #1168: "Treat oversized negative IDs as rejection
    # sentinels").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "id": -(10**400), "message": "order refused"},
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_checks_failure_envelope_id_bounds_before_stringifying():
    """Codex review, PR #1169: "Check failure-envelope ID bounds before
    stringifying". Fresh evidence beyond the fixed 10**400 case above is a
    coefficient beyond Python's integer-to-string CONVERSION digit limit
    (not just the smaller float-conversion bound `_is_representable_
    numeric_order_id` itself checks) -- the blank/negative detection used
    to call a bare `str(envelope_order_id)` twice before
    `_envelope_order_id_unrepresentable` (computed just above, using only
    magnitude-safe `math.isfinite`) was ever consulted, raising
    `ValueError` instead of producing the intended classification."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "id": 10**5000, "message": "timeout"},
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)

    # An oversized NEGATIVE id beyond that same digit limit must still be
    # treated as an unambiguous rejection sentinel, not crash either --
    # Python's arbitrary-precision int comparison (`< 0`) never raises
    # regardless of magnitude, so the negative check no longer needs a
    # bare str() at all for a numeric candidate.
    negative_broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "id": -(10**5000), "message": "order refused"},
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        negative_broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_treats_a_nan_order_id_as_unresolved_not_proven():
    # Codex review, PR #1169: "Reject NaN IDs on failure envelopes". A NaN
    # id can never satisfy `envelope_order_id >= 0` -- NaN compares false
    # against everything, including itself -- so it fell through the
    # `_envelope_order_id_unrepresentable` gate entirely (unlike the
    # oversized-positive case above, which the same gate correctly
    # catches) and was accepted as a nonblank id despite
    # `_is_representable_numeric_order_id` (which calls `math.isfinite`,
    # itself false for NaN) already saying it is unusable. Must stay
    # unresolved exactly like the oversized-positive case, not proven.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "id": float("nan"), "message": "timeout"},
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_finds_live_order_nested_in_error_container():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "error": {"order": {"id": "ORD-NESTED", "state": "queued"}},
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert exc_info.value.order_id == "ORD-NESTED"


def test_broker_place_finds_live_order_nested_in_nonstandard_failure_container():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "connector_failure": {
                "context": {"order": {"id": "ORD-HIDDEN", "state": "queued"}}
            },
        },
    )

    with pytest.raises(AmbiguousPlacementResponseError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")

    assert exc_info.value.order_id == "ORD-HIDDEN"


def test_broker_place_handles_cyclic_failure_response_without_looping():
    response = {"success": False, "message": "timeout"}
    response["error"] = response
    broker = RobinhoodBroker(
        account="999000111", enable_live_orders=True, place_fn=lambda **k: response
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


@pytest.mark.parametrize("sentinel_id", [0, -1, -100, False, "", "   ", {}, []])
def test_broker_place_classifies_explicit_rejection_with_sentinel_order_id(sentinel_id):
    # Explicit no-ID sentinels (0, -1, False, containers, whitespace) must not be
    # treated as real order IDs on the explicit rejection path.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "id": sentinel_id,
            "message": "order refused",
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


@pytest.mark.parametrize("sentinel_id", [0, -1, -100, False, "", "   ", {}, []])
def test_broker_place_live_raises_on_sentinel_order_id_without_error(sentinel_id):
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {"id": sentinel_id, "state": "confirmed"}},
        },
    )
    with pytest.raises(RuntimeError, match="no order id"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_live_raises_on_terminal_state():
    # Explicit zero execution proves a terminal order consumed no capital.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {"id": "ORD-7", "state": "rejected", "cumulative_quantity": "0"}}
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


@pytest.mark.parametrize("quantity", [None, "0.25"])
def test_broker_place_terminal_state_without_proven_zero_execution_is_ambiguous(quantity):
    order = {"id": "ORD-7", "state": "cancelled"}
    if quantity is not None:
        order["cumulative_quantity"] = quantity
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": order}},
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


@pytest.mark.parametrize(
    "field",
    [
        "cumulative_quantity",
        "executed_quantity",
        "filled_quantity",
        "quantity_filled",
        "executed_notional",
        "executed_dollar_amount",
        "filled_notional",
    ],
)
def test_broker_place_terminal_state_checks_every_fill_field(field):
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {
                "id": "ORD-7", "state": "cancelled",
                field: "2.5",
            }}
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


@pytest.mark.parametrize(
    "field",
    [
        "executed_quantity",
        "filled_quantity",
        "quantity_filled",
        "executed_notional",
        "executed_dollar_amount",
        "filled_notional",
    ],
)
def test_broker_place_terminal_state_positive_fill_with_zero_cumulative_quantity_is_ambiguous(field):
    # If cumulative_quantity is 0 but another execution field is positive, it must remain ambiguous.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {
                "id": "ORD-7", "state": "cancelled",
                "cumulative_quantity": "0", field: "2.5",
            }}
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_terminal_state_zero_quantity_with_nonzero_price_is_ambiguous():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {
                "id": "ORD-7", "state": "cancelled",
                "cumulative_quantity": "0", "average_price": "100.00",
            }}
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_terminal_state_requires_zero_for_each_order():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {
            "order": {"id": "ORD-A", "state": "cancelled", "cumulative_quantity": "0"},
            "sibling": {"id": "ORD-B", "state": "cancelled"},
        }},
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_multiple_orders_with_explicit_zero_fill_is_rejected():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {
            "order": {"id": "ORD-A", "state": "cancelled", "cumulative_quantity": "0"},
            "sibling": {"id": "ORD-B", "state": "cancelled", "cumulative_quantity": "0"},
        }},
    )
    with pytest.raises(OrderPlacementRejectedError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_does_not_share_one_zero_fill_between_conflicting_ids():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {
            "id": "REQUEST-1", "order_id": "ORDER-2", "state": "cancelled",
            "cumulative_quantity": "0",
        }}},
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_finds_a_sibling_order_named_on_the_raw_response():
    # {"success": true, "sibling": {"id": "ORD-B", "state": "queued"},
    # "data": {"order": {"id": "ORD-A", "state": "cancelled",
    # "cumulative_quantity": "0"}}}: "sibling" sits at the RAW response's
    # own top level, alongside (not inside) "data" -- invisible to a
    # sibling scan that only ever looked at the unwrapped `data` mapping.
    # "A" is cleanly proven dead while live, queued "B" sits one key over
    # at the outer level; proving the response from "A" alone would let a
    # possibly-accepted second order sit outside reconciliation and cap
    # accounting entirely (Codex review, PR #859/#911 round 44: "Scan
    # outer envelopes for sibling orders").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "sibling": {"id": "ORD-B", "state": "queued"},
            "data": {"order": {"id": "ORD-A", "state": "cancelled", "cumulative_quantity": "0"}},
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_finds_a_sibling_order_nested_two_levels_deep():
    # {"success": true, "data": {"order": {"id": "A", "state": "cancelled",
    # "cumulative_quantity": "0"}, "meta": {"sibling": {"id": "B", "state":
    # "queued"}}}}: "B" is nested two levels down from "data", inside
    # "meta" -- "meta" itself carries no id/state/fill of its own, so it is
    # not a competing order, but its OWN "sibling" key names one. A scan
    # that only ever checked immediate children never descended into
    # "meta" to find it (Codex review, PR #859/#911 round 46: "Traverse
    # nested sibling containers before proving rejection").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "data": {
                "order": {"id": "A", "state": "cancelled", "cumulative_quantity": "0"},
                "meta": {"sibling": {"id": "B", "state": "queued"}},
            },
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


@pytest.mark.parametrize("live_state", ["queued", "confirmed", "new", "pending", "unconfirmed", "working"])
def test_broker_place_sibling_order_with_live_state_and_zero_fill_is_ambiguous(live_state):
    # Even if both orders report cumulative_quantity: 0, a non-terminal sibling is live/working.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {
            "order": {"id": "ORD-A", "state": "cancelled", "cumulative_quantity": "0"},
            "sibling": {"id": "ORD-B", "state": live_state, "cumulative_quantity": "0"},
        }},
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_outer_unattributed_zero_with_single_order_is_rejected():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "cumulative_quantity": "0",
            "data": {"order": {"id": "ORD-A", "state": "cancelled"}},
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_conflicting_outer_and_inner_fill_evidence_is_unresolved():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "executed_notional": "5",
            "data": {"order": {"id": "ORD-A", "state": "cancelled", "executed_notional": "3"}},
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_outer_unattributed_zero_with_multiple_orders_is_ambiguous():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "cumulative_quantity": "0",
            "data": {
                "order": {"id": "ORD-A", "state": "cancelled"},
                "sibling": {"id": "ORD-B", "state": "cancelled"},
            },
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_echoed_order_id_with_single_zero_is_rejected():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "id": "ORD-A",
            "state": "cancelled",
            "cumulative_quantity": "0",
            "data": {"order": {"id": "ORD-A", "state": "cancelled"}},
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_differently_typed_order_ids_require_distinct_zeros():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "id": 1,
            "state": "cancelled",
            "cumulative_quantity": "0",
            "data": {"order": {"id": "1", "state": "cancelled"}},
        },
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_terminal_state_rejects_boolean_fill_as_ambiguous():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {
            "id": "ORD-7", "state": "cancelled", "cumulative_quantity": False,
        }}},
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_requires_every_zero_fill_order_to_be_terminal():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {
            "order": {"id": "ORD-A", "state": "cancelled", "cumulative_quantity": "0"},
            "sibling": {"id": "ORD-B", "state": "queued", "cumulative_quantity": "0"},
        }},
    )
    with pytest.raises(RuntimeError, match="terminal state") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_outer_failure_inspects_sibling_order_evidence():
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "order": {"id": "ORD-SIBLING", "state": "queued"},
            "data": {"error": "timeout"},
        },
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_failure_finds_a_sibling_order_named_on_the_raw_response():
    # {"success": false, "sibling": {"id": "ORD-B", "state": "queued",
    # "cumulative_quantity": "0"}, "data": {"order": {"id": "ORD-A",
    # "state": "cancelled", "cumulative_quantity": "0"}}}: "sibling" sits
    # at the RAW response's own top level, alongside (not inside) "data" --
    # invisible to the failure-envelope path's proven-rejection reasoning,
    # which never scanned for a second named order the way the success
    # path's identical check already does. "A" is cleanly proven dead while
    # live, queued "B" sits one key over; proving the rejection from "A"
    # alone would let a possibly-accepted second order sit outside
    # reconciliation and cap accounting entirely (Codex review, PR
    # #859/#911 round 45: "Inspect every represented order before proving
    # rejection").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "sibling": {"id": "ORD-B", "state": "queued", "cumulative_quantity": "0"},
            "data": {"order": {"id": "ORD-A", "state": "cancelled", "cumulative_quantity": "0"}},
        },
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_does_not_rescan_a_diagnostic_error_container_as_a_sibling():
    # The new sibling scan added to the failure-envelope path (see the test
    # above) must not rediscover the error container itself: {"success":
    # false, "error": {"id": "ERR-123", "code": "BAD_REQ", "message":
    # "order refused"}} has no real order anywhere, only a diagnostic id
    # inside "error". `_failure_error_layer` is already given first-class
    # treatment via its own dedicated evidence merge, but scanning "error"
    # a SECOND time as an ordinary sibling key on `data`/`response` treated
    # that diagnostic id as an unproven live order and blocked the clean
    # rejection `test_broker_place_classifies_explicit_rejection_with_
    # error_container_id` (above) already covers, permanently -- a bare
    # diagnostic row with no state/fill evidence can never itself prove
    # `terminal_broker_row_proves_no_order` (Codex review, PR #859/#911
    # round 45: "Do not rescan the error container as a sibling order").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "error": {"id": "ERR-123", "code": "BAD_REQ", "message": "order refused"},
        },
    )
    with pytest.raises(OrderPlacementRejectedError, match="rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


@pytest.mark.parametrize("evidence", [{"state": "queued"}, {"executed_dollar_amount": "1.25"}])
def test_broker_place_idless_failure_with_acceptance_evidence_is_ambiguous(evidence):
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": False, "data": evidence},
    )
    with pytest.raises(RuntimeError, match="failed") as exc_info:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(exc_info.value, OrderPlacementRejectedError)


def test_broker_place_live_raises_on_missing_order_id():
    # An id-less order row whose ONLY lifecycle evidence is a DECLINED state
    # ("rejected"/"failed"/"voided", read through the "status" alias here)
    # is proven dead by the shared classifier without needing a fill figure
    # -- a declined order never held anything to report -- so this is a
    # proven rejection, not a bare "no order id" failure. This assertion
    # was briefly reverted to an older, pre-round-43 expectation by a merge
    # conflict resolution that kept the base branch's stale test body
    # alongside this branch's own already-advanced classifier; restored to
    # match the shared terminal_broker_row_proves_no_order rule this file's
    # id-less "envelope itself is the evidence" branches already apply
    # everywhere else (Codex review, PR #859/#911 round 44: "Restore the
    # round-43 declined-state expectation this test lost to a merge").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"status": "rejected"}}},
    )
    with pytest.raises(FractionalPlacementRejectedError, match="explicitly rejected"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_rejects_a_boolean_order_id():
    # A connector sentinel -- id=True (a bool, which a bare isinstance(x,
    # (str, int)) check does NOT catch, since bool subclasses int in
    # Python) -- is Python-truthy and passes str/int typing, so this used
    # to be accepted as a real broker identifier. `{"order": {"id": true,
    # "state": "queued"}}` then became order id "True", and the caller
    # marked the placement accepted and consumed budget against an id that
    # can never actually be queried back from the broker. The whole-share
    # path already excludes bool explicitly; this success path did not
    # (Codex review, PR #859/#911 round 45: "Reject boolean order
    # identifiers").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": True, "state": "queued"}}},
    )
    with pytest.raises(RuntimeError, match="no order id"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_rejects_a_stringified_zero_order_id():
    """Codex review, PR #1168: "Reject stringified zero placement IDs".
    This validation rejected a leading "-" but accepted a stringified
    zero (`id="0"`), even though the durable normalizer and reference-ID
    validator both already reject "0" -- `place_market_buy()` returned it
    as a real broker id and the caller recorded the requested notional as
    if submission were proven, leaving the attempt without usable
    provenance for later reconciliation."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "0", "state": "queued"}}},
    )
    with pytest.raises(RuntimeError, match="no order id"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_whole_share_place_falls_through_an_unusable_primary_to_a_usable_secondary_id():
    """Codex review, PR #1169: "Fall through unusable primary placement
    IDs" -- the identical fix applied to place_whole_share_buy's own
    success path."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": True, "order_id": "REAL", "state": "confirmed"}}},
    )
    assert broker.place_whole_share_buy("BYDDY", 1, "ref1") == "REAL"


def test_broker_place_falls_through_an_unusable_primary_to_a_usable_secondary_id():
    """Codex review, PR #1169: "Skip unusable aliases in the identity-
    conflict check". The identical fix applied to the whole-share path
    above didn't fully resolve this scenario for the fractional
    `place_market_buy` path: `_order_identifier_aliases_conflict` still
    reported `{"id": true, "order_id": "REAL"}` as conflicting
    (`_normalized_order_id(true)` is `None`, which `!= "REAL"`), gating a
    broader `AmbiguousPlacementResponseError` raise independent of the
    order_id-fallback fix. Now a known-unusable alias is not treated as
    disagreeing with a usable one, so the fallback can actually take
    effect."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"success": True, "order": {"id": True, "order_id": "REAL", "state": "queued"}},
    )
    assert broker.place_market_buy("AAPL", 10.0, "ref1") == "REAL"


def test_broker_place_rejects_a_stringified_non_finite_order_id():
    """Codex review, PR #1169: "Reject serialized non-finite placement
    IDs". A serialized sentinel ("NaN") is truthy, doesn't start with
    "-", and isn't caught by the stringified-non-positive check (a NaN
    comparison against 0 is always False) -- so this validation accepted
    it as a real broker identifier and place_market_buy() returned "NaN"
    as a proven submission, even though fill lookup and recovery can
    never identify an order by that id."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "NaN", "state": "queued"}}},
    )
    with pytest.raises(RuntimeError, match="no order id"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_accepts_a_tiny_positive_scientific_order_id():
    """Codex review, PR #1169: "Preserve tiny positive numeric order IDs".
    `_is_stringified_non_positive_number` used to parse via `float()`,
    which underflows a tiny but genuinely POSITIVE scientific spelling
    ("1e-5000") to 0.0 -- misclassified as non-positive, so
    place_market_buy() discarded that valid id and raised "no order id"
    after the broker had already accepted the order. Fixed by parsing
    via Decimal, which stays exact at any magnitude."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "1e-5000", "state": "queued"}}},
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "1e-5000"


def test_broker_place_accepts_a_finite_huge_magnitude_scientific_order_id():
    """Codex review, PR #1169: "Detect non-finite strings without
    binary-float overflow". A finite positive numeric string outside
    binary-float range ("1e10000") converts through float() to inf --
    no exception raised -- so _is_stringified_non_finite_number
    misclassified it as non-finite even though _normalized_order_id
    deliberately preserves that exact identifier. Fixed by parsing via
    Decimal.is_finite(), which distinguishes an actual NaN/infinity
    spelling from a merely large-magnitude one."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": "1e10000", "state": "queued"}}},
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "1e10000"


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


def test_broker_whole_share_place_honors_a_status_only_rejection():
    """Codex review, PR #1169: "Honor the status alias in whole-share
    placement responses". A connector reporting lifecycle only through
    "status" ({"id": "A", "status": "rejected", "cumulative_quantity":
    "0"}) read as an empty state (a bare order.get("state", "")), so it
    matched neither terminal check and returned "A" as a successful
    whole-share submission despite the broker's own rejection."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {"id": "ORD-9", "status": "rejected", "cumulative_quantity": "0"}}
        },
    )
    with pytest.raises(RuntimeError, match="terminal state"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


def test_broker_whole_share_place_rejects_conflicting_state_and_status():
    """Codex review, PR #1169: "Honor the status alias in whole-share
    placement responses". A row whose state and status disagree (one
    working, one terminal) is unresolved evidence -- picking either
    alias silently discards the other's own, possibly-authoritative
    story, so this must route through the same terminal-error path
    rather than being accepted as a successful submission.

    Updated for Codex review, PR #1251 ("Halt lifecycle-conflicting
    whole-share responses"): a conflicting pair is no longer packaged as
    WholeSharePlacementTerminalError (whose caller SKIPS broker-history
    reconciliation and trusts the response's own state as authoritative
    terminal fact), since that would let one alias's "terminal" story
    silently overrule the other alias's "still live" story. It now
    raises AmbiguousPlacementResponseError, the structurally-ambiguous
    halt path that forces the real reconciliation lookup instead."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {"order": {"id": "ORD-10", "state": "queued", "status": "rejected"}}
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError, match="conflicting lifecycle aliases") as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.structurally_ambiguous is True


def test_broker_whole_share_lifecycle_conflict_exception_redacts_order_secrets():
    """Codex review, PR #1251: "Redact lifecycle-conflict payloads before
    raising". The AmbiguousPlacementResponseError message for a
    state/status lifecycle conflict is built from `_log_safe_repr` (no
    redaction) rather than `_artifact_safe_repr`, even though this
    exception's message -- unlike a plain log call -- can be
    interpolated into a caller's `skip_reason` and persisted into a run
    artifact."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "order": {
                    "id": "ORD-CONFLICT",
                    "state": "queued",
                    "status": "rejected",
                    "account_number": "999000111",
                    "access_token": "super-secret-token",
                }
            }
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")

    message = str(exc_info.value)
    assert "999000111" not in message
    assert "super-secret-token" not in message
    assert "[REDACTED]" in message


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


def test_broker_whole_share_place_validates_a_conflicting_orders_collection():
    """Codex review, PR #1169: "Validate whole-share order collections
    before returning". A whole-share placement response can carry both
    a singular "order" and a conflicting "orders" collection naming a
    SECOND, potentially-live broker order ({"order": {"id": "A", "state":
    "queued"}, "orders": [{"id": "B", "state": "queued"}]}) -- a bare
    data.get("order") or data selected "A" and returned it without ever
    inspecting "B", so the caller recorded and settled only "A" while
    "B" stayed outside attempt and notional accounting."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "order": {"id": "A", "state": "queued"},
                "orders": [{"id": "B", "state": "queued"}],
            }
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.structurally_ambiguous is True


def test_broker_whole_share_place_halts_on_conflicting_lifecycle_aliases():
    """Codex review, PR #1251: "Halt lifecycle-conflicting whole-share
    responses". A response reporting state="rejected" alongside
    status="queued" is self-contradictory, not an ordinary terminal
    response: the caller's terminal-only path trusts a
    WholeSharePlacementTerminalError's own `order.state` as authoritative
    and SKIPS the broker-history reconciliation lookup entirely, which
    would have released the reserved claim as a proven zero-fill even
    though the OTHER alias still says the order is live. This must raise
    AmbiguousPlacementResponseError instead -- the structurally-ambiguous
    halt path the caller's generic exception handler does NOT skip the
    reconciliation lookup for -- so the real broker-history evidence
    resolves it rather than either alias being trusted blindly."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "order": {
                    "id": "ORD-CONFLICT",
                    "state": "rejected",
                    "status": "queued",
                    "cumulative_quantity": "0",
                }
            }
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert exc_info.value.structurally_ambiguous is True


def test_broker_whole_share_outer_failure_exception_redacts_inner_order_secrets():
    """Placement diagnostics can flow into durable skip-reason artifacts."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "error": "access_token=outer-secret-token",
            "data": {
                "order": {
                    "id": "ORD-SECRET",
                    "state": "queued",
                    "account_number": "999000111",
                    "access_token": "super-secret-token",
                }
            },
        },
    )

    with pytest.raises(WholeSharePlacementTerminalError) as exc_info:
        broker.place_whole_share_buy("BYDDY", 1, "ref1")

    message = str(exc_info.value)
    assert "999000111" not in message
    assert "super-secret-token" not in message
    assert "outer-secret-token" not in message
    assert "[REDACTED]" in message


def test_broker_whole_share_place_accepts_a_singular_order_with_an_equivalent_numeric_collection_id():
    """Codex review, PR #1168/#1169: "Canonicalize numeric IDs across
    whole-share collection rows" / "Canonicalize numeric IDs before
    validating whole-share collections" -- two independently-authored
    fixes for the identical gap. A broker can represent the SAME accepted
    order as a numeric float in the singular row and an equivalent string
    in the collection -- {"order": {"id": 123.0, "state": "queued"},
    "orders": [{"id": "123", "state": "queued"}]}. A raw string
    comparison ("123.0" != "123") read this as a disagreement and raised
    WholeSharePlacementTerminalError for a collection that actually
    agrees, sending an accepted order into ambiguous recovery."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "order": {"id": 123.0, "state": "queued"},
                "orders": [{"id": "123", "state": "queued"}],
            }
        },
    )
    # The collection row's own id spelling ("123") now wins after merging
    # in its evidence (Codex review, PR #1168: "Normalize IDs when merging
    # agreeing collection evidence" -- _agreeing_collection_row's own
    # numeric-aware id comparison now finds this row and merges it in,
    # innermost-wins, the same as any other agreeing collection row's
    # fields). This test is about the AMBIGUITY going away in the first
    # place (the normalized-numeric agreement check no longer treats
    # "123.0"/"123" as a disagreement), not about which spelling of the id
    # ultimately wins.
    assert broker.place_whole_share_buy("BYDDY", 1, "ref1") == "123"


def test_broker_whole_share_place_merges_agreeing_collection_evidence_before_accepting():
    """Codex review, PR #1168: "Merge agreeing collection evidence before
    accepting". When the singular row omits lifecycle data but an
    agreeing same-ID collection row supplies it -- {"order": {"id": "A"},
    "orders": [{"id": "A", "state": "rejected", "cumulative_quantity":
    "0"}]} -- the selector used to return the bare singular row without
    folding in the collection row's evidence, so place_whole_share_buy()
    returned "A" as an accepted placement despite the broker's own
    collection row proving it was rejected."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "order": {"id": "A"},
                "orders": [{"id": "A", "state": "rejected", "cumulative_quantity": "0"}],
            }
        },
    )
    with pytest.raises(WholeSharePlacementTerminalError):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


def test_broker_whole_share_place_normalizes_ids_when_merging_agreeing_collection_evidence():
    """Codex review, PR #1168: "Normalize IDs when merging agreeing
    collection evidence". `order.id=123.0` (singular) and
    `orders[0].id="123"` (collection) name the same order under
    `_normalized_order_identifier`'s numeric-aware comparison -- the
    selector already treats them as agreeing -- but `_agreeing_collection_
    row` still used a bare string comparison (`"123.0" != "123"`) and
    found no matching collection row, so the collection row's own
    rejection evidence (`state="rejected"`, `cumulative_quantity="0"`)
    never merged in, and `place_whole_share_buy()` accepted "123.0" as a
    successful placement despite the broker's own collection row proving
    it was rejected."""
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "order": {"id": 123.0},
                "orders": [{"id": "123", "state": "rejected", "cumulative_quantity": "0"}],
            }
        },
    )
    with pytest.raises(WholeSharePlacementTerminalError):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")


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


@pytest.mark.parametrize("live_state", ["new", "confirmed", "partially_filled"])
def test_broker_whole_share_place_live_preserves_a_still_live_inner_order_from_an_outer_failure(live_state):
    """Codex review ("Preserve accepted nonterminal orders in failure
    envelopes"): an inner order that is still LIVE/working (e.g. "new" or
    "confirmed"), not yet terminal, is just as real a broker acceptance as
    a terminal one -- discarding it (an earlier version of this fix did,
    only preserving TERMINAL inner states) forced the caller to rely
    exclusively on its own separate order-history lookups, which can BOTH
    independently miss a just-accepted, not-yet-indexed order and restore
    the full reservation while it is still live at the broker.
    """
    order_payload = {"id": "ORD-STILL-LIVE", "state": live_state}
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


@pytest.mark.parametrize("sentinel_id", [True, -1, -100, "   ", "0", "NaN"])
def test_broker_whole_share_place_rejects_sentinel_order_ids(sentinel_id):
    # A connector sentinel -- id=True (a bool, which a bare isinstance(x,
    # int) check would NOT catch, since bool subclasses int in Python),
    # id=-1, or a whitespace-only string -- is Python-truthy, so a plain
    # `if not order_id` check accepted it as a real broker identifier. The
    # caller would then mark the placement accepted and settle accumulator
    # credit for an order that can never actually be queried back from the
    # broker by that id (Codex review, PR #859/#911 round 44: "Restore
    # whole-share order-ID validation"). A stringified zero ("0") is
    # rejected the same way as a leading "-" (Codex review, PR #1168:
    # "Reject stringified zero placement IDs").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {"data": {"order": {"id": sentinel_id, "state": "queued"}}},
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


def test_broker_same_day_order_detection_sees_past_a_domain_field_wrapper():
    # Codex review, PR #1164/#1169: "Keep domain fields from masking
    # nested order history". `_unwrap`'s ordinary descent stops on a
    # recognized domain payload key (`buying_power`) before reaching the
    # nested `orders` list one layer deeper -- `_iter_same_day_orders`
    # then read the missing `orders` key as an empty list and the
    # duplicate guard authorized what could have been a duplicate
    # purchase despite the nested working order.
    # `_unwrap_seeking_order_evidence` seeks the `orders`-bearing layer
    # instead of stopping on the domain field.
    day = datetime(2026, 7, 6).date()
    live = {"data": {"buying_power": "100", "data": {"orders": [{"id": "LIVE", "state": "queued"}]}}}
    assert RobinhoodBroker(orders_fn=lambda **k: live).same_day_strategy_order_exists("AAPL", day) is True


def test_broker_same_day_fails_closed_on_error_response():
    # An error-shaped lookup must not read as "no order exists" and allow a duplicate buy.
    day = datetime(2026, 7, 6).date()
    err = {"data": {"error": "orders service unavailable"}}
    with pytest.raises(RuntimeError, match="same-day order lookup"):
        RobinhoodBroker(orders_fn=lambda **k: err).same_day_strategy_order_exists("AAPL", day)
    with pytest.raises(RuntimeError, match="same-day order lookup"):
        RobinhoodBroker(orders_fn=lambda **k: {"data": {"success": False}}).same_day_strategy_order_exists("AAPL", day)


def test_broker_same_day_fails_closed_on_a_failure_past_a_domain_wrapper():
    # Codex review, PR #1164/#1169: "Scan nested history failures past
    # domain wrappers". `_response_failure` only inspects `response` and
    # its own single, non-recursive `_unwrap(response)` layer -- a failure
    # sitting past a recognized domain field (`buying_power`) is invisible
    # to that narrow check, so `_iter_same_day_orders` proceeded to
    # `_unwrap_seeking_order_evidence`, which correctly settles on the
    # deeper failure layer -- but nothing inspected THAT layer for failure
    # evidence either, so the ordinary duplicate-order gate read the
    # missing `orders` key as an empty page instead of a genuine service
    # failure.
    day = datetime(2026, 7, 6).date()
    err = {
        "data": {
            "buying_power": "100",
            "data": {"success": False, "error": "down", "data": {"orders": [{"id": "LIVE"}]}},
        }
    }
    with pytest.raises(RuntimeError, match="same-day order lookup"):
        RobinhoodBroker(orders_fn=lambda **k: err).same_day_strategy_order_exists("AAPL", day)


def test_broker_same_day_fails_closed_on_a_self_contradictory_response():
    # Codex review, PR #1168: "Keep contradictory order-history wrappers
    # fail-closed". {"data": {"success": true, "data": {"success": false,
    # "orders": [...]}}} has `_unwrap()` stop on the OUTER, order-less layer
    # (correct for a single-order placement verdict), so reading it as a
    # clean page silently hid the real "orders" collection one layer deeper
    # -- reporting "no order today" and authorizing a duplicate purchase
    # despite the queued order underneath.
    day = datetime(2026, 7, 6).date()
    resp = {"data": {"success": True, "data": {"success": False, "orders": [{"id": "LIVE", "state": "queued"}]}}}
    with pytest.raises(RuntimeError, match="self-contradictory"):
        RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists("AAPL", day)


def test_broker_same_day_order_detection_sees_orders_past_a_zero_balance_wrapper():
    # Codex review, PR #1168: "Do not let zero balance fields hide nested
    # orders". {"data": {"success": true, "buying_power": 0, "data":
    # {"orders": [queued_order]}}} used to have `_unwrap()`'s presence-only
    # balance check anchor on the incidental zero-balance sibling, hiding
    # the real "orders" collection from `_iter_same_day_orders` and
    # reporting a false "no order today".
    day = datetime(2026, 7, 6).date()
    resp = {"data": {"success": True, "buying_power": 0, "data": {"orders": [{"state": "queued"}]}}}
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists("AAPL", day) is True


def test_broker_same_day_fails_closed_on_a_contradiction_past_a_domain_wrapper():
    # Codex review, PR #1164/#1169: "Scan contradictions beyond domain
    # wrappers". `_unwrap_hides_contradicted_nested_data` used the DEFAULT
    # (domain-aware) `_layer_carries_envelope_evidence` policy, so an
    # intervening `buying_power` layer -- which carries no health flag of
    # its own to compare -- stopped the contradiction check before it ever
    # reached the outer/inner `success` disagreement one layer further in.
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "buying_power": "100",
            "data": {
                "success": True,
                "data": {"success": False, "error": "down", "orders": [{"id": "LIVE", "state": "queued"}]},
            },
        }
    }
    with pytest.raises(RuntimeError, match="self-contradictory"):
        RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists("AAPL", day)


def test_broker_same_day_fails_closed_on_a_failure_past_a_pagination_pointer():
    # Codex review, PR #1164: "Scan failures beyond pagination-pointer
    # layers". `_unwrap_seeking_order_evidence` halts descent on a
    # pagination pointer (`next_cursor`/`cursor`) just as readily as on a
    # domain field, so a failure sitting one layer PAST the pointer was
    # invisible to a check that only inspected the settled pointer layer.
    day = datetime(2026, 7, 6).date()
    err = {"data": {"next_cursor": "abc", "data": {"success": False, "error": "history down"}}}
    with pytest.raises(RuntimeError, match="same-day order lookup"):
        RobinhoodBroker(orders_fn=lambda **k: err).same_day_strategy_order_exists("AAPL", day)


def test_broker_same_day_order_detection_sees_orders_nested_below_a_pagination_pointer():
    # Codex review, PR #1169: "Preserve nested orders when retaining
    # pagination pointers". `_unwrap_seeking_order_evidence` correctly
    # settles on a `next_cursor`-bearing layer rather than descending past
    # it (a genuine pagination pointer must not be silently skipped), but
    # that SAME layer's own order rows can still sit one level deeper, in
    # its own nested "data": `{"data": {"next_cursor": "PAGE2", "data":
    # {"orders": [{"id": "LIVE", "state": "queued"}]}}}` has the settled
    # layer's bare "orders" key absent even though the same page's actual
    # rows are right there. Reading only the settled layer's own "orders"
    # key discarded them, and the ordinary (non-provable-page) duplicate
    # gate advanced straight to a page 2 that never repeats the live
    # order, missing it entirely.
    day = datetime(2026, 7, 6).date()
    resp = {"data": {"next_cursor": "PAGE2", "data": {"orders": [{"id": "LIVE", "state": "queued"}]}}}
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists("AAPL", day) is True

    # Codex review, PR #1169: "Traverse all wrappers below pagination
    # pointers" -- the fresh-evidence follow-up. A cursor-bearing layer
    # can wrap the page's rows in MORE than one additional "data"
    # pass-through envelope, not just one: `{"next_cursor": "P2", "data":
    # {"data": {"orders": [LIVE]}}}` has the rows sitting past TWO
    # wrappers, and a fallback checking only the immediate child still
    # discarded them.
    resp_two_levels = {
        "data": {"next_cursor": "PAGE2", "data": {"data": {"orders": [{"id": "LIVE", "state": "queued"}]}}}
    }
    assert (
        RobinhoodBroker(orders_fn=lambda **k: resp_two_levels).same_day_strategy_order_exists("AAPL", day) is True
    )

    # Codex review, PR #1169: "Inspect nested rows before accepting an
    # empty direct collection". A cursor-bearing wrapper can carry a
    # stale/placeholder EMPTY "orders" directly on the settled layer while
    # its own nested "data" chain holds the actual (non-empty) page rows:
    # {"next_cursor": "P2", "orders": [], "data": {"orders": [LIVE]}}.
    # Returning the direct empty list as soon as it was present (not None)
    # discarded the nested live order, and the ordinary duplicate gate
    # advanced past this page as though it were genuinely empty.
    resp_direct_empty_with_nested_rows = {
        "data": {
            "next_cursor": "PAGE2",
            "orders": [],
            "data": {"orders": [{"id": "LIVE", "state": "queued"}]},
        }
    }
    assert (
        RobinhoodBroker(orders_fn=lambda **k: resp_direct_empty_with_nested_rows).same_day_strategy_order_exists(
            "AAPL", day
        )
        is True
    )


def test_provable_ref_lookup_sees_orders_nested_below_a_pagination_pointer():
    # Codex review, PR #1168: "Validate nested orders before rejecting a
    # provable page". `_is_provable_orders_page` used to check ONLY the
    # settled layer's bare "orders" key, not its own nested "data" -- the
    # same nested-orders shape the sibling PR #1169 fix already taught
    # `_iter_same_day_orders`'s own fallback to read. A REQUIRE_PROVABLE_
    # HISTORY caller (strategy_order_with_ref_id) therefore rejected
    # `{"data": {"next_cursor": "PAGE2", "data": {"orders": [...]}}}}` as
    # an "unrecognizable page" before ever reaching that fallback, even
    # though the response contains a valid, visible orders list -- blocking
    # crash-lease recovery indefinitely on a response it could have read.
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "next_cursor": "PAGE2",
            "data": {"orders": [{"id": "LIVE", "state": "queued", "ref_id": "crashed-fire"}]},
        }
    }
    found = RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id(
        "AAPL", day, "crashed-fire"
    )
    assert found is not None
    assert found["id"] == "LIVE"


def test_provable_ref_lookup_rejects_a_malformed_nested_order_collection():
    """Codex review, PR #1169: "Reject malformed nested order collections".
    A pagination-bearing settled layer whose collection sits deeper in
    "data" can be malformed there -- `{"next_cursor": "P2", "data":
    {"orders": "truncated"}}}` has a non-list "orders" one layer down.
    Silently walking past it (treating it as "nothing here yet") returned
    the OUTER layer's absent collection as though this were an ordinary
    unprovable page, rather than surfacing that the page itself could not
    be read at all -- `require_provable_history=True` must reject this
    outright, not let the ordinary duplicate gate advance past it."""
    day = datetime(2026, 7, 6).date()
    resp = {"data": {"next_cursor": "P2", "data": {"orders": "truncated"}}}
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "crashed-fire")


def test_provable_ref_lookup_rejects_a_singular_order_beside_a_nested_collection():
    """Codex review, PR #1169: "Reject singular orders beside nested
    history lists". A settled layer can name a singular "order" (naming
    a DIFFERENT, live order) alongside a nested "orders" collection one
    level deeper -- {"order": {"id": "LIVE", "ref_id": "TARGET", "state":
    "queued"}, "data": {"orders": [{"id": "OTHER", "state": "rejected"}]}}.
    Trusting the nested collection alone silently discarded the singular
    row, yielding only "OTHER" and returning None for a ref that this
    very response already proved live."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "order": {"id": "LIVE", "ref_id": "TARGET", "state": "queued"},
            "data": {"orders": [{"id": "OTHER", "state": "rejected"}]},
        }
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "TARGET")


def test_provable_ref_lookup_rejects_a_malformed_singular_order():
    """Codex review, PR #1169: "Reject malformed singular orders before
    descending". A present but non-dict "order" value (e.g. a truncated
    string) fails the `isinstance(..., dict)` check the sibling
    non-empty-singular-order fix added, and used to fall through
    silently, discarding this unreadable evidence in favor of a nested
    collection instead of failing closed."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "order": "truncated",
            "data": {"orders": [{"id": "OTHER"}]},
        }
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "TARGET")


def test_provable_ref_lookup_rejects_a_nested_singular_order_beside_direct_rows():
    """Codex review, PR #1169: "Reconcile nested singular orders before
    trusting direct rows". The reverse nesting of the sibling case above:
    direct "orders" rows naming only "OTHER" beside a NESTED singular
    "order" naming a different, live "TARGET" -- {"orders": [{"id":
    "OTHER"}], "data": {"order": {"id": "LIVE", "ref_id": "TARGET",
    "state": "queued"}}}. The cursor-only scan over nested layers only
    ever read their "orders" key, silently ignoring "order" entirely."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "orders": [{"id": "OTHER", "state": "rejected"}],
        "data": {"order": {"id": "LIVE", "ref_id": "TARGET", "state": "queued"}},
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "TARGET")


def test_provable_ref_lookup_rejects_a_singular_order_below_nested_history_rows():
    """Codex review, PR #1169: "Reject singular orders below nested
    history rows". The direct-row variant is now guarded, but when a
    pagination layer wraps a nested "orders" list and that row-bearing
    layer itself wraps a DEEPER singular "order", the cursor-only scan
    below the row-bearing layer only ever checked its own "orders" key --
    {"data": {"orders": [{"id": "A", "ref_id": "OTHER"}], "data":
    {"order": {"id": "B", "ref_id": "TARGET", "state": "queued"}}}}
    silently ignored the deeper singular "order" naming "TARGET"."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "orders": [{"id": "A", "ref_id": "OTHER"}],
            "data": {"order": {"id": "B", "ref_id": "TARGET", "state": "queued"}},
        }
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "TARGET")


def test_provable_ref_lookup_rejects_a_singular_order_on_an_intermediate_wrapper():
    """Codex review, PR #1169: "Validate singular orders on intermediate
    wrappers". An intermediate wrapper ABOVE the eventual row-bearing
    layer can carry its own singular "order" -- {"data": {"next_cursor":
    "P2", "data": {"order": {"id": "LIVE", "ref_id": "TARGET", "state":
    "queued"}, "data": {"orders": [{"id": "OTHER", "state":
    "rejected"}]}}}}} -- but the descent loop only ever inspected each
    node's "orders" field, silently walking straight past the singular
    order naming "TARGET" one layer up from the deeper "orders" list."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "next_cursor": "P2",
            "data": {
                "order": {"id": "LIVE", "ref_id": "TARGET", "state": "queued"},
                "data": {"orders": [{"id": "OTHER", "state": "rejected"}]},
            },
        }
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "TARGET")


def test_provable_ref_lookup_rejects_a_malformed_singular_order_during_cursor_scan():
    """Codex review, PR #1169: "Reject malformed singular orders during
    nested cursor scans". The reverse nesting of the fixed malformed
    outer-singular case: direct "orders" rows beside a nested cursor
    layer whose own "order" value is present but non-dict (e.g. a
    truncated string) -- {"orders": [{"id": "A"}], "data": {"order":
    "truncated"}}. The non-blank-dict conflict check alone let this
    malformed value fall through silently, leaving the direct rows alone
    provable even though part of this response's evidence was never
    actually readable."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "orders": [{"id": "A", "ref_id": "OTHER"}],
        "data": {"order": "truncated"},
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "TARGET")


def test_provable_ref_lookup_skips_falsey_reference_id_sentinels():
    """Codex review, PR #1168: "Skip falsey reference-ID sentinels before
    aliases". A falsey non-string sentinel (ref_id=0) alongside a valid
    client_order_id used to be accepted as the row's reference -- not
    blank per `_is_blank_broker_field` -- stringified into "0" instead of
    falling through to the real, usable alias. The target ref was hidden
    behind that fabricated sentinel, and a clean None match on "0" was
    misread as proof the claimed order was never submitted."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "orders": [
                {"id": "LIVE", "state": "queued", "ref_id": 0, "client_order_id": "crashed-fire"},
            ]
        }
    }
    found = RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id(
        "AAPL", day, "crashed-fire"
    )
    assert found is not None
    assert found["id"] == "LIVE"


def test_broker_same_day_lookup_terminates_on_a_cyclic_response():
    # Codex review, PR #1169: "Bound recursive data-layer traversal", and
    # its fresh-evidence follow-up "Fail closed when envelope descent
    # reaches its cap". A cyclic malformed mapping from an injected
    # orders_fn must not hang this lookup forever -- and since
    # `_all_data_layers`'s own OWN full-chain failure scan (independent of
    # whatever layer `_unwrap_seeking_order_evidence` settles on) always
    # hits the depth cap on a genuinely cyclic payload, this now correctly
    # fails closed (RuntimeError) rather than silently trusting a
    # necessarily-incomplete scan for hidden failure evidence.
    day = datetime(2026, 7, 6).date()
    cyclic: dict = {"orders": [{"id": "LIVE", "state": "queued"}]}
    cyclic["data"] = cyclic
    with pytest.raises(RuntimeError):
        RobinhoodBroker(orders_fn=lambda **k: cyclic).same_day_strategy_order_exists("AAPL", day)

    # A shape that genuinely cannot resolve without descending -- no
    # recognized evidence key at any depth -- must still terminate,
    # whatever verdict it reaches (the depth cap makes
    # `_unwrap_hides_contradicted_nested_data` fail closed here too).
    unresolvable: dict = {"unrecognized_wrapper_key": True}
    unresolvable["data"] = unresolvable
    with pytest.raises(RuntimeError):
        RobinhoodBroker(orders_fn=lambda **k: unresolvable).same_day_strategy_order_exists("AAPL", day)


def test_broker_buying_power_terminates_on_a_cyclic_response():
    # Codex review, PR #1169: "Bound recursive data-layer traversal", and
    # its fresh-evidence follow-up "Fail closed when envelope descent
    # reaches its cap". A cyclic response makes the full-chain failure
    # scan hit the depth cap every time, so this must fail closed to 0.0
    # (a truncated scan is not proof the lookup didn't fail) rather than
    # returning the early `buying_power` value found before the cap.
    cyclic: dict = {"buying_power": "100"}
    cyclic["data"] = cyclic
    result = RobinhoodBroker(buying_power_fn=lambda **k: cyclic).available_buying_power()
    assert result == 0.0


def test_all_data_layers_truncation_fails_closed_past_the_depth_cap():
    # Codex review, PR #1169: "Fail closed when envelope descent reaches
    # its cap". A response with more nested "data" wrappers than
    # `_MAX_ENVELOPE_DESCENT_DEPTH`, with an early affirmative `success:
    # true` layer and a `success: false`/error layer sitting PAST the cap,
    # must not have that deeper failure evidence silently ignored just
    # because the bounded walk stopped before reaching it.
    deep_failure: dict = {"success": False, "error": "down past the cap"}
    payload: dict = deep_failure
    for _ in range(60):
        payload = {"success": True, "data": payload}

    # available_buying_power: truncation must fail closed to 0.0, not
    # return whatever early balance a shallow layer happened to carry.
    payload_with_balance = {"success": True, "buying_power": "999", "data": payload}
    assert RobinhoodBroker(buying_power_fn=lambda **k: payload_with_balance).available_buying_power() == 0.0

    # _interpret_review: truncation must not read as an empty/approved
    # response.
    ok, reason = _interpret_review(payload)
    assert ok is False
    assert "max envelope depth" in reason


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


def test_broker_buying_power_rejects_a_failure_reached_through_a_success_wrapper():
    # Codex review, PR #1164: "Reject failed balance layers reached through
    # success wrappers". `_unwrap`/`_unwrap_traversed_layers` transparently
    # descends through an affirmative `success: true` wrapper and settles
    # on the deeper layer purely because it carries `buying_power` -- but
    # that settled layer can itself report `success: false`, and nothing
    # checked it before trusting the balance figure it also carries.
    resp = {"data": {"success": True, "data": {"success": False, "buying_power": "100"}}}
    assert RobinhoodBroker(buying_power_fn=lambda **k: resp).available_buying_power() == 0.0


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


def test_robinhood_broker_history_ref_id_sees_orders_nested_below_a_pagination_pointer():
    # Codex review, PR #1169: "Validate nested rows before rejecting a
    # provable page". `strategy_order_with_ref_id` (crash recovery) uses
    # `require_provable_history=True`, which rejects an "unrecognizable
    # page" via `_is_provable_orders_page` BEFORE `_iter_same_day_orders`'
    # own nested-row fallback ever runs. `_is_provable_orders_page` used
    # to read only the settled cursor layer's bare "orders" key, so a
    # valid page whose rows sit one level deeper (`{"next_cursor": "P2",
    # "data": {"orders": [...]}}`) was rejected as unrecognizable even
    # though the requested order is right there -- a crash lease could be
    # preserved indefinitely over a response that in fact proves the
    # order was submitted.
    resp = {
        "data": {
            "next_cursor": "PAGE2",
            "data": {"orders": [{"state": "cancelled", "ref_id": "crashed-fire"}]},
        }
    }
    order = RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order == resp["data"]["data"]["orders"][0]


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


def test_robinhood_broker_pagination_follows_a_nested_continuation_pointer():
    """Codex review, PR #1168: "Preserve nested continuation pointers with
    nested orders". Page one is wrapped as {"cursor": "P2", "data":
    {"orders": [...], "next_cursor": "P3"}} -- the settled (outer) layer
    carries a bare "cursor", so the ROWS sit one layer deeper, alongside
    their OWN genuine next_cursor="P3". Reading the cursor only from the
    outer settled layer missed that nested next_cursor entirely and could
    even misread the outer bare "cursor" as an echo of itself, ending
    pagination before page 3 -- where the actually-requested order sits --
    was ever reached."""
    pages = [
        {
            "cursor": "P2",
            "data": {
                "orders": [{"state": "filled", "ref_id": "other"}],
                "next_cursor": "P3",
            },
        },
        {"data": {"orders": [{"state": "cancelled", "ref_id": "crashed-fire"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "P3"]
    assert order == pages[1]["data"]["orders"][0]


def test_robinhood_broker_pagination_carries_an_intervening_continuation_pointer():
    """Codex review, PR #1168: "Carry intervening pagination cursors
    forward". Page one is {"orders": [], "data": {"next_cursor": "P3",
    "data": {"orders": [...]}}}} -- the outer settled layer's own
    "orders": [] is a stale placeholder (not the real rows), the
    row-bearing layer TWO levels down carries no cursor of its own, and
    the one genuine next_cursor="P3" sits on the INTERVENING layer
    between them. Checking only the row-bearing layer and the outer
    settled layer (as an earlier, narrower fix did) skips that
    intervening layer and loses the real continuation pointer entirely."""
    pages = [
        {
            "orders": [],
            "data": {
                "next_cursor": "P3",
                "data": {"orders": [{"state": "filled", "ref_id": "other"}]},
            },
        },
        {"data": {"orders": [{"state": "cancelled", "ref_id": "crashed-fire"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "P3"]
    assert order == pages[1]["data"]["orders"][0]


def test_robinhood_broker_pagination_retains_the_requested_cursor_during_fallback():
    """Codex review, PR #1168: "Retain the requested cursor during
    fallback parsing". Page P2 has nested rows with NO cursor of their
    own, while the outer page echoes back the SAME cursor ("P2") it was
    requested with -- a genuine end-of-list signal. Computing the
    row-bearing layer's own cursor first (finding nothing -- None) and
    then reusing that None, instead of the real requested "P2", as the
    current_cursor argument for the outer-layer fallback made the echo
    look like a FRESH continuation instead of the end-of-list signal it
    is, requesting "P2" again and raising "repeated a pagination cursor"
    instead of cleanly completing the scan with no match found."""
    pages = [
        {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": "P2"}},
        {"cursor": "P2", "data": {"orders": [{"state": "filled", "ref_id": "also-not-it"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "P2"]
    assert order is None


def test_robinhood_broker_pagination_reuses_the_requested_cursor_for_the_outer_layer_echo():
    """Codex review, PR #1169: "Reuse the requested cursor when checking the
    outer layer". Page 2 (requested with cursor="PAGE2") wraps its rows one
    layer deeper than a bare "cursor" field that merely ECHOES the requested
    page: {"data": {"cursor": "PAGE2", "data": {"orders": [...]}}}}. The
    row-bearing (inner) layer has no continuation info of its own, so the
    lookup falls back to the outer layer's cursor field -- but it used to
    compare that outer field against the ALREADY-REASSIGNED `cursor`
    variable (None, from the first, row-bearing-layer lookup) instead of the
    cursor this page was actually requested with, so "PAGE2" != None looked
    like a brand-new continuation rather than the end-of-list echo it is.
    The next iteration then re-requested "PAGE2" and tripped the
    repeated-cursor guard, failing closed on a perfectly valid, complete
    two-page history."""
    pages = [
        {"data": {"orders": [{"id": "order-1", "state": "filled"}], "next_cursor": "PAGE2"}},
        {"data": {"cursor": "PAGE2", "data": {"orders": [{"id": "order-2", "state": "filled"}]}}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    broker = RobinhoodBroker(orders_fn=orders_fn)
    orders = list(broker._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))

    assert [o["id"] for o in orders] == ["order-1", "order-2"]
    assert seen_cursors == [None, "PAGE2"], "must not re-request PAGE2 a second time"


def test_robinhood_broker_pagination_retains_cursors_from_intermediate_order_wrappers():
    """Codex review, PR #1169: "Retain cursors from intermediate order
    wrappers". Page 2 (requested with cursor="P1") echoes "P1" on its OUTER
    settled layer, but the REAL continuation ("P2") sits on an INTERMEDIATE
    wrapper between that outer layer and the row-bearing layer:
    {"data": {"cursor": "P1", "data": {"next_cursor": "P2", "data":
    {"orders": [...]}}}}}. The row-bearing (innermost) layer has no
    continuation info of its own, and checking only it and then the outer
    settled layer skipped the intermediate wrapper's own genuine
    next_cursor="P2" entirely, silently ending pagination one page short --
    omitting page 3's live order from the ordinary duplicate gate."""
    pages = [
        {"data": {"orders": [{"id": "order-1", "state": "filled"}], "next_cursor": "P1"}},
        {
            "data": {
                "cursor": "P1",
                "data": {"next_cursor": "P2", "data": {"orders": [{"id": "order-2", "state": "filled"}]}},
            }
        },
        {"data": {"orders": [{"id": "order-3", "state": "filled"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    broker = RobinhoodBroker(orders_fn=orders_fn)
    orders = list(broker._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))

    assert [o["id"] for o in orders] == ["order-1", "order-2", "order-3"]
    assert seen_cursors == [None, "P1", "P2"], "must follow the intermediate wrapper's own next_cursor"


def test_robinhood_broker_pagination_rejects_a_malformed_direct_orders_collection():
    """Codex review, PR #1168: "Reject malformed direct order collections
    before descending". A malformed DIRECT "orders" value on the settled
    layer -- present, but not a list at all -- is unreadable evidence on
    that layer itself, not an absent/empty placeholder a nested chain is
    free to override. Falling through to a nested non-empty list let this
    unparseable direct value be silently discarded in favor of the nested
    rows, reading the page as provably complete and letting
    strategy_order_with_ref_id() return None for a missing ref even though
    the settled layer's own evidence was never actually readable."""
    pages = [
        {"data": {"orders": "truncated", "data": {"orders": [{"state": "filled", "ref_id": "other"}]}}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_preserves_cursors_traversed_while_resolving_empty_pages():
    """Codex review, PR #1168: "Preserve cursors traversed while resolving
    empty pages". Page P2 (requested with cursor="P2") echoes that SAME
    cursor on its outer settled layer -- a genuine end-of-list signal for
    the outer layer alone -- but an INTERMEDIATE wrapper between the outer
    layer and the (empty) row-bearing layer carries its own genuine
    next_cursor="P3": {"cursor": "P2", "data": {"next_cursor": "P3",
    "data": {"orders": []}}}}. Collapsing the traversed-layers list to just
    the outer settled layer whenever the effective orders collection ended
    up empty lost that intermediate P3 pointer entirely, concluding
    history ended at P2 while a live order still sat on P3."""
    pages = [
        {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": "P2"}},
        {"data": {"cursor": "P2", "data": {"next_cursor": "P3", "data": {"orders": []}}}},
        {"data": {"orders": [{"state": "filled", "ref_id": "crashed-fire"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "P2", "P3"], "must not stop at P2's echo without following intermediate P3"
    assert order == pages[2]["data"]["orders"][0]


def test_robinhood_broker_pagination_rejects_conflicting_continuation_cursors():
    """Codex review, PR #1168: "Reject conflicting continuation cursors
    across wrappers". Two layers along the SAME traversal each carry their
    own genuine (non-echo) continuation pointer that DISAGREES with the
    other's: an inner next_cursor="P3" alongside an outer next_cursor="P4".
    Silently taking whichever layer happens to be checked first let the
    scan follow one of the two pointers while an order sitting on the
    OTHER page was never reached -- proving a ref absent from a truncated
    view of the real history. Genuinely conflicting evidence must fail
    closed, not pick a winner."""
    pages = [
        {
            "data": {
                "next_cursor": "P4",
                "data": {"next_cursor": "P3", "data": {"orders": [{"state": "filled", "ref_id": "other"}]}},
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="conflicting pagination cursors"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_rejects_conflicting_continuation_cursors_direct_vs_nested():
    """Codex review, PR #1169: "Reject conflicting continuation cursors".
    The outer settled layer advertises next_cursor="P3" while its
    row-bearing child independently advertises a DIFFERENT next_cursor=
    "P2" -- two genuine (non-echo) continuations that disagree. Silently
    following whichever layer was checked first abandoned the OTHER
    page's live orders with no signal anything was skipped, and
    same_day_strategy_order_exists could finish after the wrong page and
    authorize a duplicate purchase. Must fail closed instead of picking
    one by layer order."""
    pages = [
        {
            "data": {
                "next_cursor": "P3",
                "data": {"next_cursor": "P2", "data": {"orders": [{"id": "order-1", "state": "filled"}]}},
            }
        },
    ]

    broker = RobinhoodBroker(orders_fn=lambda **k: pages[0])
    with pytest.raises(RuntimeError, match="conflicting pagination cursors"):
        list(broker._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))


def test_robinhood_broker_pagination_inspects_nested_cursors_even_with_direct_rows():
    """Codex review, PR #1169: "Inspect nested cursors even with direct
    rows". Page 1 exposes its rows DIRECTLY on the settled layer, but puts
    its own continuation one wrapper deeper: {"orders": [...], "data":
    {"next_cursor": "P2"}}}. The direct-rows early return used to record
    only that one layer, so the cursor scan never saw the nested
    next_cursor="P2" -- silently ending pagination after page one even
    though a live order sits on page two."""
    pages = [
        {"data": {"orders": [{"id": "order-1", "state": "filled"}], "data": {"next_cursor": "P2"}}},
        {"data": {"orders": [{"id": "order-2", "state": "filled"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    broker = RobinhoodBroker(orders_fn=orders_fn)
    orders = list(broker._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))

    assert [o["id"] for o in orders] == ["order-1", "order-2"]
    assert seen_cursors == [None, "P2"], "must follow the nested cursor even though rows were direct"


def test_robinhood_broker_pagination_keeps_scanning_cursors_below_nested_order_rows():
    """Codex review, PR #1168: "Keep scanning cursors below nested order
    rows". Page 2 (requested with cursor="P1") echoes "P1" on its OUTER
    settled layer, but its OWN rows sit nested one wrapper deeper, and
    that row-bearing layer's own continuation sits ANOTHER wrapper deeper
    still: {"cursor": "P1", "data": {"orders": [...], "data":
    {"next_cursor": "P2"}}}}. Returning as soon as the nested rows were
    found discarded that deeper next_cursor="P2" entirely, letting the
    outer layer's echoed "P1" read as the end of history even though a
    live order sits on page 3."""
    pages = [
        {"data": {"orders": [{"id": "order-1", "state": "filled"}], "next_cursor": "P1"}},
        {
            "data": {
                "cursor": "P1",
                "data": {"orders": [{"id": "order-2", "state": "filled"}], "data": {"next_cursor": "P2"}},
            }
        },
        {"data": {"orders": [{"id": "order-3", "state": "filled"}]}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    broker = RobinhoodBroker(orders_fn=orders_fn)
    orders = list(broker._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))

    assert [o["id"] for o in orders] == ["order-1", "order-2", "order-3"]
    assert seen_cursors == [None, "P1", "P2"], "must follow the deeper cursor sitting below the nested rows"


def test_provable_ref_lookup_rejects_a_malformed_collection_below_nested_order_rows():
    """Codex review, PR #1169: "Validate collections below nested order
    rows". Page 2's rows sit nested one wrapper deeper on the settled
    layer, and a layer walked purely for cursor evidence BELOW that
    row-bearing layer itself carries a malformed (non-list) "orders"
    value: {"cursor": "P1", "data": {"orders": [...], "data": {"orders":
    "truncated"}}}}. Trusting the already-found nested rows and ignoring
    that deeper malformed collection let a genuinely unreadable layer pass
    as though it had nothing to say -- must reject the whole page instead."""
    day = datetime(2026, 7, 6).date()
    resp = {
        "data": {
            "cursor": "P1",
            "data": {"orders": [{"id": "LIVE", "state": "queued"}], "data": {"orders": "truncated"}},
        }
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id("AAPL", day, "crashed-fire")


def test_robinhood_broker_pagination_rejects_malformed_nested_order_collections():
    """Codex review, PR #1168: "Reject malformed nested order collections".
    A settled page carries valid DIRECT rows, but the nested layer walked
    purely for cursor evidence has its OWN malformed "orders" value (a
    truncated string, not a list): {"orders": [other_order], "data":
    {"orders": "truncated"}}}. Trusting the direct rows alone and only
    walking the nested layer for cursors let this malformed nested
    collection go unnoticed, reading the page as fully provable even
    though part of its own evidence was never actually readable."""
    pages = [
        {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "data": {"orders": "truncated"}}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_fails_closed_when_envelope_descent_hits_its_limit():
    """Codex review, PR #1168: "Fail closed when envelope descent hits its
    limit". A settled page's DIRECT "orders" is an empty list, and the
    nested "data" chain searched for a non-empty replacement is CYCLIC --
    the search hits `_MAX_ENVELOPE_DESCENT_DEPTH` without ever finding a
    non-empty nested list, or ruling one out. Returning the direct empty
    list here (as the ordinary natural-end-of-chain case does) let this
    truncated, only-partially-inspected page read as PROVABLY empty --
    real rows past the depth cap were never actually examined."""
    cyclic: dict = {"orders": []}
    cyclic["data"] = cyclic
    response = {"data": cyclic}

    # A cyclic envelope also trips the unrelated, full-depth failure scan
    # (`_all_data_layers`) that runs before this page-provability check --
    # both are legitimate fail-closed reactions to a truncated, cannot-
    # rule-out-hidden-evidence envelope, so either message is acceptable
    # here; what matters is that this raises rather than reading the
    # direct empty list as a provably empty page.
    with pytest.raises(RuntimeError, match="exceeded the max envelope depth|unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: response).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_fails_closed_when_cursor_only_descent_hits_its_limit():
    """Codex review, PR #1168: "Fail closed when cursor-only descent
    reaches its limit". A settled page's DIRECT "orders" is a genuinely
    NON-EMPTY list -- real, positive evidence -- but the nested "data"
    chain searched purely for a CONTINUATION CURSOR beyond those rows is
    CYCLIC. Returning the already-found direct rows here (as the
    ordinary natural-end-of-chain case does) marked the page fully
    provable even though a continuation cursor -- or a matching order on
    a deeper layer this scan never reached -- can still exist beyond the
    depth cap, letting a search for a DIFFERENT ref (not present in the
    visible direct row) conclude "no match" and release a recovery claim
    instead of failing closed."""
    cyclic: dict = {}
    cyclic["data"] = cyclic
    response = {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "data": cyclic}}

    with pytest.raises(RuntimeError, match="exceeded the max envelope depth|unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: response).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_rejects_conflicting_nested_order_lists():
    """Codex review, PR #1168: "Reject conflicting nested order lists". A
    settled page has non-empty order collections at BOTH the direct AND a
    nested layer, and they disagree: {"orders": [other], "data": {"orders":
    [matching_order]}}}. Treating the direct list as sole authority and
    walking the nested layer only for cursor evidence silently discarded
    the nested list's genuinely different rows -- including a match for
    the ref this lookup is searching for -- letting recovery release a
    claim and authorize a duplicate purchase. Two disagreeing non-empty
    collections is unreconcilable, ambiguous evidence and must fail
    closed, not silently pick the direct side."""
    pages = [
        {
            "data": {
                "orders": [{"state": "filled", "ref_id": "other"}],
                "data": {"orders": [{"state": "filled", "ref_id": "crashed-fire"}]},
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_validates_outer_order_collections_before_unwrapping():
    """Codex review, PR #1169: "Validate outer order collections before
    unwrapping". A response carries order evidence at BOTH its own OUTER
    top level AND under a sibling "data" key -- {"orders": [live],
    "data": {"orders": [other]}} -- not one level deeper as the sibling
    test above covers. `_unwrap_seeking_order_evidence`'s unconditional
    first descent into "data" used to discard the outer "orders"
    entirely without ever comparing it, silently losing the live target
    order. Now the outer layer's own genuine evidence is checked before
    ever descending, so this fails closed as an unrecognizable page
    (matching the established "conflicting nested order collections"
    behavior) instead of quietly returning None for a ref the response
    itself named."""
    resp = {
        "orders": [{"id": "LIVE", "ref_id": "TARGET", "state": "queued"}],
        "data": {"orders": [{"id": "OTHER", "state": "rejected"}]},
    }
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=lambda **k: resp).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "TARGET"
        )
    # When the outer and nested collections actually AGREE, the lookup
    # must still succeed -- this fix must not turn every such shape into
    # a false conflict.
    agreeing_resp = {
        "orders": [{"id": "LIVE", "ref_id": "TARGET", "state": "queued"}],
        "data": {"orders": [{"id": "LIVE", "ref_id": "TARGET", "state": "queued"}]},
    }
    order = RobinhoodBroker(orders_fn=lambda **k: agreeing_resp).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "TARGET"
    )
    assert order is not None


def test_robinhood_broker_pagination_tolerates_a_redundant_nested_order_list_with_a_different_id_encoding():
    """Codex review, PR #1169: "Compare redundant order collections
    semantically". A connector mirroring the SAME broker order across a
    direct and a nested "orders" collection can spell one row's
    identifier field in a different SUPPORTED numeric encoding on each
    layer (id=123 directly, id="123" nested) -- neither is a genuine
    disagreement, and must not fail the page closed the way a truly
    conflicting nested collection does."""
    pages = [
        {
            "data": {
                "orders": [{"id": 123, "state": "filled", "ref_id": "crashed-fire"}],
                "data": {"orders": [{"id": "123", "state": "filled", "ref_id": "crashed-fire"}]},
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order is not None


def test_robinhood_broker_pagination_tolerates_a_redundant_nested_order_list_with_different_key_insertion_order():
    """Codex review, PR #1169: "Canonicalize mapping key order before
    multiset sorting". Sorting canonicalized rows by bare `repr()` still
    depends on each dict's insertion order -- two equivalent rows
    ({"id": "1", "state": "z"} vs. the same pair with "state" inserted
    before "id") render to different repr strings and sort into a
    different relative order, so two otherwise-identical collections
    compared unequal even though dict equality proves they match."""
    pages = [
        {
            "data": {
                "orders": [
                    {"id": "1", "state": "z", "ref_id": "crashed-fire"},
                    {"state": "a", "id": "2"},
                ],
                "data": {
                    "orders": [
                        {"state": "a", "id": "2"},
                        {"state": "z", "id": "1", "ref_id": "crashed-fire"},
                    ]
                },
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order is not None


def test_robinhood_broker_pagination_rejects_conflicting_order_lists_below_nested_rows():
    """Codex review, PR #1168: "Reject conflicting order lists below nested
    rows". A settled page's rows sit NESTED (no direct "orders"), and a
    layer BELOW that row-bearing one has its OWN, genuinely different
    non-empty "orders" list: {"next_cursor": "P2", "data": {"orders":
    [other], "data": {"orders": [matching_order]}}}}. Walking that deeper
    layer only for cursor evidence silently discarded its conflicting rows
    -- including a match for the ref this lookup is searching for --
    letting recovery follow P2 and return None for an order that was
    actually present in this very response."""
    pages = [
        {
            "data": {
                "next_cursor": "P2",
                "data": {
                    "orders": [{"state": "filled", "ref_id": "other"}],
                    "data": {"orders": [{"state": "filled", "ref_id": "crashed-fire"}]},
                },
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_rejects_nested_singular_order_beside_direct_rows():
    """Codex review, PR #1168: "Reject nested singular orders beside direct
    history rows". A settled page's rows sit DIRECT -- {"orders": [A]} --
    while a layer BELOW that one carries its OWN non-empty SINGULAR "order"
    (not a collection): {"orders": [A], "data": {"order": {"id": "B",
    "ref_id": "crashed-fire", "state": "queued"}}}. The cursor-only descent
    loop only ever checked nested "orders" collections for malformed/
    conflicting evidence, silently ignoring this singular-order sibling key
    entirely -- so the page was accepted on the direct collection alone and
    a live order matching the searched-for ref_id, sitting one layer down,
    was never seen."""
    pages = [
        {
            "orders": [{"state": "filled", "ref_id": "other"}],
            "data": {"order": {"id": "B", "ref_id": "crashed-fire", "state": "queued"}},
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_unwrap_seeking_order_evidence_stops_on_malformed_outer_orders():
    """Codex review, PR #1168: "Reject malformed outer order evidence
    before descending". A present but malformed outer "orders" value
    (a string, not a list) used to fall through the well-shaped-nonempty
    check and be silently discarded by the unconditional descent into
    "data" -- {"orders": "truncated", "data": {"orders": []}} unwrapped to
    the nested empty list, and _is_provable_orders_page() read that as a
    clean, provable empty page even though the outer evidence was never
    actually readable."""
    payload = {"orders": "truncated", "data": {"orders": []}}
    assert _unwrap_seeking_order_evidence(payload) == payload

    payload_order = {"order": "truncated", "data": {"orders": []}}
    assert _unwrap_seeking_order_evidence(payload_order) == payload_order


def test_unwrap_seeking_order_evidence_still_descends_past_empty_outer_orders():
    """Companion to the above: a well-shaped but EMPTY outer "orders"/
    "order" value is the ordinary "nothing here yet" case, not malformed
    evidence -- descent into "data" must still proceed."""
    payload = {"orders": [], "data": {"orders": [{"id": "LIVE"}]}}
    assert _unwrap_seeking_order_evidence(payload) == {"orders": [{"id": "LIVE"}]}


def test_is_provable_orders_page_rejects_an_empty_row():
    """Codex review, PR #1169: "Reject empty rows in provable order
    pages". A row that is a genuinely EMPTY mapping (`{}`) carries no
    order evidence at all -- the broker truncated or omitted the actual
    data rather than describing a real (if unmatched) order. Declaring
    the page provable purely because it is a list let a caller like
    `strategy_order_with_ref_id()` scan straight over that empty
    placeholder and report a clean "no match" even though the sole row
    was truncated and could have represented the target."""
    assert _is_provable_orders_page({"orders": [{}]}) is False
    assert _is_provable_orders_page({"orders": [{}, {"id": "A"}]}) is False
    # An ordinary empty PAGE (no rows at all) is unaffected.
    assert _is_provable_orders_page({"orders": []}) is True
    # A page with only genuine rows is unaffected.
    assert _is_provable_orders_page({"orders": [{"id": "A"}]}) is True


def test_is_provable_orders_page_rejects_a_nonempty_identity_free_row():
    """Codex review, PR #1169: "Reject nonempty rows without any order
    identity". A row that is nonempty but carries no usable broker
    id/order_id alias AND no usable reference alias (`{"state":
    "queued"}`, `{"error": "truncated"}`) carries no order evidence
    either -- reference matching has nothing to compare it against, so
    it can never be proven to be (or not be) the target order."""
    assert _is_provable_orders_page({"orders": [{"state": "queued"}]}) is False
    assert _is_provable_orders_page({"orders": [{"error": "truncated"}]}) is False
    # A row with a usable reference alias (but no id) still counts.
    assert _is_provable_orders_page({"orders": [{"ref_id": "TARGET", "state": "queued"}]}) is True
    # A row with a usable id (but no ref alias) still counts.
    assert _is_provable_orders_page({"orders": [{"id": "A", "state": "queued"}]}) is True


def test_is_provable_orders_page_rejects_a_row_with_only_an_unusable_id_sentinel():
    """Codex review, PR #1169: "Require a genuinely usable identity on
    every history row". `_first_usable_order_id_alias` only screens
    blank/falsey values, so a known-UNUSABLE sentinel (`id=True`,
    `id=-1`, `id=NaN`, `id="0"`) with no usable reference alias used to
    read as a genuine identity, when it names no real order at all."""
    assert _is_provable_orders_page({"orders": [{"id": True, "state": "queued"}]}) is False
    assert _is_provable_orders_page({"orders": [{"id": -1, "state": "queued"}]}) is False
    assert _is_provable_orders_page({"orders": [{"id": float("nan"), "state": "queued"}]}) is False
    assert _is_provable_orders_page({"orders": [{"id": "0", "state": "queued"}]}) is False
    # A genuinely usable id still counts.
    assert _is_provable_orders_page({"orders": [{"id": "REAL-1", "state": "queued"}]}) is True


def test_canonicalized_order_row_preserves_conflicting_identifier_aliases():
    """Codex review, PR #1168: "Preserve conflicting identifier aliases
    during comparison". A row whose own "id"/"order_id" aliases DISAGREE
    ({"id": "A", "order_id": "B"}) used to collapse onto the same single
    canonical "id" a plain {"id": "A"} row does, via _first_usable_order_
    id_alias picking only the preferred alias and silently discarding
    "B" -- erasing the conflicted row's own distinct identity from the
    multiset comparison."""
    plain = {"id": "A"}
    conflicted = {"id": "A", "order_id": "B"}
    assert _canonicalized_order_row(plain) != _canonicalized_order_row(conflicted)
    # A row with no conflict still collapses onto a unified "id" key, the
    # ordinary case this function exists to handle.
    assert _canonicalized_order_row({"id": "A"}) == _canonicalized_order_row({"order_id": "A"})


def test_order_collections_semantically_agree_ignores_dict_key_order():
    """Codex review, PR #1168: "Compare row multisets independently of
    dictionary key order". A bare `repr(row)` reflects a dict's own key
    INSERTION order (`repr({"a": 1, "z": 2}) != repr({"z": 2, "a": 1})`
    even though the two dicts are `==`), so using it directly as a
    multiset sort key let two redundant envelope layers carrying the
    identical rows -- just spelled with a different key order in their
    own JSON -- sort those rows differently RELATIVE TO A THIRD row,
    misaligning the sorted lists and reporting a spurious conflict."""
    a = [{"a": 1, "z": 2}, {"m": 3}]
    b = [{"z": 2, "a": 1}, {"m": 3}]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuine content difference is still caught.
    assert _order_collections_semantically_agree(a, [{"a": 1, "z": 2}, {"m": 4}]) is False


def test_order_collections_semantically_agree_ignores_nested_dict_key_order():
    """Codex review, PR #1169: "Canonicalize nested mappings before
    sorting order rows". The top-level fix above still embedded a NESTED
    dict value through its own insertion-order-sensitive `repr()` --
    `{"id": "1", "meta": {"a": 1, "z": 2}}` and the equivalent row with
    "meta" inserted `{"z": 2, "a": 1}`, sitting beside a second row whose
    own nested key sorts between "a" and "z", could sort into different
    relative positions and report a spurious conflict even though the
    collections agree as multisets. `_dict_order_independent_sort_key`
    recurses into nested dict/list values, not just the top-level row."""
    a = [
        {"id": "1", "meta": {"a": 1, "z": 2}},
        {"id": "2", "meta": {"m": 1}},
    ]
    b = [
        {"id": "2", "meta": {"m": 1}},
        {"id": "1", "meta": {"z": 2, "a": 1}},
    ]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuine difference in the nested mapping is still caught.
    c = [
        {"id": "2", "meta": {"m": 1}},
        {"id": "1", "meta": {"z": 2, "a": 99}},
    ]
    assert _order_collections_semantically_agree(a, c) is False


def test_robinhood_broker_pagination_compares_deeper_order_collections_semantically():
    """Codex review, PR #1168: "Compare deeper order collections
    semantically". Companion to the conflicting-lists-below-nested-rows
    test above: when the deeper layer's "orders" list names the SAME rows
    as the row-bearing layer's own -- just with a different SUPPORTED
    numeric ID encoding (`id=123` vs. `id="123"`) -- that is not a genuine
    conflict, the same reasoning `_order_collections_semantically_agree`
    already applies to the DIRECT-rows branch. The old raw `!=` list
    comparison here reported these as conflicting anyway, raising on an
    otherwise fully provable page."""
    pages = [
        {
            "data": {
                "next_cursor": "P2",
                "data": {
                    "orders": [{"id": 123, "state": "filled", "ref_id": "crashed-fire"}],
                    "data": {"orders": [{"id": "123", "state": "filled", "ref_id": "crashed-fire"}]},
                },
            }
        },
        {"data": {"orders": []}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert order is not None


def test_robinhood_broker_pagination_rejects_an_unusable_cursor_even_with_a_valid_one_elsewhere():
    """Codex review, PR #1168: "Reject unusable cursors even when another
    layer has a valid one". One traversed layer reports a genuine
    next_cursor="P2" while ANOTHER independently reports an unusable
    continuation (next_cursor=0, which _next_order_cursor cannot parse).
    Finding the usable "P2" first used to skip the unusable-pointer check
    entirely, following P2 and proving a ref absent even though the other
    layer explicitly advertised history that could not be followed."""
    pages = [
        {"data": {"next_cursor": "P2", "data": {"next_cursor": 0, "data": {"orders": [{"state": "filled", "ref_id": "other"}]}}}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unusable pagination pointer"):
        list(RobinhoodBroker(orders_fn=orders_fn)._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))


def test_robinhood_broker_pagination_rejects_an_unusable_next_url_beside_a_valid_next_cursor():
    """Codex review, PR #1169: "Reject unusable cursor aliases beside a
    valid one". A single layer carrying BOTH a valid next_cursor="P2" AND
    a next URL with no cursor query param looked entirely usable --
    _next_order_cursor prefers next_cursor and returns "P2" -- silently
    hiding that the SAME layer also advertised a second continuation
    (the next URL) it could not interpret. The scan then followed "P2"
    as if it were the layer's only signal and could prove a same-day
    reference absent from a page that was never actually the last one."""
    pages = [
        {
            "data": {
                "next_cursor": "P2",
                "next": "https://example.com/orders/?other=x",
                "orders": [{"state": "filled", "ref_id": "other"}],
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unusable pagination pointer"):
        list(RobinhoodBroker(orders_fn=orders_fn)._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))


def test_robinhood_broker_pagination_rejects_a_next_url_with_a_blank_secondary_cursor_value():
    """Codex review, PR #1169: "Validate every cursor value in next URLs".
    Fresh evidence beyond the fixed valid-next_cursor/unusable-next case:
    a next URL repeating the cursor query parameter with one usable value
    and one blank one ("...?cursor=P2&cursor=%20"). _has_unusable_next_url_
    cursor used to inspect only cursor_values[0] ("P2"), so it read the
    URL as entirely usable and never surfaced the blank second value --
    the same "there is more we cannot fully interpret" signal a wholly
    unusable next URL already raises for."""
    pages = [
        {
            "data": {
                "next": "https://example.com/orders/?cursor=P2&cursor=%20",
                "orders": [{"state": "filled", "ref_id": "other"}],
            }
        },
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    with pytest.raises(RuntimeError, match="unusable pagination pointer"):
        list(RobinhoodBroker(orders_fn=orders_fn)._iter_same_day_orders("AAPL", datetime(2026, 7, 28).date()))


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


def test_robinhood_broker_pagination_falls_through_falsey_primary_ids_while_deduplicating():
    """Codex review, PR #1169: "Fall through falsey primary IDs while
    deduplicating history". `id=0` is not BLANK per `_is_blank_broker_
    field` (only None/""/whitespace reads as blank), so two DIFFERENT
    rows -- a rejected one and a still-live one -- each carrying the
    sentinel `id=0` alongside their own distinct `order_id` were
    deduplicated together under the shared key "0", and the live row
    could be silently discarded. `same_day_strategy_order_exists` must
    still see the live order even though it comes after a same-id-0
    rejected row."""
    resp = {
        "data": {
            "orders": [
                {"id": 0, "order_id": "REJECT", "state": "rejected"},
                {"id": 0, "order_id": "LIVE", "state": "queued"},
            ]
        }
    }
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
        "AAPL", datetime(2026, 7, 28).date()
    ) is True


def test_robinhood_broker_pagination_canonicalizes_numeric_ids_before_deduplicating():
    """Codex review, PR #1168: "Canonicalize numeric IDs before pagination
    deduplication". A broker repeating an order at a pagination boundary
    but changing its numeric encoding (id=123 on one page, id=123.0 on
    the next) used to stringify to different dedup keys, so both rows
    survived deduplication and same_day_ticker_notional_usd() summed the
    same order twice."""
    resp = {
        "data": {
            "orders": [
                {"id": 123, "order_id": "IGNORED-1", "state": "filled", "cumulative_quantity": "1"},
                {"id": 123.0, "order_id": "IGNORED-2", "state": "filled", "cumulative_quantity": "1"},
            ]
        }
    }
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    orders = broker._same_day_orders("AAPL", datetime(2026, 7, 28).date())
    assert len(orders) == 1, "id=123 and id=123.0 must dedupe to the same order"


def test_robinhood_broker_pagination_canonicalizes_numeric_string_ids_before_deduplicating():
    """Codex review, PR #1169: "Canonicalize numeric-string IDs during
    pagination dedup". Fresh evidence beyond the fixed native-int/native-
    float case: a broker repeating an order as id=123 on one page and
    id="123.0" (a numeric-STRING spelling of the same integral value) on
    another. The float-only canonicalization above catches id=123.0 but
    not the string spelling "123.0", so the keys became "123" and "123.0"
    and both rows survived deduplication, letting
    same_day_ticker_notional_usd() count the same order twice."""
    resp = {
        "data": {
            "orders": [
                {"id": 123, "order_id": "IGNORED-1", "state": "filled", "cumulative_quantity": "1"},
                {"id": "123.0", "order_id": "IGNORED-2", "state": "filled", "cumulative_quantity": "1"},
            ]
        }
    }
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    orders = broker._same_day_orders("AAPL", datetime(2026, 7, 28).date())
    assert len(orders) == 1, 'id=123 and id="123.0" must dedupe to the same order'


def test_robinhood_broker_pagination_falls_through_non_scalar_primary_ids_while_deduplicating():
    """Codex review, PR #1168: "Validate primary IDs before deduplicating
    history". A malformed row's primary `id` can be a non-scalar (a list)
    -- present and truthy, so neither the blank nor the falsey check
    catches it, but stringifying it into "['BAD']" made two UNRELATED
    rows sharing that identical non-scalar `id` (a rejected one and a
    still-live one, each with their own distinct `order_id`) deduplicate
    together under that shared fabricated key, silently discarding the
    live order. `same_day_strategy_order_exists` must still see the live
    order even though it comes after a same-malformed-id rejected row."""
    resp = {
        "data": {
            "orders": [
                {"id": ["BAD"], "order_id": "REJECT", "state": "rejected"},
                {"id": ["BAD"], "order_id": "LIVE", "state": "queued"},
            ]
        }
    }
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
        "AAPL", datetime(2026, 7, 28).date()
    ) is True


def test_robinhood_broker_pagination_falls_through_unusable_numeric_primary_ids_while_deduplicating():
    """Codex review, PR #1168: "Validate numeric primary IDs before
    deduplicating". A truthy numeric sentinel -- `id=-1` or
    `id=float("nan")` -- passes the blank/falsey/non-scalar checks alone,
    even though this module's own placement/reconciliation predicates
    already reject both as unusable. A rejected row and a queued row
    sharing that identical sentinel, each with their own distinct valid
    `order_id` alias, must not be deduplicated together under the
    sentinel, dropping the queued row."""
    for _sentinel in (-1, float("nan")):
        resp = {
            "data": {
                "orders": [
                    {"id": _sentinel, "order_id": "REJECT", "state": "rejected"},
                    {"id": _sentinel, "order_id": "LIVE", "state": "queued"},
                ]
            }
        }
        assert RobinhoodBroker(orders_fn=lambda resp=resp, **k: resp).same_day_strategy_order_exists(
            "AAPL", datetime(2026, 7, 28).date()
        ) is True, f"sentinel {_sentinel!r} must not mask the live row"


def test_robinhood_broker_pagination_falls_through_stringified_numeric_sentinels_while_deduplicating():
    """Codex review, PR #1168: "Reject stringified sentinels before
    deduplicating history". A connector can serialize its numeric
    placeholder as a STRING (`id="-1"`), not just a Python int/float --
    the numeric-typed sentinel check alone missed this, so a rejected row
    and a queued row sharing that identical stringified sentinel, each
    with their own distinct valid `order_id` alias, still deduplicated
    together under it, dropping the queued row."""
    resp = {
        "data": {
            "orders": [
                {"id": "-1", "order_id": "REJECT", "state": "rejected"},
                {"id": "-1", "order_id": "LIVE", "state": "queued"},
            ]
        }
    }
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
        "AAPL", datetime(2026, 7, 28).date()
    ) is True


def test_robinhood_broker_pagination_falls_through_stringified_zero_sentinels_while_deduplicating():
    """Codex review, PR #1168: "Reject stringified zero IDs before
    history deduplication". A stringified ZERO (`id="0"`) is truthy and
    does not start with "-", so it survived the sentinel check above and
    a rejected row sharing that placeholder with a queued row (each with
    their own distinct valid `order_id` alias) still deduplicated
    together under it, dropping the queued row."""
    resp = {
        "data": {
            "orders": [
                {"id": "0", "order_id": "REJECT", "state": "rejected"},
                {"id": "0", "order_id": "LIVE", "state": "queued"},
            ]
        }
    }
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
        "AAPL", datetime(2026, 7, 28).date()
    ) is True


def test_all_order_cursor_aliases_collects_every_conflicting_cursor_in_a_next_url():
    """Codex review, PR #1168: "Reject conflicting cursor values in a next
    URL". When a malformed continuation URL carries more than one
    distinct `cursor` query parameter (`?cursor=P2&cursor=P3`), this
    collector previously kept only the FIRST value ("P2") -- the
    pagination scan then followed P2 and could return None without ever
    inspecting P3, letting a recovery caller treat that truncated result
    as proof a claimed order was absent. Every usable value must be
    collected so the cross-layer conflict check downstream can see all
    of them."""
    import dca_live_adapters

    aliases = dca_live_adapters._all_order_cursor_aliases({"next": "https://x/orders?cursor=P2&cursor=P3"})
    assert aliases == ["P2", "P3"]


def test_first_usable_order_id_alias_falls_through_a_falsey_primary_placeholder():
    """Codex review, PR #1168: "Fall through falsey primary order-ID
    placeholders". `_is_blank_broker_field()` does not consider numeric
    zero (or another falsey non-string value) blank, so `{"id": 0,
    "order_id": "REAL", "state": "queued"}` was non-blank and still
    selected the placeholder. Both live placement paths then reject that
    selected value outright and report the already-accepted order as
    ID-less instead of using "REAL". The previous `id or order_id`
    expression this function replaced did fall through falsey primaries;
    this restores that behavior."""
    import dca_live_adapters

    assert dca_live_adapters._first_usable_order_id_alias({"id": 0, "order_id": "REAL"}) == "REAL"
    assert dca_live_adapters._first_usable_order_id_alias({"id": [], "order_id": "REAL"}) == "REAL"
    assert dca_live_adapters._first_usable_order_id_alias({"id": {}, "order_id": "REAL"}) == "REAL"
    # A genuinely usable primary is unaffected.
    assert dca_live_adapters._first_usable_order_id_alias({"id": "REAL-2", "order_id": "REAL-1"}) == "REAL-2"


def test_robinhood_broker_pagination_validates_fallback_ids_before_deduplicating():
    """Codex review, PR #1168: "Validate fallback IDs before pagination
    deduplication". Two rows have DISTINCT malformed primary ids
    (`id=["BAD1"]`, `id=["BAD2"]`) but share the SAME malformed secondary
    `order_id=["SHARED"]`. The primary-id validation correctly falls
    through to the secondary for both rows, but the secondary itself was
    never validated -- both got stringified into the identical fabricated
    dedup key "['SHARED']", discarding the still-live second row even
    though neither row's own reference actually collided. Both rows must
    be yielded, id-less, rather than falsely deduplicated on fabricated
    evidence."""
    resp = {
        "data": {
            "orders": [
                {"id": ["BAD1"], "order_id": ["SHARED"], "state": "rejected"},
                {"id": ["BAD2"], "order_id": ["SHARED"], "state": "queued"},
            ]
        }
    }
    orders = list(RobinhoodBroker(orders_fn=lambda **k: resp)._iter_same_day_orders(
        "AAPL", datetime(2026, 7, 28).date()
    ))
    assert len(orders) == 2, "both malformed-id rows must be yielded, not falsely deduplicated"


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


def test_has_unusable_next_url_cursor_flags_a_blank_value_beside_a_usable_one():
    """Codex review, PR #1168: "Preserve empty cursor query values during
    validation". ``parse_qs``'s default ``keep_blank_values=False`` silently
    DROPS an empty occurrence of a repeated query key, so a ``next`` URL like
    ``"...?cursor=P2&cursor="`` used to parse to only ``["P2"]`` -- the truly
    blank second value vanished before this function ever got a chance to
    flag it, and a layer that advertised a second, unusable continuation
    signal alongside a usable one was reported as fully usable."""
    data = {"orders": [], "next": "https://api.robinhood.com/orders/?cursor=P2&cursor="}
    assert _has_unusable_next_url_cursor(data) is True


def test_has_unusable_next_url_cursor_accepts_a_single_usable_value():
    data = {"orders": [], "next": "https://api.robinhood.com/orders/?cursor=P2"}
    assert _has_unusable_next_url_cursor(data) is False


def test_has_unusable_direct_cursor_alias_flags_a_malformed_next_cursor_beside_a_usable_next_url():
    """Codex review, PR #1168: "Reject malformed direct cursors beside
    usable aliases". `_next_order_cursor` reads `next_cursor` first, then
    the `next` URL's own cursor param -- a malformed (non-string)
    `next_cursor` is silently skipped in favor of a usable `next` URL
    cursor, hiding the fact that this layer also advertised a second,
    unreadable continuation signal."""
    data = {"next_cursor": 0, "next": "https://api.robinhood.com/orders/?cursor=P2"}
    assert _has_unusable_direct_cursor_alias(data) is True


def test_has_unusable_direct_cursor_alias_flags_a_malformed_bare_cursor_beside_a_usable_next_cursor():
    data = {"next_cursor": "P2", "cursor": 0}
    assert _has_unusable_direct_cursor_alias(data) is True


def test_has_unusable_direct_cursor_alias_accepts_a_single_usable_next_cursor():
    data = {"next_cursor": "P2"}
    assert _has_unusable_direct_cursor_alias(data) is False


def test_has_unusable_direct_cursor_alias_accepts_a_blank_bare_cursor():
    # A blank/whitespace-only STRING cursor is the ordinary end-of-list
    # convention, not a malformed sentinel -- only a non-string present
    # value is flagged.
    data = {"next_cursor": "P2", "cursor": "  "}
    assert _has_unusable_direct_cursor_alias(data) is False


def test_robinhood_broker_pagination_fails_closed_on_a_malformed_next_cursor_beside_a_usable_next_url():
    """Integration-level companion to
    test_has_unusable_direct_cursor_alias_flags_a_malformed_next_cursor_
    beside_a_usable_next_url: a page whose `next_cursor` is malformed but
    whose `next` URL carries a usable cursor must still fail closed
    rather than silently following the usable alias and masking the
    unreadable sibling."""
    page = {
        "data": {
            "orders": [{"state": "filled", "ref_id": "other"}],
            "next_cursor": 0,
            "next": "https://api.robinhood.com/orders/?cursor=P2",
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


@pytest.mark.parametrize("pointer", [0, [], {}, False, 0.0])
def test_robinhood_broker_fails_closed_on_a_malformed_bare_cursor(pointer):
    """Regression test for "Reject malformed bare cursors after recursive
    unwrapping". A recursively wrapped page -- {"data": {"data": {"orders":
    [], "cursor": 0}}} -- makes the inner page provable once _unwrap stops
    descending at the cursor-bearing layer, but a non-string bare "cursor"
    is unusable to _next_order_cursor (its bare-cursor fallback requires a
    string) and _has_continuation_pointer only checked "next"/"next_cursor",
    never bare "cursor". The iterator therefore read this truncated page as
    a complete, empty history and returned None -- releasing a crash lease
    and authorizing a duplicate purchase even though a real order could be
    waiting behind the unusable pointer.
    """
    page = {"data": {"data": {"orders": [], "cursor": pointer}}}
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


def test_robinhood_broker_ref_id_prefix_falls_through_a_whitespace_only_primary_alias():
    """Codex review, PR #1169: "Validate nested reference aliases before
    proving absence". A raw `order.get("ref_id") or order.get(
    "client_order_id") or ...` chain picks whichever alias is merely
    non-empty-string-truthy FIRST -- a whitespace-only `ref_id` (" ") is
    truthy and wins over a real, usable `client_order_id` sitting right
    beside it, hiding the match. `same_day_strategy_order_exists_with_ref_
    id_prefix` and `strategy_order_with_ref_id` must still find the order
    via `client_order_id` when `ref_id` is present but blank."""
    resp = {"data": {"orders": [{"state": "filled", "ref_id": " ", "client_order_id": "TARGET"}]}}
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    order = broker.same_day_strategy_order_exists_with_ref_id_prefix(
        "AAPL", datetime(2026, 7, 28).date(), ("TARGET",)
    )
    assert order is not None
    order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
    assert order is not None


def test_robinhood_broker_ref_id_prefix_falls_through_falsey_non_scalar_primary_aliases():
    """Codex review, PR #1169: "Fall through malformed primary reference
    aliases". `_is_blank_broker_field` only reads None/""/whitespace as
    blank, so a falsey non-scalar (`[]`, `{}`) or falsey scalar (`0`,
    `False`) on `ref_id` is non-blank and was selected and stringified into
    an apparently-valid reference ("[]") instead of falling through to a
    valid `client_order_id` beside it. `strategy_order_with_ref_id` must
    still find the order via `client_order_id` for each malformed
    sentinel."""
    for _sentinel in ([], {}, 0, False):
        resp = {"data": {"orders": [{"state": "filled", "ref_id": _sentinel, "client_order_id": "TARGET"}]}}
        broker = RobinhoodBroker(orders_fn=lambda resp=resp, **k: resp)
        order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
        assert order is not None, f"sentinel {_sentinel!r} must not mask client_order_id"


def test_robinhood_broker_ref_id_prefix_falls_through_a_non_finite_primary_alias():
    """Codex review, PR #1169: "Reject non-finite reference aliases". A
    non-finite float (`nan`/`inf`) on `ref_id` is truthy and a supported
    scalar type, so it survived the blank/falsey/non-scalar checks and
    stringified into the apparently-valid reference "nan", hiding a valid
    `client_order_id` beside it. `strategy_order_with_ref_id` must still
    find the order via `client_order_id`."""
    for _sentinel in (float("nan"), float("inf")):
        resp = {"data": {"orders": [{"state": "filled", "ref_id": _sentinel, "client_order_id": "TARGET"}]}}
        broker = RobinhoodBroker(orders_fn=lambda resp=resp, **k: resp)
        order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
        assert order is not None, f"sentinel {_sentinel!r} must not mask client_order_id"


def test_usable_ref_id_aliases_returns_every_usable_alias_not_just_the_first():
    """Codex review, PR #1168: "Inspect every usable reference-ID alias".
    Matching only against `_first_usable_ref_id_alias`'s single pick misses
    a row that carries TWO usable aliases where the caller's actual target
    is the SECOND one -- e.g. `{"ref_id": "OTHER", "client_order_id":
    "TARGET"}`, where "OTHER" is a perfectly usable (non-blank, non-
    malformed) value that simply isn't what the caller is looking for.
    `_usable_ref_id_aliases` must return both, so callers can check
    membership across every usable alias on the row instead of equality
    against only the highest-priority one."""
    order = {"state": "filled", "ref_id": "OTHER", "client_order_id": "TARGET"}
    assert _usable_ref_id_aliases(order) == ["OTHER", "TARGET"]


def test_robinhood_broker_ref_id_prefix_matches_a_target_hidden_behind_a_usable_sibling_alias():
    """Companion to the above at the `RobinhoodBroker` call-site level: a row
    with a usable `ref_id` that isn't the caller's target must still be
    found via a usable `client_order_id` sitting beside it -- unlike the
    falls-through-a-malformed-primary tests above, here BOTH aliases are
    individually usable, so the bug isn't about validity, it's about only
    ever comparing against the first one."""
    resp = {"data": {"orders": [{"state": "filled", "ref_id": "OTHER", "client_order_id": "TARGET"}]}}
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
    assert order is not None
    order = broker.same_day_strategy_order_exists_with_ref_id_prefix(
        "AAPL", datetime(2026, 7, 28).date(), ("TARGET",)
    )
    assert order is not None


def test_usable_ref_id_aliases_falls_through_an_oversized_primary_alias():
    """Codex review, PR #1169: "Fall through oversized reference-ID
    aliases". An oversized native int candidate (beyond Python's
    integer-string conversion digit limit) is truthy, positive, and a
    supported scalar type, so it survived every other check and reached
    the final `str(_candidate)` call, which raises `ValueError` for a
    literal this large -- aborting the whole alias scan before it ever
    reached the valid "TARGET" alias sitting right beside it."""
    order = {"state": "filled", "ref_id": 10**5000, "client_order_id": "TARGET"}
    assert _usable_ref_id_aliases(order) == ["TARGET"]


def test_robinhood_broker_ref_id_prefix_falls_through_an_oversized_primary_alias():
    """Companion to the above at the `RobinhoodBroker` call-site level: a
    row whose primary reference alias is an oversized native int must not
    crash the scan, and the caller's target must still be found via the
    usable `client_order_id` sitting beside it."""
    resp = {"data": {"orders": [{"state": "filled", "ref_id": 10**5000, "client_order_id": "TARGET"}]}}
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
    assert order is not None


def test_is_unusable_live_placement_order_id_rejects_an_oversized_native_int_without_crashing():
    """Codex review, PR #1169: "Check numeric representability before
    stringifying placement IDs". A native int beyond Python's integer-
    to-string conversion digit limit used to reach a bare `str(order_id)`
    call before its magnitude was checked, raising `ValueError` instead
    of cleanly classifying it as unusable -- aborting placement-response
    validation entirely for an already-broker-accepted response."""
    assert _is_unusable_live_placement_order_id(10**5000) is True


def test_envelope_layer_shares_order_identity_compares_oversized_ids_numerically():
    """Codex review, PR #1169: same finding, reached via the envelope-
    identity-sharing helper -- comparing two oversized native ints via a
    bare `str(...)` raised `ValueError` even though both share the
    identical representable type and Python's arbitrary-precision `int`
    equality can compare them directly with no magnitude limit."""
    huge = 10**5000
    assert _envelope_layer_shares_order_identity({"id": huge}, huge) is True
    assert _envelope_layer_shares_order_identity({"id": huge}, huge + 1) is False


def test_envelope_layers_conflict_with_order_tolerates_oversized_ids_without_crashing():
    """Codex review, PR #1169: same finding, reached via the envelope-
    conflict helper -- stringifying an oversized native int id while
    comparing layers used to raise `ValueError` instead of treating it
    as unrepresentable-but-safe. An unrepresentable `order`-level id is
    treated the same as no anchor at all (matching how every other
    unusable-id path in this module already treats it), so a single
    other layer naming a different id is not by itself flagged as a
    conflict -- the point of this test is that the comparison completes
    at all instead of raising."""
    huge = 10**5000
    order = {"id": huge, "state": "queued"}
    assert _envelope_layers_conflict_with_order(order, {"id": huge, "state": "queued"}) is False
    assert _envelope_layers_conflict_with_order(order, {"id": "OTHER", "state": "queued"}) is False
    # Two OTHER layers naming different ids of their own, with no usable
    # order-level anchor, still conflict with EACH OTHER.
    assert (
        _envelope_layers_conflict_with_order(
            order, {"id": "OTHER-A", "state": "queued"}, {"id": "OTHER-B", "state": "queued"}
        )
        is True
    )


def test_normalized_order_identifier_tolerates_an_oversized_native_int_without_crashing():
    """Codex review, PR #1169: "Guard unrepresentable IDs before fallback
    stringification". `_normalized_order_id` already safely rejects an
    oversized native int (returning None), but the fallback used to call
    a bare `str(value)` on it anyway, raising `ValueError` instead of
    falling back to a safe result."""
    assert _normalized_order_identifier(10**5000) is None


def test_order_collections_semantically_agree_preserves_opaque_ref_id_differences():
    """Codex review, PR #1169: "Preserve opaque reference IDs in
    collection comparisons". `ref_id`/`client_order_id`/etc. are OPAQUE
    client-supplied references, not broker order identifiers --
    `ref_id="001"` and `ref_id="1"` are two DISTINCT references that
    merely happen to look numeric, not two encodings of one value.
    Canonicalizing them the same way `id`/`order_id` are declared two
    otherwise-identical rows "agreeing" even though their references
    genuinely differ."""
    a = [{"id": "1", "state": "filled", "ref_id": "001"}]
    b = [{"id": "1", "state": "filled", "ref_id": "1"}]
    assert _order_collections_semantically_agree(a, b) is False
    # `id`/`order_id` themselves are still canonicalized numerically.
    a2 = [{"id": 1, "state": "filled", "ref_id": "SAME"}]
    b2 = [{"id": "1", "state": "filled", "ref_id": "SAME"}]
    assert _order_collections_semantically_agree(a2, b2) is True


def test_order_collections_semantically_agree_normalizes_reference_alias_types():
    """Codex review, PR #1169: "Normalize reference alias types before
    comparing rows". A TYPE-only encoding difference -- ref_id=123 (int)
    vs ref_id="123" (str) -- is not the same as a genuine opaque-content
    difference like "001" vs "1": `_usable_ref_id_aliases()` (the actual
    reference-matching helper) already converts both supported scalar
    encodings to the identical string "123" for matching, so this row-
    agreement check must not treat them as disagreeing."""
    a = [{"id": "1", "state": "filled", "ref_id": 123}]
    b = [{"id": "1", "state": "filled", "ref_id": "123"}]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuinely different opaque string spelling must still disagree.
    c = [{"id": "1", "state": "filled", "ref_id": "001"}]
    assert _order_collections_semantically_agree(a, c) is False


def test_order_collections_semantically_agree_treats_id_and_order_id_as_the_same_key():
    """Codex review, PR #1169: "Treat id and order_id as equivalent
    collection keys". Two rows describing the identical broker order but
    spelling it through different SUPPORTED aliases -- `{"id": "123"}`
    directly and `{"order_id": "123"}` nested -- previously canonicalized
    to two dicts with different KEY NAMES, which still compare unequal as
    dicts even though their identifier values agree."""
    a = [{"id": "123", "state": "filled"}]
    b = [{"order_id": "123", "state": "filled"}]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuine disagreement in the unified identity is still caught.
    c = [{"order_id": "999", "state": "filled"}]
    assert _order_collections_semantically_agree(a, c) is False


def test_order_collections_semantically_agree_falls_through_an_unusable_primary_id():
    """Codex review, PR #1169: "Fall through unusable IDs when
    canonicalizing collections". `{"id": true, "order_id": "REAL"}` and
    `{"order_id": "REAL"}` describe the identical order, but a bare
    `_first_usable_order_id_alias` selects the truthy boolean primary
    (it only screens for blank/falsey, not full usability), so the first
    row canonicalized to "True" while the second canonicalized to "REAL"
    -- reported as a conflict even though every other id-selection path
    in this module already falls through this exact sentinel."""
    a = [{"id": True, "order_id": "REAL", "state": "queued"}]
    b = [{"order_id": "REAL", "state": "queued"}]
    assert _order_collections_semantically_agree(a, b) is True


def test_order_collections_semantically_agree_guards_oversized_reference_aliases():
    """Codex review, PR #1169: "Guard oversized reference aliases before
    stringifying". A collection row carrying an astronomically large
    native int in `ref_id` (beyond Python's integer-to-string conversion
    digit limit) used to reach a bare `str()` call inside the old per-key
    reference-alias loop, raising `ValueError` -- uncaught by this
    function's own `except TypeError`, aborting the whole comparison
    before the row's OTHER, perfectly valid `client_order_id="TARGET"`
    alias could ever be matched against a sibling row. The oversized value
    must be silently screened out (matching `_usable_ref_id_aliases()`'s
    own definition of "usable"), not crash the comparison."""
    a = [{"id": "1", "state": "filled", "ref_id": 10**5000, "client_order_id": "TARGET"}]
    b = [{"id": "1", "state": "filled", "client_order_id": "TARGET"}]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuinely different usable reference beside the oversized one is
    # still caught as a real conflict.
    c = [{"id": "1", "state": "filled", "client_order_id": "OTHER"}]
    assert _order_collections_semantically_agree(a, c) is False


def test_order_collections_semantically_agree_avoids_unsafe_repr_in_sort_keys():
    """Codex review, PR #1169: "Avoid unsafe repr in collection sort
    keys". An oversized native int sitting in an ORDINARY, non-identifier
    field (e.g. `quantity`) -- not one of the special-cased reference/id
    fields -- used to reach a bare `repr()` call inside
    `_dict_order_independent_sort_key`, raising `ValueError` that this
    function's own `except TypeError` never caught, aborting history
    parsing even when both collections were otherwise identical."""
    a = [{"id": "1", "state": "filled", "quantity": 10**5000}]
    b = [{"id": "1", "state": "filled", "quantity": 10**5000}]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuinely different ordinary value is still a real conflict, not
    # silently swallowed by the placeholder repr.
    c = [{"id": "1", "state": "filled", "quantity": 5}]
    assert _order_collections_semantically_agree(a, c) is False


def test_order_collections_semantically_agree_collapses_equivalent_reference_aliases():
    """Codex review, PR #1169: "Collapse equivalent reference aliases in
    collection rows". Two redundant rows naming the IDENTICAL reference
    through two different alias KEY NAMES (`ref_id` vs `client_order_id`)
    previously canonicalized to two dicts differing purely on which key
    held the value, rejecting an otherwise completely redundant pair --
    the exact key-naming disagreement `_usable_ref_id_aliases()` already
    treats as equivalent everywhere else this module matches references."""
    a = [{"id": "1", "state": "filled", "ref_id": "TARGET"}]
    b = [{"id": "1", "state": "filled", "client_order_id": "TARGET"}]
    assert _order_collections_semantically_agree(a, b) is True
    # A genuinely DIFFERENT reference value must still conflict, whatever
    # alias key holds it.
    c = [{"id": "1", "state": "filled", "client_order_id": "OTHER"}]
    assert _order_collections_semantically_agree(a, c) is False
    # Two DIFFERENT, both-usable references on the same row must not be
    # collapsed away -- neither alone accounts for both.
    d = [{"id": "1", "state": "filled", "ref_id": "A", "client_order_id": "B"}]
    e = [{"id": "1", "state": "filled", "ref_id": "A"}]
    assert _order_collections_semantically_agree(d, e) is False


def test_canonicalized_order_row_guards_an_oversized_reference_alias():
    """Codex review, PR #1169: "Guard oversized reference aliases before
    stringifying". `ref_id=10**5000` used to raise `ValueError` from a
    bare `str(...).strip()` inside `_canonicalized_order_row`, aborting
    the row-agreement comparison instead of leaving the unrepresentable
    value as-is."""
    huge = 10**5000
    a = [{"id": "1", "state": "filled", "ref_id": huge, "client_order_id": "OTHER"}]
    b = [{"id": "1", "state": "filled", "ref_id": huge, "client_order_id": "OTHER"}]
    assert _order_collections_semantically_agree(a, b) is True


def test_order_collections_semantically_agree_normalizes_lifecycle_aliases():
    """Codex review, PR #1169: "Normalize lifecycle aliases in collection
    rows". Redundant rows describing the same order as
    `{"id": "1", "state": "queued"}` and `{"id": "1", "status": "queued"}`
    remain "unequal" after canonicalization purely because of which key
    held the lifecycle value, and even a shared key name still disagreed
    on the "cancelled"/"canceled" spelling alone."""
    a = [{"id": "1", "state": "queued"}]
    b = [{"id": "1", "status": "queued"}]
    assert _order_collections_semantically_agree(a, b) is True
    a2 = [{"id": "1", "state": "cancelled"}]
    b2 = [{"id": "1", "state": "canceled"}]
    assert _order_collections_semantically_agree(a2, b2) is True
    # Both the key-name AND the spelling difference at once must still
    # canonicalize identically.
    a3 = [{"id": "1", "state": "cancelled"}]
    b3 = [{"id": "1", "status": "canceled"}]
    assert _order_collections_semantically_agree(a3, b3) is True
    # A row whose OWN state/status genuinely disagree is still a real
    # conflict, not silently collapsed away.
    conflicted = [{"id": "1", "state": "queued", "status": "rejected"}]
    plain = [{"id": "1", "state": "queued"}]
    assert _order_collections_semantically_agree(conflicted, plain) is False


def test_robinhood_broker_ref_id_prefix_falls_through_a_negative_primary_alias():
    """Codex review, PR #1169: "Fall through negative reference-ID
    sentinels". A finite negative numeric candidate (-1) is truthy, finite,
    and a supported scalar type, so it survived every other check and
    stringified into the apparently-valid reference "-1" -- no legitimate
    broker reference id is ever negative. `strategy_order_with_ref_id`
    must still find the order via `client_order_id`."""
    resp = {"data": {"orders": [{"state": "filled", "ref_id": -1, "client_order_id": "TARGET"}]}}
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
    assert order is not None


def test_robinhood_broker_ref_id_prefix_falls_through_a_stringified_negative_primary_alias():
    """Codex review, PR #1168: "Skip stringified reference-ID sentinels".
    The negative-reference rejection also applies when the sentinel is a
    STRING (`ref_id="-1"`), not just a Python int/float -- the numeric-
    typed check alone missed this, so `{"ref_id": "-1", "client_order_id":
    "TARGET"}` still stringified into the apparently-valid reference
    "-1", hiding "TARGET" the identical way."""
    resp = {"data": {"orders": [{"state": "filled", "ref_id": "-1", "client_order_id": "TARGET"}]}}
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
    assert order is not None


def test_robinhood_broker_ref_id_prefix_falls_through_a_stringified_zero_primary_alias():
    """Codex review, PR #1168: "Skip stringified zero reference
    sentinels". A stringified ZERO (`ref_id="0"`) bypassed both the
    falsey and numeric-negative checks and still stringified into the
    apparently-valid reference "0", hiding a valid secondary alias
    (`client_order_id="TARGET"`) beside it -- a provable
    `strategy_order_with_ref_id` scan then reported no match."""
    resp = {"data": {"orders": [{"state": "filled", "ref_id": "0", "client_order_id": "TARGET"}]}}
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
    assert order is not None


def test_robinhood_broker_ref_id_prefix_falls_through_stringified_non_finite_primary_aliases():
    """Codex review, PR #1169: "Fall through stringified non-finite
    reference aliases". `_is_stringified_non_positive_number` cannot
    catch these: a NaN comparison against 0 is always False, and
    positive infinity is not <= 0 either. A connector serializing its
    non-finite sentinel as a string ({"ref_id": "NaN", "client_order_id":
    "TARGET"}) selected it as a usable reference over a valid secondary
    alias beside it."""
    for _sentinel in ("NaN", "nan", "Infinity", "-Infinity", "inf"):
        resp = {"data": {"orders": [{"state": "filled", "ref_id": _sentinel, "client_order_id": "TARGET"}]}}
        broker = RobinhoodBroker(orders_fn=lambda resp=resp, **k: resp)
        order = broker.strategy_order_with_ref_id("AAPL", datetime(2026, 7, 28).date(), "TARGET")
        assert order is not None, f"sentinel {_sentinel!r} must not mask client_order_id"


def test_robinhood_broker_pagination_rejects_a_stringified_zero_primary_id_while_deduplicating():
    """Codex review, PR #1168: "Reject stringified zero IDs before
    deduplicating history". A stringified zero sentinel (id="0") is
    truthy and does not start with "-", so it survived
    _is_unusable_dedup_id and deduplicated a rejected row against a
    still-live queued row sharing that same fabricated "0" key, even
    though their own distinct order_id aliases prove them different
    orders. same_day_strategy_order_exists must still see the live order
    even though it comes after a same-stringified-zero rejected row."""
    resp = {
        "data": {
            "orders": [
                {"id": "0", "order_id": "REJECT", "state": "rejected"},
                {"id": "0", "order_id": "LIVE", "state": "queued"},
            ]
        }
    }
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
        "AAPL", datetime(2026, 7, 28).date()
    ) is True


def test_robinhood_broker_pagination_rejects_a_stringified_non_finite_primary_id_while_deduplicating():
    """Codex review, PR #1168: "Reject serialized non-finite IDs before
    history deduplication". A stringified NaN sentinel (id="NaN") is not
    caught by _is_stringified_non_positive_number (a NaN comparison
    against 0 is always False), so it survived _is_unusable_dedup_id and
    deduplicated a rejected row against a still-live queued row sharing
    that same fabricated "NaN" key. same_day_strategy_order_exists must
    still see the live order even though it comes after a
    same-stringified-NaN rejected row."""
    resp = {
        "data": {
            "orders": [
                {"id": "NaN", "order_id": "REJECT", "state": "rejected"},
                {"id": "NaN", "order_id": "LIVE", "state": "queued"},
            ]
        }
    }
    assert RobinhoodBroker(orders_fn=lambda **k: resp).same_day_strategy_order_exists(
        "AAPL", datetime(2026, 7, 28).date()
    ) is True


def test_robinhood_broker_pagination_rejects_conflicting_cursor_aliases_within_one_layer():
    """Codex review, PR #1168: "Reject conflicting cursor aliases within
    one layer". _next_order_cursor deliberately returns only the FIRST
    usable alias it finds -- right for selecting which cursor to follow,
    but wrong for detecting a conflict entirely within one layer: a
    layer carrying both next_cursor="P2" and a next URL whose cursor is
    "P3" must raise instead of silently following P2, because the scan
    would prove absence from a truncated view while a live order sits on
    P3."""
    resp = {
        "data": {
            "orders": [{"id": "1", "order_id": "SOME", "state": "filled"}],
            "next_cursor": "P2",
            "next": "https://example.com/orders/?cursor=P3",
        }
    }
    broker = RobinhoodBroker(orders_fn=lambda **k: resp)
    with pytest.raises(RuntimeError, match="conflicting pagination cursors"):
        broker._same_day_orders("AAPL", datetime(2026, 7, 28).date())


def test_interpret_review_rejects_malformed_transport_verdicts_beside_valid_approvals():
    """Codex review, PR #1168: "Reject malformed transport verdicts beside
    valid approvals". A malformed success/ok/isError value beside a
    genuinely valid approved field passes the shape check but satisfies
    none of the strict `is False`/`is True` identity tests either --
    neither recognized as the blocker it represents nor rejected as
    unreadable. _interpret_review must fail closed on a wrong-typed
    transport verdict."""
    for _malformed in (
        {"approved": True, "success": 0},
        {"approved": True, "success": "false"},
        {"approved": True, "isError": 1},
    ):
        ok, _reason = _interpret_review(_malformed)
        assert ok is False, f"{_malformed!r} must not be approved"


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


def test_interpret_review_renders_an_oversized_error_item_safely():
    """Codex review, PR #1169: "Render malformed review errors without
    unsafe string conversion". A truthy error item that is a native int
    beyond Python's integer-to-string conversion digit limit (e.g.
    `{"approved": True, "errors": [10**5000]}`) made the old bare
    `str(item)` call itself raise `ValueError`, aborting
    `_interpret_review()` instead of returning the intended blocking
    verdict."""
    huge = 10**5000
    ok, reason = _interpret_review({"data": {"approved": True, "errors": [huge]}})
    assert ok is False
    assert "unrepresentable" in reason


def test_interpret_review_treats_data_wrapped_empty_response_as_empty():
    # Codex review, PR #1164/#1165: "Treat data-wrapped empty reviews as
    # empty". {"data": {"data": {}}} produces the traversed-layer list
    # [{"data": {}}, {}] -- the FIRST layer's own "data" key made a bare
    # `any(_dict_layers)` truthy, so this previously-accepted empty-
    # envelope shape fell through to the shape check and was rejected as
    # "unexpected" instead of approved as the empty response it is.
    ok, reason = _interpret_review({"data": {"data": {}}})
    assert ok is True
    assert "empty" in reason


def test_interpret_review_inspects_blockers_before_a_data_wrapped_empty_response():
    # Codex review, PR #1164/#1165: "Inspect skipped blockers before
    # approving empty review data". A real blocker on the OUTER layer
    # alongside an empty nested "data" must still block -- the empty-
    # envelope reading only applies when NO layer carries any content
    # beyond a bare "data" pass-through key.
    ok, reason = _interpret_review({"data": {"success": True, "approved": False, "data": {}}})
    assert ok is False
    assert "approved=false" in reason


def test_interpret_review_rejects_a_malformed_value_inside_an_empty_wrapper():
    # Codex review, PR #1164/#1165: "Reject malformed values inside empty
    # review wrappers". {"data": {"data": "broker error"}} stops the
    # traversal at {"data": "broker error"} (a string is never walked
    # into) -- its only KEY is the bare "data" pass-through, which passes
    # the per-layer emptiness check, but the STRING it points to is real,
    # unread diagnostic content that must not be silently treated as
    # "nothing here".
    ok, reason = _interpret_review({"data": {"data": "broker error"}})
    assert ok is False
    assert "unexpected" in reason

    # The same for a list value hanging off the last reachable layer; None
    # is genuinely blank and still reads as empty.
    assert _interpret_review({"data": {"data": ["broker error"]}})[0] is False
    assert _interpret_review({"data": {"data": None}})[0] is True


def test_interpret_review_rejects_terminal_review_placeholders():
    # Codex review, PR #1168: "Reject terminal review placeholders". A
    # bare NULL alert container or a falsy "error" flag on the already-
    # fully-unwrapped terminal layer is a placeholder that was never
    # actually populated by a reviewer, not evidence of a real "checked,
    # nothing to flag" outcome -- unlike a genuine EMPTY container
    # ({"alerts": []}), which the "Accept empty terminal alert containers
    # as review evidence" fix still must treat as real evidence.
    ok, reason = _interpret_review({"data": {"alerts": None}})
    assert ok is False
    assert "unexpected" in reason

    ok, reason = _interpret_review({"data": {"error": False}})
    assert ok is False
    assert "unexpected" in reason

    # Genuine empty containers remain real evidence, not placeholders.
    assert _interpret_review({"data": {"alerts": []}})[0] is True
    assert _interpret_review({"data": {"warnings": []}})[0] is True


def test_interpret_review_rejects_malformed_terminal_review_containers():
    # Codex review, PR #1168: "Reject malformed terminal review
    # containers". A non-None placeholder that is not an actual container
    # shape (a falsy bool, an empty string, a bare number) still must not
    # satisfy the terminal-layer shape check -- only a genuine list/dict
    # container (populated or empty) counts as real evidence.
    ok, reason = _interpret_review({"data": {"warnings": False, "quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected" in reason

    ok, reason = _interpret_review({"data": {"alerts": "", "quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected" in reason

    ok, reason = _interpret_review({"data": {"checks": 0, "quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected" in reason

    # Genuine containers, populated or empty, remain real evidence.
    assert _interpret_review({"data": {"checks": []}})[0] is True
    assert _interpret_review({"data": {"warnings": {}}})[0] is True


def test_interpret_review_accepts_informational_string_review_containers():
    # Codex review, PR #1168: "Accept informational string review
    # containers". `_as_list` (used by the blocking scan) deliberately
    # treats a bare scalar as a single informational entry, so a NONBLANK
    # string on a terminal alert/warning/etc. field is a genuinely
    # supported review shape, not a malformed placeholder -- requiring
    # list/dict alone regressed this previously-accepted shape.
    assert _interpret_review({"data": {"warnings": "price moved"}})[0] is True
    assert _interpret_review({"data": {"messages": "informational"}})[0] is True

    # A BLANK string is still rejected, same as every other placeholder.
    ok, reason = _interpret_review({"data": {"warnings": "   ", "quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected" in reason


def test_interpret_review_rejects_malformed_truthy_blocking_alert_flags():
    """Codex review, PR #1168: "Reject malformed truthy blocking-alert
    flags". A malformed but truthy blocking marker (`"blocking": 1`,
    `"blocking": "true"`) failed the old strict `is True` identity check
    and fell through to the other, unrelated blocking signals -- an
    otherwise-affirmative alert read as non-blocking and permitted the
    live placement."""
    ok, reason = _interpret_review({"data": {"alerts": [{"blocking": 1, "message": "spread too wide"}]}})
    assert ok is False
    assert "spread too wide" in reason

    ok, reason = _interpret_review({"data": {"alerts": [{"blocking": "true", "message": "halted"}]}})
    assert ok is False
    assert "halted" in reason

    # An explicitly falsy blocking marker remains non-blocking.
    assert _interpret_review({"data": {"alerts": [{"blocking": 0, "message": "fyi"}]}})[0] is True
    assert _interpret_review({"data": {"alerts": [{"blocking": False, "message": "fyi"}]}})[0] is True


def test_interpret_review_rejects_blank_secondary_review_states():
    """Codex review, PR #1168: "Reject blank secondary review states".
    `_field_is_substantive` correctly refuses to use a blank `state` as
    review evidence, but the malformed-verdict check considered every
    STRING well-formed regardless of content. A response with a valid
    `approved` alongside a blank `state` (`{"approved": true, "state":
    ""}`) established the shape through `approved` alone, and the
    blocking scan ignores a blank state, so the malformed second verdict
    was silently ignored and the response was approved."""
    ok, reason = _interpret_review({"data": {"approved": True, "state": ""}})
    assert ok is False
    assert "unexpected" in reason

    ok, reason = _interpret_review({"data": {"approved": True, "state": "   "}})
    assert ok is False
    assert "unexpected" in reason

    # A non-blank, recognized state alongside approved remains fine.
    assert _interpret_review({"data": {"approved": True, "state": "confirmed"}})[0] is True


def test_interpret_review_rejects_every_malformed_verdict_field():
    # Codex review, PR #1168: "Reject every malformed review verdict". A
    # layer with ONE genuinely valid verdict field (satisfying the shape
    # check on its own) alongside a SECOND, malformed verdict field of the
    # wrong type -- {"approved": true, "tradeable": 0} or {"approved":
    # true, "state": 0} -- must fail closed on the malformed field, not
    # approve just because a different field already established the
    # shape. The blocking scan alone never catches these: it only tests
    # `tradeable is False` (a strict identity check that a serialized `0`
    # never satisfies) and only reads a STRING `state`.
    ok, reason = _interpret_review({"data": {"approved": True, "tradeable": 0}})
    assert ok is False
    assert "unexpected" in reason

    ok, reason = _interpret_review({"data": {"approved": True, "state": 0}})
    assert ok is False
    assert "unexpected" in reason

    # A genuinely well-typed second verdict field is unaffected.
    assert _interpret_review({"data": {"approved": True, "tradeable": True}})[0] is True


def test_interpret_review_rejects_malformed_trailing_data_alongside_a_recognized_field():
    # Codex review, PR #1168: "Reject malformed trailing review data before
    # approval". {"data": {"approved": true, "data": "broker error"}} has a
    # nonblank non-dict trailing value this module never walks into --
    # malformed evidence -- but its one traversed layer's own "approved:
    # true" satisfies the shape check and blocks nothing on its own, so
    # this used to fall through to a clean approval for a response it
    # never actually finished reading. (A bare "success: true" no longer
    # satisfies the shape check on its own at all -- see "Exclude
    # intermediate transport flags from review evidence" -- so this uses
    # "approved", a genuinely review-specific field, to isolate the
    # malformed-trailing-data path being tested here.)
    ok, reason = _interpret_review({"data": {"approved": True, "data": "broker error"}})
    assert ok is False
    assert "broker error" in reason

    # A genuine blocker elsewhere in the same malformed response still
    # reports its own specific reason, not the generic malformed-data one.
    ok, reason = _interpret_review({"data": {"approved": False, "data": "broker error"}})
    assert ok is False
    assert "approved=false" in reason


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


def test_interpret_review_preserves_blockers_on_a_skipped_success_wrapper():
    # Codex review, PR #1164: "Preserve review blockers while descending
    # success wrappers". `_unwrap`'s isError/success/ok blank-value
    # exception (added for the unrelated order/pagination descent case)
    # made `success: true` a non-evidence value, so an outer review wrapper
    # reporting `{"success": true, "approved": false, "tradeable": false,
    # "alerts": [...]}` alongside a deeper, unrelated `{"success": true}`
    # layer had `_unwrap` descend straight past the outer layer's own
    # blocking evidence, and `_interpret_review` read only the inner
    # "clean" layer as an approval.
    response = {
        "data": {
            "success": True,
            "approved": False,
            "tradeable": False,
            "alerts": [{"severity": "critical", "message": "halted"}],
            "data": {"success": True},
        }
    }
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "approved=false" in reason


def test_interpret_review_preserves_tradeable_and_alerts_on_a_skipped_layer():
    # Same fix, isolating each blocker individually and exercising the
    # "ok" flag (rather than "success") as the skippable trigger.
    response = {
        "data": {
            "ok": True,
            "tradeable": False,
            "data": {"ok": True},
        }
    }
    assert _interpret_review(response)[0] is False

    response = {
        "data": {
            "success": True,
            "alerts": [{"severity": "error", "message": "wash sale risk"}],
            "data": {"success": True},
        }
    }
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "wash sale risk" in reason


def test_interpret_review_checks_every_supplied_alert_severity_alias():
    """Codex review, PR #1169: "Check every supplied alert severity alias".
    A raw `severity or level or type` chain trusted only whichever alias
    came first, so an alert supplying both a benign `severity` and a
    blocking `level` (e.g. `{"severity": "warning", "level": "critical"}`)
    read as non-blocking on the benign alias alone even though `level`
    explicitly marked it critical. Every supplied alias must be checked."""
    response = {
        "data": {
            "alerts": [{"severity": "warning", "level": "critical", "message": "halt"}],
        }
    }
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "halt" in reason


def test_interpret_review_rejects_malformed_structured_alert_severities():
    """Codex review, PR #1168: "Reject malformed structured alert
    severities". A truthy non-scalar severity ({"severity": ["critical"]})
    stringifies into "['critical']", matching no recognized blocking
    spelling, so the loop silently read an alert flagged critical as
    non-blocking. An unreadable severity value must fail closed."""
    response = {
        "data": {
            "alerts": [{"severity": ["critical"], "message": "halt"}],
        }
    }
    ok, _reason = _interpret_review(response)
    assert ok is False


def test_interpret_review_rejects_non_string_scalar_alert_severities():
    """Codex review, PR #1168: "Reject non-string alert severity values".
    `_has_unreadable_severity` used to only flag NON-SCALAR severities
    (lists/dicts) -- a truthy but wrongly-typed SCALAR like `True` or `1`
    passed that check, then got silently stringified to "true"/"1" by the
    old `str/int/float/bool`-accepting comprehension, matching no
    recognized blocking spelling and reading as non-blocking. Every
    severity value not already a string must fail closed the same way a
    non-scalar one does."""
    response = {
        "data": {
            "alerts": [{"severity": True, "message": "halt"}],
        }
    }
    ok, _reason = _interpret_review(response)
    assert ok is False

    response = {
        "data": {
            "alerts": [{"severity": 1, "message": "halt"}],
        }
    }
    ok, _reason = _interpret_review(response)
    assert ok is False


def test_interpret_review_inspects_nested_blockers_after_an_affirmative_outer_layer():
    # `approved`/`tradeable`/`alerts` were briefly added to
    # `_ENVELOPE_EVIDENCE_KEYS` to anchor `_unwrap` on a review response's
    # blocking outer layer -- but presence-only anchoring on `approved`
    # stopped `_unwrap` at the FIRST layer carrying that key regardless of
    # its value, so a genuine rejection one layer deeper than an
    # affirmative outer layer was never reached at all (Codex review, PR
    # #1164 round 15: "Inspect nested blockers after affirmative review
    # fields"). `_interpret_review` now scans every traversed layer
    # instead of relying on `_unwrap` to anchor on the right one.
    response = {"data": {"approved": True, "data": {"approved": False}}}
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "approved=false" in reason


def test_interpret_review_checks_failure_aliases_on_every_layer():
    # Codex review, PR #1164: "Check failure aliases on every review
    # layer". The nested-blocker scan checked `approved`/`state`/
    # `tradeable` on every traversed layer but not the equally-recognized
    # `success`/`ok`/`error` failure aliases -- a deeper `success: false`
    # (or `ok: false`, or a nonblank `error`) past an affirmative outer
    # `approved: true` was invisible to both `outer_failure` and this loop.
    ok, reason = _interpret_review({"data": {"approved": True, "data": {"success": False}}})
    assert ok is False
    assert "success=false" in reason

    ok, reason = _interpret_review({"data": {"approved": True, "data": {"ok": False}}})
    assert ok is False
    assert "success=false" in reason

    ok, reason = _interpret_review({"data": {"approved": True, "data": {"error": "broker rejected"}}})
    assert ok is False
    assert "broker rejected" in reason


def test_interpret_review_preserves_top_level_blockers_during_data_descent():
    # Codex review, PR #1168: "Preserve top-level review blockers during
    # data descent". `_all_data_layers` excludes the RAW top-level
    # `response` from its own layer list whenever it has a "data" key to
    # descend into (the "Keep outer success from satisfying review shape
    # checks" fix). That exclusion is right for the shape check, but
    # `_response_failure` never inspects `approved`/`tradeable`/alerts, so
    # a raw outer `{"approved": false, "data": {"approved": true}}` had its
    # own explicit rejection vanish entirely -- neither the shape check nor
    # the blocking scan ever saw it, and the response was approved from its
    # inner, affirmative layer alone.
    ok, reason = _interpret_review({"approved": False, "data": {"approved": True}})
    assert ok is False
    assert "approved=false" in reason

    # Same for tradeable and a blocking structured alert on the raw layer.
    ok, reason = _interpret_review({"tradeable": False, "data": {"approved": True}})
    assert ok is False
    assert "not tradeable" in reason

    ok, reason = _interpret_review(
        {"alerts": [{"severity": "critical", "message": "halted"}], "data": {"approved": True}}
    )
    assert ok is False
    assert "halted" in reason

    # A response with NO "data" key at all must not be double-scanned --
    # `_all_data_layers` already includes the raw payload as its own single
    # layer there, so prepending it again would just duplicate the same
    # blocker, not introduce a new bug, but the reason string should still
    # read cleanly.
    ok, reason = _interpret_review({"approved": False})
    assert ok is False
    assert "approved=false" in reason


def test_interpret_review_scans_raw_blockers_before_approving_an_empty_response():
    # Codex review, PR #1169: "Scan raw blockers before approving an empty
    # review". `{"approved": false, "data": {}}` has `_all_data_layers`
    # exclude the raw top-level layer (it has a "data" key pointing to a
    # dict) and produce `_dict_layers == [{}]` -- an empty mapping that
    # satisfied both the clean-chain check and the "no content of its own"
    # check, so this response was approved as "empty" before the shape
    # check or the blocking scan (which DOES include the raw layer) ever
    # ran, entirely missing the raw layer's own explicit rejection. Fixed
    # by refusing the early "empty" approval whenever the excluded raw
    # layer itself carries any content beyond a bare "data" pass-through --
    # this response now correctly fails closed (the shape check, which
    # deliberately still excludes the raw layer, reports it as an
    # unrecognized shape rather than the specific "approved=false" the raw
    # layer names, but the security-relevant outcome -- never approving a
    # response an outer layer explicitly rejected -- holds either way).
    ok, reason = _interpret_review({"approved": False, "data": {}})
    assert ok is False

    # A genuinely empty response -- no raw-layer content beyond "data" --
    # is unaffected and still approves.
    ok, reason = _interpret_review({"data": {}})
    assert ok is True


def test_interpret_review_preserves_affirmative_review_fields_on_the_raw_wrapper():
    # Codex review, PR #1168: "Preserve affirmative review fields on the
    # raw wrapper". {"approved": true, "data": {}} has the raw layer
    # excluded from the empty-response fast path (it carries "approved"
    # beyond "data"), correctly disqualifying it -- but the shape check
    # right after scanned only `_dict_layers == [{}]`, which recognizes no
    # review field, so an explicitly-APPROVED response regressed into a
    # rejected "unexpected shape" the instant that exclusion fix landed.
    ok, reason = _interpret_review({"approved": True, "data": {}})
    assert ok is True

    # A bare TRANSPORT `success: true` on the raw layer must still NOT
    # satisfy the shape check on its own -- the exact "Keep outer success
    # from satisfying review shape checks" gap this must not reopen.
    ok, reason = _interpret_review({"success": True, "data": {"quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected review response shape" in reason


def test_interpret_review_excludes_intermediate_transport_flags_from_shape_evidence():
    # Codex review, PR #1168: "Exclude intermediate transport flags from
    # review evidence". {"data": {"success": true, "data": {"quote": {...}}}}
    # has an INTERMEDIATE `_dict_layers` entry (not the raw top-level
    # wrapper the sibling "Exclude transport placeholders from raw review
    # evidence" fix already covered) whose own "success: true" satisfied
    # the shape check on its bare presence alone, even though neither it
    # nor the innermost payload carries any actual review outcome --
    # approving a live placement using an unrelated payload.
    ok, reason = _interpret_review({"data": {"success": True, "data": {"quote": {"ask": "1.0"}}}})
    assert ok is False
    assert "unexpected review response shape" in reason

    # A genuinely review-specific field on that same intermediate layer
    # still satisfies the shape check, exactly as before.
    ok, reason = _interpret_review({"data": {"approved": True, "data": {"quote": {"ask": "1.0"}}}})
    assert ok is True


def test_interpret_review_accepts_empty_terminal_alert_containers_as_evidence():
    # Codex review, PR #1168: "Accept empty terminal alert containers as
    # review evidence". A manual/ad hoc review response can BE the fully
    # unwrapped terminal payload itself -- not a wrapper hiding something
    # deeper -- and `{"alerts": []}` legitimately reports "checked, no
    # alerts". Applying the empty-container suppression (added to close
    # the sibling "Exclude transport placeholders..." gaps) to the
    # TERMINAL layer too made this genuine review regress into a rejected
    # "unexpected shape" and block placement despite there being no
    # blocking alert.
    ok, reason = _interpret_review({"alerts": []})
    assert ok is True
    ok, reason = _interpret_review({"warnings": []})
    assert ok is True
    ok, reason = _interpret_review({"data": {"alerts": []}})
    assert ok is True

    # An empty alert container on a genuinely INTERMEDIATE pass-through
    # layer -- one this module descends PAST on its way to a deeper,
    # actually-terminal payload -- must still be excluded: it is exactly
    # the placeholder-beside-unrelated-data shape the sibling fixes exist
    # to catch, not real review evidence.
    ok, reason = _interpret_review({"data": {"alerts": [], "data": {"quote": {"ask": "1.0"}}}})
    assert ok is False
    assert "unexpected review response shape" in reason


def test_interpret_review_validates_pass_through_review_fields_before_approving():
    # Codex review, PR #1169: "Validate pass-through review fields before
    # approving". A pass-through wrapper's own falsy/blank placeholder
    # fields must not satisfy the shape check just by being present.
    # `error: false` alongside an unrelated deeper payload is a falsy
    # transport placeholder, not review evidence -- `_is_blank_broker_
    # field(False)` reads it as "not blank" (only None/blank-string are
    # blank by that check), so the old `not _is_blank_broker_field(value)`
    # test wrongly returned True for it.
    ok, reason = _interpret_review({"data": {"error": False, "data": {"quote": {"ask": "1.0"}}}})
    assert ok is False
    assert "unexpected review response shape" in reason

    # `approved: null` / `tradeable: null` on a pass-through layer carry no
    # actual verdict either way -- the blocking scan itself never treats a
    # null as blocking (only an explicit `is False`) -- and must not
    # manufacture shape evidence on their own.
    ok, reason = _interpret_review({"data": {"approved": None, "data": {"quote": {"ask": "1.0"}}}})
    assert ok is False
    assert "unexpected review response shape" in reason
    ok, reason = _interpret_review({"data": {"tradeable": None, "data": {"quote": {"ask": "1.0"}}}})
    assert ok is False
    assert "unexpected review response shape" in reason

    # A genuine falsy verdict -- `approved: false` -- is itself the review
    # outcome, not a placeholder, and still satisfies the shape check
    # (and blocks) exactly as before.
    ok, reason = _interpret_review({"data": {"approved": False, "data": {"quote": {"ask": "1.0"}}}})
    assert ok is False
    assert "approved=false" in reason


def test_interpret_review_treats_raw_empty_alert_reviews_as_terminal_evidence():
    # Codex review, PR #1169: "Treat raw empty-alert reviews as terminal
    # evidence". `{"alerts": [], "data": {}}` has the raw top-level layer
    # excluded from `_dict_layers` (it has a "data" key), but the nested
    # chain from that "data" bottoms out to nothing -- the raw layer's own
    # `alerts: []` IS the entire, already-terminal review payload, not a
    # pass-through wrapper hiding something deeper. Hardcoding
    # `is_terminal=False` for the raw layer's own fields applied the
    # pass-through-only "empty alert container carries no evidence"
    # reading to this genuinely complete "checked, nothing to flag"
    # response, rejecting a valid no-alert review as an unexpected shape.
    ok, reason = _interpret_review({"alerts": [], "data": {}})
    assert ok is True
    ok, reason = _interpret_review({"warnings": [], "data": {}})
    assert ok is True

    # A genuinely BLOCKING alert on the same raw+empty-chain shape must
    # still block -- this fix only concerns EMPTY alert containers.
    ok, reason = _interpret_review({"alerts": [{"severity": "critical", "message": "halted"}], "data": {}})
    assert ok is False
    assert "halted" in reason

    # An empty alert container on the raw layer alongside a DEEPER,
    # non-empty payload is still a genuine pass-through -- unaffected.
    ok, reason = _interpret_review({"alerts": [], "data": {"quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected review response shape" in reason


def test_interpret_review_requires_a_valid_verdict_not_just_a_present_one():
    # Codex review, PR #1168: "Require a valid raw review verdict". A raw
    # excluded wrapper's own placeholder `null` verdict -- present, but not
    # an actual verdict -- must not satisfy the shape check on bare
    # presence alone: the blocker scan only ever rejects `approved is
    # False`, never treats `None` as a blocker, so this let an unrelated
    # nested quote payload through as an approved review with no real
    # outcome at all.
    ok, reason = _interpret_review({"approved": None, "data": {"quote": {"ask": "1.0"}}})
    assert ok is False
    assert "unexpected review response shape" in reason

    # Applies on the terminal layer too, not just the raw/intermediate ones.
    ok, reason = _interpret_review({"tradeable": None})
    assert ok is False
    ok, reason = _interpret_review({"state": ""})
    assert ok is False

    # A real, meaningful verdict -- including an explicit rejection --
    # still satisfies the shape check exactly as before.
    ok, reason = _interpret_review({"approved": False, "data": {"quote": {"ask": "1.0"}}})
    assert ok is False
    assert "approved=false" in reason
    ok, reason = _interpret_review({"approved": True, "data": {"quote": {"ask": "1.0"}}})
    assert ok is True


def test_interpret_review_requires_typed_review_verdicts_before_approving():
    # Codex review, PR #1169: "Require typed review verdicts before
    # approving". `{"approved": 0}`, `{"approved": "false"}`, and
    # `{"tradeable": 0}` are all NONBLANK per `_is_blank_broker_field`
    # (only None/blank-string reads as blank), so they satisfied the
    # shape check as a "recognized" verdict -- but the blocking scan only
    # ever tests `is False` (a strict identity check against the actual
    # Python boolean), never `0` or the string `"false"`, so neither
    # serialized/malformed negative is ever caught as blocking, and the
    # response reads as a shape-recognized, non-blocked review approving
    # a live order from a verdict that was never a real `True` either.
    ok, reason = _interpret_review({"approved": 0})
    assert ok is False
    assert "unexpected review response shape" in reason
    ok, reason = _interpret_review({"approved": "false"})
    assert ok is False
    assert "unexpected review response shape" in reason
    ok, reason = _interpret_review({"tradeable": 0})
    assert ok is False
    assert "unexpected review response shape" in reason

    # `state=0` (non-string) is likewise not a genuine state string.
    ok, reason = _interpret_review({"state": 0})
    assert ok is False
    assert "unexpected review response shape" in reason

    # Real, typed verdicts are unaffected.
    ok, reason = _interpret_review({"approved": True})
    assert ok is True
    ok, reason = _interpret_review({"approved": False})
    assert ok is False
    assert "approved=false" in reason
    ok, reason = _interpret_review({"tradeable": False})
    assert ok is False
    ok, reason = _interpret_review({"state": "rejected"})
    assert ok is False


def test_interpret_review_rejects_unrecognized_review_states():
    # Codex review, PR #1169: "Reject unsupported review states". The
    # blocking scan only ever checked `state in {"rejected", "failed"}`,
    # so a genuine negative or unknown verdict spelling this module has
    # never seen -- "denied", "blocked", or any other string -- fell
    # through as non-blocking and approved a live order on a review
    # verdict never actually understood. Now an ALLOWLIST: only a small
    # set of recognized approval strings pass; everything else -- known
    # negatives and unrecognized ones alike -- blocks.
    ok, reason = _interpret_review({"state": "denied"})
    assert ok is False
    assert "review state=denied" in reason
    ok, reason = _interpret_review({"state": "blocked"})
    assert ok is False
    assert "review state=blocked" in reason
    # Known recognized approvals still pass.
    ok, reason = _interpret_review({"state": "approved"})
    assert ok is True
    ok, reason = _interpret_review({"state": "CONFIRMED"})
    assert ok is True


def test_interpret_review_blocks_a_success_flag_contradicted_by_a_deeper_layer():
    # {"data": {"success": true, "data": {"success": false, "approved":
    # true}}}: the outer "success": true is skippable, so
    # `_unwrap_traversed_layers` (order-placement focused) stops descending
    # right there once it sees the inner layer flatly contradicts it --
    # correct for THAT caller, but it means the inner "success": false is
    # never reachable via that walk at all. `_interpret_review` must not
    # inherit that stopping point: a self-contradictory review response is
    # not a clean approval just because the OUTER layer happened to say so
    # (Codex review, PR #1164/#1165: "Block false health flags from every
    # review layer").
    response = {
        "data": {
            "success": True,
            "data": {"success": False, "approved": True},
        }
    }
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "success=false" in reason


def test_interpret_review_blocks_an_isError_flag_contradicted_by_a_deeper_layer():
    response = {
        "data": {
            "isError": False,
            "data": {"isError": True, "approved": True},
        }
    }
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "isError=true" in reason


def test_interpret_review_does_not_approve_on_a_bare_transport_success_flag():
    # {"success": true, "data": {"quote": {...}}}: the RAW top-level
    # envelope's own "success": true is a connector transport flag, not
    # review evidence -- the actual unwrapped content ({"quote": ...}) has
    # no review/alert fields at all. Including the bare transport envelope
    # unconditionally in the layer scan let this transport-level "success"
    # satisfy the shape check on its own, silently approving a response
    # that was never a review outcome to begin with (Codex review, PR
    # #1164/#1165: "Keep outer success from satisfying review shape
    # checks").
    response = {"success": True, "data": {"quote": {"ask": "1.0"}}}
    ok, reason = _interpret_review(response)
    assert ok is False
    assert "unexpected review response shape" in reason
    # A genuine outer-envelope FAILURE must still be caught -- excluding
    # the transport envelope from the review-evidence scan must not weaken
    # _response_failure's own separate outer-layer check.
    failing_response = {"success": False, "data": {"quote": {"ask": "1.0"}}}
    assert _interpret_review(failing_response)[0] is False


def test_interpret_review_ignores_falsy_error_placeholders():
    # Codex review, PR #1164: "Ignore falsy review error placeholders".
    # `_is_error_response`'s own rule already treats a falsy "error" --
    # False, 0, [], {} -- as a non-error placeholder. `_is_blank_broker_
    # field` only reads None/""/whitespace as blank, so these falsy-but-
    # non-blank values used to block every live placement on a broker's
    # own explicit "nothing wrong" sentinel.
    for falsy_error in (False, 0, [], {}):
        ok, reason = _interpret_review({"data": {"approved": True, "data": {"error": falsy_error}}})
        assert ok is True, f"error={falsy_error!r} should not block"

    # A genuinely truthy error on the same layer still blocks.
    ok, reason = _interpret_review({"data": {"approved": True, "data": {"error": "broker rejected"}}})
    assert ok is False
    assert "broker rejected" in reason


def test_unwrap_does_not_anchor_on_empty_review_metadata_in_order_history_responses():
    # Fresh evidence after the review-blocker fix above: adding review
    # field names as presence-only envelope evidence recreated the same
    # truncation for UNRELATED order-history calls, since `warnings` (and
    # friends) are read by `_unwrap`'s own callers too -- an orders
    # response shaped `{"data": {"warnings": [], "data": {"orders":
    # [...]}}}` stopped at the empty metadata layer, and the ordinary
    # same-day-order-exists path read that truncated view as an empty
    # final page (Codex review, PR #1164 round 15: "Skip empty review
    # metadata in order-history wrappers"). Review fields are no longer in
    # `_ENVELOPE_EVIDENCE_KEYS` at all -- `_interpret_review` finds its own
    # evidence by scanning every traversed layer instead.
    response = {"data": {"warnings": [], "data": {"orders": [{"id": "LIVE", "state": "queued"}]}}}
    unwrapped = _unwrap(response)
    assert unwrapped.get("orders") == [{"id": "LIVE", "state": "queued"}]


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


def test_unwrap_stops_descending_at_an_isError_bearing_layer():
    # {"data": {"isError": true, "error": "timeout", "data": {"order": {"id":
    # "ORD", "state": "queued"}}}}: the round-43 descent loop used to keep
    # walking past a layer reporting its own transport failure because
    # neither "isError" nor "error" was in `_ENVELOPE_EVIDENCE_KEYS`, landing
    # on the innermost `{"order": {...}}` as if it were a clean success and
    # leaving `_response_failure` unable to see the timeout at all (Codex
    # review, PR #859/#911 round 44: "Stop the envelope descent at a
    # failure-bearing layer").
    payload = {
        "data": {
            "isError": True,
            "error": "timeout",
            "data": {"order": {"id": "ORD", "state": "queued"}},
        }
    }
    assert _unwrap(payload) == {
        "isError": True,
        "error": "timeout",
        "data": {"order": {"id": "ORD", "state": "queued"}},
    }
    assert _response_failure(payload) == "timeout"


def test_unwrap_preserves_domain_fields_alongside_an_affirmative_flag():
    # Codex review, PR #1164: "Keep successful non-order payload fields
    # during unwrapping". `_unwrap` is shared by non-order consumers whose
    # payload fields (`buying_power`, `results`, ...) are not in
    # `_ENVELOPE_EVIDENCE_KEYS` at all. {"data": {"success": true,
    # "buying_power": "100", "data": {}}} used to let the affirmative
    # `success` flag license descent straight past the usable sibling
    # `buying_power` into the empty innermost layer, leaving
    # `available_buying_power` reading zero instead of the real value.
    payload = {"data": {"success": True, "buying_power": "100", "data": {}}}
    assert _unwrap(payload) == {"success": True, "buying_power": "100", "data": {}}

    # An unrecognized key with a BLANK value must not itself block descent
    # -- "warnings": [] carries no real content, so the layer below is
    # still the one that should win.
    payload_blank = {"data": {"success": True, "warnings": [], "data": {"orders": []}}}
    assert _unwrap(payload_blank) == {"orders": []}


def test_unwrap_descends_past_incidental_metadata_in_order_history_wrappers():
    # Codex review, PR #1164: "Descend past incidental metadata in
    # order-history wrappers". A NON-empty but INCIDENTAL review-bookkeeping
    # field (`warnings`, `approved`, `tradeable`, ...) on an order-history
    # wrapper is not the kind of caller-visible domain content the
    # "preserve domain fields" fix above was written for -- unlike
    # `buying_power`, it must not anchor descent and hide the real `orders`
    # collection one layer deeper.
    payload = {
        "data": {
            "success": True,
            "warnings": ["deprecated"],
            "data": {"orders": [{"id": "LIVE", "state": "queued"}]},
        }
    }
    assert _unwrap(payload) == {"orders": [{"id": "LIVE", "state": "queued"}]}

    # A genuine domain field (not incidental metadata) alongside the same
    # affirmative flag still anchors descent, per the fix above.
    payload_domain = {"data": {"success": True, "buying_power": "100", "data": {}}}
    assert _unwrap(payload_domain) == {"success": True, "buying_power": "100", "data": {}}


def test_unwrap_skips_informational_message_metadata():
    # Codex review, PR #1164: "Skip informational message metadata during
    # unwrapping". `_is_error_response` explicitly treats a bare `message`/
    # `detail` as informational, not an intrinsic failure signal, and
    # `request_id` is pure request-tracing metadata -- none of these are
    # caller-visible domain content, so they must not anchor descent any
    # more than `warnings` does.
    payload = {
        "data": {
            "message": "ok",
            "data": {"orders": [{"id": "LIVE", "state": "queued"}]},
        }
    }
    assert _unwrap(payload) == {"orders": [{"id": "LIVE", "state": "queued"}]}

    for key in ("detail", "request_id"):
        payload = {"data": {key: "abc123", "data": {"orders": []}}}
        assert _unwrap(payload) == {"orders": []}


def test_unwrap_ignores_arbitrary_unrecognized_metadata_keys():
    # Codex review, PR #1164: "Look past metadata containers when
    # unwrapping order history". `_DOMAIN_PAYLOAD_KEYS` is a finite
    # ALLOWLIST, not a denylist of "every incidental key" -- an earlier
    # denylist-based fix still anchored descent on ANY unrecognized truthy
    # sibling, including one with no relationship to any known incidental
    # category at all (a connector-added "metadata" wrapper). The
    # allowlist design means a genuinely novel, unlisted key never
    # anchors descent, whatever a connector happens to call it.
    payload = {
        "data": {
            "metadata": {"latency_ms": 12},
            "data": {"orders": [{"id": "LIVE", "state": "queued"}]},
        }
    }
    assert _unwrap(payload) == {"orders": [{"id": "LIVE", "state": "queued"}]}


def test_unwrap_descends_past_a_zero_valued_balance_field():
    # Codex review, PR #1168: "Do not let zero balance fields hide nested
    # orders". Balance aliases moved from presence-checked to truthy-
    # checked (alongside `results`) so an incidental `buying_power: 0` on
    # an order-history wrapper no longer anchors `_unwrap()`'s descent and
    # hides a real `orders` collection one layer deeper -- `_unwrap` now
    # settles PAST the zero-carrying layer, same as it already does for an
    # empty `results` list.
    payload = {
        "data": {
            "success": True,
            "buying_power": 0,
            "data": {"orders": [{"id": "LIVE", "state": "queued"}]},
        }
    }
    assert _unwrap(payload) == {"orders": [{"id": "LIVE", "state": "queued"}]}


def test_broker_available_buying_power_preserves_an_explicit_zero_across_layers():
    # Codex review, PR #1164/#1165: "Preserve explicit zero-valued domain
    # fields" (Codex review, PR #1168: "Do not let zero balance fields
    # hide nested orders" moved the anchoring behavior into
    # `available_buying_power` itself). A broker-reported `buying_power: 0`
    # is exactly as real an answer as a positive one, and must not be
    # discarded in favor of a stale nonzero figure sitting deeper in the
    # same envelope just because `_unwrap()` no longer settles on the
    # zero-carrying layer -- `available_buying_power` scans every
    # traversed layer itself instead of relying on `_unwrap`'s single
    # settled layer.
    payload = {"data": {"success": True, "buying_power": 0, "data": {"buying_power": "100"}}}
    broker = RobinhoodBroker(account="999000111", buying_power_fn=lambda **k: payload)
    assert broker.available_buying_power() == 0.0


def test_unwrap_does_not_let_an_empty_results_list_mask_order_history():
    # Codex review, PR #1164: "Do not let results mask nested order
    # history". `results` is a genuine domain-payload key too (every
    # quote/bar/tradability parser in this module reads it), but `_unwrap`
    # is shared with order-history unwrapping, and an EMPTY `results` list
    # on an intermediate wrapper carries no evidence at all -- exactly the
    # "nothing reported" reading every `results` consumer already gives it
    # via its own `data.get("results") or []`. Unlike the balance keys
    # (where 0 is a meaningful answer), `results` must be checked for
    # truthiness, not presence.
    payload = {
        "data": {
            "results": [],
            "data": {"orders": [{"id": "LIVE", "state": "queued"}]},
        }
    }
    assert _unwrap(payload) == {"orders": [{"id": "LIVE", "state": "queued"}]}

    # A genuinely non-empty `results` list still anchors descent.
    payload_nonempty = {"data": {"results": [{"quote": {"ask": "1.00"}}], "data": {}}}
    assert _unwrap(payload_nonempty) == {"results": [{"quote": {"ask": "1.00"}}], "data": {}}


def test_unwrap_stops_descending_at_a_next_cursor_bearing_layer():
    # {"data": {"next_cursor": "abc", "data": {"orders": []}}}: the pagination
    # pointer sits on an INTERMEDIATE data layer, sibling to (not inside) the
    # deeper layer that carries the actual "orders" list. Neither "next" nor
    # "next_cursor" counted as evidence, so the descent loop walked straight
    # past the layer advertising more history and returned only the deeper,
    # pointer-less {"orders": []} -- losing the pagination pointer entirely
    # (Codex review, PR #859/#911 round 44: "Retain pagination pointers
    # during envelope descent").
    payload = {"data": {"next_cursor": "abc", "data": {"orders": []}}}
    assert _unwrap(payload) == {"next_cursor": "abc", "data": {"orders": []}}


def test_robinhood_broker_pagination_fails_closed_on_a_next_cursor_nested_below_the_first_data_layer():
    # Same shape as above, exercised end to end through the crash-recovery
    # lookup: a first page advertises more history via a next_cursor sitting
    # above its own nested "orders" list -- a genuinely unprovable page,
    # since the descent now correctly stops at the next_cursor layer rather
    # than silently reading the pointer-less "orders": [] one level deeper
    # as if it were the whole (empty) history.
    #
    # BEFORE this fix, _unwrap discarded the next_cursor entirely, landed on
    # {"orders": []}, and strategy_order_with_ref_id (require_provable_
    # history=True) read that as a complete, empty history and returned
    # None -- "the claimed order was never submitted" -- letting a caller
    # release a crash lease and authorize a duplicate purchase even though a
    # real order was waiting on page two the lookup never reached.
    #
    # AFTER this fix, the ambiguous page (a next_cursor with no "orders" of
    # its own visible at the layer descent stopped on) is correctly
    # unprovable and raises instead, per strategy_order_with_ref_id's own
    # documented contract: "raises instead of returning None, so the caller
    # preserves the lease rather than authorizing a duplicate purchase."
    # Fail-closed, not a silent false negative, is the fix (Codex review, PR
    # #859/#911 round 44: "Retain pagination pointers during envelope
    # descent").
    pages = [{"data": {"next_cursor": "PAGE2", "data": {"orders": []}}}]

    def orders_fn(**kwargs):
        return pages[0]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_unwrap_stops_descending_at_a_bare_cursor_bearing_layer():
    # {"data": {"cursor": "abc", "data": {"orders": []}}}: _next_order_cursor
    # reads a bare "cursor" field too (its own last-resort fallback, used
    # when a page carries no "next"/"next_cursor"), but "cursor" was missing
    # from _ENVELOPE_EVIDENCE_KEYS even after the next/next_cursor fix, so
    # the descent loop walked past THIS pointer the same way (Codex review,
    # PR #859/#911 round 44: "Preserve bare cursors during recursive
    # unwrapping").
    payload = {"data": {"cursor": "abc", "data": {"orders": []}}}
    assert _unwrap(payload) == {"cursor": "abc", "data": {"orders": []}}


def test_unwrap_stops_descending_at_a_contradicted_success_flag():
    # {"data": {"success": true, "data": {"success": false, "order": {"id":
    # "A", "state": "rejected", "cumulative_quantity": "0"}}}}: the outer
    # layer's "success": true is normally skippable (affirms nothing went
    # wrong), but the layer descent would land on next reports the OPPOSITE
    # value for the identical flag -- a genuine self-contradiction, not an
    # empty wrapper. Descending anyway lost the outer "success": true
    # entirely (nothing else ever inspects that layer again), leaving
    # _failure_is_broker_authored seeing only the inner "success": false and
    # proving a rejection the response's own outer layer just as clearly
    # disputed (Codex review, PR #1164: "Preserve contradictory success
    # flags during envelope descent").
    payload = {
        "data": {
            "success": True,
            "data": {
                "success": False,
                "order": {"id": "A", "state": "rejected", "cumulative_quantity": "0"},
            },
        }
    }
    assert _unwrap(payload) == {
        "success": True,
        "data": {
            "success": False,
            "order": {"id": "A", "state": "rejected", "cumulative_quantity": "0"},
        },
    }
    assert _failure_is_broker_authored(payload) is False


def test_unwrap_stops_descending_at_a_contradicted_ok_flag():
    payload = {"data": {"ok": True, "data": {"ok": False, "order": {"id": "A"}}}}
    assert _unwrap(payload) == {"ok": True, "data": {"ok": False, "order": {"id": "A"}}}


def test_unwrap_stops_descending_at_a_contradicted_isError_flag():
    payload = {"data": {"isError": False, "data": {"isError": True, "error": "boom"}}}
    assert _unwrap(payload) == {"isError": False, "data": {"isError": True, "error": "boom"}}


def test_unwrap_still_descends_past_a_skippable_flag_with_no_contradiction():
    # The contradiction check must not make _unwrap stop descending on
    # ordinary, non-contradictory affirmative flags -- only a genuine
    # opposite value one layer deeper halts it.
    payload = {"data": {"success": True, "data": {"order": {"id": "A", "state": "queued"}}}}
    assert _unwrap(payload) == {"order": {"id": "A", "state": "queued"}}


def test_unwrap_stops_descending_on_a_cross_aliased_health_flag_contradiction():
    # Codex review, PR #1164: "Compare equivalent health flags before
    # descending". isError/success/ok are interchangeable health-flag
    # spellings a connector can mix ACROSS layers of the same response --
    # an outer "ok: true" contradicted by a nested "success: false" is the
    # identical self-contradiction as two layers using the SAME flag name,
    # but a same-name-only comparison missed it and let _unwrap descend
    # straight past the outer's own affirmation.
    payload = {"data": {"ok": True, "data": {"success": False, "order": {"id": "A", "state": "rejected"}}}}
    assert _unwrap(payload) == {"ok": True, "data": {"success": False, "order": {"id": "A", "state": "rejected"}}}

    # The mirror aliasing: outer "success: true" contradicted by a nested
    # "ok: false".
    payload = {"data": {"success": True, "data": {"ok": False, "order": {"id": "B"}}}}
    assert _unwrap(payload) == {"success": True, "data": {"ok": False, "order": {"id": "B"}}}


def test_unwrap_terminates_on_a_self_referential_cyclic_payload():
    # Codex review, PR #1169: "Bound recursive data-layer traversal". None
    # of the envelope-descent loops (_unwrap_traversed_layers,
    # _unwrap_seeking_order_evidence, _unwrap_hides_contradicted_nested_data,
    # _all_data_layers) tracked visited objects or imposed a depth limit --
    # an injected broker callable returning a CYCLIC malformed mapping
    # (response["data"] = response) made every one of them loop forever,
    # since the "not a dict"/"stop descending" exit conditions never fire
    # on a node that keeps pointing back to itself. This must terminate
    # (via `_MAX_ENVELOPE_DESCENT_DEPTH`), not hang.
    cyclic: dict = {"order": {"id": "A", "state": "queued"}}
    cyclic["data"] = cyclic
    result = _unwrap(cyclic)
    assert isinstance(result, dict)

    # _interpret_review is built on _all_data_layers, the other unbounded
    # walk Codex's own repro named directly -- it must also terminate,
    # whatever verdict it reaches on a malformed cyclic review response.
    ok, reason = _interpret_review(cyclic)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)

    # A two-node cycle (A -> B -> A), not just direct self-reference.
    node_a: dict = {"approved": True}
    node_b: dict = {"approved": False}
    node_a["data"] = node_b
    node_b["data"] = node_a
    ok, reason = _interpret_review(node_a)
    assert isinstance(ok, bool)
    assert isinstance(reason, str)


def test_order_rows_contradict_agrees_on_complementary_fill_units():
    # A singular row reporting fill ONLY via a notional alias beside a
    # same-ID collection row reporting the SAME fill only via a quantity
    # alias must not be flagged contradictory just because each unit group
    # is "one-sided" on its own -- both rows agree the order filled, just
    # through complementary units (Codex review, PR #1164: "Avoid flagging
    # complementary fill units as one-sided").
    a = {"id": "A", "state": "filled", "executed_notional": "5"}
    b = {"id": "A", "state": "filled", "cumulative_quantity": "5"}
    assert _order_rows_contradict(a, b) is False


def test_order_rows_contradict_still_flags_a_genuinely_one_sided_fill():
    # Sanity check the fix above did not just disable the one-sided rule
    # entirely: a row with fill evidence in either unit against a row with
    # NO fill evidence at all is still one-sided.
    a = {"id": "A", "state": "filled", "executed_notional": "5"}
    b = {"id": "A", "state": "filled"}
    assert _order_rows_contradict(a, b) is True


def test_order_rows_contradict_flags_zero_versus_positive_across_complementary_units():
    # A ZERO reading in one unit (executed_notional="0") beside a POSITIVE
    # reading in the complementary unit (cumulative_quantity="1") both count
    # as non-blank "has evidence" for their own group -- neither the
    # same-group comparison (the groups never overlap) nor the one-sided
    # rule (both sides have SOME evidence) used to catch this, silently
    # treating a partially-executed order as an agreeing rejection (Codex
    # review, PR #1164: "Treat zero complementary fill evidence as
    # contradictory").
    a = {"id": "A", "state": "rejected", "executed_notional": "0"}
    b = {"id": "A", "state": "rejected", "cumulative_quantity": "1"}
    assert _order_rows_contradict(a, b) is True
    # Symmetric direction.
    assert _order_rows_contradict(b, a) is True
    # Both proving zero, across complementary units, is still agreement.
    c = {"id": "A", "state": "rejected", "executed_notional": "0"}
    d = {"id": "A", "state": "rejected", "cumulative_quantity": "0"}
    assert _order_rows_contradict(c, d) is False
    # An unparseable reading on one side must not be treated as proving
    # zero OR positive itself -- neither this check nor the both-sided
    # comparison (different unit groups) catches it -- but it is not a
    # free pass to agreement either: a LATER fix (see
    # test_order_rows_contradict_rejects_unreadable_complementary_fill_
    # evidence, Codex review, PR #1164: "Reject unreadable complementary
    # fill evidence") makes unreadable evidence alongside the other side's
    # genuine evidence a conflict in its own right, since we cannot verify
    # what the unreadable figure would have agreed with.
    e = {"id": "A", "state": "rejected", "executed_notional": "not-a-number"}
    f = {"id": "A", "state": "rejected", "cumulative_quantity": "1"}
    assert _order_rows_contradict(e, f) is True
    # A JSON integer too large for float() (OverflowError, not ValueError)
    # must be treated the same as any other unparseable reading, not raise
    # (Codex review, PR #1164/#1165: "Catch overflow while parsing fill
    # execution state") -- and, per the same later fix, still a conflict
    # against the other side's genuine evidence.
    g = {"id": "A", "state": "rejected", "executed_notional": 10**1000}
    h = {"id": "A", "state": "rejected", "cumulative_quantity": "1"}
    assert _order_rows_contradict(g, h) is True


def test_robinhood_broker_pagination_fails_closed_on_a_bare_cursor_nested_below_the_first_data_layer():
    # Same shape as above, exercised end to end: a first page advertises
    # more history via a bare "cursor" (not "next_cursor") sitting above its
    # own nested "orders" list. Before this fix, _unwrap discarded that
    # pointer and strategy_order_with_ref_id read the deeper, pointer-less
    # {"orders": []} as a complete empty history and returned None instead
    # of raising -- the same false-negative "order never submitted" outcome
    # the next_cursor fix closed, for the bare-cursor alias specifically.
    pages = [{"data": {"cursor": "PAGE2", "data": {"orders": []}}}]

    def orders_fn(**kwargs):
        return pages[0]

    with pytest.raises(RuntimeError, match="unrecognizable page"):
        RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
            "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
        )


def test_robinhood_broker_pagination_accepts_nested_empty_orders_after_an_echoed_cursor():
    """Codex review, PR #1168: "Accept nested empty orders after an echoed
    cursor". Page one advertises more history via next_cursor="PAGE2".
    Page two -- requested WITH cursor="PAGE2" -- echoes that same value
    back as a bare "cursor" (the broker's own end-of-list convention
    _next_order_cursor already reads as "no more pages"), wrapping a
    genuinely empty nested orders list: {"cursor": "PAGE2", "data":
    {"orders": []}}}. This is NOT "a pointer to more history paired with
    an empty sub-page" (the shape the sibling fail-closed tests exist to
    catch) -- it is the terminal page's own echo, and require_provable_
    history=True must recognize it as a genuine, provably empty final
    page rather than raising and blocking crash recovery indefinitely."""
    pages = [
        {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": "PAGE2"}},
        {"cursor": "PAGE2", "data": {"orders": []}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "PAGE2"]
    assert order is None


def test_robinhood_broker_pagination_accepts_doubly_nested_empty_orders_after_an_echoed_cursor():
    """Codex review, PR #1169: "Traverse all wrappers for echoed empty order
    pages". Same echoed-cursor final-page shape as the sibling test above,
    but the empty orders collection sits behind TWO pass-through "data"
    wrappers instead of one: {"cursor": "PAGE2", "data": {"data": {"orders":
    []}}}}. Checking only the immediate child missed the empty collection
    entirely and returned False, so require_provable_history=True rejected
    a valid, complete history and could strand a crash-recovery lease
    indefinitely."""
    pages = [
        {"data": {"orders": [{"state": "filled", "ref_id": "other"}], "next_cursor": "PAGE2"}},
        {"cursor": "PAGE2", "data": {"data": {"orders": []}}},
    ]
    seen_cursors = []

    def orders_fn(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    order = RobinhoodBroker(orders_fn=orders_fn).strategy_order_with_ref_id(
        "AAPL", datetime(2026, 7, 28).date(), "crashed-fire"
    )
    assert seen_cursors == [None, "PAGE2"]
    assert order is None


def test_broker_place_does_not_treat_isError_wrapped_order_as_success():
    # Same shape as above, exercised end to end through place_market_buy:
    # before the fix this returned "ORD" as a successfully placed order id,
    # silently dropping a connector timeout that may have left an actual
    # live order this run never accounted for (Codex review, PR #859/#911
    # round 44: "Stop the envelope descent at a failure-bearing layer").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "data": {
                "isError": True,
                "error": "timeout",
                "data": {"order": {"id": "ORD", "state": "queued"}},
            }
        },
    )
    # The unresolved connector failure must halt the batch, not hand back
    # the nested order's id as if the placement had succeeded. No usable
    # id survives to the caller either way (the order sits two "data"
    # levels below the isError layer, out of reach of the envelope-level
    # id carriage this path uses), so a plain RuntimeError -- not a
    # returned "ORD" -- is the correct, fail-closed outcome here.
    with pytest.raises(RuntimeError, match="timeout"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_fill_evidence_values_conflict_ignores_formatting_differences():
    # Outer "0" vs inner "0.0" describe the identical quantity; a raw string
    # compare flagged this as a conflict (Codex review, PR #859/#911 round
    # 44: "Compare fill aliases numerically before declaring conflicts").
    assert _fill_evidence_values_conflict("0", "0.0") is False
    assert _fill_evidence_values_conflict("5", "5.00") is False
    assert _fill_evidence_values_conflict("5", "3") is True
    # Unparseable values fall back to the pre-fix string-inequality reading
    # -- still a conflict when they differ, no regression there.
    assert _fill_evidence_values_conflict("abc", "xyz") is True
    assert _fill_evidence_values_conflict("abc", "abc") is False


def test_order_rows_contradict_unwraps_mapping_shaped_fill_aliases_before_comparing():
    # A Robinhood money value is a scalar OR an {"amount": ...} mapping.
    # executed_notional={"amount": "5"} on one duplicate row and plain
    # filled_notional="5" on another report the identical $5 fill in two
    # encodings this module already treats as equivalent elsewhere -- but
    # feeding the raw mapping into _fill_evidence_values_conflict made
    # float(dict) raise, falling back to a string-inequality reading that
    # declared the pair contradictory and halted an otherwise consistent,
    # agreeing accepted order (Codex review, PR #1164 round 13: "Unwrap
    # mapping-shaped fill aliases before comparing rows").
    a = {"id": "ORD", "state": "filled", "executed_notional": {"amount": "5"}}
    b = {"id": "ORD", "state": "filled", "filled_notional": "5"}
    assert _order_rows_contradict(a, b) is False

    # A genuine mismatch under the same mapping encoding must still be
    # caught as a real conflict.
    c = {"id": "ORD", "state": "filled", "executed_notional": {"amount": "5"}}
    d = {"id": "ORD", "state": "filled", "filled_notional": "3"}
    assert _order_rows_contradict(c, d) is True


def test_order_rows_contradict_still_flags_a_one_sided_fill_against_a_cancelled_row():
    # Same one-sided sanity check as test_order_rows_contradict_still_flags_a_
    # genuinely_one_sided_fill above, with a genuinely TERMINAL opposing state
    # (cancelled, no fill evidence at all) rather than a bare absence.
    c = {"id": "ORD", "state": "filled", "executed_notional": "5"}
    d = {"id": "ORD", "state": "cancelled"}
    assert _order_rows_contradict(c, d) is True


def test_order_rows_contradict_rejects_zero_versus_positive_complementary_units():
    # Codex review, PR #1164: "Reject zero-versus-positive complementary
    # fill units". An explicit ZERO in one unit alongside a POSITIVE figure
    # in the OTHER unit on the same-ID row cannot both describe the same
    # execution -- unlike two positive figures in different units (a
    # genuine $/share pair), this is a genuine disagreement about whether
    # anything filled at all.
    a = {"id": "ORD", "state": "filled", "executed_notional": "0"}
    b = {"id": "ORD", "state": "filled", "cumulative_quantity": "5"}
    assert _order_rows_contradict(a, b) is True

    # The mirror: explicit zero quantity beside a positive notional.
    c = {"id": "ORD", "state": "filled", "cumulative_quantity": "0"}
    d = {"id": "ORD", "state": "filled", "executed_notional": "5"}
    assert _order_rows_contradict(c, d) is True


def test_order_rows_contradict_rejects_mismatched_reconstructed_notional():
    # Codex review, PR #1164: "Check reconstructed amounts across
    # complementary fill units". A quantity-side row that also carries
    # `average_price` reconstructs a comparable notional (quantity *
    # average_price) -- $5 against a reconstructed $20 for the SAME order
    # is a real conflict, not two agreeing complementary units.
    a = {"id": "ORD", "state": "filled", "executed_notional": "5"}
    b = {
        "id": "ORD",
        "state": "filled",
        "cumulative_quantity": "2",
        "average_price": "10",
    }
    assert _order_rows_contradict(a, b) is True

    # The mirror direction.
    assert _order_rows_contradict(b, a) is True

    # A reconstructed notional that actually agrees is still not a conflict.
    c = {"id": "ORD", "state": "filled", "executed_notional": "20"}
    assert _order_rows_contradict(c, b) is False


def test_order_rows_contradict_uses_a_money_tolerance_for_reconstructed_notionals():
    # Codex review, PR #1164: "Use a money tolerance for reconstructed
    # notionals". Independently-rounded quantity and average price can
    # reconstruct to a few tenths of a cent off a genuinely agreeing direct
    # notional -- 0.3333 * 15 = 4.9995 against "5.00" -- and the tight
    # `_FILL_EVIDENCE_NUMERIC_TOLERANCE` (1e-9) used to declare that
    # ordinary rounding gap a conflict. The $0.001 money tolerance
    # `_recovered_broker_notional`'s own callers use must apply here too.
    a = {"id": "ORD", "state": "filled", "executed_notional": "5.00"}
    b = {
        "id": "ORD",
        "state": "filled",
        "cumulative_quantity": "0.3333",
        "average_price": "15",
    }
    assert _order_rows_contradict(a, b) is False

    # A gap beyond the money tolerance is still a real conflict.
    c = {"id": "ORD", "state": "filled", "executed_notional": "4.90"}
    assert _order_rows_contradict(c, b) is True


def test_order_rows_contradict_rejects_unreadable_complementary_fill_evidence():
    # Codex review, PR #1164: "Reject unreadable complementary fill
    # evidence". A MALFORMED value in one unit group (`executed_notional=
    # "bad"`) alongside a same-ID row's genuine evidence in the OTHER
    # group (`cumulative_quantity="1"`) used to qualify for the
    # complementary-units exception just as readily as a real $/share
    # pair -- neither the both-sided comparison (different groups) nor
    # the zero-vs-positive check (unparseable) caught it, and the
    # one-sided rule never fires since both sides read as "has evidence".
    # We cannot verify what the unreadable figure would have agreed with,
    # so this must be a conflict, not agreement.
    a = {"id": "ORD", "state": "filled", "executed_notional": "bad"}
    b = {"id": "ORD", "state": "filled", "cumulative_quantity": "1"}
    assert _order_rows_contradict(a, b) is True
    assert _order_rows_contradict(b, a) is True

    # A negative fill value is equally untrustworthy.
    c = {"id": "ORD", "state": "filled", "cumulative_quantity": "-1"}
    assert _order_rows_contradict(a, c) is True

    # Two genuinely readable complementary values still agree.
    d = {"id": "ORD", "state": "filled", "executed_notional": "5"}
    assert _order_rows_contradict(d, b) is False


def test_order_rows_contradict_lets_identical_unreadable_fill_values_agree():
    # Codex review, PR #1169: "Let identical unreadable fill values agree".
    # Both same-ID rows reporting the IDENTICAL unreadable value in the
    # SAME unit group (`executed_notional="N/A"` on both) is not a real
    # dispute -- they cannot both be wrong about the same value the same
    # way by accident -- but the blanket "unreadable on one side plus ANY
    # fill evidence on the other" check used to treat the opposing row's
    # own copy of that identical "N/A" as conflicting evidence.
    a = {"id": "ORD", "state": "filled", "executed_notional": "N/A"}
    b = {"id": "ORD", "state": "filled", "executed_notional": "N/A"}
    assert _order_rows_contradict(a, b) is False
    assert _order_rows_contradict(b, a) is False

    # A genuinely DIFFERENT unreadable value in the same group still
    # conflicts -- these are not both wrong in the same way.
    c = {"id": "ORD", "state": "filled", "executed_notional": "bad"}
    assert _order_rows_contradict(a, c) is True

    # An identical unreadable value in the SAME group, but with a genuine
    # readable value ALSO present on one side, still conflicts.
    d = {"id": "ORD", "state": "filled", "executed_notional": "N/A", "cumulative_quantity": "5"}
    assert _order_rows_contradict(a, d) is True


def test_order_rows_contradict_survives_an_oversized_unreadable_fill_value():
    """Codex review, PR #1169: "Safely render oversized unreadable fill
    values". A native int beyond Python's integer-to-string conversion
    digit limit (e.g. `cumulative_quantity=10**5000`) is already
    classified unreadable by `_is_readable_nonnegative_fill_value`, but
    the bare `str(_v)` call used to build the comparison key raised
    `ValueError` the instant anything rendered it, escaping
    `_order_rows_contradict()` entirely instead of comparing the two
    rows' unreadable evidence."""
    huge = 10**5000
    a = {"id": "ORD", "state": "filled", "cumulative_quantity": huge}
    b = {"id": "ORD", "state": "filled", "cumulative_quantity": huge}
    assert _order_rows_contradict(a, b) is False
    c = {"id": "ORD", "state": "filled", "cumulative_quantity": "bad"}
    assert _order_rows_contradict(a, c) is True


def test_order_rows_contradict_rejects_unreadable_prices_in_complementary_evidence():
    # Codex review, PR #1164: "Reject unreadable prices in complementary
    # fill evidence". A quantity-side row's `average_price` can be
    # PRESENT but unreadable -- malformed, non-numeric, or non-finite --
    # a different reading than the price being ABSENT altogether.
    # `executed_notional="5"` beside `cumulative_quantity="1",
    # average_price="bad"` used to return no reconstructed value for
    # either reason alike, so it fell back to the ordinary
    # notional-only-vs-quantity-only "complementary units" reading and
    # was accepted as agreeing -- discarding the collection's own
    # unreadable valuation instead of preserving it for reconciliation.
    a = {"id": "ORD", "state": "filled", "executed_notional": "5"}
    b = {
        "id": "ORD",
        "state": "filled",
        "cumulative_quantity": "1",
        "average_price": "bad",
    }
    assert _order_rows_contradict(a, b) is True
    assert _order_rows_contradict(b, a) is True

    # A row with a genuinely ABSENT price still gets the ordinary
    # complementary-units reading, unaffected by this fix.
    c = {"id": "ORD", "state": "filled", "cumulative_quantity": "1"}
    assert _order_rows_contradict(a, c) is False

    # Codex review, PR #1164: "Require quantity before rejecting an
    # unreadable price". A row carrying a malformed `average_price` but
    # NO quantity evidence at all has nothing to reconstruct, so it must
    # not be flagged as an unreadable-price conflict -- a rejected
    # singular row with only `average_price="bad"` beside an agreeing
    # collection row's `executed_notional="0"` is a proven, agreeing
    # zero-fill rejection.
    d = {"id": "ORD", "state": "rejected", "average_price": "bad"}
    e = {"id": "ORD", "state": "rejected", "executed_notional": "0"}
    assert _order_rows_contradict(d, e) is False

    # Codex review, PR #1164/#1169: "Ignore unreadable prices when
    # executed quantity is zero". A row carrying a malformed
    # `average_price` alongside an EXPLICIT ZERO quantity has nothing to
    # reconstruct either -- a zero quantity needs no price to value it --
    # so `cumulative_quantity="0", average_price="bad"` beside an
    # agreeing `executed_notional="0"` is still a proven, agreeing
    # zero-fill rejection, not a conflict.
    f = {"id": "ORD", "state": "rejected", "cumulative_quantity": "0", "average_price": "bad"}
    g = {"id": "ORD", "state": "rejected", "executed_notional": "0"}
    assert _order_rows_contradict(f, g) is False
    assert _order_rows_contradict(g, f) is False
    assert _order_rows_contradict(e, d) is False

    # Codex review, PR #1169: "Ignore unreadable prices when executed
    # quantity is zero". A row carrying a malformed `average_price`
    # alongside an EXPLICIT ZERO quantity has nothing to reconstruct
    # either -- a zero quantity needs no price to value it -- so
    # `cumulative_quantity="0", average_price="bad"` beside an agreeing
    # `executed_notional="0"` is still a proven, agreeing zero-fill
    # rejection, not a conflict.
    f = {"id": "ORD", "state": "rejected", "cumulative_quantity": "0", "average_price": "bad"}
    g = {"id": "ORD", "state": "rejected", "executed_notional": "0"}
    assert _order_rows_contradict(f, g) is False
    assert _order_rows_contradict(g, f) is False


def test_order_rows_contradict_uses_either_rows_price_to_reconstruct_notional():
    # Codex review, PR #1164: "Use prices from either duplicate
    # representation". A singular row's `executed_notional="5",
    # average_price="10"` beside a same-ID collection row's bare
    # `cumulative_quantity="2"` (no `average_price` of its own) used to
    # reconstruct nothing for either side -- the quantity-bearing row has
    # no price of its own, so it fell back to the ordinary
    # notional-only-vs-quantity-only "complementary units" reading and was
    # accepted as agreeing, even though $5 direct and 2 x $10 = $20
    # reconstructed (using the OTHER row's own price) describe the same
    # shared order.
    a = {"id": "ORD", "state": "filled", "executed_notional": "5", "average_price": "10"}
    b = {"id": "ORD", "state": "filled", "cumulative_quantity": "2"}
    assert _order_rows_contradict(a, b) is True
    assert _order_rows_contradict(b, a) is True

    # A genuinely agreeing pair using the counterpart's price still agrees:
    # 2 shares x $10/share reconstructs to exactly the reported $20 direct.
    c = {"id": "ORD", "state": "filled", "executed_notional": "20", "average_price": "10"}
    assert _order_rows_contradict(c, b) is False
    assert _order_rows_contradict(b, c) is False


def test_order_rows_contradict_compares_average_price_numerically():
    # Codex review, PR #1169: "Compare average prices numerically across
    # duplicate rows". `average_price` is not a fill-evidence alias, so it
    # falls into the generic any-other-key comparison loop, which compared
    # it as a raw string -- "10" vs "10.0" is the identical price in two
    # equivalent numeric spellings, but a raw string comparison declared
    # them contradictory, discarding an otherwise agreeing order's usable
    # ID.
    a = {"id": "ORD", "state": "filled", "average_price": "10"}
    b = {"id": "ORD", "state": "filled", "average_price": "10.0"}
    assert _order_rows_contradict(a, b) is False
    assert _order_rows_contradict(b, a) is False

    # A genuinely different price still conflicts.
    c = {"id": "ORD", "state": "filled", "average_price": "11"}
    assert _order_rows_contradict(a, c) is True

    # The supported {"amount": ...} money-wrapper encoding is unwrapped
    # before the numeric comparison, matching every other average_price
    # reader in this module.
    d = {"id": "ORD", "state": "filled", "average_price": {"amount": "10"}}
    assert _order_rows_contradict(a, d) is False

    # A malformed price on either side falls back to the original
    # string/equality comparison, unchanged.
    e = {"id": "ORD", "state": "filled", "average_price": "bad"}
    assert _order_rows_contradict(a, e) is True


def test_order_rows_contradict_rejects_conflicting_fill_aliases_within_one_row():
    # Codex review, PR #1169: "Reject conflicting fill aliases within each
    # row". Row `a` contradicts ITSELF through two notional aliases:
    # executed_notional="5" and filled_notional="6" cannot both be the
    # same fill's dollar amount. Row `b` reports only a COMPLEMENTARY unit
    # (cumulative_quantity="1"), so the notional group never populates on
    # both sides and the cross-side check alone never sees `a`'s own
    # internal conflict -- but a row that cannot describe one coherent
    # fill on its own must never be read as agreeing with anything.
    a = {"id": "ORD", "state": "filled", "executed_notional": "5", "filled_notional": "6"}
    b = {"id": "ORD", "state": "filled", "cumulative_quantity": "1"}
    assert _order_rows_contradict(a, b) is True
    assert _order_rows_contradict(b, a) is True

    # The identical shape in the quantity group.
    c = {"id": "ORD", "state": "filled", "cumulative_quantity": "1", "executed_quantity": "2"}
    d = {"id": "ORD", "state": "filled", "executed_notional": "5"}
    assert _order_rows_contradict(c, d) is True

    # Two aliases within the SAME group that agree numerically ("5" and
    # "5.00") are not a self-conflict.
    e = {"id": "ORD", "state": "filled", "executed_notional": "5", "filled_notional": "5.00"}
    f = {"id": "ORD", "state": "filled", "cumulative_quantity": "1"}
    assert _order_rows_contradict(e, f) is False


def test_order_rows_contradict_agrees_on_a_one_sided_explicit_zero_fill():
    # A cancelled row with NO fill-evidence fields at all, alongside a
    # same-ID row reporting a single explicit zero quantity, is a
    # consistent terminal zero-fill rejection reported through only one of
    # the two rows -- not a genuine one-sided conflict. The one-sided
    # check used to build its "does the lone side's own evidence prove a
    # fill" probe by reading EVERY field in the alias group via `.get()`,
    # materializing every ABSENT field in the group as an explicit `None`
    # alongside the one genuinely-present zero. `_reconciled_order_may_
    # have_executed` treats an explicit `None` as unreadable, blocking
    # evidence -- different from the field being absent -- so the probe
    # came back "unresolved" for a row that in fact reports a clean,
    # explicit zero, and `_order_rows_contradict` raised a spurious
    # conflict (Codex review, PR #1164 round 15: "Preserve absent aliases
    # when checking one-sided zero fills").
    a = {"id": "ORD", "state": "cancelled"}
    b = {"id": "ORD", "state": "cancelled", "cumulative_quantity": "0"}
    assert _order_rows_contradict(a, b) is False


def test_failure_is_broker_authored_sees_a_success_flag_skipped_during_unwrap():
    # The intermediate layer's affirmative success:true is skipped by
    # _unwrap's descent (treated as "no evidence" so descent can reach the
    # real order data), but a deeper layer's success:false directly
    # contradicts it. _failure_is_broker_authored used to compare only
    # `response` and `_unwrap(response)` -- neither of which retains the
    # skipped middle layer's claim -- and called the inner failure a clean,
    # broker-authored rejection despite the self-contradiction (Codex
    # review, PR #1164 round 14: "Preserve contradictory success flags
    # during envelope descent").
    response = {
        "data": {
            "success": True,
            "data": {
                "success": False,
                "order": {"id": "A", "state": "rejected", "cumulative_quantity": "0"},
            },
        }
    }
    assert _failure_is_broker_authored(response) is False

    # An ordinary, non-contradictory failure (no affirmative flag anywhere
    # in the descent path) is still reported as broker-authored.
    plain_failure = {"success": False, "error": "instrument is not tradable"}
    assert _failure_is_broker_authored(plain_failure) is True


def test_failure_is_broker_authored_sees_past_a_domain_field_on_an_intermediate_layer():
    # Codex review, PR #1164: "Continue scanning past domain fields for
    # success conflicts". `_unwrap_traversed_layers`'s descent loop stops
    # early on a recognized DOMAIN payload key (`buying_power`, `results`)
    # -- correct for `_unwrap`'s own single-answer descent, but wrong for
    # this scan: `{"success": false, "data": {"buying_power": "100",
    # "data": {"success": true}}}` stopped the traversal at the
    # buying-power layer, hiding the deeper `success: true` that disputes
    # the outer refusal. This scan now uses `_all_data_layers`, which
    # walks unconditionally, so the domain field no longer hides it.
    response = {
        "success": False,
        "data": {
            "buying_power": "100",
            "data": {"success": True},
        },
    }
    assert _failure_is_broker_authored(response) is False


def test_merge_envelope_layers_treats_differently_formatted_equal_fill_values_as_non_conflicting():
    outer = {"success": True, "cumulative_quantity": "0"}
    inner = {"order": {"id": "ORD", "state": "filled", "cumulative_quantity": "0.0"}}
    merged = _merge_envelope_layers_prefer_innermost_nonblank(outer, inner, inner["order"])
    assert "_conflicting_fill_evidence" not in merged


def test_merge_envelope_layers_still_flags_a_genuine_fill_value_mismatch():
    outer = {"cumulative_quantity": "5"}
    inner = {"cumulative_quantity": "3"}
    merged = _merge_envelope_layers_prefer_innermost_nonblank(outer, inner)
    assert merged["_conflicting_fill_evidence"] is True


def test_broker_place_does_not_raise_for_differently_formatted_equal_fill_evidence():
    # outer cumulative_quantity="0" vs inner "0.0" on an otherwise clean,
    # agreeing order used to raise AmbiguousPlacementResponseError and halt
    # the batch over nothing but a stringification difference (Codex
    # review, PR #859/#911 round 44: "Compare fill aliases numerically
    # before declaring conflicts").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "cumulative_quantity": "0",
            "data": {"order": {"id": "ORD", "state": "filled", "cumulative_quantity": "0.0"}},
        },
    )
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "ORD"


def test_broker_place_preserves_structural_ambiguity_from_failure_envelopes():
    # {"success": false, "error": "order rejected downstream", "orders":
    # [{"id": "A", "state": "queued"}, {"id": "B", "state": "queued"}]}:
    # a broker-authored failure naming TWO conflicting nested orders with
    # NO usable envelope-level id. The old fallback raised a plain
    # `RuntimeError` -- no `structurally_ambiguous` marker at all -- so
    # near_close_dca's own structural-ambiguity downgrade (which exists
    # precisely to stop a coincidental history match on ONE disputed
    # identity from resolving a response's real ambiguity) never even
    # applied, since it only fires for `AmbiguousPlacementResponseError`.
    # An exact-ref history match for "A" alone could then be accepted
    # outright while "B" silently disappeared (Codex review, PR #859/#911
    # round 43: "Preserve structural ambiguity from failure envelopes").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "error": "order rejected downstream",
            "orders": [{"id": "A", "state": "queued"}, {"id": "B", "state": "queued"}],
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.structurally_ambiguous is True


def test_order_lifecycle_aliases_conflict_treats_cancelled_and_canceled_as_the_same_state():
    # Robinhood reports both "cancelled" and "canceled" for the identical
    # terminal state. A bare lowercased-string compare read that spelling
    # difference alone as a genuine conflict between state/status (Codex
    # review, PR #859/#911 round 44: "Canonicalize canceled lifecycle
    # aliases before comparing").
    assert _order_lifecycle_aliases_conflict({"state": "cancelled", "status": "canceled"}) is False
    assert _order_lifecycle_aliases_conflict({"state": "Cancelled", "status": "CANCELED"}) is False
    # A genuine disagreement must still be caught.
    assert _order_lifecycle_aliases_conflict({"state": "queued", "status": "rejected"}) is True


def test_broker_place_does_not_raise_for_cancelled_canceled_spelling_alone():
    # Same shape as above, exercised end to end: an order whose state/status
    # only disagree on the "cancelled"/"canceled" spelling is otherwise a
    # clean, self-consistent, zero-fill terminal row and must be read as a
    # proven rejection, not routed into AmbiguousPlacementResponseError over
    # nothing but the dialect difference.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "data": {
                "order": {
                    "id": "ORD",
                    "state": "cancelled",
                    "status": "canceled",
                    "cumulative_quantity": "0",
                }
            },
        },
    )
    with pytest.raises(FractionalPlacementRejectedError):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_carries_failure_envelope_fill_evidence_into_reconciliation():
    # {"success": false, "error": "order rejected downstream", "data":
    # {"order": {"id": "ORD", "state": "rejected", "executed_notional":
    # "100"}}}: the failure-envelope's AmbiguousPlacementResponseError
    # constructor preserved only the id, dropping the $100 fill evidence
    # the success-envelope path's identical raise already carries. An
    # immediate history row showing "ORD" merely queued for the approved
    # $2 would then be accepted with no way to notice it contradicts this
    # exact response's own $100 execution (Codex review, PR #859/#911
    # round 43: "Carry failure-envelope fill evidence into
    # reconciliation").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "error": "order rejected downstream",
            "data": {"order": {"id": "ORD", "state": "rejected", "executed_notional": "100"}},
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.order_id == "ORD"
    assert excinfo.value.response_notional_usd == 100.0


def test_broker_place_failure_envelope_preserves_a_finite_float_order_id():
    # {"success": false, "data": {"order": {"id": 123.0, "state":
    # "cancelled", "cumulative_quantity": "0"}}}: a finite positive FLOAT
    # id is a previously-supported shape (the success-envelope path's own
    # validator accepts it), but the failure-envelope path's own narrower
    # `isinstance(order_id, (str, int))` check rejected a plain float
    # outright, discarding a reconcilable order's own id and reporting the
    # rejection as though the response were id-less (Codex review, PR
    # #1164/#1165: "Reuse the complete order-ID validator for failure
    # envelopes").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "data": {"order": {"id": 123.0, "state": "cancelled", "cumulative_quantity": "0"}},
        },
    )
    with pytest.raises(FractionalPlacementRejectedError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.order_id == 123.0


def test_broker_place_failure_envelope_rejects_a_boolean_order_id():
    # The mirror case: a boolean `id` is truthy but not a real broker
    # identifier -- accepted by the OLD narrow `(str, int)` check (a bool
    # is an int subclass in Python) even though the success-envelope path
    # explicitly rejects it. Routes this envelope down the id-less proven-
    # rejection reading instead of carrying a fabricated "True" id forward.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "data": {"order": {"id": True, "state": "cancelled", "cumulative_quantity": "0"}},
        },
    )
    with pytest.raises(FractionalPlacementRejectedError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.order_id is None


def test_broker_place_failure_envelope_gates_outer_evidence_on_exact_identity():
    # {"success": false, "id": 1, "cumulative_quantity": "0", "data":
    # {"order": {"id": "1", "state": "queued"}}}: the outer envelope's own
    # INTEGER id (1) and the inner selected order's STRING id ("1") name
    # what MAY be the same broker order, but disagree in TYPE -- exactly
    # the untrustworthy coincidence `_envelope_layer_shares_order_identity`
    # exists to catch. Merging the outer layer's zero-fill unconditionally
    # proved a rejection for the inner "1" even though its OWN
    # representation (a working "queued" state) never itself proved zero
    # execution (Codex review, PR #859/#911 round 44/PR #1164: "Gate
    # failure-layer evidence on exact order identity").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "id": 1,
            "cumulative_quantity": "0",
            "data": {"order": {"id": "1", "state": "queued"}},
        },
    )
    # Must NOT be proven a clean rejection -- the inner order's own state is
    # "queued" (working), so this must halt as ambiguous/unresolved instead
    # of a FractionalPlacementRejectedError.
    with pytest.raises(RuntimeError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(excinfo.value, FractionalPlacementRejectedError)


def test_broker_place_failure_envelope_preserves_ambiguity_on_an_envelope_id_conflict():
    # Codex review, PR #1169: "Preserve structural ambiguity for envelope
    # ID conflicts". `{"success": false, "id": "B", "data": {"order":
    # {"id": "A", "state": "queued"}}}` has the outer envelope name "B"
    # while the selected order names "A" -- `_order_identity_conflicts`
    # clears the carryable id precisely because neither is safe to trust,
    # but the FINAL structural-ambiguity branch (reached once no id
    # survives) omitted `_order_identity_conflicts` from its own
    # condition, so this exact shape fell through to a bare `RuntimeError`
    # with neither an id nor the `structurally_ambiguous` marker -- a
    # caller's exact-ref history match on "A" alone could then resolve a
    # response that also names a separate, still-unproven "B".
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "id": "B",
            "data": {"order": {"id": "A", "state": "queued"}},
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.structurally_ambiguous is True


def test_broker_place_validates_envelope_ids_before_the_anchorless_early_return():
    # {"success": true, "id": "DEAD", "order_id": "LIVE", "state":
    # "rejected", "cumulative_quantity": "0", "data": {"order":
    # {"cumulative_quantity": "0"}}}: the selected `order` carries only
    # fill evidence -- no id, no state of its own -- so an early return
    # used to bail out of the whole layer-comparison function before ever
    # checking a SINGLE layer, even the outer envelope's own internally
    # conflicting id/order_id ("DEAD" vs "LIVE") that the unconditional
    # self-conflict check exists to catch. The response was classified a
    # proven rejection with no order id at all, discarding "LIVE" entirely
    # (Codex review, PR #859/#911 round 43: "Validate envelope IDs before
    # the anchorless early return").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "id": "DEAD",
            "order_id": "LIVE",
            "state": "rejected",
            "cumulative_quantity": "0",
            "data": {"order": {"cumulative_quantity": "0"}},
        },
    )
    with pytest.raises(RuntimeError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(excinfo.value, FractionalPlacementRejectedError)


def test_order_identifier_aliases_conflict_canonicalizes_equivalent_numeric_ids():
    """Codex review, PR #1169: "Canonicalize equivalent numeric order
    aliases". A successful response can carry equivalent aliases in two
    different SUPPORTED numeric encodings ({"id": 123.0, "order_id":
    "123"}), and a raw str(...).strip() comparison reported that as a
    disagreement ("123.0" != "123") even though both spell the identical
    broker order -- place_market_buy() then converted an accepted
    response into an AmbiguousPlacementResponseError and halted later
    placements over what is, in fact, complete agreement."""
    import dca_live_adapters

    assert dca_live_adapters._order_identifier_aliases_conflict({"id": 123.0, "order_id": "123"}) is False
    assert dca_live_adapters._order_identifier_aliases_conflict({"id": 123, "order_id": "123.0"}) is False
    # Genuinely different ids still conflict.
    assert dca_live_adapters._order_identifier_aliases_conflict({"id": "DEAD", "order_id": "LIVE"}) is True
    assert dca_live_adapters._order_identifier_aliases_conflict({"id": 123, "order_id": "124"}) is True


def test_broker_place_inspects_recursively_nested_placement_envelopes():
    # {"success": false, "data": {"data": {"order": {"id": "LIVE", "state":
    # "queued"}}}}: a DOUBLY nested "data" wrapper. `_unwrap` stopped after
    # ONE unwrap, landing on {"data": {...}} -- a layer carrying no
    # order/orders/id/state/fill evidence of its own -- so every
    # classifier reading that layer found nothing, and the adapter raised
    # a PROVEN rejection with no order id at all, discarding the doubly-
    # nested "LIVE" queued order entirely (Codex review, PR #859/#911
    # round 43: "Inspect recursively nested placement envelopes").
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": False,
            "data": {"data": {"order": {"id": "LIVE", "state": "queued"}}},
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.order_id == "LIVE"


def test_broker_place_checks_layer_lifecycle_aliases_without_a_selected_state():
    # {"success": true, "state": "rejected", "status": "queued", "data":
    # {"order": {"id": "ORD"}, "state": "rejected", "status": "rejected",
    # "cumulative_quantity": "0"}}: the selected `order` carries an id but
    # no state/status of its own, so there is nothing to anchor a
    # comparison to -- but the OUTER response layer's own state/status
    # ("rejected" vs "queued") self-conflict was only checked when
    # `order_state is not None`, so with no anchor it fell straight into
    # the no-anchor accumulation set as though "rejected" (the "state or
    # status" preference) were a single trustworthy reading, silently
    # dropping the outer layer's own unresolved ambiguity (Codex review,
    # PR #859/#911 round 43: "Check layer lifecycle aliases without a
    # selected state"). Must stay unresolved, not a proven rejection.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "state": "rejected",
            "status": "queued",
            "data": {
                "order": {"id": "ORD"},
                "state": "rejected",
                "status": "rejected",
                "cumulative_quantity": "0",
            },
        },
    )
    with pytest.raises(AmbiguousPlacementResponseError):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_compares_every_collection_row_before_collapsing_duplicates():
    # {"data": {"orders": [{"id": "ORD"}]}, "orders": [{"id": "ORD",
    # "state": "queued"}, {"id": "ORD", "state": "rejected",
    # "cumulative_quantity": "0"}]}: the merged collection has 3 entries,
    # the FIRST of which (from "data", merged first) carries no state at
    # all. The old collapse predicate compared every later row only
    # against that evidence-free first row -- neither the "queued" nor the
    # "rejected" row contradicts a row with no state of its own -- and
    # never compared the "queued" and "rejected" rows against EACH OTHER,
    # even though they flatly disagree about the SAME order's lifecycle
    # (Codex review, PR #859/#911 round 43: "Compare every collection row
    # before collapsing duplicates"). This must stay ambiguous.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "success": True,
            "data": {"orders": [{"id": "ORD"}]},
            "orders": [
                {"id": "ORD", "state": "queued"},
                {"id": "ORD", "state": "rejected", "cumulative_quantity": "0"},
            ],
        },
    )
    with pytest.raises(RuntimeError) as excinfo:
        broker.place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(excinfo.value, FractionalPlacementRejectedError)
    # This id-less branch now raises AmbiguousPlacementResponseError with
    # structurally_ambiguous=True (PR #1164: "Preserve ambiguity for
    # ID-less success responses"), rather than a bare RuntimeError -- the
    # caller's own downgrade-prevention logic (keyed on
    # structurally_ambiguous) is strictly MORE conservative than a bare
    # RuntimeError: it still attempts history reconciliation via ref_id
    # either way, but now also forces the verdict back to unresolved even
    # if that lookup happens to find a matching order, instead of quietly
    # trusting a coincidental match the way a bare RuntimeError would.
    assert isinstance(excinfo.value, AmbiguousPlacementResponseError)
    assert excinfo.value.structurally_ambiguous is True


def test_broker_place_agrees_on_a_fill_value_formatted_differently_across_rows():
    # {"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity":
    # "0"}, "orders": [{"id": "ORD", "state": "cancelled",
    # "cumulative_quantity": "0.0"}]}: the singular row and its own
    # collection entry name the SAME order, the SAME terminal state, and
    # the SAME zero fill -- merely formatted differently ("0" vs "0.0").
    # `_order_rows_contradict`'s raw `!=` compared those strings directly
    # and declared them disagreeing, so the selector discarded an
    # otherwise clean, agreeing match as ambiguous over nothing but a
    # stringification difference (Codex review, PR #859/#911 round 45:
    # "Compare fill aliases numerically across duplicate rows"). This must
    # resolve to a clean proven rejection, not stay ambiguous.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "0"},
            "orders": [{"id": "ORD", "state": "cancelled", "cumulative_quantity": "0.0"}],
        },
    )
    with pytest.raises(FractionalPlacementRejectedError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


def test_broker_place_agrees_on_a_lifecycle_alias_spelled_differently_across_rows():
    # {"order": {"id": "ORD", "state": "cancelled", "cumulative_quantity":
    # "0"}, "orders": [{"id": "ORD", "status": "canceled",
    # "cumulative_quantity": "0"}]}: the singular row and its own
    # collection entry name the SAME order and the SAME zero fill, and
    # "cancelled"/"canceled" are the SAME terminal state spelled two ways
    # -- one broker answering itself in two dialects, not two disagreeing
    # answers. `_order_rows_contradict`'s cross-row state/status compare
    # used a bare lowercased string equality instead of the SAME
    # _normalize_broker_lifecycle_alias the within-row check
    # (_order_lifecycle_aliases_conflict) already applies, so this read
    # the spelling difference alone as a conflict and discarded an
    # otherwise clean, agreeing match as ambiguous (Codex review, PR
    # #859/#911 round 46: "Canonicalize lifecycle aliases across order
    # rows"). This must resolve to a clean proven rejection, not stay
    # ambiguous.
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders=True,
        place_fn=lambda **k: {
            "order": {"id": "ORD", "state": "cancelled", "cumulative_quantity": "0"},
            "orders": [{"id": "ORD", "status": "canceled", "cumulative_quantity": "0"}],
        },
    )
    with pytest.raises(FractionalPlacementRejectedError, match="terminal state"):
        broker.place_market_buy("AAPL", 1.0, "ref1")


# --- C:F1 (2026-09-07 diagnostic): a LIVE broker must fail closed on unwired lookups ---
#
# RobinhoodBroker documents that a missing callable "degrades that check to a
# safe default" -- simulated buying power, "tradable", "no order today". That
# is the right contract for a dry-run/progressively-wired broker, but with
# enable_live_orders=True the same defaults are the exact opposite of safe: a
# partially wired live broker would place real orders against a fabricated
# $300 of headroom, skip the fractional-tradability check, and run its
# same-day duplicate gate against an empty history. review_market_buy already
# refuses in this situation; these tests pin the other three lookups to the
# same fail-closed rule while leaving the dry-run defaults untouched.


def _live_broker_with_only_placement():
    return RobinhoodBroker(
        enable_live_orders=True,
        place_fn=lambda **k: {"id": "RH-1", "state": "queued"},
    )


def test_live_broker_without_buying_power_fn_refuses_to_simulate_headroom():
    with pytest.raises(RuntimeError, match="no buying_power_fn wired"):
        _live_broker_with_only_placement().available_buying_power()


def test_dry_run_broker_without_buying_power_fn_keeps_the_simulated_default():
    # The dry-run contract is unchanged: no callable, no live gate, simulated headroom.
    assert RobinhoodBroker(simulated_buying_power_usd=250.0).available_buying_power() == 250.0


def test_live_broker_without_tradability_fn_refuses_to_assume_tradable():
    broker = _live_broker_with_only_placement()
    with pytest.raises(RuntimeError, match="no tradability_fn wired"):
        broker.is_tradable_for_fractional_buy("AAPL")
    with pytest.raises(RuntimeError, match="no tradability_fn wired"):
        broker.is_tradable_for_whole_share_buy("BYDDY")


def test_dry_run_broker_without_tradability_fn_stays_permissive():
    assert RobinhoodBroker().is_tradable_for_fractional_buy("AAPL") is True
    assert RobinhoodBroker().is_tradable_for_whole_share_buy("BYDDY") is True


def test_live_broker_without_orders_fn_refuses_to_report_no_order_today():
    broker = _live_broker_with_only_placement()
    with pytest.raises(RuntimeError, match="no orders_fn wired"):
        broker.same_day_strategy_order_exists("AAPL", date(2026, 9, 8))


def test_dry_run_broker_without_orders_fn_still_reports_no_order_today():
    assert RobinhoodBroker().same_day_strategy_order_exists("AAPL", date(2026, 9, 8)) is False


# --- 2026-09-07 diagnostic: adapter hardening (S:ADP-01, S:ADP-02, S:ADP-03, S:ADP-04, C:F11) ---


def _live_broker(place_fn, **extra):
    return RobinhoodBroker(account="999000111", enable_live_orders=True, place_fn=place_fn, **extra)


def _placed(order):
    return lambda **k: {"data": {"order": order}}


@pytest.mark.parametrize(
    "mismatch, reason",
    [
        ({"symbol": "MSFT"}, "symbol"),
        ({"side": "sell"}, "side"),
        ({"ref_id": "someone-elses-ref"}, "ref_id"),
        ({"client_order_id": "someone-elses-ref"}, "ref_id"),
        ({"dollar_amount": "5.00"}, "notional"),
    ],
)
def test_live_placement_refuses_a_response_that_does_not_describe_this_request(mismatch, reason):
    """S:ADP-01. place_market_buy validated only the order id, lifecycle
    state and fill evidence of the response, never that the order it was
    handed is the one it asked for. A connector echoing a stale response, a
    broker-side substitution, or a wrong-order echo was booked as this
    ticker's buy at the requested notional and consumed budget for the wrong
    instrument. Any non-blank identity field that disagrees with the request
    is now an ambiguous placement, which halts the batch with evidence."""
    order = {"id": "ORD-1", "state": "queued", "symbol": "AAPL", "side": "buy", "ref_id": "ref1", "dollar_amount": "1.00"}
    order.update(mismatch)
    with pytest.raises(AmbiguousPlacementResponseError, match=reason) as excinfo:
        _live_broker(_placed(order)).place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.structurally_ambiguous is True
    assert excinfo.value.order_id == "ORD-1"


def test_live_placement_accepts_a_response_that_echoes_or_omits_the_request_identity():
    echoed = {"id": "ORD-1", "state": "queued", "symbol": "aapl", "side": "BUY", "ref_id": "ref1", "dollar_amount": "1.00"}
    assert _live_broker(_placed(echoed)).place_market_buy("AAPL", 1.0, "ref1") == "ORD-1"
    bare = {"id": "ORD-2", "state": "queued"}
    assert _live_broker(_placed(bare)).place_market_buy("AAPL", 1.0, "ref1") == "ORD-2"
    blank = {"id": "ORD-3", "state": "queued", "symbol": " ", "side": "", "ref_id": None, "dollar_amount": ""}
    assert _live_broker(_placed(blank)).place_market_buy("AAPL", 1.0, "ref1") == "ORD-3"


def test_live_whole_share_placement_refuses_a_response_naming_another_symbol_or_quantity():
    other_symbol = {"id": "ORD-1", "state": "confirmed", "symbol": "TSLA", "quantity": "1"}
    with pytest.raises(AmbiguousPlacementResponseError, match="symbol"):
        _live_broker(_placed(other_symbol)).place_whole_share_buy("BYDDY", 1, "ref1")
    other_quantity = {"id": "ORD-1", "state": "confirmed", "symbol": "BYDDY", "quantity": "3"}
    with pytest.raises(AmbiguousPlacementResponseError, match="quantity"):
        _live_broker(_placed(other_quantity)).place_whole_share_buy("BYDDY", 1, "ref1")
    echoed = {"id": "ORD-9", "state": "confirmed", "symbol": "BYDDY", "quantity": "1.0000", "ref_id": "ref1"}
    assert _live_broker(_placed(echoed)).place_whole_share_buy("BYDDY", 1, "ref1") == "ORD-9"


def test_terminal_placement_diagnostics_redact_account_shaped_values(caplog):
    """S:ADP-02. Broker payloads were logged and embedded in exception text
    through the repr-only _log_safe_repr; the redacting _artifact_safe_repr
    already existed but was not used at those sites, so an account number
    echoed by the broker reached the scheduler's log stream and persisted
    skip_reason fields."""
    import logging

    # An accepted-looking order with no usable id: the adapter logs and raises
    # with the response embedded, which is one of the eight sites that used
    # the repr-only helper.
    idless = {"state": "queued", "account_number": "123456789"}
    adapter_logger = logging.getLogger("near_close_dca.live_adapters")
    adapter_logger.addHandler(caplog.handler)  # the module logger does not propagate to root
    try:
        with caplog.at_level("ERROR", logger="near_close_dca.live_adapters"):
            with pytest.raises(RuntimeError, match="no order id") as excinfo:
                _live_broker(_placed(idless)).place_market_buy("AAPL", 1.0, "ref1")
    finally:
        adapter_logger.removeHandler(caplog.handler)
    assert "123456789" not in str(excinfo.value)
    assert "123456789" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_truthy_non_bool_live_gate_never_places():
    """C:F11. The adapter gated placement on truthiness while dca_runner
    requires the literal True; a passthrough like enable_live_orders="false"
    reached real placement here and was refused there."""
    calls = []
    broker = RobinhoodBroker(
        account="999000111",
        enable_live_orders="false",  # type: ignore[arg-type]
        place_fn=lambda **k: calls.append(k) or {"data": {"order": {"id": "X", "state": "queued"}}},
    )
    with pytest.raises(RuntimeError, match="live gate disabled"):
        broker.place_market_buy("AAPL", 1.0, "ref1")
    with pytest.raises(RuntimeError, match="live gate disabled"):
        broker.place_whole_share_buy("BYDDY", 1, "ref1")
    assert calls == []


def test_to_float_rejects_booleans_and_tradability_requires_a_real_true():
    """S:ADP-03. float(True) == 1.0 passed the finiteness check, so a JSON
    `true` in a price field became a 1.0 close (a ~100% drop, the maximum
    ladder multiplier); and a string "false" read as tradable by truthiness."""
    from dca_live_adapters import _to_float

    assert _to_float(True, default=-1.0) == -1.0
    assert _to_float(False, default=-1.0) == -1.0
    assert _to_float("1.5") == 1.5

    def tradability(**k):
        return {"results": [{"symbol": "AAPL", "tradeable": "false", "state": "active", "fractional_tradability": "tradable"}]}

    broker = RobinhoodBroker(account="999000111", tradability_fn=tradability)
    assert broker.is_tradable_for_fractional_buy("AAPL") is False
    assert broker.is_tradable_for_whole_share_buy("AAPL") is False


def test_same_day_ticker_notional_fails_closed_on_a_non_finite_amount():
    """S:ADP-04. A bare float() accepted 'NaN'/'inf' strings, and the consumer
    only trusts the broker figure when it is GREATER than the artifact prior,
    so a NaN silently disabled the broker ground-truth cross-check."""
    def orders(**k):
        return {"data": {"orders": [{"id": "O1", "state": "queued", "symbol": "AAPL", "dollar_amount": "NaN"}]}}

    broker = RobinhoodBroker(account="999000111", orders_fn=orders)
    with pytest.raises(RuntimeError, match="non-finite"):
        broker.same_day_ticker_notional_usd("AAPL", date(2026, 9, 8))


# --- Copilot review of PR #1394 ------------------------------------------------
_SYNTHETIC_ACCOUNT = "123456789"  # synthetic, allow-listed account-shaped value


def test_identity_mismatch_text_is_redacted_before_it_reaches_the_exception():
    """The echoed mismatching value was rendered with the overflow-only
    ``_log_safe_repr`` and embedded in ``AmbiguousPlacementResponseError``,
    which callers persist as reconciliation evidence, so a broker-supplied
    alias carrying an account-shaped or credential-bearing string bypassed
    the redaction every other placement diagnostic applies."""
    order = {
        "id": "ORD-1",
        "state": "queued",
        "symbol": "AAPL",
        "side": "buy",
        "client_order_id": f"acct-{_SYNTHETIC_ACCOUNT}-token",
        "dollar_amount": "1.00",
    }
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        _live_broker(_placed(order)).place_market_buy("AAPL", 1.0, "ref1")
    assert _SYNTHETIC_ACCOUNT not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


def test_connector_exception_text_is_redacted_in_the_ambiguous_placement_error():
    """Both connector-exception branches interpolated the raw ``exc`` into
    ``OrderPlacementAmbiguousError``; a transport error echoing a request URL
    with an account number or token in it left the process through the
    persisted exception text."""
    from near_close_dca import OrderPlacementAmbiguousError

    def boom(**kwargs):
        raise RuntimeError(f"POST /orders?account_number={_SYNTHETIC_ACCOUNT} timed out")

    with pytest.raises(OrderPlacementAmbiguousError) as excinfo:
        _live_broker(boom).place_market_buy("AAPL", 1.0, "ref1")
    assert _SYNTHETIC_ACCOUNT not in str(excinfo.value)
    assert "timed out" in str(excinfo.value)
    with pytest.raises(OrderPlacementAmbiguousError) as excinfo:
        _live_broker(boom).place_whole_share_buy("BYDDY", 1, "ref2")
    assert _SYNTHETIC_ACCOUNT not in str(excinfo.value)


def test_a_terminal_response_for_another_symbol_is_ambiguous_not_a_clean_rejection():
    """The identity check ran AFTER the terminal-state branches, so a stale
    ``rejected`` response naming another symbol was raised as a definitive
    rejection of THIS ticker: the batch isolated it and moved on, treating
    the ticker as unbought, although the broker may well hold this ticker's
    own order. A mismatch is decided before any terminal interpretation and
    stays structurally ambiguous."""
    from near_close_dca import FractionalPlacementRejectedError, MarketBuyPlacementTerminalError

    stale_rejection = {"id": "ORD-9", "state": "rejected", "symbol": "MSFT", "side": "buy", "cumulative_quantity": "0"}
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        _live_broker(_placed(stale_rejection)).place_market_buy("AAPL", 1.0, "ref1")
    assert not isinstance(excinfo.value, (FractionalPlacementRejectedError, MarketBuyPlacementTerminalError))
    assert excinfo.value.structurally_ambiguous is True
    assert excinfo.value.response_may_have_executed is True


def test_a_terminal_whole_share_response_for_another_symbol_is_ambiguous_not_terminal():
    """Whole-share twin of the test above: ``WholeSharePlacementTerminalError``
    is special-cased by the caller as authoritative fill evidence for THIS
    claim, so a stale terminal response naming another symbol or quantity
    could restore or settle the wrong ticker's credit."""
    from near_close_dca import WholeSharePlacementTerminalError

    stale_rejection = {
        "id": "ORD-9",
        "state": "rejected",
        "symbol": "MSFT",
        "side": "buy",
        "quantity": "1",
        "cumulative_quantity": "0",
    }
    with pytest.raises(AmbiguousPlacementResponseError) as excinfo:
        _live_broker(_placed(stale_rejection)).place_whole_share_buy("BYDDY", 1, "ref1")
    assert not isinstance(excinfo.value, WholeSharePlacementTerminalError)
    assert excinfo.value.structurally_ambiguous is True


def test_an_order_id_already_accepted_in_this_run_is_a_stale_echo_not_a_second_order():
    """A bare ``{id, state}`` response with every identity field omitted is
    accepted by contract: the live prompts and
    ``dca_runner._placement_failure_reason`` compare identity fields only when
    present, because a fresh Robinhood placement omits several of them, and
    requiring them would fail every real placement closed. The stale-echo
    shape that contract leaves open is a connector replaying an EARLIER
    placement's response, and that response names an order id this broker
    instance already booked for another request; it cannot describe a second
    one, so it is refused as structurally ambiguous instead of booked twice."""
    responses = iter([{"id": "ORD-1", "state": "queued"}, {"id": "ORD-1", "state": "queued"}])
    broker = _live_broker(lambda **kwargs: {"data": {"order": next(responses)}})
    assert broker.place_market_buy("AAPL", 1.0, "ref1") == "ORD-1"
    with pytest.raises(AmbiguousPlacementResponseError, match="already accepted") as excinfo:
        broker.place_market_buy("MSFT", 1.0, "ref2")
    assert excinfo.value.structurally_ambiguous is True
    assert excinfo.value.response_may_have_executed is True
    # The first, legitimate acceptance is still the only booked row.
    assert [row["ticker"] for row in broker.get_raw_placement_responses()] == ["AAPL"]


def test_strict_placement_identity_refuses_a_response_that_names_nothing_it_can_prove():
    """Copilot review of PR #1394 asked for placement responses to carry
    sufficient matching identity fields. The default contract compares
    fields only when present, because a fresh Robinhood placement omits
    several of them and the live response shape cannot be verified from this
    repository; requiring them by default would fail every real placement
    closed. ``require_placement_identity=True`` is the owner-controlled
    switch for a deployment that HAS verified its connector echoes the
    symbol and client reference: a bare ``{id, state}`` response is then
    structurally ambiguous, while a response that echoes both is booked."""
    strict = _live_broker(_placed({"id": "ORD-1", "state": "queued"}), require_placement_identity=True)
    with pytest.raises(AmbiguousPlacementResponseError, match="symbol") as excinfo:
        strict.place_market_buy("AAPL", 1.0, "ref1")
    assert excinfo.value.structurally_ambiguous is True

    partial = _live_broker(_placed({"id": "ORD-1", "state": "queued", "symbol": "AAPL"}), require_placement_identity=True)
    with pytest.raises(AmbiguousPlacementResponseError, match="client reference"):
        partial.place_market_buy("AAPL", 1.0, "ref1")

    echoed = {"id": "ORD-2", "state": "queued", "instrument_symbol": "AAPL", "client_order_uuid": "ref1"}
    assert _live_broker(_placed(echoed), require_placement_identity=True).place_market_buy("AAPL", 1.0, "ref1") == "ORD-2"

    # The default stays the documented when-present contract.
    assert _live_broker(_placed({"id": "ORD-3", "state": "queued"})).place_market_buy("AAPL", 1.0, "ref1") == "ORD-3"


def test_instrument_symbol_alias_is_compared_like_symbol():
    """Parity with dca_runner._placement_failure_reason, which already treats
    ``instrument_symbol`` as a symbol spelling."""
    order = {"id": "ORD-1", "state": "queued", "instrument_symbol": "MSFT", "side": "buy"}
    with pytest.raises(AmbiguousPlacementResponseError, match="symbol"):
        _live_broker(_placed(order)).place_market_buy("AAPL", 1.0, "ref1")


def test_a_live_broker_refuses_an_unrecognizable_order_page_instead_of_reading_it_as_empty():
    """2026-09-07 mock beta run of the executor classes. The
    ``_iter_same_day_orders`` docstring says a LIVE broker gets the
    provable-history rule unconditionally (C:F1), but the code applied it only
    to an unwired ``orders_fn``: a wired callable returning a non-empty page in
    a shape the adapter does not recognize (``{"results": [...]}``) was read
    as "no order today" and $0 notional, and the executors' sole duplicate
    gate re-placed every order on a retry. A live broker now raises on such a
    page exactly as ``require_provable_history=True`` does; the dry-run broker
    keeps its established "unwired means no order" contract."""
    from datetime import date

    unrecognized = lambda **kwargs: {"results": [{"id": "ORD-1", "symbol": "AAPL", "state": "filled"}]}  # noqa: E731
    live = _live_broker(lambda **kwargs: {"id": "x", "state": "queued"}, orders_fn=unrecognized)
    with pytest.raises(RuntimeError, match="unrecognizable page"):
        list(live._iter_same_day_orders("AAPL", date(2026, 9, 4)))

    dry = RobinhoodBroker(account="999000111", enable_live_orders=False, orders_fn=unrecognized)
    assert list(dry._iter_same_day_orders("AAPL", date(2026, 9, 4))) == []
