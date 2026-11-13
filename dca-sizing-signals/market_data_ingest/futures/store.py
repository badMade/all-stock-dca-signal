"""FuturesStore: append-only, point-in-time DuckDB store for futures (ADR D5, phase 3).

Same write and read rules as market_data_ingest/prices/store.py:

* Rows are never updated or deleted in place. Each primary key includes a content_hash of the row's values, so
  re-ingesting identical data is dropped (ON CONFLICT DO NOTHING) and a vendor correction lands
  as a new row with a later as_of.
* One batch is validated, staged to <staging_dir>/<run_id>/<name>.parquet, counted, and inserted
  in one transaction. A failure anywhere writes nothing.
* known_at reads return the newest version whose as_of <= known_at.
* Timestamps are stored as naive UTC TIMESTAMP and converted to aware UTC at this boundary.

Futures-specific read rule: close_at(t) is the close of the bar that ENDS at t (starts at
t - 1 minute). It never reads a bar starting at or after t, and never fills forward from an
earlier minute: a missing minute is None, so the tracker refuses that night (D9).
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from market_data_ingest.prices.errors import ValidationError
from market_data_ingest.prices.store import _from_db, _hash, _json_value, _sql_path, safe_name, to_utc_naive

from .models import FuturesBar, FuturesContract, validate_bars, validate_contracts

ONE_MINUTE = timedelta(minutes=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_contract (
    contract_id VARCHAR NOT NULL, root VARCHAR NOT NULL, underlying_index VARCHAR NOT NULL,
    expiry_utc TIMESTAMP NOT NULL, multiplier DOUBLE NOT NULL,
    source VARCHAR NOT NULL, content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (source, contract_id, content_hash)
);
CREATE TABLE IF NOT EXISTS futures_bar_1m (
    contract_id VARCHAR NOT NULL, bar_start_utc TIMESTAMP NOT NULL,
    open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
    volume BIGINT NOT NULL,
    source VARCHAR NOT NULL, content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (source, contract_id, bar_start_utc, content_hash)
);
"""

_PROVENANCE = {"content_hash": "VARCHAR", "as_of": "TIMESTAMP", "ingested_at": "TIMESTAMP", "run_id": "VARCHAR"}
_CONTRACT_COLUMNS = {
    "contract_id": "VARCHAR", "root": "VARCHAR", "underlying_index": "VARCHAR", "expiry_utc": "TIMESTAMP",
    "multiplier": "DOUBLE", "source": "VARCHAR", **_PROVENANCE,
}
_BAR_COLUMNS = {
    "contract_id": "VARCHAR", "bar_start_utc": "TIMESTAMP", "open": "DOUBLE", "high": "DOUBLE", "low": "DOUBLE",
    "close": "DOUBLE", "volume": "BIGINT", "source": "VARCHAR", **_PROVENANCE,
}
_TABLES = frozenset({"futures_contract", "futures_bar_1m"})

