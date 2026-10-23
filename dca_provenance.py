"""Provenance sidecars for fetched market-data files.

A fetched CSV/JSON that feeds sizing, evaluation or a model retrain carried no
record of *when* it was fetched, *from where*, or *what bytes it held*, so a
later "which data produced this result?" question could only be answered by
guessing from file mtimes (which the Drive mirror rewrites; see the checkout's
hard-link notes). This module writes one small ``<file>.provenance.json`` next
to the data file: the SHA-256 of the exact bytes on disk, a byte size, the
source label, a caller-supplied fetch timestamp, and optional row count / date
range. Downstream records (the ml-forecaster registry) reuse :func:`sha256_file`
so a dataset fingerprint means the same thing everywhere.

Standard library plus :mod:`dca_fileio` only, no network, and no clock: the
fetch time is a *parameter*, so a record is reproducible in tests and a caller
cannot accidentally stamp the write time on data fetched earlier.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from dca_fileio import atomic_write_text

#: Appended to the data file's *full* name (``TICK.csv`` -> ``TICK.csv.provenance.json``)
#: so the sidecar never ends in ``.csv`` and a ``*.csv`` archive glob cannot pick it up.
SIDECAR_SUFFIX = ".provenance.json"

SCHEMA_VERSION = 1

# 1 MiB reads keep memory flat for the multi-hundred-MB archive CSVs.
_HASH_CHUNK_BYTES = 1 << 20


def sha256_file(path: str | Path, *, chunk_size: int = _HASH_CHUNK_BYTES) -> str:
    """Hex SHA-256 of the file's bytes, streamed so large archives never load whole."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_path(path: str | Path) -> Path:
    """Where the provenance record for ``path`` lives."""
    target = Path(path)
    return target.with_name(target.name + SIDECAR_SUFFIX)


def _iso(value: str | date | datetime | None, *, field: str) -> Optional[str]:
    """Normalize a caller-supplied date/time to an ISO string; reject anything else.

    A naive ``datetime`` is refused for ``fetched_at`` by the caller's own check
    below, not here: dates (``first_date``/``last_date``) are legitimately naive.
    """
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{field} must be a date, datetime or non-empty string, got {value!r}")


def build_record(
    path: str | Path,
    *,
    source: str,
    fetched_at: str | datetime,
    rows: Optional[int] = None,
    first_date: str | date | datetime | None = None,
    last_date: str | date | datetime | None = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the provenance record for the file currently at ``path``.

    The hash is computed from the bytes on disk *now*, so call this after the
    data file's atomic write has landed. ``fetched_at`` must be timezone-aware
    when given as a ``datetime``: a naive timestamp would be ambiguous across
    the ET/UTC boundary the scheduled slots straddle.
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if isinstance(fetched_at, datetime) and fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    fetched = _iso(fetched_at, field="fetched_at")
    if fetched is None:
        raise ValueError("fetched_at is required")
    target = Path(path)
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": target.name,
        "file_sha256": sha256_file(target),
        "size_bytes": target.stat().st_size,
        "source": source.strip(),
        "fetched_at": fetched,
        "rows": rows,
        "first_date": _iso(first_date, field="first_date"),
        "last_date": _iso(last_date, field="last_date"),
    }
    if extra:
        record["extra"] = dict(extra)
    return record


def write_sidecar(
    path: str | Path,
    *,
    source: str,
    fetched_at: str | datetime,
    rows: Optional[int] = None,
    first_date: str | date | datetime | None = None,
    last_date: str | date | datetime | None = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Write ``<path>.provenance.json`` atomically and return its path."""
    record = build_record(
        path,
        source=source,
        fetched_at=fetched_at,
        rows=rows,
        first_date=first_date,
        last_date=last_date,
        extra=extra,
    )
    out = sidecar_path(path)
    atomic_write_text(out, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return out


def read_sidecar(path: str | Path) -> Optional[Dict[str, Any]]:
    """The provenance record for ``path``, or ``None`` if absent or unparseable.

    Degrades to ``None`` rather than raising: a missing or torn sidecar means
    "provenance unknown", which callers must treat as unverified, not as an error
    that blocks reading the data itself.
    """
    try:
        loaded = json.loads(sidecar_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def matches_data_file(path: str | Path) -> bool:
    """True when the sidecar exists and its hash still equals the data file's bytes."""
    record = read_sidecar(path)
    if record is None:
        return False
    try:
        return record.get("file_sha256") == sha256_file(path)
    except OSError:
        return False
