"""Adapters parse real Polymarket responses into typed rows (ADR §4, §6, D10).

Every test here runs against a captured response from fixtures/, never a hand-written body,
except where a test copies a real body and changes one field to probe a failure.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import pytest

from .conftest import (
    INTL,
    KRESS_US_SLUG,
    NVDA_INTL_SLUG,
    NVDA_US_SLUG,
    US,
    FakeResponse,
    FakeSession,
    pm,
    response_body,
)

INTL_EVENT = "intl_gamma_event_next-ceo-of-nvidia.json"
US_EVENT = "us_gateway_event_nvidiaceo-2028-12-31.json"


def _intl_source(session, config, clock):
    return pm("polymarket_intl").PolymarketIntlSource(session, config=config, clock=clock.now, sleep=clock.sleep)


def _us_source(session, config, clock, **kw):
    return pm("polymarket_us").PolymarketUSSource(session, config=config, clock=clock.now, sleep=clock.sleep, **kw)


def _intl_event_batch(config, clock, body=None):
    body = response_body(INTL_EVENT) if body is None else body
    session = FakeSession([FakeResponse.json_body(body)])
    return _intl_source(session, config, clock).fetch_event(NVDA_INTL_SLUG), session


def _by_title(batch):
    return {m.title: m for m in batch.markets}


def _quote_for(batch, market_id):
    return [q for q in batch.quotes if q.market_id == market_id and q.outcome == "Yes"]


def test_parses_stringified_outcome_arrays(config, clock):
    """Gamma serves outcomes, outcomePrices and clobTokenIds as JSON *strings*; they become typed lists."""
    raw = {m["groupItemTitle"]: m for m in response_body(INTL_EVENT)["markets"]}
    assert isinstance(raw["Colette Kress"]["outcomes"], str), "fixture premise: the venue double-encodes"

    batch, session = _intl_event_batch(config, clock)
    kress = next(m for m in batch.markets if m.market_id == raw["Colette Kress"]["conditionId"])

    assert kress.outcomes == ["Yes", "No"]
    assert kress.outcome_prices == [pytest.approx(0.1545), pytest.approx(0.8455)]
    assert all(isinstance(p, float) for p in kress.outcome_prices)
    assert kress.token_ids == json.loads(raw["Colette Kress"]["clobTokenIds"])
    assert all(isinstance(t, str) and t.isdigit() for t in kress.token_ids)
    assert session.calls[0].url == f"https://gamma-api.polymarket.com/events/slug/{NVDA_INTL_SLUG}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45182.33698", 45182.33698),
        ("0.1545", 0.1545),
        (0.29, 0.29),
        (0, 0.0),
        ("", None),
        (None, None),
        ("not-a-number", None),
        ("NaN", None),
        ("Infinity", None),
        (True, None),
    ],
)
def test_casts_numeric_strings(raw, expected):
    to_float = pm("polymarket_intl").to_float
    got = to_float(raw)
    if expected is None:
        assert got is None
    else:
        assert isinstance(got, float) and got == pytest.approx(expected)


def test_casts_numeric_strings_bad_liquidity_becomes_null_and_unusable(config, clock):
    """A bad numeric in a real body becomes null; unknown liquidity can never count as usable."""
    body = response_body(INTL_EVENT)
    cook = next(m for m in body["markets"] if m["groupItemTitle"] == "Tim Cook")
    cook["liquidity"] = "not-a-number"
    cook["liquidityNum"] = "not-a-number"

    batch, _ = _intl_event_batch(config, clock, body)
    (row,) = _quote_for(batch, cook["conditionId"])
    assert row.liquidity_usd is None
    assert row.usable is False
    assert row.bid == pytest.approx(0.04) and row.ask == pytest.approx(0.05), "other fields still parse"


def test_quote_rows_carry_ts_and_ingested_at(config, clock):
    """Every quote has the venue's event time and our ingest time, both aware UTC."""
    batch, _ = _intl_event_batch(config, clock)
    history_session = FakeSession([FakeResponse.from_fixture("intl_clob_batch_prices_history_2_tokens.json")])
    tokens = list(response_body("intl_clob_batch_prices_history_2_tokens.json")["history"])
    history = _intl_source(history_session, config, clock).fetch_price_history(tokens, interval="1d", fidelity=60)

    rows = batch.quotes + history.quotes
    assert batch.quotes and history.quotes
    for row in rows:
        assert isinstance(row.ts_utc, datetime) and row.ts_utc.utcoffset() is not None
        assert row.ingested_at == clock.now()
    first_point = response_body("intl_clob_batch_prices_history_2_tokens.json")["history"][tokens[0]][0]
    assert any(int(r.ts_utc.timestamp()) == first_point["t"] for r in history.quotes)


