"""PriceStore: the append-only, point-in-time DuckDB store.

Write rules (tests/prices/test_store_point_in_time.py):

* There is no UPDATE or DELETE anywhere in this module.
* Each primary key includes a ``content_hash`` of the row's values, so re-ingesting identical
  data conflicts and is dropped (``ON CONFLICT DO NOTHING``), while a vendor correction has a
  new hash and lands as a new row with a later ``as_of``.
* ``daily_bars_raw`` dedupes by version instead (PR #2265 review, the same fix as the futures
  store): a bar is new unless the newest version known for (source, symbol, session) at the
  batch's as_of already has its hash (``duckdb_batch.drop_unchanged``), and its key adds as_of
  and run_id. With content_hash alone, a vendor correcting a close A -> B and then reverting to
  A had the revert dropped and the store served B. An older file whose key lacks as_of/run_id
  refuses such a revert loudly (``require_all_new``) instead of dropping it.
* ``corporate_actions`` dedupes by version the same way (PR #2265 follow-up). Its version
  identity is ``ACTION_VERSION_SQL``: (source, vendor_id), or (source, symbol|action_type|ex_date)
  when the vendor sends no id -- the same partition ``actions_known_at`` reads by, so the write
  and read sides agree on what "the newest version" is. Its key adds as_of and run_id. An exact
  repeat inside one batch is stored once; two different actions with one identity are held back
  (``split_ambiguous_actions``), the rest of the batch is written, and ``AmbiguousActionsError``
  then fails the run. ``actions_known_at`` hides an id-less version whenever the same source has
  an id-bearing version of that symbol/type/ex_date, so a vendor that starts (or stops) sending
  ids cannot make one split apply twice (PR #2282 review).
  ``yahoo_legacy_bars`` keeps the content_hash key: the Yahoo archive is a one-time import.
* One batch is validated, staged to ``<staging_dir>/<run_id>/<name>.parquet``, counted, and
  inserted from that Parquet file in a single transaction. A failure anywhere writes nothing.

Read rules: adjusted values are never stored. ``bars_known_at``, ``actions_known_at`` and
``adjusted_bars`` are DuckDB table macros kept in the database file, so the same point-in-time
logic is available from the DuckDB CLI. A corporate action applies to a bar only when
``bar.session < ex_date <= as_of_date``; ``known_at`` hides any row whose ``as_of`` is later.

Timestamps are stored as naive UTC ``TIMESTAMP`` (reading ``TIMESTAMPTZ`` back into Python
would need ``pytz``) and converted to aware UTC at this module's boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from ..duckdb_batch import bulk_insert, drop_unchanged
from .models import (
    ACTION_VERSION_SQL,
    AdjustedBar,
    CorporateAction,
    DailyBar,
    Quote,
    ReconciliationResult,
    action_version_key,
)
from .errors import AmbiguousActionsError
from .validate import split_ambiguous_actions, validate_actions, validate_bars

FAR_FUTURE = datetime(9999, 12, 31)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars_raw (
    source VARCHAR NOT NULL CHECK (source IN ('massive', 'alpaca')),
    symbol VARCHAR NOT NULL,
    session DATE NOT NULL,
    open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL, vwap DOUBLE, trades BIGINT,
    content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (source, symbol, session, content_hash, as_of, run_id)
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    source VARCHAR NOT NULL, vendor_id VARCHAR,
    action_type VARCHAR NOT NULL CHECK (action_type IN
        ('forward_split', 'reverse_split', 'stock_dividend', 'cash_dividend')),
    symbol VARCHAR NOT NULL, ex_date DATE NOT NULL,
    split_from DOUBLE, split_to DOUBLE, cash_amount DOUBLE, currency VARCHAR,
    declaration_date DATE, record_date DATE, pay_date DATE,
    historical_adjustment_factor DOUBLE,
    content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (source, symbol, action_type, ex_date, content_hash, as_of, run_id)
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id VARCHAR NOT NULL, job VARCHAR NOT NULL, session DATE,
    status VARCHAR NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'skipped_closed')),
    source VARCHAR, row_count BIGINT, error_class VARCHAR, error_detail VARCHAR,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, status)
);
CREATE TABLE IF NOT EXISTS intraday_quotes (
    symbol VARCHAR NOT NULL, ts TIMESTAMP NOT NULL, price DOUBLE NOT NULL,
    feed VARCHAR NOT NULL CHECK (feed IN ('iex', 'delayed_sip')),
    delayed BOOLEAN NOT NULL, slot VARCHAR NOT NULL,
    source VARCHAR NOT NULL DEFAULT 'alpaca',
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (run_id, symbol, ts, feed)
);
CREATE TABLE IF NOT EXISTS yahoo_legacy_bars (
    source VARCHAR NOT NULL DEFAULT 'yahoo_legacy' CHECK (source = 'yahoo_legacy'),
    symbol VARCHAR NOT NULL, session DATE NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE NOT NULL, adj_close DOUBLE,
    volume DOUBLE, dividends DOUBLE, stock_splits DOUBLE,
    adjustment_basis VARCHAR NOT NULL DEFAULT 'split_adjusted_at_download',
    file_sha256 VARCHAR NOT NULL, content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (symbol, session, content_hash)
);
CREATE TABLE IF NOT EXISTS reconciliation_results (
    source VARCHAR NOT NULL, reference_source VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL, window_start DATE, window_end DATE, sessions INTEGER NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('pass', 'flag', 'block', 'insufficient')),
    worst_close_diff_pct DOUBLE, volume_flag BOOLEAN NOT NULL, adj_close_worst_diff_pct DOUBLE,
    splice_date DATE, splice_factor DOUBLE,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (run_id, symbol, as_of)
);
"""

