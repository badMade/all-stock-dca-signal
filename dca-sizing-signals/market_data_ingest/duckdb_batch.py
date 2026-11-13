"""One-transaction batch insert shared by the append-only DuckDB stores.

Extracted unchanged from ``PriceStore._bulk_insert`` (market_data_ingest/prices/store.py) so
the futures store (docs/adr-overnight-futures-tracker.md, phase 3) can reuse it instead of
carrying a copy. The write rules it enforces are the price store's:

* rows go to a temporary NDJSON file, optionally staged as Parquet, and are counted; a count
  that does not match the batch raises before anything is written;
* the insert is ``SELECT DISTINCT ... ON CONFLICT DO NOTHING`` in one transaction, so identical
  re-ingests add nothing and any failure rolls the whole batch back.
* ``drop_unchanged`` is the versioned dedupe (PR #2265 review): a row is new unless the newest
  version already known for its key, as of the batch's as_of, has the same content_hash. A
  primary key on content_hash alone cannot tell a re-ingest of A from a vendor reverting B back
  to A, and dropped the revert. Callers that dedupe this way pass ``require_all_new=True``, so a
  row the primary key still swallows (an older database file whose key lacks as_of and run_id,
  or a reused run_id) raises and rolls back instead of silently losing the revert.

``table`` and the ``columns`` keys are interpolated into SQL, so callers must pass their own
internal constants only, never caller-supplied text. PriceStore checks the table name against
its allowlist before calling.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from .prices.errors import ValidationError


def json_value(value: Any) -> Any:
    """Make one cell JSON-serialisable in the form DuckDB's read_json expects."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def sql_path(path: Path) -> str:
    """Quote a filesystem path for use inside a single-quoted SQL string."""
    return str(path).replace("'", "''")


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def bulk_insert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: dict[str, str],
    rows: list[dict[str, Any]],
    *,
    parquet_path: Path | None,
    require_all_new: bool = False,
) -> int:
    """Write ``rows`` to a file, optionally stage them as Parquet, insert in one transaction.

    Args:
        con: an open DuckDB connection.
        table: the target table; an internal constant, never caller text.
        columns: column name to DuckDB type, in insert order.
        rows: one dict per row; a missing key is inserted as NULL.
        parquet_path: where to stage the batch as Parquet first, or None to skip staging.
        require_all_new: the caller already removed unchanged rows (``drop_unchanged``), so every
            distinct staged row must insert; a primary-key conflict then means a real change was
            about to be dropped, and the batch is refused instead.

    Returns:
        How many rows were actually inserted (duplicates already stored count as zero).

    Raises:
        ValidationError: the staged row count does not match ``len(rows)``, or
            ``require_all_new`` is set and a distinct row conflicted with a stored one.
    """
    if not rows:
        return 0
    cols = ", ".join(columns)
    struct = "{" + ", ".join(f"'{k}': '{v}'" for k, v in columns.items()) + "}"
    with tempfile.TemporaryDirectory() as tmp:
        ndjson = Path(tmp) / "batch.ndjson"
        with ndjson.open("w") as fh:
            for row in rows:
                fh.write(json.dumps({k: json_value(row.get(k)) for k in columns}, allow_nan=False) + "\n")
        source = f"read_json('{sql_path(ndjson)}', format = 'newline_delimited', columns = {struct})"
        if parquet_path is not None:
            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"COPY (SELECT {cols} FROM {source}) TO '{sql_path(parquet_path)}' (FORMAT parquet)")
            source = f"read_parquet('{sql_path(parquet_path)}')"
        staged = con.execute(f"SELECT count(*) FROM {source}").fetchone()
        if not staged or int(staged[0]) != len(rows):
            raise ValidationError(f"staged {staged[0] if staged else 0} rows for a {len(rows)}-row batch")
        con.execute("BEGIN TRANSACTION")
        try:
            before = _count(con, table)
            con.execute(f"INSERT INTO {table} ({cols}) SELECT DISTINCT {cols} FROM {source} ON CONFLICT DO NOTHING")
            inserted = _count(con, table) - before
            if require_all_new:
                distinct = con.execute(f"SELECT count(*) FROM (SELECT DISTINCT {cols} FROM {source})").fetchone()
                expected = int(distinct[0]) if distinct else 0
                if inserted != expected:
                    raise ValidationError(
                        f"{table}: {expected - inserted} changed row(s) collided with the primary key; "
                        "the database file predates the versioned key (as_of, run_id) or a run_id was reused"
                    )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return inserted


def drop_unchanged(
    con: duckdb.DuckDBPyConnection, table: str, keys: list[str], rows: list[dict[str, Any]], *, as_of: datetime
) -> list[dict[str, Any]]:
    """The rows whose content_hash differs from the newest version known for their key at ``as_of``.

    Args:
        con: an open DuckDB connection.
        table: the target table; an internal constant, never caller text.
        keys: the columns that identify one versioned row (the natural key, no content_hash).
            Each must be NOT NULL and stored in the same Python form the rows carry (naive UTC
            datetimes, dates, strings), because stored and batch keys are compared directly.
        rows: the prepared batch, each with a ``content_hash``.
        as_of: the batch's naive-UTC as_of; versions known only after it are ignored.

    A re-ingest of the newest value is dropped; a revert to an older value is kept, because the
    newest version no longer matches it. The lookup is bounded to the batch's own key values
    (one ``list_contains`` filter per key column), not the table's whole history.
    """
    if not rows:
        return []
    partition = ", ".join(keys)
    bounds = " AND ".join(f"list_contains(?, {k})" for k in keys)
    found = con.execute(
        f"SELECT {partition}, content_hash FROM (SELECT *, row_number() OVER ("
        f"PARTITION BY {partition} ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn "
        f"FROM {table} WHERE as_of <= ? AND {bounds}) WHERE rn = 1",
        [as_of, *(sorted({r[k] for r in rows}) for k in keys)],
    ).fetchall()
    newest = {tuple(row[:-1]): row[-1] for row in found}
    return [r for r in rows if newest.get(tuple(r[k] for k in keys)) != r["content_hash"]]
