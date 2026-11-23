"""Tests for the not-yet-written market_data_ingest/edgar_bulk.py (step 3 of docs/fundamentals_feed_scope.md).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

The SEC publishes every company's facts in one nightly zip (``companyfacts.zip``), one ``CIK<10 digits>.json``
member per company, each member shaped like the per-company ``companyfacts`` API document. Reading that file
once replaces about 5,500 separate downloads. This module reads it WITHOUT any network access: it walks the
zip one member at a time (memory stays bounded to one company), keeps only the requested companies and
concepts, and returns the same ``PointInTimeFact`` objects ``edgar_fundamentals`` already produces. Choosing
the first-filed value and computing profitability or book-to-price are later steps.

The fixtures are SYNTHETIC. The member layout (``facts`` -> taxonomy -> concept -> ``units`` -> unit -> list of
entries) is the one the existing fetcher's live-verified fixtures use. Which concepts the real zip carries, and
how large it is, were NOT checked here. No run of 6 or more digits appears below (the account-number guard in
tests/test_config_invariants.py), so zero-padded CIKs are built with ``zfill(10)``.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest
from market_data_ingest.edgar_bulk import (
    DEFAULT_CONCEPTS,
    Concept,
    parse_companyfacts_document,
    read_bulk_companyfacts,
)
from market_data_ingest.edgar_fundamentals import PointInTimeFact

ASSETS = Concept("us-gaap", "Assets", "USD")
EQUITY = Concept("us-gaap", "StockholdersEquity", "USD")
SHARES = Concept("dei", "EntityCommonStockSharesOutstanding", "shares")


def entry(end="2025-12-31", val=1000, filed="2026-02-11", start=None, form="10-K", accn="A-1"):
    return {"start": start, "end": end, "val": val, "fy": 2025, "fp": "FY", "form": form, "filed": filed,
            "frame": None, "accn": accn}


def document(**concepts):
    """A companyfacts document. ``concepts`` maps ``taxonomy__Name__unit`` to a list of entries."""
    facts: dict = {}
    for spec, entries in concepts.items():
        taxonomy, name, unit = spec.split("__")
        facts.setdefault(taxonomy, {}).setdefault(name, {"units": {}})["units"][unit] = entries
    return {"cik": 1, "entityName": "Synthetic Corp", "facts": facts}


def member_name(cik: str) -> str:
    return f"CIK{cik.zfill(10)}.json"


def make_zip(tmp_path: Path, members: dict[str, object]) -> Path:
    """Write a zip whose members are ``{name: JSON-able object or raw text}``."""
    path = tmp_path / "companyfacts.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body if isinstance(body, str) else json.dumps(body))
    return path


# --- parse_companyfacts_document ---------------------------------------------------------------------------


def test_a_document_yields_facts_keyed_by_taxonomy_and_concept():
    doc = document(
        **{"us-gaap__Assets__USD": [entry(val=500)], "dei__EntityCommonStockSharesOutstanding__shares": [entry(val=70)]}
    )
    parsed = parse_companyfacts_document(doc, [ASSETS, SHARES])
    assert set(parsed) == {"us-gaap:Assets", "dei:EntityCommonStockSharesOutstanding"}
    fact = parsed["us-gaap:Assets"][0]
    assert isinstance(fact, PointInTimeFact)
    assert (fact.val, fact.end, fact.filed, fact.accn) == (500, date(2025, 12, 31), date(2026, 2, 11), "A-1")


def test_a_concept_the_company_never_reported_is_omitted_not_empty():
    parsed = parse_companyfacts_document(document(**{"us-gaap__Assets__USD": [entry()]}), [ASSETS, EQUITY])
    assert set(parsed) == {"us-gaap:Assets"}


def test_a_concept_reported_only_in_another_unit_is_omitted():
    parsed = parse_companyfacts_document(document(**{"us-gaap__Assets__EUR": [entry()]}), [ASSETS])
    assert parsed == {}


def test_only_the_requested_concepts_are_extracted():
    doc = document(**{"us-gaap__Assets__USD": [entry()], "us-gaap__StockholdersEquity__USD": [entry()]})
    assert set(parse_companyfacts_document(doc, [EQUITY])) == {"us-gaap:StockholdersEquity"}


def test_a_document_with_no_facts_block_yields_nothing():
    assert parse_companyfacts_document({"cik": 1, "entityName": "x"}, [ASSETS]) == {}


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(["not", "an", "object"], id="document_not_an_object"),
        pytest.param({"facts": []}, id="facts_not_an_object"),
        pytest.param(document(**{"us-gaap__Assets__USD": ["not an entry"]}), id="entry_not_an_object"),
        pytest.param(document(**{"us-gaap__Assets__USD": [entry(val="12")]}), id="value_not_numeric"),
        pytest.param(document(**{"us-gaap__Assets__USD": [entry(filed="not a date")]}), id="bad_filed_date"),
    ],
)
def test_a_malformed_document_is_refused(doc):
    with pytest.raises(ValueError):
        parse_companyfacts_document(doc, [ASSETS])


# --- DEFAULT_CONCEPTS ----------------------------------------------------------------------------------------


def test_the_default_concepts_cover_profitability_book_value_and_share_count():
    keys = {c.key for c in DEFAULT_CONCEPTS}
    assert keys == {
        "us-gaap:GrossProfit",
        "us-gaap:Revenues",
        "us-gaap:CostOfRevenue",
        "us-gaap:Assets",
        "us-gaap:StockholdersEquity",
        "dei:EntityCommonStockSharesOutstanding",
    }
    assert Concept("dei", "EntityCommonStockSharesOutstanding", "shares") in DEFAULT_CONCEPTS


# --- read_bulk_companyfacts -----------------------------------------------------------------------------------


def test_requested_companies_are_read_and_the_rest_are_ignored(tmp_path):
    path = make_zip(
        tmp_path,
        {
            member_name("111"): document(**{"us-gaap__Assets__USD": [entry(val=1)]}),
            member_name("222"): document(**{"us-gaap__Assets__USD": [entry(val=2)]}),
            member_name("333"): document(**{"us-gaap__Assets__USD": [entry(val=3)]}),  # not requested
            "README.txt": "not a company",
        },
    )
    result = read_bulk_companyfacts(path, ciks=["111", "222"], concepts=[ASSETS])
    assert set(result.facts) == {"111".zfill(10), "222".zfill(10)}
    assert result.facts["222".zfill(10)]["us-gaap:Assets"][0].val == 2
    assert result.missing_ciks == []
    assert result.unreadable == {}


def test_a_requested_company_absent_from_the_zip_is_reported_missing(tmp_path):
    # Foreign filers and funds often have no companyfacts document. That is reported, not an error.
    path = make_zip(tmp_path, {member_name("111"): document(**{"us-gaap__Assets__USD": [entry()]})})
    result = read_bulk_companyfacts(path, ciks=["111", "444", "222"], concepts=[ASSETS])
    assert result.missing_ciks == ["222".zfill(10), "444".zfill(10)]  # sorted, padded


def test_a_company_present_but_with_none_of_the_concepts_is_present_with_no_facts(tmp_path):
    path = make_zip(tmp_path, {member_name("111"): document(**{"us-gaap__Liabilities__USD": [entry()]})})
    result = read_bulk_companyfacts(path, ciks=["111"], concepts=[ASSETS])
    assert result.facts == {"111".zfill(10): {}}
    assert result.missing_ciks == []


def test_ciks_may_be_given_padded_or_not_and_repeats_are_harmless(tmp_path):
    path = make_zip(tmp_path, {member_name("111"): document(**{"us-gaap__Assets__USD": [entry()]})})
    result = read_bulk_companyfacts(path, ciks=["111", "111".zfill(10), "111"], concepts=[ASSETS])
    assert list(result.facts) == ["111".zfill(10)]


def test_a_malformed_member_is_reported_and_the_others_are_still_read(tmp_path):
    path = make_zip(
        tmp_path,
        {
            member_name("111"): "{ this is not json",
            member_name("222"): document(**{"us-gaap__Assets__USD": [entry(val=2)]}),
        },
    )
    result = read_bulk_companyfacts(path, ciks=["111", "222"], concepts=[ASSETS])
    assert "222".zfill(10) in result.facts and "111".zfill(10) not in result.facts
    assert set(result.unreadable) == {"111".zfill(10)}
    assert result.missing_ciks == []  # it was in the zip, just unreadable


def test_a_member_with_duplicate_json_keys_is_unreadable(tmp_path):
    path = make_zip(tmp_path, {member_name("111"): '{"facts": {}, "facts": {}}'})
    result = read_bulk_companyfacts(path, ciks=["111"], concepts=[ASSETS])
    assert set(result.unreadable) == {"111".zfill(10)}


def test_a_member_with_a_bad_fact_entry_is_unreadable_not_fatal(tmp_path):
    path = make_zip(tmp_path, {member_name("111"): document(**{"us-gaap__Assets__USD": [entry(val="x")]})})
    result = read_bulk_companyfacts(path, ciks=["111"], concepts=[ASSETS])
    assert set(result.unreadable) == {"111".zfill(10)}


def test_a_member_over_the_size_cap_is_unreadable_and_never_parsed(tmp_path):
    path = make_zip(tmp_path, {member_name("111"): document(**{"us-gaap__Assets__USD": [entry()]})})
    result = read_bulk_companyfacts(path, ciks=["111"], concepts=[ASSETS], max_member_bytes=10)
    assert set(result.unreadable) == {"111".zfill(10)}
    assert "size" in result.unreadable["111".zfill(10)]


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    path = tmp_path / "companyfacts.zip"
    path.write_bytes(b"this is not a zip")
    with pytest.raises(ValueError, match="zip"):
        read_bulk_companyfacts(path, ciks=["111"], concepts=[ASSETS])


def test_a_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        read_bulk_companyfacts(tmp_path / "absent.zip", ciks=["111"], concepts=[ASSETS])


@pytest.mark.parametrize("bad", ["abc", "", "1" * 11])
def test_a_bad_cik_in_the_request_is_refused_before_reading(tmp_path, bad):
    path = make_zip(tmp_path, {})
    with pytest.raises(ValueError):
        read_bulk_companyfacts(path, ciks=[bad], concepts=[ASSETS])


def test_reading_accepts_a_file_object_as_well_as_a_path(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name("111"), json.dumps(document(**{"us-gaap__Assets__USD": [entry(val=9)]})))
    buffer.seek(0)
    result = read_bulk_companyfacts(buffer, ciks=["111"], concepts=[ASSETS])
    assert result.facts["111".zfill(10)]["us-gaap:Assets"][0].val == 9