# Source priority: Massive is primary, Alpaca the failover.
_PRIORITY = "CASE source WHEN 'massive' THEN 0 WHEN 'alpaca' THEN 1 ELSE 2 END"

_MACROS = f"""
CREATE OR REPLACE MACRO bars_known_at(known_at) AS TABLE
    SELECT source, symbol, session, open, high, low, close, volume, vwap, trades, as_of, run_id
    FROM (
        SELECT *, row_number() OVER (
            PARTITION BY symbol, session
            ORDER BY {_PRIORITY}, as_of DESC, ingested_at DESC, content_hash) AS rn
        FROM daily_bars_raw WHERE as_of <= known_at)
    WHERE rn = 1;

CREATE OR REPLACE MACRO actions_known_at(known_at) AS TABLE
    WITH versions AS (
        SELECT *, row_number() OVER (
            PARTITION BY source, {ACTION_VERSION_SQL}
            ORDER BY as_of DESC, ingested_at DESC, content_hash) AS vrn
        FROM corporate_actions WHERE as_of <= known_at),
    latest AS (
        SELECT *, {_PRIORITY} AS prio FROM versions AS v
        WHERE vrn = 1 AND NOT (v.vendor_id IS NULL AND EXISTS (
            SELECT 1 FROM versions AS w
            WHERE w.vendor_id IS NOT NULL AND w.source = v.source AND w.symbol = v.symbol
                AND w.action_type = v.action_type AND w.ex_date = v.ex_date))),
    ranked AS (
        SELECT *, min(prio) OVER (PARTITION BY symbol, action_type, ex_date) AS best FROM latest)
    SELECT source, vendor_id, action_type, symbol, ex_date, split_from, split_to, cash_amount, as_of
    FROM ranked WHERE prio = best;

CREATE OR REPLACE MACRO adjusted_bars(as_of_date, known_at) AS TABLE
    WITH b AS (SELECT * FROM bars_known_at(known_at) WHERE session <= as_of_date),
    a AS (SELECT * FROM actions_known_at(known_at) WHERE ex_date <= as_of_date),
    splits AS (
        SELECT b.symbol, b.session, product(a.split_from / a.split_to) AS price_factor
        FROM b JOIN a ON a.symbol = b.symbol AND b.session < a.ex_date
            AND a.action_type IN ('forward_split', 'reverse_split', 'stock_dividend')
        GROUP BY b.symbol, b.session),
    divs AS (
        SELECT a.symbol, a.ex_date, a.cash_amount,
            (SELECT p.close FROM b AS p WHERE p.symbol = a.symbol AND p.session < a.ex_date
             ORDER BY p.session DESC LIMIT 1) AS prev_close
        FROM a WHERE a.action_type = 'cash_dividend'),
    div_factors AS (
        SELECT b.symbol, b.session,
            product(1 - d.cash_amount / d.prev_close) AS div_factor,
            count(*) FILTER (WHERE d.prev_close IS NULL OR d.prev_close <= 0) AS unknown
        FROM b JOIN divs AS d ON d.symbol = b.symbol AND b.session < d.ex_date
        GROUP BY b.symbol, b.session)
    SELECT b.symbol, b.session, b.source,
        b.close AS close_raw,
        b.close * coalesce(s.price_factor, 1.0) AS close_split_adjusted,
        CASE WHEN coalesce(f.unknown, 0) > 0 THEN NULL
             ELSE b.close * coalesce(s.price_factor, 1.0) * coalesce(f.div_factor, 1.0) END AS close_adjusted,
        b.volume AS volume_raw,
        b.volume / coalesce(s.price_factor, 1.0) AS volume_split_adjusted
    FROM b LEFT JOIN splits AS s USING (symbol, session) LEFT JOIN div_factors AS f USING (symbol, session);
"""

