"""DuckDB store for prediction-market data (ADR §6).

Rules, each pinned by tests/prediction_markets/test_pm_store.py:

* License gate (D7): every write of venue data checks the source's ``license_scope`` and
  raises ``LicenseScopeError`` unless it is ``store_ok``. ``run_ingestion`` checks first and
  records ``not_persisted``; this is the second lock on the same door.
* Append-only (D9): there is no delete. A market gets a new ``pm_markets`` version row when
  its rules hash -- or its status, resolution, title or dates -- changes; an identical pull
  adds nothing. Earlier versions are never rewritten.
* Venue labels never merge (ADR §3): a row whose ``venue`` disagrees with the venue it is
  written under is refused, not relabelled.
* One batch, one transaction: ``write_batch`` writes everything or nothing.
* Timestamps are stored as naive UTC ``TIMESTAMP`` and handed back timezone-aware, which keeps
  DuckDB's TIMESTAMPTZ conversion (and its pytz requirement) out of the picture.

Point-in-time reads never filter on ``close_ts``/``resolved_ts`` to decide what was known:
``markets_as_of(t)`` returns, per market, the latest version ingested at or before ``t``,
excluding markets created after ``t``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from .models import CompositeRow, IngestBatch, MarketRecord, MarketVersion, OrderbookLevel, QuoteRow, TradeRow


class LicenseScopeError(PermissionError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_markets (
    venue VARCHAR NOT NULL, market_id VARCHAR NOT NULL, version INTEGER NOT NULL, title VARCHAR,
    rules_text VARCHAR, rules_hash VARCHAR NOT NULL, created_ts TIMESTAMP, close_ts TIMESTAMP,
    resolved_ts TIMESTAMP, resolution VARCHAR, status VARCHAR NOT NULL, outcomes_json VARCHAR,
    outcome_prices_json VARCHAR, token_ids_json VARCHAR, first_seen_at TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (venue, market_id, version)
);
CREATE TABLE IF NOT EXISTS pm_quotes (
    venue VARCHAR NOT NULL, market_id VARCHAR NOT NULL, outcome VARCHAR, ts_utc TIMESTAMP NOT NULL,
    bid DOUBLE, ask DOUBLE, mid DOUBLE, last DOUBLE, volume_24h DOUBLE, liquidity_usd DOUBLE,
    usable BOOLEAN NOT NULL, resolution_sec INTEGER, backfilled BOOLEAN NOT NULL, source_api VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_trades (
    venue VARCHAR NOT NULL, market_id VARCHAR NOT NULL, outcome VARCHAR, ts_utc TIMESTAMP NOT NULL,
    price DOUBLE, size DOUBLE, taker_side VARCHAR, source_api VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_orderbook_snapshots (
    venue VARCHAR NOT NULL, market_id VARCHAR NOT NULL, outcome VARCHAR, side VARCHAR NOT NULL,
    level INTEGER NOT NULL, price DOUBLE, size DOUBLE, ts_utc TIMESTAMP NOT NULL,
    ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_events (
    event_id VARCHAR PRIMARY KEY, title VARCHAR, kind VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pm_event_links (
    event_id VARCHAR NOT NULL, venue VARCHAR NOT NULL, market_id VARCHAR NOT NULL, outcome VARCHAR,
    relation VARCHAR NOT NULL, valid_from TIMESTAMP NOT NULL, valid_to TIMESTAMP, reviewed BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_composite (
    event_id VARCHAR NOT NULL, outcome VARCHAR NOT NULL, ts_utc TIMESTAMP NOT NULL, prob DOUBLE NOT NULL,
    n_venues INTEGER NOT NULL, weights_json VARCHAR NOT NULL, method_version VARCHAR NOT NULL,
    computed_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id VARCHAR PRIMARY KEY, venue VARCHAR NOT NULL, status VARCHAR NOT NULL, reason VARCHAR,
    rows_written INTEGER NOT NULL, started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP NOT NULL
);
"""

_TABLES = (
    "pm_markets", "pm_quotes", "pm_trades", "pm_orderbook_snapshots", "pm_events", "pm_event_links",
    "pm_composite", "ingestion_runs",
)
_RELATIONS = ("same", "inverse", "subset")
_QUOTE_COLS = [f.name for f in dataclasses.fields(QuoteRow)]
_TRADE_COLS = [f.name for f in dataclasses.fields(TradeRow)]
_BOOK_COLS = [f.name for f in dataclasses.fields(OrderbookLevel)]
_TS_FIELDS = {"ts_utc", "ingested_at", "created_ts", "close_ts", "resolved_ts", "first_seen_at",
              "valid_from", "valid_to", "created_at", "computed_at", "started_at", "finished_at"}