# Newest known version per bar, as of known_at. ingested_at then content_hash break exact ties
# deterministically, the same ordering bars_known_at uses in the price store.
_LATEST_BARS = """
SELECT * FROM (
    SELECT *, row_number() OVER (
        PARTITION BY contract_id, bar_start_utc
        ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn
    FROM futures_bar_1m WHERE contract_id = ? AND as_of <= ?)
WHERE rn = 1
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FuturesStore:
    """Owns one DuckDB connection to the futures database."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        con = duckdb.connect(str(self.path))
        con.execute(_SCHEMA)
        self._con: duckdb.DuckDBPyConnection | None = con

    def __enter__(self) -> FuturesStore:
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
            raise RuntimeError("FuturesStore is closed")
        return self._con

    # --- generic helpers ------------------------------------------------------------------------

    def _rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cursor = self.con.execute(sql, params)
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

    def raw_rows(self, table: str) -> list[dict[str, Any]]:
        return self._rows(f"SELECT * FROM {self._check_table(table)}", [])

    def _bulk_insert(self, table: str, columns: dict[str, str], rows: list[dict[str, Any]], parquet_path: Path) -> int:
        """Stage rows as Parquet, check the count, insert in one transaction. Mirrors
        PriceStore._bulk_insert; kept separate so this phase does not refactor the price store."""
        if not rows:
            return 0
        cols = ", ".join(columns)
        struct = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
        with tempfile.TemporaryDirectory() as tmp:
            ndjson = Path(tmp) / "batch.ndjson"
            with ndjson.open("w") as fh:
                for row in rows:
                    fh.write(json.dumps({k: _json_value(row[k]) for k in columns}, allow_nan=False) + "\n")
            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            src = f"read_json('{_sql_path(ndjson)}', format = 'newline_delimited', columns = {struct})"
            self.con.execute(f"COPY (SELECT {cols} FROM {src}) TO '{_sql_path(parquet_path)}' (FORMAT parquet)")
            staged_src = f"read_parquet('{_sql_path(parquet_path)}')"
            staged = self.con.execute(f"SELECT count(*) FROM {staged_src}").fetchone()
            if not staged or int(staged[0]) != len(rows):
                raise ValidationError(f"staged {staged[0] if staged else 0} rows for a {len(rows)}-row batch")
            self.con.execute("BEGIN TRANSACTION")
            try:
                before = self.row_count(table)
                self.con.execute(
                    f"INSERT INTO {table} ({cols}) SELECT DISTINCT {cols} FROM {staged_src} ON CONFLICT DO NOTHING"
                )
                inserted = self.row_count(table) - before
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return inserted

    def _provenance(self, values: list[Any], run_id: str, as_of: datetime) -> dict[str, Any]:
        return {
            "content_hash": _hash(values),
            "as_of": to_utc_naive(as_of),
            "ingested_at": to_utc_naive(self._clock()),
            "run_id": run_id,
        }

    # --- writes ---------------------------------------------------------------------------------

    def ingest_contracts(
        self, contracts: Iterable[FuturesContract], *, run_id: str, as_of: datetime, staging_dir: Path
    ) -> int:
        batch = list(contracts)
        validate_contracts(batch)
        rows = []
        for c in batch:
            expiry = to_utc_naive(c.expiry_utc)
            values = [c.contract_id, c.root, c.underlying_index, expiry.isoformat(), c.multiplier]
            rows.append({
                "contract_id": c.contract_id, "root": c.root, "underlying_index": c.underlying_index,
                "expiry_utc": expiry, "multiplier": c.multiplier, "source": c.source,
                **self._provenance(values, run_id, as_of),
            })
        name = f"contracts_{_hash([r['content_hash'] for r in rows])[:12]}.parquet" if rows else ""
        return self._bulk_insert(
            "futures_contract", _CONTRACT_COLUMNS, rows, Path(staging_dir) / safe_name(run_id) / (name or "empty")
        )

    def _known_contract_ids(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT DISTINCT contract_id FROM futures_contract").fetchall()}

    def ingest_bars(self, bars: Iterable[FuturesBar], *, run_id: str, as_of: datetime, staging_dir: Path) -> int:
        batch = list(bars)
        validate_bars(batch, self._known_contract_ids())
        rows = []
        for b in batch:
            start = to_utc_naive(b.bar_start_utc)
            values = [b.contract_id, start.isoformat(), b.open, b.high, b.low, b.close, b.volume]
            rows.append({
                "contract_id": b.contract_id, "bar_start_utc": start, "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume, "source": b.source,
                **self._provenance(values, run_id, as_of),
            })
        if not rows:
            return 0
        first = min(r["bar_start_utc"] for r in rows).strftime("%Y%m%dT%H%M")
        name = f"bars_{first}_{_hash([r['content_hash'] for r in rows])[:12]}.parquet"
        return self._bulk_insert("futures_bar_1m", _BAR_COLUMNS, rows, Path(staging_dir) / safe_name(run_id) / name)

    # --- reads ----------------------------------------------------------------------------------

    def _known_at(self, known_at: datetime | None) -> datetime:
        return to_utc_naive(known_at if known_at is not None else self._clock())

    def bars(
        self, contract_id: str, *, start_utc: datetime, end_utc: datetime, known_at: datetime | None = None
    ) -> list[FuturesBar]:
        """Latest known version of each bar with start_utc <= bar_start_utc < end_utc."""
        rows = self._rows(
            f"SELECT * FROM ({_LATEST_BARS}) WHERE bar_start_utc >= ? AND bar_start_utc < ? ORDER BY bar_start_utc",
            [contract_id, self._known_at(known_at), to_utc_naive(start_utc), to_utc_naive(end_utc)],
        )
        return [
            FuturesBar(
                contract_id=r["contract_id"], bar_start_utc=r["bar_start_utc"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=int(r["volume"]), source=r["source"],
            )
            for r in rows
        ]

    def close_at(self, contract_id: str, at_utc: datetime, *, known_at: datetime | None = None) -> float | None:
        """Close of the bar ending at ``at_utc``, or None. No lookahead, no fill-forward."""
        start = to_utc_naive(at_utc) - ONE_MINUTE  # raises ValueError on a naive time
        rows = self._rows(
            f"SELECT close FROM ({_LATEST_BARS}) WHERE bar_start_utc = ?",
            [contract_id, self._known_at(known_at), start],
        )
        return float(rows[0]["close"]) if rows else None

    def contract(self, contract_id: str, *, known_at: datetime | None = None) -> FuturesContract | None:
        rows = self._rows(
            "SELECT * FROM futures_contract WHERE contract_id = ? AND as_of <= ? "
            "ORDER BY as_of DESC, ingested_at DESC, content_hash LIMIT 1",
            [contract_id, self._known_at(known_at)],
        )
        if not rows:
            return None
        r = rows[0]
        return FuturesContract(
            contract_id=r["contract_id"], root=r["root"], underlying_index=r["underlying_index"],
            expiry_utc=r["expiry_utc"], multiplier=r["multiplier"], source=r["source"],
        )
