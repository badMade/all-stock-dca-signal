"""Typed rows. Field names follow the DuckDB schema in docs/adr-prediction-markets.md §6.

``ts_utc`` is always the venue's event time and ``ingested_at`` the time we read it; both are
timezone-aware UTC. A venue gives probabilities as prices in [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class MarketRecord:
    venue: str
    market_id: str
    title: str | None
    # The venue's structured resolution rules. Hashed into rules_hash (D9). Never a
    # venue-generated summary (D10).
    rules_text: str | None
    created_ts: datetime | None
    close_ts: datetime | None
    resolved_ts: datetime | None
    resolution: str | None
    status: str
    outcomes: list[str] = field(default_factory=list)
    outcome_prices: list[float | None] = field(default_factory=list)
    token_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MarketVersion(MarketRecord):
    """One append-only ``pm_markets`` row: a ``MarketRecord`` plus its version bookkeeping."""

    version: int = 1
    rules_hash: str = ""
    first_seen_at: datetime | None = None
    ingested_at: datetime | None = None


@dataclass(frozen=True)
class QuoteRow:
    venue: str
    market_id: str
    outcome: str | None
    ts_utc: datetime
    bid: float | None
    ask: float | None
    mid: float | None
    last: float | None
    volume_24h: float | None
    liquidity_usd: float | None
    # False below the configured liquidity threshold, or when liquidity is unknown (D4).
    usable: bool
    # Seconds per point for history rows (3600, 43200, ...) so resolutions never mix; None
    # for a live snapshot.
    resolution_sec: int | None
    backfilled: bool
    source_api: str
    ingested_at: datetime


@dataclass(frozen=True)
class TradeRow:
    venue: str
    market_id: str
    outcome: str | None
    ts_utc: datetime
    price: float | None
    size: float | None
    taker_side: str | None
    source_api: str
    ingested_at: datetime


@dataclass(frozen=True)
class OrderbookLevel:
    venue: str
    market_id: str
    outcome: str | None
    side: str  # "bid" | "ask"
    level: int  # 0 = best
    price: float | None
    size: float | None
    ts_utc: datetime
    ingested_at: datetime


@dataclass
class IngestBatch:
    """Everything one fetch produced. ``run_ingestion`` writes it in one transaction or not at all."""

    markets: list[MarketRecord] = field(default_factory=list)
    quotes: list[QuoteRow] = field(default_factory=list)
    trades: list[TradeRow] = field(default_factory=list)
    orderbook: list[OrderbookLevel] = field(default_factory=list)

    def row_count(self) -> int:
        return len(self.markets) + len(self.quotes) + len(self.trades) + len(self.orderbook)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    venue: str
    ok: bool
    # "ok" | "failed" | "not_persisted" (fetched and parsed, but the source is not store_ok)
    status: str
    reason: str
    rows_written: int


@dataclass(frozen=True)
class CompositeRow:
    event_id: str
    outcome: str
    ts_utc: datetime
    prob: float
    n_venues: int
    weights_json: str
    method_version: str
    computed_at: datetime