def test_quote_stores_resolution_and_backfilled_flag(config, clock):
    """History rows record their resolution and are marked backfilled; live snapshots are not."""
    token = response_body(INTL_EVENT)["markets"][0]["clobTokenIds"]
    token = json.loads(token)[0]
    hourly = FakeSession([FakeResponse.from_fixture("intl_clob_prices_history_kress_yes_1d_f60.json")])
    twelve_hour = FakeSession([FakeResponse.from_fixture("intl_clob_prices_history_kress_yes_max_f720.json")])

    h1 = _intl_source(hourly, config, clock).fetch_token_history(token, interval="1d", fidelity=60)
    h12 = _intl_source(twelve_hour, config, clock).fetch_token_history(token, interval="max", fidelity=720)
    live, _ = _intl_event_batch(config, clock)

    assert h1.quotes and {q.resolution_sec for q in h1.quotes} == {3600}
    assert h12.quotes and {q.resolution_sec for q in h12.quotes} == {43200}
    assert all(q.backfilled is True for q in h1.quotes + h12.quotes)
    assert all(q.backfilled is False for q in live.quotes)
    assert len(h12.quotes) == len(response_body("intl_clob_prices_history_kress_yes_max_f720.json")["history"])


def test_quote_stores_resolution_from_data_api_v2(config, clock):
    """Data API v2 history states its own resolution_seconds; the adapter keeps it, not a guess."""
    body = response_body("intl_data_v2_prices_history_kress_yes.json")
    session = FakeSession([FakeResponse.json_body(body)])
    token = json.loads(response_body(INTL_EVENT)["markets"][0]["clobTokenIds"])[0]

    batch = _intl_source(session, config, clock).fetch_data_v2_history(token, bucket_seconds=3600)

    assert [q.resolution_sec for q in batch.quotes] == [p["resolution_seconds"] for p in body["data"]]
    assert all(q.backfilled is True and q.source_api == "data_v2" for q in batch.quotes)
    assert session.calls[0].url.startswith("https://data-api.polymarket.com/v2/prices-history")


def test_data_api_v1_is_never_called(config, clock):
    """v1 answers 410 'retired' (fixture); trades must come from /v2/trades."""
    body = response_body("intl_data_v2_trades_kress.json")
    session = FakeSession([FakeResponse.json_body(body)])
    cond = body["data"][0]["condition_id"]

    batch = _intl_source(session, config, clock).fetch_trades(cond, limit=5)

    assert session.calls[0].url.startswith("https://data-api.polymarket.com/v2/trades")
    assert len(batch.trades) == len(body["data"])
    assert {t.taker_side for t in batch.trades} <= {"BUY", "SELL"}
    assert all(t.venue == INTL and t.market_id == cond for t in batch.trades)


def test_narrative_fields_not_ingested(config, clock):
    """Venue summary text never populates price fields (D10; methods doc §4: 'Jay Puri leads at 83%')."""
    body = response_body(INTL_EVENT)
    narrative = "AI summary: Colette Kress leads at 83% and Tim Cook at 61%."
    body["eventMetadata"] = {"context_description": narrative, "context_requires_regen": False}
    for m in body["markets"]:
        m.setdefault("events", [{"description": narrative}])

    batch, _ = _intl_event_batch(config, clock, body)
    raw = {m["conditionId"]: m for m in response_body(INTL_EVENT)["markets"]}

    prices = {v for q in batch.quotes for v in (q.bid, q.ask, q.mid, q.last) if v is not None}
    assert not any(math.isclose(v, 0.83) or math.isclose(v, 0.61) for v in prices)
    for market in batch.markets:
        assert narrative not in (market.title or "")
        # Polymarket's per-market `description` IS the resolution rules (structured), so it
        # is kept verbatim as rules_text; the event-level generated summary never is.
        assert market.rules_text == raw[market.market_id]["description"]
    kress = next(q for q in batch.quotes if q.market_id == next(
        k for k, v in raw.items() if v["groupItemTitle"] == "Colette Kress"))
    assert (kress.bid, kress.ask) == (pytest.approx(0.154), pytest.approx(0.155))


