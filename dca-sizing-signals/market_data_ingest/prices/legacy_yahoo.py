"""Import the existing Yahoo archive (``ticker_data/*.csv``) as ``source='yahoo_legacy'``.

Rows go to ``yahoo_legacy_bars``, never ``daily_bars_raw``: Yahoo ``Close`` is split-adjusted
as of download, so the raw views would adjust it twice (ADR §7). A file is parsed whole
before anything is written; one bad line rejects that file with its name and line number.
A directory import lists every rejected file in its report rather than skipping it.

Only ``Close`` must be positive. Older Yahoo history routinely carries zero ``Open``/``High``/
``Low`` placeholders, and rejecting those would reject most long histories for a field the
reconciliation does not compare.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .models import ImportReport
from .store import PriceStore

logger = logging.getLogger(__name__)

HEADER = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
MIN_YEAR = 1900  # a yearless "04-27" can surface as year 0001 elsewhere; refuse anything implausible
_FIELDS = [
    ("open", "Open"),
    ("high", "High"),
    ("low", "Low"),
    ("close", "Close"),
    ("adj_close", "Adj Close"),
    ("volume", "Volume"),
    ("dividends", "Dividends"),
    ("stock_splits", "Stock Splits"),
]


def _parse_line(row: dict[str, str], where: str) -> dict[str, Any]:
    raw_date = (row.get("Date") or "").strip()
    try:
        session = date.fromisoformat(raw_date[:10]) if len(raw_date) >= 10 else None
    except ValueError:
        session = None
    if session is None or session.year < MIN_YEAR:
        raise ValidationError(f"{where}: unparseable or implausible date {raw_date[:20]!r}")
    out: dict[str, Any] = {"session": session}
    for key, column in _FIELDS:
        text = (row.get(column) or "").strip()
        try:
            value = float(text)
        except ValueError:
            raise ValidationError(f"{where}: {column} is missing or not a number") from None
        if not math.isfinite(value) or value < 0:
            raise ValidationError(f"{where}: {column} is not a finite non-negative number")
        out[key] = value
    if out["close"] <= 0:
        raise ValidationError(f"{where}: Close must be positive")
    return out


def read_csv(path: Path) -> list[dict[str, Any]]:
    """Parse and validate a whole Yahoo CSV, or raise ``ValidationError`` naming file and line."""
    rows: list[dict[str, Any]] = []
    seen: set[date] = set()
    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != HEADER:
            raise ValidationError(f"{path.name} line 1: header is not {','.join(HEADER)}")
        for line_no, values in enumerate(reader, start=2):
            where = f"{path.name} line {line_no}"
            if len(values) != len(HEADER):
                raise ValidationError(f"{where}: expected {len(HEADER)} columns, found {len(values)}")
            parsed = _parse_line(dict(zip(HEADER, values, strict=True)), where)
            if parsed["session"] in seen:
                raise ValidationError(f"{where}: duplicate session {parsed['session']}")
            seen.add(parsed["session"])
            rows.append(parsed)
    return rows


def import_csv(store: PriceStore, path: Path, *, run_id: str, now: datetime) -> int:
    path = Path(path)
    rows = read_csv(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return store.ingest_yahoo_legacy(rows, symbol=path.stem, run_id=run_id, as_of=now, file_sha256=digest)


def import_dir(store: PriceStore, directory: Path, *, run_id: str, now: datetime) -> ImportReport:
    report = ImportReport()
    for path in sorted(Path(directory).glob("*.csv")):
        try:
            report.imported[path.stem] = import_csv(store, path, run_id=run_id, now=now)
        except (ValidationError, OSError, UnicodeDecodeError) as exc:
            logger.error("yahoo_legacy import rejected %s: %s", path.name, exc)
            report.failed[path.stem] = f"{type(exc).__name__}: {exc}"
    return report