_TABLES = frozenset(
    {
        "daily_bars_raw",
        "corporate_actions",
        "ingestion_runs",
        "intraday_quotes",
        "yahoo_legacy_bars",
        "reconciliation_results",
    }
)

_BAR_COLUMNS = {
    "source": "VARCHAR",
    "symbol": "VARCHAR",
    "session": "DATE",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "volume": "DOUBLE",
    "vwap": "DOUBLE",
    "trades": "BIGINT",
    "content_hash": "VARCHAR",
    "as_of": "TIMESTAMP",
    "ingested_at": "TIMESTAMP",
    "run_id": "VARCHAR",
}
_ACTION_COLUMNS = {
    "source": "VARCHAR",
    "vendor_id": "VARCHAR",
    "action_type": "VARCHAR",
    "symbol": "VARCHAR",
    "ex_date": "DATE",
    "split_from": "DOUBLE",
    "split_to": "DOUBLE",
    "cash_amount": "DOUBLE",
    "currency": "VARCHAR",
    "declaration_date": "DATE",
    "record_date": "DATE",
    "pay_date": "DATE",
    "historical_adjustment_factor": "DOUBLE",
    "content_hash": "VARCHAR",
    "as_of": "TIMESTAMP",
    "ingested_at": "TIMESTAMP",
    "run_id": "VARCHAR",
}
_YAHOO_COLUMNS = {
    "source": "VARCHAR",
    "symbol": "VARCHAR",
    "session": "DATE",
    "open": "DOUBLE",
    "high": "DOUBLE",
    "low": "DOUBLE",
    "close": "DOUBLE",
    "adj_close": "DOUBLE",
    "volume": "DOUBLE",
    "dividends": "DOUBLE",
    "stock_splits": "DOUBLE",
    "adjustment_basis": "VARCHAR",
    "file_sha256": "VARCHAR",
    "content_hash": "VARCHAR",
    "as_of": "TIMESTAMP",
    "ingested_at": "TIMESTAMP",
    "run_id": "VARCHAR",
}


