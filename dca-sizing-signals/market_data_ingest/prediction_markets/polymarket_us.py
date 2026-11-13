"""Polymarket US adapter (D12): the keyless public gateway now, the signed API once approved.

Public reads go to ``gateway.polymarket.us/v1`` with no credentials (docs.polymarket.us,
api-reference/introduction: "No API key needed"). The authenticated API at
``api.polymarket.us`` needs three headers -- ``X-PM-Access-Key``, ``X-PM-Timestamp`` (ms) and
``X-PM-Signature``, an Ed25519 signature over ``timestamp + method + path``.

Ed25519 is not in the standard library, and this package takes no new dependency for it
(operator decision 2026-11-12). The signature is produced by an injected
``signer(secret_key, message) -> str``; with no credentials or no signer an authenticated
call raises ``CredentialsError`` before any request is sent.

Captured quirk (fixture us_gateway_event_nvidiaceo-2028-12-31.json): a US market's
``outcomePrices`` is the *Yes bid/ask pair* -- ``["0.1400", "0.1500"]`` next to
``bestBidQuote`` 0.14 and ``bestAskQuote`` 0.15 -- not ``[P(Yes), P(No)]`` as on Gamma. It is
therefore never stored as outcome prices; quotes come from the explicit bid/ask fields.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import CredentialsError, FetchError
from .models import IngestBatch, MarketRecord, OrderbookLevel, QuoteRow
from .source import PredictionMarketSource, parse_json_list, parse_ts, to_float

VENUE = "polymarket_us"


@dataclass(frozen=True)
class PMUSCredentials:
    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "PMUSCredentials(<redacted>)"

    __str__ = __repr__


def load_credentials(
    config, *, env: Mapping[str, str] | None = None, vault: Callable[[str], str | None] | None = None
) -> PMUSCredentials | None:
    """Keys from the vault first, then the environment; never from config, code or fixtures.

    Config holds only the variable *names* (``credential_env``). Half a pair is no pair.
    """
    names = config.sources[VENUE].credential_env
    env = os.environ if env is None else env

    def lookup(name: str | None) -> str | None:
        if not name:
            return None
        value = vault(name) if vault else None
        return value or env.get(name) or None

    key_id, secret = lookup(names.get("key_id")), lookup(names.get("secret_key"))
    if not key_id or not secret:
        return None
    return PMUSCredentials(key_id=key_id, secret_key=secret)


def _value(px: Any) -> float | None:
    """US prices arrive as ``{"value": "0.1400", "currency": "USD"}``."""
    return to_float(px.get("value")) if isinstance(px, dict) else to_float(px)


def _mid(bid: float | None, ask: float | None) -> float | None:
    return (bid + ask) / 2 if bid is not None and ask is not None else None


class PolymarketUSSource(PredictionMarketSource):
    venue = VENUE

    def __init__(
        self,
        session: Any,
        *,
        config: Any,
        clock,
        sleep,
        credentials: PMUSCredentials | None = None,
        signer: Callable[[str, str], str] | None = None,
        use_authenticated_api: bool = False,
    ):
        super().__init__(session, config=config, clock=clock, sleep=sleep)
        self.__credentials = credentials
        self.__signer = signer
        self._authenticated = use_authenticated_api

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self._authenticated:
            return self._request("GET", "gateway", path, params=params)
        if self.__credentials is None or self.__signer is None:
            raise CredentialsError()
        ts = str(int(self._clock().timestamp() * 1000))
        try:
            signature = self.__signer(self.__credentials.secret_key, f"{ts}GET{path}")
        except Exception as exc:  # noqa: BLE001 - a signer failure is a credentials failure, not a raise
            raise CredentialsError("signer failed") from exc
        headers = {
            "X-PM-Access-Key": self.__credentials.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": signature,
        }
        return self._request("GET", "api", path, params=params, headers=headers)

    def fetch_event(self, slug: str) -> IngestBatch:
        body = self._get(f"/v1/events/slug/{slug}")
        event = body.get("event")
        if not isinstance(event, dict):
            raise FetchError("parse", "event is not an object")
        now = self._clock()
        batch = IngestBatch()
        for raw in event.get("markets") or []:
            market_id = raw["slug"]
            outcomes = [str(o) for o in parse_json_list(raw.get("outcomes"))]
            batch.markets.append(
                MarketRecord(
                    venue=VENUE,
                    market_id=market_id,
                    title=raw.get("question"),
                    rules_text=raw.get("description"),
                    created_ts=parse_ts(raw.get("createdAt")),
                    close_ts=parse_ts(raw.get("endDate")),
                    resolved_ts=None,
                    resolution=None,
                    status=str(raw.get("status") or ("closed" if raw.get("closed") else "active")),
                    outcomes=outcomes,
                    # Deliberately empty: see the module docstring on US outcomePrices.
                    outcome_prices=[],
                    token_ids=[],
                )
            )
            bid, ask = _value(raw.get("bestBidQuote")), _value(raw.get("bestAskQuote"))
            batch.quotes.append(
                QuoteRow(
                    venue=VENUE, market_id=market_id, outcome="Yes", ts_utc=parse_ts(raw.get("updatedAt")) or now,
                    bid=bid, ask=ask, mid=_mid(bid, ask), last=None, volume_24h=None,
                    # The event payload carries no liquidity figure; unknown is never usable (D4).
                    liquidity_usd=None, usable=False, resolution_sec=None, backfilled=False,
                    source_api="us_gateway_event", ingested_at=now,
                )
            )
        return batch

    def fetch_quote(self, slug: str) -> IngestBatch:
        data = self._get(f"/v1/markets/{slug}/bbo").get("marketData")
        if not isinstance(data, dict):
            raise FetchError("parse", "marketData is not an object")
        now = self._clock()
        bid, ask = _value(data.get("bestBid")), _value(data.get("bestAsk"))
        sample = data.get("lastPriceSample") or {}
        return IngestBatch(
            quotes=[
                QuoteRow(
                    venue=VENUE, market_id=slug, outcome="Yes", ts_utc=parse_ts(sample.get("ts")) or now,
                    bid=bid, ask=ask, mid=_mid(bid, ask), last=_value(data.get("lastTradePx")), volume_24h=None,
                    liquidity_usd=None, usable=False, resolution_sec=None, backfilled=False,
                    source_api="us_gateway_bbo", ingested_at=now,
                )
            ]
        )

    def fetch_orderbook(self, market_id: str) -> IngestBatch:
        data = self._get(f"/v1/markets/{market_id}/book").get("marketData")
        if not isinstance(data, dict):
            raise FetchError("parse", "marketData is not an object")
        now = self._clock()
        levels = []
        # The gateway lists both sides best-first.
        for side, key in (("bid", "bids"), ("ask", "offers")):
            for i, row in enumerate(data.get(key) or []):
                levels.append(
                    OrderbookLevel(
                        venue=VENUE, market_id=market_id, outcome="Yes", side=side, level=i,
                        price=_value(row.get("px")), size=to_float(row.get("qty")), ts_utc=now, ingested_at=now,
                    )
                )
        return IngestBatch(orderbook=levels)

    def fetch_price_history(self, ids: Any, *, interval: str = "INTERVAL_1D", fidelity: int = 60) -> IngestBatch:
        """Gateway ``/v1/price-history`` for one slug (or each slug in a list). ``longPrice`` is the Yes price."""
        slugs = [ids] if isinstance(ids, str) else list(ids)
        now = self._clock()
        quotes = []
        for slug in slugs:
            body = self._get("/v1/price-history", {"symbol": slug, "fixedInterval": interval, "fidelity": fidelity})
            points = body.get("history")
            if not isinstance(points, list):
                raise FetchError("parse", "history is not a list")
            for point in points:
                ts = parse_ts(point["timestamp"])
                if ts is None:
                    raise FetchError("parse", "history point without a timestamp")
                quotes.append(
                    QuoteRow(
                        venue=VENUE, market_id=slug, outcome="Yes", ts_utc=ts, bid=None, ask=None,
                        mid=to_float(point.get("longPrice")), last=None, volume_24h=None, liquidity_usd=None,
                        usable=False, resolution_sec=fidelity * 60, backfilled=True,
                        source_api="us_gateway_history", ingested_at=now,
                    )
                )
        return IngestBatch(quotes=quotes)
