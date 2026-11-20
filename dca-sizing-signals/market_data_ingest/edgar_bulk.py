"""Read the SEC's nightly ``companyfacts.zip`` without any network access (feed step 3).

Specified first by tests/test_edgar_bulk.py (docs/fundamentals_feed_scope.md). The SEC publishes every
company's facts in one zip, one ``CIK<10 digits>.json`` member per company, each shaped like the per-company
``companyfacts`` API document. Reading it once replaces about 5,500 separate downloads.

Only the requested companies are opened (a direct member lookup, so the other members are never read), one at
a time, so memory stays bounded to one company's document. Only the requested concepts are kept, as the same
``PointInTimeFact`` objects ``edgar_fundamentals`` produces. Choosing the first-filed value and computing
profitability or book-to-price are later steps.

Failure rules, fail closed but not brittle for one bad company out of thousands:

* A requested company with no member is ``missing_ciks`` (foreign filers and funds often have none). Reported,
  not an error.
* A member that cannot be read (invalid or duplicate-key JSON, a malformed fact entry, a body over the size
  cap) is listed in ``unreadable`` with a reason that never contains response text, and the rest are still read.
  The caller decides how many unreadable members it will tolerate.
* A file that is not a zip, or a malformed CIK in the request, is refused.

Research ingest only: nothing on the live DCA side imports this (tests/test_architecture_boundaries.py).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from ._json_get import parse_strict_json_object
from .edgar_fundamentals import PointInTimeFact, _normalize_cik, _parse_fact

# The biggest filers' documents are a few tens of MB; this refuses anything absurd rather than parsing it.
DEFAULT_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_SOURCE = "SEC EDGAR companyfacts.zip"


@dataclass(frozen=True)
class Concept:
    """One XBRL concept to keep: its taxonomy, name and unit, for example ``us-gaap`` ``Assets`` ``USD``."""

    taxonomy: str
    name: str
    unit: str

    @property
    def key(self) -> str:
        return f"{self.taxonomy}:{self.name}"


# What the profitability (gross profit over assets, with revenue minus cost of revenue as a fallback) and
# book-to-price (equity over shares outstanding times price) factors need.
DEFAULT_CONCEPTS: tuple[Concept, ...] = (
    Concept("us-gaap", "GrossProfit", "USD"),
    Concept("us-gaap", "Revenues", "USD"),
    Concept("us-gaap", "CostOfRevenue", "USD"),
    Concept("us-gaap", "Assets", "USD"),
    Concept("us-gaap", "StockholdersEquity", "USD"),
    Concept("dei", "EntityCommonStockSharesOutstanding", "shares"),
)


@dataclass(frozen=True)
class BulkResult:
    """``facts[cik][concept.key]`` lists, ``missing_ciks`` (no member), ``unreadable[cik]`` (reason)."""

    facts: dict[str, dict[str, list[PointInTimeFact]]]
    missing_ciks: list[str]
    unreadable: dict[str, str]


def parse_companyfacts_document(document: Any, concepts: Iterable[Concept]) -> dict[str, list[PointInTimeFact]]:
    """The requested concepts' facts from one companyfacts document. Raises ``ValueError`` on a malformed one.

    A concept the company never reported, or reported only in another unit, is omitted (not an empty list).
    """
    if not isinstance(document, dict):
        raise ValueError(f"{_SOURCE} document is not an object")
    facts = document.get("facts")
    if facts is None:
        return {}
    if not isinstance(facts, dict):
        raise ValueError(f"{_SOURCE} 'facts' is not an object")
    out: dict[str, list[PointInTimeFact]] = {}
    for concept in concepts:
        taxonomy = facts.get(concept.taxonomy)
        if taxonomy is None:
            continue
        if not isinstance(taxonomy, dict):
            raise ValueError(f"{_SOURCE} taxonomy block is not an object")
        block = taxonomy.get(concept.name)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ValueError(f"{_SOURCE} concept block is not an object")
        entries = (block.get("units") or {}).get(concept.unit)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"{_SOURCE} unit entries are not a list")
        if entries:
            out[concept.key] = [_parse_fact(entry) for entry in entries]
    return out


def read_bulk_companyfacts(
    source: str | Path | BinaryIO,
    *,
    ciks: Iterable[str],
    concepts: Iterable[Concept] = DEFAULT_CONCEPTS,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
) -> BulkResult:
    """Read the requested companies' requested concepts from a ``companyfacts.zip`` (path or binary file object).

    Raises:
        ValueError: ``source`` is not a zip file, or a requested CIK is not up to ten digits.
        OSError: ``source`` cannot be opened.
    """
    wanted = sorted({_normalize_cik(cik) for cik in ciks})
    concept_list = list(concepts)
    try:
        archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{_SOURCE}: the file is not a zip file") from exc
    facts: dict[str, dict[str, list[PointInTimeFact]]] = {}
    missing: list[str] = []
    unreadable: dict[str, str] = {}
    with archive:
        for cik in wanted:
            try:
                info = archive.getinfo(f"CIK{cik}.json")
            except KeyError:
                missing.append(cik)
                continue
            if info.file_size > max_member_bytes:
                unreadable[cik] = f"member size {info.file_size} bytes exceeds the {max_member_bytes} byte cap"
                continue
            try:
                text = archive.read(info).decode("utf-8")
                document = parse_strict_json_object(text, source=_SOURCE)
                facts[cik] = parse_companyfacts_document(document, concept_list)
            except (ValueError, zipfile.BadZipFile) as exc:
                # Parser messages name the problem, never the body (.jules/sentinel.md).
                unreadable[cik] = f"unreadable member ({type(exc).__name__}: {exc})"
    return BulkResult(facts=facts, missing_ciks=missing, unreadable=unreadable)