def _db_ts(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _py_ts(value: datetime | None) -> datetime | None:
    return None if value is None else value.replace(tzinfo=timezone.utc)


def _row_dict(names: list[str], row: tuple) -> dict[str, Any]:
    return {n: (_py_ts(v) if n in _TS_FIELDS else v) for n, v in zip(names, row, strict=True)}


def rules_hash(rules_text: str | None) -> str:
    return hashlib.sha256((rules_text or "").encode("utf-8")).hexdigest()


def _version_key(m: MarketRecord, h: str) -> tuple:
    return (h, m.title, m.status, m.resolution, _db_ts(m.close_ts), _db_ts(m.resolved_ts), _db_ts(m.created_ts))


class PMStore:
    def __init__(self, path: str | Path, *, config: Any):
        self.path = Path(path)
        self.config = config
        self._con = duckdb.connect(str(self.path))
        self._con.execute(_SCHEMA)

    def close(self) -> None:
        self._con.close()

    # --- gates -----------------------------------------------------------------------------

    def _check(self, venue: str, rows: list[Any]) -> None:
        scope = self.config.scope_for(venue)
        if scope != "store_ok":
            raise LicenseScopeError(f"{venue} license_scope={scope}: rows are not persisted (D7)")
        for row in rows:
            if row.venue != venue:
                raise ValueError(f"row venue {row.venue!r} written under {venue!r}; venue labels never merge")

    # --- writes ----------------------------------------------------------------------------

    def write_batch(self, venue: str, batch: IngestBatch, *, run_id: str, ingested_at: datetime) -> int:
        self._check(venue, [*batch.markets, *batch.quotes, *batch.trades, *batch.orderbook])
        self._con.begin()
        try:
            n = self._write_markets(venue, batch.markets, run_id, ingested_at)
            n += self._insert("pm_quotes", _QUOTE_COLS, batch.quotes, run_id)
            n += self._insert("pm_trades", _TRADE_COLS, batch.trades, run_id)
            n += self._insert("pm_orderbook_snapshots", _BOOK_COLS, batch.orderbook, run_id)
            self._con.commit()
        except BaseException:
            self._con.rollback()
            raise
        return n

    def write_markets(self, venue: str, records: list[MarketRecord], *, run_id: str,
                      ingested_at: datetime | None = None) -> int:
        return self.write_batch(venue, IngestBatch(markets=list(records)), run_id=run_id,
                                ingested_at=ingested_at or datetime.now(timezone.utc))

    def write_quotes(self, venue: str, rows: list[QuoteRow], *, run_id: str) -> int:
        return self.write_batch(venue, IngestBatch(quotes=list(rows)), run_id=run_id,
                                ingested_at=datetime.now(timezone.utc))

    def _insert(self, table: str, cols: list[str], rows: list[Any], run_id: str) -> int:
        if not rows:
            return 0
        values = [
            [_db_ts(getattr(r, c)) if c in _TS_FIELDS else getattr(r, c) for c in cols] + [run_id] for r in rows
        ]
        placeholders = ", ".join("?" for _ in range(len(cols) + 1))
        self._con.executemany(f"INSERT INTO {table} ({', '.join(cols)}, run_id) VALUES ({placeholders})", values)
        return len(rows)

    def _write_markets(self, venue: str, records: list[MarketRecord], run_id: str, ingested_at: datetime) -> int:
        written = 0
        for m in records:
            h = rules_hash(m.rules_text)
            latest = self._latest_version(venue, m.market_id)
            if latest is not None and _version_key(latest, latest.rules_hash) == _version_key(m, h):
                continue  # identical pull: nothing new to record
            version = 1 if latest is None else latest.version + 1
            first_seen = ingested_at if latest is None else latest.first_seen_at
            self._con.execute(
                "INSERT INTO pm_markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    venue, m.market_id, version, m.title, m.rules_text, h, _db_ts(m.created_ts), _db_ts(m.close_ts),
                    _db_ts(m.resolved_ts), m.resolution, m.status, json.dumps(m.outcomes),
                    json.dumps(m.outcome_prices), json.dumps(m.token_ids), _db_ts(first_seen), _db_ts(ingested_at),
                    run_id,
                ],
            )
            written += 1
        return written

    def record_run(self, *, run_id: str, venue: str, status: str, reason: str, rows_written: int,
                   started_at: datetime, finished_at: datetime) -> None:
        self._con.execute(
            "INSERT INTO ingestion_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            [run_id, venue, status, reason, rows_written, _db_ts(started_at), _db_ts(finished_at)],
        )

    def write_event(self, event_id: str, *, title: str, kind: str, created_at: datetime) -> None:
        self._con.execute("INSERT INTO pm_events VALUES (?, ?, ?, ?)", [event_id, title, kind, _db_ts(created_at)])

    def write_event_link(self, event_id: str, *, venue: str, market_id: str, outcome: str | None, relation: str,
                         valid_from: datetime, valid_to: datetime | None = None, reviewed: bool = False) -> None:
        if relation not in _RELATIONS:
            raise ValueError(f"relation must be one of {_RELATIONS}")
        self._con.execute(
            "INSERT INTO pm_event_links VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [event_id, venue, market_id, outcome, relation, _db_ts(valid_from), _db_ts(valid_to), bool(reviewed)],
        )

    def write_composite(self, rows: list[CompositeRow]) -> None:
        for r in rows:
            self._con.execute(
                "INSERT INTO pm_composite VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [r.event_id, r.outcome, _db_ts(r.ts_utc), r.prob, r.n_venues, r.weights_json, r.method_version,
                 _db_ts(r.computed_at)],
            )

    # --- reads -----------------------------------------------------------------------------

    def _market_rows(self, sql: str, params: list[Any]) -> list[MarketVersion]:
        cur = self._con.execute(sql, params)
        names = [d[0] for d in cur.description]
        out = []
        for values in cur.fetchall():
            row = dict(zip(names, values, strict=True))
            out.append(
                MarketVersion(
                    venue=row["venue"], market_id=row["market_id"], title=row["title"], rules_text=row["rules_text"],
                    created_ts=_py_ts(row["created_ts"]), close_ts=_py_ts(row["close_ts"]),
                    resolved_ts=_py_ts(row["resolved_ts"]), resolution=row["resolution"], status=row["status"],
                    outcomes=json.loads(row["outcomes_json"] or "[]"),
                    outcome_prices=json.loads(row["outcome_prices_json"] or "[]"),
                    token_ids=json.loads(row["token_ids_json"] or "[]"),
                    version=row["version"], rules_hash=row["rules_hash"], first_seen_at=_py_ts(row["first_seen_at"]),
                    ingested_at=_py_ts(row["ingested_at"]),
                )
            )
        return out

    def _latest_version(self, venue: str, market_id: str) -> MarketVersion | None:
        rows = self._market_rows(
            "SELECT * FROM pm_markets WHERE venue = ? AND market_id = ? ORDER BY version DESC LIMIT 1",
            [venue, market_id],
        )
        return rows[0] if rows else None

    def market_versions(self, venue: str, market_id: str) -> list[MarketVersion]:
        return self._market_rows(
            "SELECT * FROM pm_markets WHERE venue = ? AND market_id = ? ORDER BY version", [venue, market_id]
        )

    def markets_as_of(self, as_of: datetime, *, venue: str | None = None) -> list[MarketVersion]:
        sql = """
            SELECT * FROM pm_markets m
            WHERE m.ingested_at <= ? AND (m.created_ts IS NULL OR m.created_ts <= ?) AND (? IS NULL OR m.venue = ?)
            QUALIFY row_number() OVER (PARTITION BY m.venue, m.market_id ORDER BY m.version DESC) = 1
            ORDER BY m.venue, m.market_id
        """
        t = _db_ts(as_of)
        return self._market_rows(sql, [t, t, venue, venue])

    def _quote_rows(self, sql: str, params: list[Any]) -> list[QuoteRow]:
        cur = self._con.execute(sql, params)
        return [
            QuoteRow(**{c: (_py_ts(v) if c in _TS_FIELDS else v) for c, v in zip(_QUOTE_COLS, values, strict=True)})
            for values in cur.fetchall()
        ]

    def quotes(self, *, venue: str | None = None, market_id: str | None = None) -> list[QuoteRow]:
        return self._quote_rows(
            f"SELECT {', '.join(_QUOTE_COLS)} FROM pm_quotes"
            " WHERE (? IS NULL OR venue = ?) AND (? IS NULL OR market_id = ?)"
            " ORDER BY venue, market_id, outcome, ts_utc, ingested_at",
            [venue, venue, market_id, market_id],
        )

    def latest_quote(self, venue: str, market_id: str, outcome: str | None, *, at: datetime) -> QuoteRow | None:
        """Latest quote at or before ``at`` that we had already ingested by ``at`` (no lookahead)."""
        t = _db_ts(at)
        rows = self._quote_rows(
            f"SELECT {', '.join(_QUOTE_COLS)} FROM pm_quotes WHERE venue = ? AND market_id = ?"
            " AND outcome IS NOT DISTINCT FROM ? AND ts_utc <= ? AND ingested_at <= ?"
            " ORDER BY ts_utc DESC, ingested_at DESC LIMIT 1",
            [venue, market_id, outcome, t, t],
        )
        return rows[0] if rows else None

    def event_links(self, event_id: str) -> list[dict[str, Any]]:
        cur = self._con.execute(
            "SELECT venue, market_id, outcome, relation, valid_from, valid_to, reviewed FROM pm_event_links"
            " WHERE event_id = ? ORDER BY venue, market_id",
            [event_id],
        )
        names = [d[0] for d in cur.description]
        return [_row_dict(names, row) for row in cur.fetchall()]

    def composites(self, event_id: str) -> list[dict[str, Any]]:
        cur = self._con.execute("SELECT * FROM pm_composite WHERE event_id = ? ORDER BY ts_utc", [event_id])
        names = [d[0] for d in cur.description]
        return [_row_dict(names, row) for row in cur.fetchall()]

    def ingestion_runs(self) -> list[dict[str, Any]]:
        cur = self._con.execute("SELECT * FROM ingestion_runs ORDER BY started_at, run_id")
        names = [d[0] for d in cur.description]
        return [_row_dict(names, row) for row in cur.fetchall()]

    def table_count(self, table: str) -> int:
        if table not in _TABLES:
            raise ValueError(f"unknown table {table!r}")
        return self._con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