def test_intl_orderbook_levels_are_best_first(config, clock):
    """CLOB lists bids ascending and asks descending (captured); level 0 must be the best price."""
    raw = response_body("intl_clob_book_kress_yes.json")
    session = FakeSession([FakeResponse.json_body(raw)])

    book = _intl_source(session, config, clock).fetch_orderbook(raw["asset_id"])

    bids = [lvl for lvl in book.orderbook if lvl.side == "bid"]
    asks = [lvl for lvl in book.orderbook if lvl.side == "ask"]
    assert (len(bids), len(asks)) == (len(raw["bids"]), len(raw["asks"]))
    assert bids[0].level == 0 and bids[0].price == max(float(b["price"]) for b in raw["bids"])
    assert asks[0].level == 0 and asks[0].price == min(float(a["price"]) for a in raw["asks"])
    assert {lvl.market_id for lvl in book.orderbook} == {raw["market"]}, "stored under the conditionId"
    assert session.calls[0].params == {"token_id": raw["asset_id"]}
    assert int(bids[0].ts_utc.timestamp() * 1000) == int(raw["timestamp"])


def test_pmus_public_endpoints_work_without_keys(config, clock):
    """US gateway event, BBO, book and history parse with no credentials and send no auth headers."""
    session = FakeSession(
        [
            FakeResponse.from_fixture(US_EVENT),
            FakeResponse.from_fixture("us_gateway_bbo_colkre.json"),
            FakeResponse.from_fixture("us_gateway_book_colkre.json"),
            FakeResponse.from_fixture("us_gateway_price_history_colkre_1d.json"),
        ]
    )
    src = _us_source(session, config, clock, credentials=None)

    event = src.fetch_event(NVDA_US_SLUG)
    bbo = src.fetch_quote(KRESS_US_SLUG)
    book = src.fetch_orderbook(KRESS_US_SLUG)
    history = src.fetch_price_history(KRESS_US_SLUG, interval="INTERVAL_1D", fidelity=60)

    assert {m.market_id for m in event.markets} == {KRESS_US_SLUG, "nphc-nvidiaceo-2028-12-31-ianbuc"}
    (q,) = bbo.quotes
    assert (q.venue, q.market_id, q.outcome) == (US, KRESS_US_SLUG, "Yes")
    raw_bbo = response_body("us_gateway_bbo_colkre.json")["marketData"]
    bid, ask = float(raw_bbo["bestBid"]["value"]), float(raw_bbo["bestAsk"]["value"])
    assert (q.bid, q.ask) == (pytest.approx(bid), pytest.approx(ask))
    assert q.last == pytest.approx(float(raw_bbo["lastTradePx"]["value"]))
    assert q.mid == pytest.approx((bid + ask) / 2)
    raw_book = response_body("us_gateway_book_colkre.json")["marketData"]
    assert len([lvl for lvl in book.orderbook if lvl.side == "bid"]) == len(raw_book["bids"])
    assert history.quotes and all(r.backfilled for r in history.quotes)

    for call in session.calls:
        assert call.url.startswith("https://gateway.polymarket.us/v1/")
        assert not any(h.lower().startswith("x-pm-") for h in call.headers)


def test_pmus_outcome_prices_are_yes_bid_ask_not_yes_no(config, clock):
    """Captured quirk: US `outcomePrices` is the Yes bid/ask pair (0.14/0.15 for Kress, matching
    bestBidQuote/bestAskQuote), not [P(Yes), P(No)] as on Gamma. Reading it as Gamma does would
    store P(No) = 0.15 next to P(Yes) = 0.14."""
    raw = next(m for m in response_body(US_EVENT)["event"]["markets"] if m["slug"] == KRESS_US_SLUG)
    assert json.loads(raw["outcomePrices"]) == [raw["bestBidQuote"]["value"], raw["bestAskQuote"]["value"]]

    session = FakeSession([FakeResponse.from_fixture(US_EVENT)])
    batch = _us_source(session, config, clock).fetch_event(NVDA_US_SLUG)

    (yes,) = [q for q in batch.quotes if q.market_id == KRESS_US_SLUG and q.outcome == "Yes"]
    assert (yes.bid, yes.ask) == (pytest.approx(0.14), pytest.approx(0.15))
    assert not [q for q in batch.quotes if q.market_id == KRESS_US_SLUG and q.outcome == "No"
                and q.mid is not None and math.isclose(q.mid, 0.15)]
