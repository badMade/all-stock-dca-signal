"""Polymarket International adapter: Gamma (discovery), CLOB (book, history), Data API v2 (trades, history).

Shapes verified against the captures in tests/prediction_markets/fixtures (2026-11-13), which
differ from the research report in ADR §4 in two places:

* Data API v1 answers 410 "retired". v2 filters trades with ``conditionId=`` and history with
  ``tokenId=`` + ``bucketSeconds=``, and states each row's ``resolution_seconds``.
* CLOB ``POST /batch-prices-history`` takes ``{"markets": [token ids], "interval", "fidelity"}``
  and rejects more than 20 ids with HTTP 400.

``market_id`` is the Gamma ``conditionId`` (the on-chain id, shared by Data API trades).
Gamma's per-market ``description`` is the resolution rules and is kept verbatim as
``rules_text``; event-level metadata such as a generated summary is never read (D10).
"""

from __future__ import annotations

from typing import Any

from .errors import FetchError
from .models import IngestBatch, MarketRecord, OrderbookLevel, QuoteRow, TradeRow
from .source import PredictionMarketSource, parse_json_list, parse_ts, to_float

__all__ = ["PolymarketIntlSource", "to_float"]

VENUE = "polymarket_intl"


def _status(market: dict[str, Any]) -> str:
    if market.get("archived"):
        return "archived"
    if market.get("closed"):
        return "closed"
    return "active" if market.get("active") else "inactive"


def _mid(bid: float | None, ask: float | None) -> float | None:
    return (bid + ask) / 2 if bid is not None and ask is not None else None


