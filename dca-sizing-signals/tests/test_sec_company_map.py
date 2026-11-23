"""Tests for the not-yet-written market_data_ingest/sec_company_map.py (step 2 of docs/fundamentals_feed_scope.md).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

The module turns two SEC documents into what the daily ranking needs per symbol:

* ``company_tickers.json`` (ticker -> CIK, the key every SEC fact is stored under), and
* a company's ``submissions`` document (its SIC code, the sector and industry source the ADR chose
  because GICS is licensed).

HONESTY NOTE ON THE FIXTURES. They are SYNTHETIC and shaped after the SEC's documented layout as the
author remembers it: ``company_tickers.json`` is an object keyed "0", "1", ... whose values hold
``cik_str`` (an integer), ``ticker`` and ``title``; a submissions document carries a string ``sic`` and a
``sicDescription``. They were NOT checked against a live pull (the SEC refuses a request without a
contact-identifying User-Agent, and none is configured in the build environment). Before the fetchers are
relied on, a live pull on the operator's Mac must confirm this layout.

Two repo rules shape the literals below. No run of 6 or more digits may appear in a tracked file (the
account-number guard in tests/test_config_invariants.py), so CIKs are small numbers and their zero-padded
form is computed with ``zfill(10)`` instead of being written out. The ticker shape is ``^[A-Z.]{1,6}$``,
so the SEC's share-class dash ("BRK-B") must become a dot ("BRK.B").
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from market_data_ingest.sec_company_map import (
    CompanyRef,
    SicInfo,
    cik_for_tickers,
    fetch_company_tickers,
    fetch_sic,
    parse_company_tickers,
    parse_submissions_sic,
    sic_sector,
)

ALPHA = CompanyRef(cik="111".zfill(10), name="Alpha Corp")


def tickers_doc(*entries):
    return {
        str(i): {"cik_str": cik, "ticker": ticker, "title": title} for i, (cik, ticker, title) in enumerate(entries)
    }


# --- parse_company_tickers ---------------------------------------------------------------------------------


def test_tickers_map_to_zero_padded_ciks_and_names():
    parsed = parse_company_tickers(tickers_doc((111, "AAA", "Alpha Corp")))
    assert parsed == {"AAA": ALPHA}


def test_tickers_are_uppercased_and_a_share_class_dash_becomes_a_dot():
    parsed = parse_company_tickers(tickers_doc((222, "brk-b", "Bravo Holdings")))
    assert list(parsed) == ["BRK.B"]
    assert parsed["BRK.B"].cik == "222".zfill(10)


def test_two_tickers_for_one_company_are_both_kept():
    parsed = parse_company_tickers(tickers_doc((111, "AAA", "Alpha Corp"), (111, "AAAB", "Alpha Corp")))
    assert parsed["AAA"] == parsed["AAAB"] == ALPHA


def test_a_cik_given_as_a_digit_string_is_accepted():
    assert parse_company_tickers(tickers_doc(("111", "AAA", "Alpha Corp"))) == {"AAA": ALPHA}


def test_the_same_ticker_under_two_ciks_is_refused():
    # Guessing which company a symbol means would put another company's filings under it.
    with pytest.raises(ValueError, match="AAA"):
        parse_company_tickers(tickers_doc((111, "AAA", "Alpha Corp"), (222, "AAA", "Other Corp")))


def test_the_same_ticker_repeated_for_the_same_company_is_fine():
    assert parse_company_tickers(tickers_doc((111, "AAA", "Alpha Corp"), (111, "AAA", "Alpha Corp"))) == {"AAA": ALPHA}


def test_an_empty_document_is_an_empty_map():
    assert parse_company_tickers({}) == {}


@pytest.mark.parametrize(
    "document",
    [
        pytest.param([{"cik_str": 111, "ticker": "AAA", "title": "x"}], id="top_level_is_a_list"),
        pytest.param({"0": "not an object"}, id="entry_is_not_an_object"),
        pytest.param({"0": {"cik_str": 111, "title": "x"}}, id="ticker_missing"),
        pytest.param({"0": {"cik_str": 111, "ticker": "", "title": "x"}}, id="ticker_blank"),
        pytest.param({"0": {"ticker": "AAA", "title": "x"}}, id="cik_missing"),
        pytest.param({"0": {"cik_str": "abc", "ticker": "AAA", "title": "x"}}, id="cik_not_digits"),
        pytest.param({"0": {"cik_str": True, "ticker": "AAA", "title": "x"}}, id="cik_is_a_bool"),
        pytest.param({"0": {"cik_str": -1, "ticker": "AAA", "title": "x"}}, id="cik_negative"),
    ],
)
def test_a_malformed_document_is_refused_not_skipped(document):
    with pytest.raises(ValueError):
        parse_company_tickers(document)


# --- cik_for_tickers ---------------------------------------------------------------------------------------


def test_cik_for_tickers_splits_found_from_missing_and_ignores_order_and_repeats():
    mapping = parse_company_tickers(tickers_doc((111, "AAA", "Alpha Corp"), (222, "BBB", "Bravo Holdings")))
    found, missing = cik_for_tickers(mapping, ["ZZZ", "BBB", "AAA", "BBB", "QQQ"])
    assert sorted(found) == ["AAA", "BBB"]
    assert found["AAA"] == ALPHA
    assert missing == ["QQQ", "ZZZ"]  # sorted, each once


def test_cik_for_tickers_of_nothing_is_empty():
    assert cik_for_tickers({}, []) == ({}, [])


# --- sic_sector ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sic, sector", [("7372", "73"), ("3571", "35"), ("0100", "01"), ("2834", "28")])
def test_the_sector_is_the_two_digit_major_group(sic, sector):
    assert sic_sector(sic) == sector


@pytest.mark.parametrize("bad", ["", "abc", "35", "35710", None])
def test_sic_sector_refuses_a_code_that_is_not_four_digits(bad):
    with pytest.raises(ValueError):
        sic_sector(bad)


# --- parse_submissions_sic ----------------------------------------------------------------------------------


def test_a_submissions_document_yields_the_sic_sector_and_industry():
    info = parse_submissions_sic({"sic": "3571", "sicDescription": "Electronic Computers", "name": "Alpha Corp"})
    assert info == SicInfo(sic="3571", description="Electronic Computers", sector="35", industry="3571")


def test_a_numeric_sic_is_zero_padded_to_four_digits():
    info = parse_submissions_sic({"sic": 100, "sicDescription": "Agricultural Production-Crops"})
    assert info is not None and info.sic == "0100" and info.sector == "01"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"name": "Alpha Corp"}, id="no_sic_key"),
        pytest.param({"sic": "", "sicDescription": ""}, id="blank_sic"),
        pytest.param({"sic": None}, id="null_sic"),
    ],
)
def test_a_company_with_no_sic_is_none_not_an_error(document):
    # Funds and trusts often carry no SIC. That is "no classification", reported as such.
    assert parse_submissions_sic(document) is None


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(["not", "an", "object"], id="not_an_object"),
        pytest.param({"sic": "ABCD"}, id="letters"),
        pytest.param({"sic": "35710"}, id="too_long"),
        pytest.param({"sic": "-3571"}, id="negative"),
    ],
)
def test_a_malformed_submissions_document_is_refused(document):
    with pytest.raises(ValueError):
        parse_submissions_sic(document)


# --- fetchers (network patched out) ----------------------------------------------------------------------


@patch("market_data_ingest.sec_company_map._get_json")
def test_fetch_company_tickers_reads_the_sec_file_and_parses_it(mock_get):
    mock_get.return_value = tickers_doc((111, "AAA", "Alpha Corp"))
    assert fetch_company_tickers(user_agent="Acme Research ops@acme.example") == {"AAA": ALPHA}
    assert mock_get.call_args.args[0] == "https://www.sec.gov/files/company_tickers.json"


@patch("market_data_ingest.sec_company_map._get_json")
def test_fetch_sic_pads_the_cik_and_reads_that_companys_submissions(mock_get):
    mock_get.return_value = {"sic": "3571", "sicDescription": "Electronic Computers"}
    info = fetch_sic("1234", user_agent="Acme Research ops@acme.example")
    assert mock_get.call_args.args[0] == "https://data.sec.gov/submissions/CIK" + "1234".zfill(10) + ".json"
    assert info is not None and info.sector == "35"


@patch("market_data_ingest.sec_company_map._get_json")
def test_fetch_sic_returns_none_when_the_company_has_no_document(mock_get):
    mock_get.side_effect = LookupError("404")  # the shared GET helper raises LookupError on 404
    assert fetch_sic("1234", user_agent="Acme Research ops@acme.example") is None


@pytest.mark.parametrize("cik", ["abc", "1" * 11, ""])
def test_fetch_sic_refuses_a_cik_that_is_not_up_to_ten_digits(cik):
    with pytest.raises(ValueError):
        fetch_sic(cik, user_agent="Acme Research ops@acme.example")


def test_the_fetchers_refuse_a_blank_user_agent_before_any_request(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with patch("market_data_ingest.edgar_fundamentals.get_json_object") as network:
        with pytest.raises(ValueError):
            fetch_company_tickers(user_agent="  ")
        with pytest.raises(ValueError):
            fetch_sic("1234", user_agent=None)
    network.assert_not_called()
