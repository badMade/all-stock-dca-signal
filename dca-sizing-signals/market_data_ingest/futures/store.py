"""FuturesStore: append-only, point-in-time DuckDB store for futures (ADR D5, phase 3).

Same write and read rules as market_data_ingest/prices/store.py:

* Rows are never updated or deleted in place. A row is inserted only when its content_hash
  differs from the newest version already known for that key as of the batch's as_of, so
  re-ingesting identical data adds nothing and a vendor correction lands as a new row with a
  later as_of. The primary key includes as_of (PR #2264 review): with content_hash alone, a
  vendor that corrects A to B and then reverts to A had the revert dropped as a duplicate of
  the first A, and the store kept serving B. Prices and the multiplier are coerced to float
  before hashing, so 6031 and 6031.0 are the same content.
* Every read keeps sources apart. Pass ``source=`` to pick one; without it, a minute (or a
  contract) that more than one source has stored is ambiguous and the read raises rather than
  letting whichever vendor arrived last win.
* One batch is validated, staged to <staging_dir>/<run_id>/<name>.parquet, counted, and inserted
  in one transaction. A failure anywhere writes nothing.
* known_at reads return the newest version whose as_of <= known_at.
* Timestamps are stored as naive UTC TIMESTAMP and converted to aware UTC at this boundary.

Futures-specific read rule: close_at(t) is the close of the bar that ENDS at t (starts at
t - 1 minute). It never reads a bar starting at or after t, and never fills forward from an
earlier minute: a missing minute is None, so the tracker refuses that night (D9).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from market_data_ingest.duckdb_batch import bulk_insert
from market_data_ingest.prices.store import _from_db, _hash, safe_name, to_utc_naive

from .models import FuturesBar, FuturesContract, validate_bars, validate_contracts

ONE_MINUTE = timedelta(minutes=1)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS futures_contract (
    contract_id VARCHAR NOT NULL, root VARCHAR NOT NULL, underlying_index VARCHAR NOT NULL,
    expiry_utc TIMESTAMP NOT NULL, multiplier DOUBLE NOT NULL,
    source VARCHAR NOT NULL, content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (source, contract_id, content_hash, as_of)
);
CREATE TABLE IF NOT EXISTS futures_bar_1m (
    contract_id VARCHAR NOT NULL, bar_start_utc TIMESTAMP NOT NULL,
    open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL,
    volume BIGINT NOT NULL,
    source VARCHAR NOT NULL, content_hash VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (source, contract_id, bar_start_utc, content_hash, as_of)
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

# Newest known version per (source, bar), as of known_at. ingested_at then content_hash break
# exact ties deterministically, the same ordering bars_known_at uses in the price store. Source is
# in the partition (PR #2264 review): without it a second vendor's later bar silently replaced
# the primary feed's close. The optional source filter is NULL-safe: NULL means every source.
_LATEST_BARS = """
SELECT * FROM (
    SELECT *, row_number() OVER (
        PARTITION BY source, contract_id, bar_start_utc
        ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn
    FROM futures_bar_1m WHERE contract_id = ? AND as_of <= ? AND (CAST(? AS VARCHAR) IS NULL OR source = ?))
WHERE rn = 1
"""
_LATEST_CONTRACTS = """
SELECT * FROM (
    SELECT *, row_number() OVER (
        PARTITION BY source, contract_id
        ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn
    FROM futures_contract WHERE contract_id = ? AND as_of <= ? AND (CAST(? AS VARCHAR) IS NULL OR source = ?))
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
        """Check the table against this store's allowlist, then run the shared batch insert
        (market_data_ingest.duckdb_batch, the same code PriceStore uses)."""
        return bulk_insert(self.con, self._check_table(table), columns, rows, parquet_path=parquet_path)

    def _provenance(self, values: list[Any], run_id: str, as_of: datetime) -> dict[str, Any]:
        return {
            "content_hash": _hash(values),
            "as_of": to_utc_naive(as_of),
            "ingested_at": to_utc_naive(self._clock()),
            "run_id": run_id,
        }

    # --- writes ---------------------------------------------------------------------------------

    def _unchanged(self, table: str, keys: list[str], rows: list[dict[str, Any]], as_of: datetime) -> set[tuple]:
        """Keys (plus content_hash) whose newest version known at ``as_of`` already has that hash.

        This replaces the old PRIMARY KEY (..., content_hash) dedupe: it drops a true re-ingest
        but keeps a revert to an earlier value, which the newest version no longer matches.
        """
        contract_ids = sorted({r["contract_id"] for r in rows})
        partition = ", ".join(keys)
        found = self.con.execute(
            f"SELECT {partition}, content_hash FROM (SELECT *, row_number() OVER ("
            f"PARTITION BY {partition} ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn "
            f"FROM {self._check_table(table)} WHERE as_of <= ? AND list_contains(?, contract_id)) WHERE rn = 1",
            [to_utc_naive(as_of), contract_ids],
        ).fetchall()
        return {tuple(_from_db(v) for v in row) for row in found}

    def _new_rows(self, table: str, keys: list[str], rows: list[dict[str, Any]], as_of: datetime) -> list[dict]:
        if not rows:
            return []
        unchanged = self._unchanged(table, keys, rows, as_of)
        # Stored timestamps come back aware (from _from_db); compare on that form.
        def ident(r: dict[str, Any]) -> tuple:
            return tuple(_from_db(r[k]) for k in keys) + (r["content_hash"],)
        return [r for r in rows if ident(r) not in unchanged]

    def ingest_contracts(
        self, contracts: Iterable[FuturesContract], *, run_id: str, as_of: datetime, staging_dir: Path
    ) -> int:
        batch = list(contracts)
        validate_contracts(batch)
        rows = []
        for c in batch:
            expiry = to_utc_naive(c.expiry_utc)
            # float(): the hash is JSON of the values, so 50 and 50.0 would otherwise differ.
            multiplier = float(c.multiplier)
            values = [c.contract_id, c.root, c.underlying_index, expiry.isoformat(), multiplier]
            rows.append({
                "contract_id": c.contract_id, "root": c.root, "underlying_index": c.underlying_index,
                "expiry_utc": expiry, "multiplier": multiplier, "source": c.source,
                **self._provenance(values, run_id, as_of),
            })
        rows = self._new_rows("futures_contract", ["source", "contract_id"], rows, as_of)
        if not rows:
            return 0
        name = f"contracts_{_hash([r['content_hash'] for r in rows])[:12]}.parquet"
        path = Path(staging_dir) / safe_name(run_id) / name
        return self._bulk_insert("futures_contract", _CONTRACT_COLUMNS, rows, path)

    def _known_contract_ids(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT DISTINCT contract_id FROM futures_contract").fetchall()}

    def ingest_bars(self, bars: Iterable[FuturesBar], *, run_id: str, as_of: datetime, staging_dir: Path) -> int:
        batch = list(bars)
        validate_bars(batch, self._known_contract_ids())
        rows = []
        for b in batch:
            start = to_utc_naive(b.bar_start_utc)
            # float(): 6031 and 6031.0 are the same price, and must hash the same (PR #2264 review).
            o, h, lo, c = (float(p) for p in (b.open, b.high, b.low, b.close))
            values = [b.contract_id, start.isoformat(), o, h, lo, c, b.volume]
            rows.append({
                "contract_id": b.contract_id, "bar_start_utc": start, "open": o, "high": h,
                "low": lo, "close": c, "volume": b.volume, "source": b.source,
                **self._provenance(values, run_id, as_of),
            })
        rows = self._new_rows("futures_bar_1m", ["source", "contract_id", "bar_start_utc"], rows, as_of)
        if not rows:
            return 0
        first = min(r["bar_start_utc"] for r in rows).strftime("%Y%m%dT%H%M")
        name = f"bars_{first}_{_hash([r['content_hash'] for r in rows])[:12]}.parquet"
        return self._bulk_insert("futures_bar_1m", _BAR_COLUMNS, rows, Path(staging_dir) / safe_name(run_id) / name)

    # --- reads ----------------------------------------------------------------------------------

    def _known_at(self, known_at: datetime | None) -> datetime:
        return to_utc_naive(known_at if known_at is not None else self._clock())

    @staticmethod
    def _one_source_per(rows: list[dict[str, Any]], key: str, what: str) -> None:
        """Without an explicit source, two sources for one key are ambiguous: fail closed."""
        sources: dict[Any, set[str]] = {}
        for r in rows:
            sources.setdefault(r[key], set()).add(r["source"])
        clashes = sorted(str(k) for k, s in sources.items() if len(s) > 1)
        if clashes:
            raise ValueError(f"{what} stored by more than one source ({', '.join(clashes[:3])}); pass source=")

    def bars(
        self,
        contract_id: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
        known_at: datetime | None = None,
        source: str | None = None,
    ) -> list[FuturesBar]:
        """Latest known version of each bar with start_utc <= bar_start_utc < end_utc."""
        rows = self._rows(
            f"SELECT * FROM ({_LATEST_BARS}) WHERE bar_start_utc >= ? AND bar_start_utc < ? ORDER BY bar_start_utc",
            [contract_id, self._known_at(known_at), source, source, to_utc_naive(start_utc), to_utc_naive(end_utc)],
        )
        self._one_source_per(rows, "bar_start_utc", "bar")
        return [
            FuturesBar(
                contract_id=r["contract_id"], bar_start_utc=r["bar_start_utc"], open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=int(r["volume"]), source=r["source"],
            )
            for r in rows
        ]

    def close_at(
        self, contract_id: str, at_utc: datetime, *, known_at: datetime | None = None, source: str | None = None
    ) -> float | None:
        """Close of the bar ending at ``at_utc``, or None. No lookahead, no fill-forward."""
        start = to_utc_naive(at_utc) - ONE_MINUTE  # raises ValueError on a naive time
        rows = self._rows(
            f"SELECT source, bar_start_utc, close FROM ({_LATEST_BARS}) WHERE bar_start_utc = ?",
            [contract_id, self._known_at(known_at), source, source, start],
        )
        self._one_source_per(rows, "bar_start_utc", "bar")
        return float(rows[0]["close"]) if rows else None

    def contract(
        self, contract_id: str, *, known_at: datetime | None = None, source: str | None = None
    ) -> FuturesContract | None:
        rows = self._rows(_LATEST_CONTRACTS, [contract_id, self._known_at(known_at), source, source])
        self._one_source_per(rows, "contract_id", "contract")
        if not rows:
            return None
        r = rows[0]
        return FuturesContract(
            contract_id=r["contract_id"], root=r["root"], underlying_index=r["underlying_index"],
            expiry_utc=r["expiry_utc"], multiplier=r["multiplier"], source=r["source"],
        )
