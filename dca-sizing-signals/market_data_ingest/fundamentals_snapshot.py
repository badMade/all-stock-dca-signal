"""Build and write the fundamentals snapshot: the file contract between this feed and the daily ranking (feed step 4).

Specified first by tests/test_fundamentals_snapshot.py (docs/fundamentals_feed_scope.md). ``ticker-advisor`` reads
the file; neither subproject imports the other. The snapshot records, per symbol, what the SEC data says AS OF a
date:

* a ``status`` that says why data is present or absent (``ok``, ``no_cik``, ``no_companyfacts``, ``unreadable``,
  ``no_facts``), so a missing value is explicit and never a silent zero;
* for ``ok`` symbols, the SIC sector and industry and the RAW latest first-filed fact of each concept (annual
  windows for gross profit, revenue and cost of revenue; the latest instant for assets, equity and shares
  outstanding), chosen with the step 1 rules: first reported and public on ``as_of``. Ratios (gross profit over
  assets, book-to-price) are computed later by the adapter, never stored here;
* a coverage summary, ``as_of``, a timezone-aware ``fetched_at`` (what the 7-day freshness safeguard reads) and a
  ``content_sha256`` over everything except ``fetched_at`` and itself, like ``universe_proposal.json``.

Pure functions plus one atomic file writer. No network. Research ingest only: nothing on the live DCA side imports
this (tests/test_architecture_boundaries.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .edgar_bulk import BulkResult
from .edgar_fundamentals import PointInTimeFact, first_filed_facts, latest_fact, select_annual_facts
from .sec_company_map import CompanyRef, SicInfo

SCHEMA_VERSION = 1
KIND = "fundamentals_snapshot"
STATUSES = ("ok", "no_cik", "no_companyfacts", "unreadable", "no_facts")
# Duration concepts are read from fiscal-year windows; every other default concept is an instant.
ANNUAL_CONCEPT_KEYS = frozenset({"us-gaap:GrossProfit", "us-gaap:Revenues", "us-gaap:CostOfRevenue"})
_HASH_EXCLUDED = ("fetched_at", "content_sha256")


def _fact_record(fact: PointInTimeFact) -> dict[str, Any]:
    return {
        "val": fact.val,
        "start": fact.start.isoformat() if fact.start else None,
        "end": fact.end.isoformat(),
        "filed": fact.filed.isoformat(),
        "form": fact.form,
        "accn": fact.accn,
    }


def _latest_public_fact(key: str, facts: list[PointInTimeFact], as_of: date) -> PointInTimeFact | None:
    if key in ANNUAL_CONCEPT_KEYS:
        candidates = select_annual_facts(facts)
    else:
        candidates = [f for f in facts if f.start is None]
    return latest_fact(first_filed_facts(candidates, as_of=as_of))


def _symbol_record(
    company: CompanyRef | None, sic: SicInfo | None, bulk: BulkResult, as_of: date
) -> dict[str, Any]:
    if company is None:
        return {"status": "no_cik"}
    cik = company.cik
    if cik in bulk.unreadable:
        return {"status": "unreadable", "cik": cik, "detail": bulk.unreadable[cik]}
    if cik not in bulk.facts:
        return {"status": "no_companyfacts", "cik": cik}
    selected: dict[str, dict[str, Any]] = {}
    for key, facts in sorted(bulk.facts[cik].items()):
        latest = _latest_public_fact(key, facts, as_of)
        if latest is not None:
            selected[key] = _fact_record(latest)
    if not selected:
        return {"status": "no_facts", "cik": cik}
    return {
        "status": "ok",
        "cik": cik,
        "sic": sic.sic if sic else None,
        "sic_description": sic.description if sic else None,
        "sector": sic.sector if sic else None,
        "industry": sic.industry if sic else None,
        "facts": selected,
    }


def _canonical(payload: object) -> str:
    # allow_nan=False: a NaN would serialize as a bare token strict parsers reject and make the hash depend on it.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(document: Mapping[str, Any]) -> str:
    content = {k: v for k, v in document.items() if k not in _HASH_EXCLUDED}
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def build_snapshot(
    *,
    as_of: date,
    fetched_at: datetime,
    symbols: Iterable[str],
    companies: Mapping[str, CompanyRef],
    sic_by_cik: Mapping[str, SicInfo | None],
    bulk: BulkResult,
) -> dict[str, Any]:
    """The snapshot document for ``symbols`` as of ``as_of``. Raises ``ValueError`` on a naive ``fetched_at``."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must be timezone-aware")
    records: dict[str, dict[str, Any]] = {}
    for symbol in sorted(set(symbols)):
        company = companies.get(symbol)
        sic = sic_by_cik.get(company.cik) if company else None
        records[symbol] = _symbol_record(company, sic, bulk, as_of)
    coverage = {"symbols": len(records), **{s: sum(r["status"] == s for r in records.values()) for s in STATUSES}}
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "as_of": as_of.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "coverage": coverage,
        "symbols": records,
    }
    document["content_sha256"] = _digest(document)
    return document


def snapshot_hash_matches(document: Any) -> bool:
    """True when ``document``'s ``content_sha256`` matches its own content (``fetched_at`` is not covered)."""
    if not isinstance(document, dict) or not isinstance(document.get("content_sha256"), str):
        return False
    try:
        return _digest(document) == document["content_sha256"]
    except (TypeError, ValueError):
        return False


def write_snapshot(path: str | Path, snapshot: dict[str, Any]) -> str:
    """Write ``snapshot`` as JSON atomically (temp file, then rename) and return its hash.

    Raises:
        ValueError: the document's hash does not match its content (nothing is written).
        OSError: the file could not be written; an existing file is left as it was.
    """
    if not snapshot_hash_matches(snapshot):
        raise ValueError("snapshot hash does not match its content")
    target = Path(path)
    text = json.dumps(snapshot, sort_keys=True, indent=2, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return str(snapshot["content_sha256"])
