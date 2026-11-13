"""One-transaction batch insert shared by the append-only DuckDB stores.

Extracted unchanged from ``PriceStore._bulk_insert`` (market_data_ingest/prices/store.py) so
the futures store (docs/adr-overnight-futures-tracker.md, phase 3) can reuse it instead of
carrying a copy. The write rules it enforces are the price store's:

* rows go to a temporary NDJSON file, optionally staged as Parquet, and are counted; a count
  that does not match the batch raises before anything is written;
* the insert is ``SELECT DISTINCT ... ON CONFLICT DO NOTHING`` in one transaction, so identical
  re-ingests add nothing and any failure rolls the whole batch back.

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
) -> int:
    """Write ``rows`` to a file, optionally stage them as Parquet, insert in one transaction.

    Args:
        con: an open DuckDB connection.
        table: the target table; an internal constant, never caller text.
        columns: column name to DuckDB type, in insert order.
        rows: one dict per row; a missing key is inserted as NULL.
        parquet_path: where to stage the batch as Parquet first, or None to skip staging.

    Returns:
        How many rows were actually inserted (duplicates already stored count as zero).

    Raises:
        ValidationError: the staged row count does not match ``len(rows)``.
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
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return inserted