class PolymarketIntlSource(PredictionMarketSource):
    venue = VENUE

    # --- Gamma ---------------------------------------------------------------------------

    def fetch_event(self, slug: str) -> IngestBatch:
        """One Gamma event: a ``MarketRecord`` and a Yes-side snapshot quote per market."""
        event = self._request("GET", "gamma", f"/events/slug/{slug}")
        now = self._clock()
        batch = IngestBatch()
        for raw in event.get("markets") or []:
            market, quote = self._parse_gamma_market(raw, now)
            batch.markets.append(market)
            batch.quotes.append(quote)
        return batch

    def _parse_gamma_market(self, raw: dict[str, Any], now) -> tuple[MarketRecord, QuoteRow]:
        market_id = raw["conditionId"]
        outcomes = [str(o) for o in parse_json_list(raw.get("outcomes"))]
        prices = [to_float(p) for p in parse_json_list(raw.get("outcomePrices"))]
        tokens = [str(t) for t in parse_json_list(raw.get("clobTokenIds"))]
        if prices and len(prices) != len(outcomes):
            raise FetchError("parse", "outcomePrices and outcomes differ in length")
        market = MarketRecord(
            venue=VENUE,
            market_id=market_id,
            title=raw.get("question"),
            rules_text=raw.get("description"),
            created_ts=parse_ts(raw.get("createdAt")),
            close_ts=parse_ts(raw.get("endDate")),
            resolved_ts=parse_ts(raw.get("closedTime")),
            resolution=None,
            status=_status(raw),
            outcomes=outcomes,
            outcome_prices=prices,
            token_ids=tokens,
        )
        # bestBid/bestAsk on a Gamma market are the first outcome's ("Yes") book.
        bid, ask = to_float(raw.get("bestBid")), to_float(raw.get("bestAsk"))
        liquidity = to_float(raw.get("liquidityNum"))
        if liquidity is None:
            liquidity = to_float(raw.get("liquidity"))
        quote = QuoteRow(
            venue=VENUE,
            market_id=market_id,
            outcome=outcomes[0] if outcomes else "Yes",
            ts_utc=parse_ts(raw.get("updatedAt")) or now,
            bid=bid,
            ask=ask,
            mid=_mid(bid, ask),
            last=to_float(raw.get("lastTradePrice")),
            volume_24h=to_float(raw.get("volume24hr")),
            liquidity_usd=liquidity,
            usable=self._usable(liquidity),
            resolution_sec=None,
            backfilled=False,
            source_api="gamma",
            ingested_at=now,
        )
        return market, quote

    # --- CLOB ----------------------------------------------------------------------------

    def fetch_price_history(
        self,
        ids: list[str],
        *,
        interval: str,
        fidelity: int,
        token_map: dict[str, tuple[str, str]] | None = None,
    ) -> IngestBatch:
        """Batch history, at most ``max_history_batch`` (<= 20) token ids per POST.

        ``token_map`` maps token id -> (conditionId, outcome); an unmapped token is stored
        under its own id with outcome None. A token the venue omits has no points and adds no
        rows. Any failed chunk fails the whole fetch, so a run never stores half a batch.
        """
        tokens = list(ids)
        size = self._source.max_history_batch
        now = self._clock()
        batch = IngestBatch()
        for start in range(0, len(tokens), size):
            chunk = tokens[start : start + size]
            body = self._request(
                "POST", "clob", "/batch-prices-history",
                body={"markets": chunk, "interval": interval, "fidelity": fidelity},
            )
            history = body.get("history")
            if not isinstance(history, dict):
                raise FetchError("parse", "batch history is not an object")
            for token in chunk:
                batch.quotes.extend(
                    self._history_rows(history.get(token) or [], token, fidelity * 60, "clob_batch", now, token_map)
                )
        return batch

    def fetch_token_history(
        self, token: str, *, interval: str, fidelity: int, token_map: dict[str, tuple[str, str]] | None = None
    ) -> IngestBatch:
        body = self._request(
            "GET", "clob", "/prices-history", params={"market": token, "interval": interval, "fidelity": fidelity}
        )
        points = body.get("history")
        if not isinstance(points, list):
            raise FetchError("parse", "history is not a list")
        return IngestBatch(quotes=self._history_rows(points, token, fidelity * 60, "clob", self._clock(), token_map))

    def _history_rows(self, points, token, resolution_sec, source_api, now, token_map) -> list[QuoteRow]:
        market_id, outcome = (token_map or {}).get(token, (token, None))
        rows = []
        for point in points:
            ts = parse_ts(point["t"])
            if ts is None:
                raise FetchError("parse", "history point without a timestamp")
            rows.append(
                QuoteRow(
                    venue=VENUE, market_id=market_id, outcome=outcome, ts_utc=ts, bid=None, ask=None,
                    mid=to_float(point.get("p")), last=None, volume_24h=None, liquidity_usd=None,
                    # History carries no liquidity, so it is never usable on its own (D4).
                    usable=False, resolution_sec=resolution_sec, backfilled=True, source_api=source_api,
                    ingested_at=now,
                )
            )
        return rows

    def fetch_orderbook(self, market_id: str, *, outcome: str | None = "Yes") -> IngestBatch:
        """CLOB ``/book`` for one outcome token.

        The CLOB books are per token, so the argument here is the token id; the stored
        ``market_id`` is the conditionId the book reports as ``market``.
        """
        token = market_id
        body = self._request("GET", "clob", "/book", params={"token_id": token})
        market_id = body.get("market") or token
        now = self._clock()
        ts_ms = to_float(body.get("timestamp"))
        ts = parse_ts(ts_ms / 1000) if ts_ms else now
        levels = []
        # The CLOB lists bids ascending and asks descending; level 0 is the best price.
        for side, key, best_first in (("bid", "bids", True), ("ask", "asks", False)):
            rows = sorted(body.get(key) or [], key=lambda r: to_float(r.get("price")) or 0.0, reverse=best_first)
            for i, row in enumerate(rows):
                levels.append(
                    OrderbookLevel(
                        venue=VENUE, market_id=market_id, outcome=outcome, side=side, level=i,
                        price=to_float(row.get("price")), size=to_float(row.get("size")), ts_utc=ts, ingested_at=now,
                    )
                )
        return IngestBatch(orderbook=levels)

    # --- Data API v2 -----------------------------------------------------------------------

    def fetch_trades(self, condition_id: str, *, limit: int = 500) -> IngestBatch:
        body = self._request("GET", "data", "/v2/trades", params={"conditionId": condition_id, "limit": limit})
        rows = body.get("data")
        if not isinstance(rows, list):
            raise FetchError("parse", "trades data is not a list")
        now = self._clock()
        trades = []
        for row in rows:
            ts = parse_ts(row["timestamp"])
            if ts is None:
                raise FetchError("parse", "trade without a timestamp")
            trades.append(
                TradeRow(
                    venue=VENUE, market_id=condition_id, outcome=row.get("outcome"), ts_utc=ts,
                    price=to_float(row.get("price")), size=to_float(row.get("size")),
                    taker_side=row.get("side"), source_api="data_v2", ingested_at=now,
                )
            )
        return IngestBatch(trades=trades)

    def fetch_data_v2_history(
        self, token: str, *, bucket_seconds: int = 3600, interval: str = "1d", limit: int = 500,
        cursor: str | None = None, token_map: dict[str, tuple[str, str]] | None = None,
    ) -> IngestBatch:
        """One page of Data API v2 history. Each row's own ``resolution_seconds`` is kept."""
        params: dict[str, Any] = {
            "tokenId": token, "interval": interval, "bucketSeconds": bucket_seconds, "limit": limit
        }
        if cursor:
            params["cursor"] = cursor
        body = self._request("GET", "data", "/v2/prices-history", params=params)
        points = body.get("data")
        if not isinstance(points, list):
            raise FetchError("parse", "history data is not a list")
        market_id, outcome = (token_map or {}).get(token, (token, None))
        now = self._clock()
        quotes = []
        for point in points:
            ts = parse_ts(point["timestamp"])
            resolution = point.get("resolution_seconds")
            if ts is None or isinstance(resolution, bool) or not isinstance(resolution, int):
                raise FetchError("parse", "history row missing timestamp or resolution_seconds")
            quotes.append(
                QuoteRow(
                    venue=VENUE, market_id=market_id, outcome=outcome, ts_utc=ts, bid=None, ask=None,
                    mid=to_float(point.get("price")), last=None, volume_24h=None, liquidity_usd=None, usable=False,
                    resolution_sec=resolution, backfilled=True, source_api="data_v2", ingested_at=now,
                )
            )
        return IngestBatch(quotes=quotes)