def to_utc_naive(value: datetime) -> datetime:
    """Aware datetime -> naive UTC for storage. Naive input is refused: it is ambiguous."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_db(value: Any) -> Any:
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else value


def _hash(values: Sequence[Any]) -> str:
    return hashlib.sha256(json.dumps(list(values), default=str, separators=(",", ":")).encode()).hexdigest()


def safe_name(value: str) -> str:
    """A run id or batch name usable as one path component."""
    cleaned = _SAFE_NAME.sub("_", value)
    if not cleaned.strip("._"):
        raise ValueError(f"unusable name {value!r}")
    return cleaned


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PriceStore:
    """Owns one DuckDB connection to the price database."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        con = duckdb.connect(str(self.path))
        con.execute(_SCHEMA)
        con.execute(_MACROS)
        self._con: duckdb.DuckDBPyConnection | None = con

    def __enter__(self) -> PriceStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise RuntimeError("PriceStore is closed")
        return self._con

    # --- generic helpers -----------------------------------------------------------------------

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.con.execute(sql, list(params))
        names = [d[0] for d in cursor.description]
        return [{k: _from_db(v) for k, v in zip(names, row, strict=True)} for row in cursor.fetchall()]

    def _check_table(self, table: str) -> str:
        if table not in _TABLES:
            raise ValueError(f"unknown table {table!r}")
        return table

    def table_columns(self, table: str) -> list[str]:
        rows = self.con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
            [self._check_table(table)],
        ).fetchall()
        return [r[0] for r in rows]

    def row_count(self, table: str) -> int:
        row = self.con.execute(f"SELECT count(*) FROM {self._check_table(table)}").fetchone()
        return int(row[0]) if row else 0

    def _bulk_insert(
        self,
        table: str,
        columns: dict[str, str],
        rows: list[dict[str, Any]],
        *,
        parquet_path: Path | None,
        require_all_new: bool = False,
    ) -> int:
        """Check the table against this store's allowlist, then run the shared batch insert."""
        return bulk_insert(
            self.con,
            self._check_table(table),
            columns,
            rows,
            parquet_path=parquet_path,
            require_all_new=require_all_new,
        )

    # --- bars and corporate actions ------------------------------------------------------------

    def ingest_daily_bars(self, bars: Iterable[DailyBar], *, run_id: str, as_of: datetime, staging_dir: Path) -> int:
        batch = list(bars)
        validate_bars(batch)
        if not batch:
            return 0
        stamp, ingested_at = to_utc_naive(as_of), to_utc_naive(self._clock())
        rows = []
        for b in batch:
            values = [b.symbol, b.session.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.vwap, b.trades]
            rows.append(
                {
                    "source": b.source,
                    "symbol": b.symbol,
                    "session": b.session,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                    "vwap": b.vwap,
                    "trades": b.trades,
                    "content_hash": _hash(values),
                    "as_of": stamp,
                    "ingested_at": ingested_at,
                    "run_id": run_id,
                }
            )
        rows = drop_unchanged(self.con, "daily_bars_raw", ["source", "symbol", "session"], rows, as_of=stamp)
        if not rows:
            return 0
        sessions = sorted({r["session"] for r in rows})
        name = f"daily_bars_{sessions[0].isoformat()}_{_hash([r['content_hash'] for r in rows])[:12]}.parquet"
        return self._bulk_insert(
            "daily_bars_raw",
            _BAR_COLUMNS,
            rows,
            parquet_path=Path(staging_dir) / safe_name(run_id) / name,
            require_all_new=True,
        )

    def ingest_corporate_actions(
        self, actions: Iterable[CorporateAction], *, run_id: str, as_of: datetime, staging_dir: Path
    ) -> int:
        batch = list(actions)
        validate_actions(batch)
        batch, held = split_ambiguous_actions(batch)
        inserted = self._insert_actions(batch, run_id=run_id, as_of=as_of, staging_dir=staging_dir)
        if held:
            shown = "; ".join(held[:20]) + (f" (+{len(held) - 20} more)" if len(held) > 20 else "")
            raise AmbiguousActionsError(
                f"{len(held)} ambiguous corporate action identities held back ({inserted} other rows written): "
                f"{shown}",
                inserted=inserted,
            )
        return inserted

    def _insert_actions(
        self, batch: list[CorporateAction], *, run_id: str, as_of: datetime, staging_dir: Path
    ) -> int:
        if not batch:
            return 0
        stamp, ingested_at = to_utc_naive(as_of), to_utc_naive(self._clock())
        rows = []
        for a in batch:
            values = [
                a.source,
                a.vendor_id,
                a.action_type,
                a.symbol,
                a.ex_date,
                a.split_from,
                a.split_to,
                a.cash_amount,
                a.currency,
                a.declaration_date,
                a.record_date,
                a.pay_date,
                a.historical_adjustment_factor,
            ]
            rows.append(
                {
                    "source": a.source,
                    "vendor_id": a.vendor_id,
                    "action_type": a.action_type,
                    "symbol": a.symbol,
                    "ex_date": a.ex_date,
                    "split_from": a.split_from,
                    "split_to": a.split_to,
                    "cash_amount": a.cash_amount,
                    "currency": a.currency,
                    "declaration_date": a.declaration_date,
                    "record_date": a.record_date,
                    "pay_date": a.pay_date,
                    "historical_adjustment_factor": a.historical_adjustment_factor,
                    # Not a stored column: the dedupe key drop_unchanged compares (see models).
                    "version_key": action_version_key(a),
                    "content_hash": _hash(values),
                    "as_of": stamp,
                    "ingested_at": ingested_at,
                    "run_id": run_id,
                }
            )
        rows = drop_unchanged(
            self.con,
            "corporate_actions",
            ["source", "version_key"],
            rows,
            as_of=stamp,
            key_sql={"version_key": ACTION_VERSION_SQL},
        )
        if not rows:
            return 0
        name = f"corporate_actions_{_hash([r['content_hash'] for r in rows])[:12]}.parquet"
        return self._bulk_insert(
            "corporate_actions",
            _ACTION_COLUMNS,
            rows,
            parquet_path=Path(staging_dir) / safe_name(run_id) / name,
            require_all_new=True,
        )

    def raw_bars(self, symbol: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM daily_bars_raw WHERE symbol = ? ORDER BY session, as_of", [symbol])

    def adjusted_bars(self, symbol: str, *, as_of_date: date, known_at: datetime | None = None) -> list[AdjustedBar]:
        known = FAR_FUTURE if known_at is None else to_utc_naive(known_at)
        rows = self._rows(
            "SELECT * FROM adjusted_bars(?, ?) WHERE symbol = ? ORDER BY session", [as_of_date, known, symbol]
        )
        return [AdjustedBar(**r) for r in rows]

    def split_price_factor(self, symbol: str, *, after: date, through: date, known_at: datetime | None = None) -> float:
        """Product of split price ratios with ``after < ex_date <= through`` (1.0 if none)."""
        known = FAR_FUTURE if known_at is None else to_utc_naive(known_at)
        row = self.con.execute(
            "SELECT coalesce(product(split_from / split_to), 1.0) FROM actions_known_at(?) "
            "WHERE symbol = ? AND ex_date > ? AND ex_date <= ? "
            "AND action_type IN ('forward_split', 'reverse_split', 'stock_dividend')",
            [known, symbol, after, through],
        ).fetchone()
        return float(row[0]) if row else 1.0

    def trailing_session_counts(self, source: str, *, before: date, n: int = 20) -> list[int]:
        rows = self.con.execute(
            "SELECT session, count(DISTINCT symbol) FROM daily_bars_raw WHERE source = ? AND session < ? "
            "GROUP BY session ORDER BY session DESC LIMIT ?",
            [source, before, n],
        ).fetchall()
        return [int(r[1]) for r in rows]

    def last_session_symbols(self, source: str) -> list[str]:
        rows = self.con.execute(
            "SELECT DISTINCT symbol FROM daily_bars_raw WHERE source = ? AND session = "
            "(SELECT max(session) FROM daily_bars_raw WHERE source = ?) ORDER BY symbol",
            [source, source],
        ).fetchall()
        return [r[0] for r in rows]

    # --- run log -----------------------------------------------------------------------------------

    def record_run(
        self,
        run_id: str,
        *,
        job: str,
        status: str,
        at: datetime,
        session: date | None = None,
        source: str | None = None,
        row_count: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        self.con.execute(
            "INSERT INTO ingestion_runs (run_id, job, session, status, source, row_count, error_class, "
            "error_detail, as_of, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                job,
                session,
                status,
                source,
                row_count,
                error_class,
                error_detail,
                to_utc_naive(at),
                to_utc_naive(self._clock()),
            ],
        )

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM ingestion_runs WHERE run_id = ? "
            "ORDER BY as_of, CASE status WHEN 'running' THEN 0 ELSE 1 END, ingested_at",
            [run_id],
        )

    def run_status(self, run_id: str) -> str | None:
        events = self.run_events(run_id)
        return events[-1]["status"] if events else None

    # --- intraday ------------------------------------------------------------------------------------

    def insert_intraday_quote(self, quote: Quote, *, slot: str, delayed: bool, run_id: str) -> None:
        self.con.execute(
            "INSERT INTO intraday_quotes (symbol, ts, price, feed, delayed, slot, source, as_of, ingested_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'alpaca', ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                quote.symbol,
                to_utc_naive(quote.ts),
                quote.price,
                quote.feed,
                delayed,
                slot,
                to_utc_naive(quote.as_of),
                to_utc_naive(self._clock()),
                run_id,
            ],
        )

    def intraday_quotes(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            return self._rows("SELECT * FROM intraday_quotes ORDER BY ts, symbol")
        return self._rows("SELECT * FROM intraday_quotes WHERE run_id = ? ORDER BY ts, symbol", [run_id])

    # --- yahoo legacy and reconciliation -------------------------------------------------------------

    def ingest_yahoo_legacy(
        self, rows: Iterable[dict[str, Any]], *, symbol: str, run_id: str, as_of: datetime, file_sha256: str
    ) -> int:
        stamp, ingested_at = to_utc_naive(as_of), to_utc_naive(self._clock())
        prepared = []
        for r in rows:
            values = [
                symbol,
                r["session"].isoformat(),
                r["open"],
                r["high"],
                r["low"],
                r["close"],
                r["adj_close"],
                r["volume"],
                r["dividends"],
                r["stock_splits"],
            ]
            prepared.append(
                {
                    **r,
                    "source": "yahoo_legacy",
                    "symbol": symbol,
                    "adjustment_basis": "split_adjusted_at_download",
                    "file_sha256": file_sha256,
                    "content_hash": _hash(values),
                    "as_of": stamp,
                    "ingested_at": ingested_at,
                    "run_id": run_id,
                }
            )
        return self._bulk_insert("yahoo_legacy_bars", _YAHOO_COLUMNS, prepared, parquet_path=None)

    def yahoo_legacy_bars(self, symbol: str) -> list[dict[str, Any]]:
        """Latest known version of each session for one symbol."""
        return self._rows(
            "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY session "
            "ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM yahoo_legacy_bars WHERE symbol = ?) "
            "WHERE rn = 1 ORDER BY session",
            [symbol],
        )

    def insert_reconciliation(self, result: ReconciliationResult, *, run_id: str, as_of: datetime) -> None:
        self.con.execute(
            "INSERT INTO reconciliation_results (source, reference_source, symbol, window_start, window_end, "
            "sessions, status, worst_close_diff_pct, volume_flag, adj_close_worst_diff_pct, splice_date, "
            "splice_factor, as_of, ingested_at, run_id) "
            "VALUES ('massive', 'yahoo_legacy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                result.symbol,
                result.window_start,
                result.window_end,
                result.sessions,
                result.status,
                result.worst_close_diff_pct,
                result.volume_flag,
                result.adj_close_worst_diff_pct,
                result.splice_date,
                result.splice_factor,
                to_utc_naive(as_of),
                to_utc_naive(self._clock()),
                run_id,
            ],
        )

    def reconciliation_results(self, symbol: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM reconciliation_results WHERE symbol = ? ORDER BY as_of, ingested_at", [symbol])
