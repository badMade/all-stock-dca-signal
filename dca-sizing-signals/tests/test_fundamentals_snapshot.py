"""Tests for the not-yet-written market_data_ingest/fundamentals_snapshot.py (feed step 4 of the scope doc).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

The snapshot is the FILE CONTRACT between this feed and the ranking in ``ticker-advisor`` (neither subproject
imports the other). It records, per symbol, what the SEC data says AS OF a date:

* a ``status`` that says why data is present or absent (``ok``, ``no_cik``, ``no_companyfacts``, ``unreadable``,
  ``no_facts``), so a missing value is explicit and never a silent zero;
* for ``ok`` symbols, the SIC sector and industry and the RAW latest first-filed fact of each concept (annual
  windows for gross profit, revenue and cost of revenue; the latest instant for assets, equity and shares
  outstanding), using the step 1 rules: first reported, public on ``as_of``. Ratios (gross profit over assets,
  book-to-price) are computed later by the adapter, not stored here;
* a coverage summary, ``as_of``, ``fetched_at`` (timezone-aware, what the 7-day freshness safeguard reads), and a
  ``content_sha256`` that covers everything except ``fetched_at`` and itself, like ``universe_proposal.json``.

All inputs are SYNTHETIC. No run of 6 or more digits appears below (the account-number guard in
tests/test_config_invariants.py), so zero-padded CIKs are built with ``zfill(10)``.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from market_data_ingest.edgar_bulk import BulkResult
from market_data_ingest.edgar_fundamentals import PointInTimeFact
from market_data_ingest.fundamentals_snapshot import (
    build_snapshot,
    snapshot_hash_matches,
    write_snapshot,
)
from market_data_ingest.sec_company_map import CompanyRef, SicInfo

AS_OF = date(2026, 6, 30)
FETCHED = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def cik(n: int) -> str:
    return str(n).zfill(10)


def pit(end, val, filed, *, start=None, form="10-K", accn="A-1") -> PointInTimeFact:
    to_date = date.fromisoformat
    return PointInTimeFact(
        start=to_date(start) if start else None, end=to_date(end), val=val, fy=None, fp=None, form=form,
        filed=to_date(filed), frame=None, accn=accn,
    )


AAA_FACTS = {
    "us-gaap:GrossProfit": [
        pit("2025-12-31", 400, "2026-02-11", start="2025-01-01", accn="A-2"),   # FY2025, the latest annual
        pit("2024-12-31", 300, "2025-02-10", start="2024-01-01", accn="A-1"),   # FY2024
        pit("2025-12-31", 120, "2026-02-11", start="2025-10-01", accn="A-2"),   # a quarter: ignored
        pit("2025-12-31", 380, "2026-08-01", start="2025-01-01", accn="A-9"),   # restated AFTER as_of: ignored
    ],
    "us-gaap:Assets": [
        pit("2025-12-31", 5000, "2026-02-11", accn="A-2"),
        pit("2024-12-31", 4000, "2025-02-10", accn="A-1"),
    ],
    "us-gaap:StockholdersEquity": [pit("2025-12-31", 2000, "2026-02-11", accn="A-2")],
    "dei:EntityCommonStockSharesOutstanding": [
        pit("2026-02-05", 70, "2026-02-11", accn="A-2"),
        pit("2025-02-04", 65, "2025-02-10", accn="A-1"),
    ],
}
FFF_FACTS = {"us-gaap:Assets": [pit("2025-12-31", 10, "2026-08-15")]}  # filed after as_of: nothing public yet

COMPANIES = {
    "AAA": CompanyRef(cik=cik(1), name="Alpha Corp"),
    "CCC": CompanyRef(cik=cik(3), name="Charlie Corp"),   # no member in the zip
    "DDD": CompanyRef(cik=cik(4), name="Delta Corp"),     # member unreadable
    "EEE": CompanyRef(cik=cik(5), name="Echo Corp"),      # member present, no usable concepts
    "FFF": CompanyRef(cik=cik(6), name="Foxtrot Corp"),   # facts all filed after as_of
}
SIC = {
    cik(1): SicInfo(sic="3571", description="Electronic Computers", sector="35", industry="3571"),
    cik(5): None,
}
BULK = BulkResult(
    facts={cik(1): AAA_FACTS, cik(5): {}, cik(6): FFF_FACTS},
    missing_ciks=[cik(3)],
    unreadable={cik(4): "unreadable member (ValueError: bad)"},
)
SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]  # BBB has no CIK at all


def build(**overrides):
    kwargs = dict(as_of=AS_OF, fetched_at=FETCHED, symbols=SYMBOLS, companies=COMPANIES, sic_by_cik=SIC, bulk=BULK)
    kwargs.update(overrides)
    return build_snapshot(**kwargs)


# --- statuses and coverage -------------------------------------------------------------------------------------


def test_every_symbol_gets_a_status_that_says_why_data_is_present_or_absent():
    records = build()["symbols"]
    assert {s: records[s]["status"] for s in SYMBOLS} == {
        "AAA": "ok",
        "BBB": "no_cik",
        "CCC": "no_companyfacts",
        "DDD": "unreadable",
        "EEE": "no_facts",
        "FFF": "no_facts",  # public facts exist but none were public on as_of
    }


def test_an_unreadable_member_carries_its_reason():
    assert "bad" in build()["symbols"]["DDD"]["detail"]


def test_the_coverage_summary_counts_every_status():
    assert build()["coverage"] == {
        "symbols": 6, "ok": 1, "no_cik": 1, "no_companyfacts": 1, "unreadable": 1, "no_facts": 2,
    }


# --- what an ok record holds -------------------------------------------------------------------------------------


def test_an_ok_record_carries_the_cik_and_the_sic_sector_and_industry():
    record = build()["symbols"]["AAA"]
    assert (record["cik"], record["sic"], record["sector"], record["industry"]) == (cik(1), "3571", "35", "3571")


def test_a_company_with_facts_but_no_sic_is_still_ok_with_null_classification():
    bulk = BulkResult(facts={cik(1): AAA_FACTS}, missing_ciks=[], unreadable={})
    record = build(symbols=["AAA"], bulk=bulk, sic_by_cik={cik(1): None})["symbols"]["AAA"]
    assert record["status"] == "ok"
    assert (record["sic"], record["sector"], record["industry"]) == (None, None, None)


def test_annual_concepts_use_the_latest_first_filed_annual_window():
    gross = build()["symbols"]["AAA"]["facts"]["us-gaap:GrossProfit"]
    # FY2025 as first filed (400). Not the quarter (120) and not the restatement filed after as_of (380).
    assert gross == {"val": 400, "start": "2025-01-01", "end": "2025-12-31", "filed": "2026-02-11",
                     "form": "10-K", "accn": "A-2"}


def test_instant_concepts_use_the_latest_instant():
    facts = build()["symbols"]["AAA"]["facts"]
    assert facts["us-gaap:Assets"]["val"] == 5000 and facts["us-gaap:Assets"]["end"] == "2025-12-31"
    assert facts["us-gaap:StockholdersEquity"]["val"] == 2000
    assert facts["dei:EntityCommonStockSharesOutstanding"]["val"] == 70
    assert facts["dei:EntityCommonStockSharesOutstanding"]["start"] is None


def test_a_concept_the_company_never_reported_is_absent_from_its_facts():
    assert "us-gaap:Revenues" not in build()["symbols"]["AAA"]["facts"]


def test_the_stored_facts_are_raw_no_ratio_is_computed_here():
    record = build()["symbols"]["AAA"]
    assert not any("ratio" in key or "book_to_price" in key or "profitab" in key for key in record)


# --- the document ---------------------------------------------------------------------------------------------------


def test_the_document_records_kind_version_dates_and_symbols_in_sorted_order():
    doc = build()
    assert doc["kind"] == "fundamentals_snapshot" and doc["schema_version"] == 1
    assert doc["as_of"] == "2026-06-30" and doc["fetched_at"] == FETCHED.isoformat()
    assert list(doc["symbols"]) == sorted(SYMBOLS)


def test_input_order_never_changes_the_document():
    assert build(symbols=list(reversed(SYMBOLS))) == build()


def test_a_naive_fetched_at_is_refused():
    with pytest.raises(ValueError, match="timezone"):
        build(fetched_at=datetime(2026, 6, 30, 12, 0))


def test_a_repeated_symbol_is_counted_once():
    assert build(symbols=SYMBOLS + ["AAA"])["coverage"]["symbols"] == 6


# --- the hash ---------------------------------------------------------------------------------------------------------


def test_the_hash_ignores_fetched_at_but_changes_with_any_fact():
    a = build()
    later = build(fetched_at=FETCHED + timedelta(days=1))
    assert a["content_sha256"] == later["content_sha256"] and len(a["content_sha256"]) == 64
    changed_facts = {**AAA_FACTS, "us-gaap:Assets": [pit("2025-12-31", 5001, "2026-02-11", accn="A-2")]}
    changed = build(bulk=BulkResult(facts={**BULK.facts, cik(1): changed_facts}, missing_ciks=[cik(3)],
                                    unreadable=BULK.unreadable))
    assert changed["content_sha256"] != a["content_sha256"]


def test_the_hash_check_accepts_an_untouched_document_and_rejects_an_edited_one():
    doc = build()
    assert snapshot_hash_matches(doc) is True
    doc["symbols"]["AAA"]["facts"]["us-gaap:Assets"]["val"] = 1
    assert snapshot_hash_matches(doc) is False
    assert snapshot_hash_matches({"no": "hash"}) is False


# --- write_snapshot ------------------------------------------------------------


def test_write_snapshot_writes_json_atomically_and_returns_the_hash(tmp_path):
    doc = build()
    path = tmp_path / "deep" / "fundamentals_snapshot_2026-06-30.json"
    assert write_snapshot(path, doc) == doc["content_sha256"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(doc))
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left


def test_a_failed_write_leaves_the_existing_file_and_no_temp_file(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("old", encoding="utf-8")
    with patch.object(os, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            write_snapshot(path, build())
    assert path.read_text(encoding="utf-8") == "old"
    assert [p.name for p in Path(tmp_path).iterdir()] == ["snapshot.json"]


def test_write_snapshot_refuses_a_document_whose_hash_does_not_match(tmp_path):
    doc = build()
    doc["coverage"]["ok"] = 99
    with pytest.raises(ValueError, match="hash"):
        write_snapshot(tmp_path / "s.json", doc)
    assert list(tmp_path.iterdir()) == []
