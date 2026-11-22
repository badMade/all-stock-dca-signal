"""Tests for the not-yet-written exchange-map functions in market_data_ingest/sec_company_map.py (feed step 2b).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

The ranking's pool filter needs ``nms_listed`` per symbol. The SEC publishes ``company_tickers_exchange.json``:
a ``fields`` list naming the columns and a ``data`` list of rows, one per listed ticker, with the exchange the
SEC records for it. These functions turn that file into ``{ticker: exchange}`` for ``universe_inputs`` to read.

HONESTY NOTE. The layout used by the fixtures (``{"fields": ["cik", "name", "ticker", "exchange"], "data":
[[<int>, <str>, <str>, <str>], ...]}``) is the author's recollection of the SEC file and was NOT checked against a
live pull (the SEC refuses a request without a contact-identifying User-Agent). Columns are therefore found BY
NAME, so a reordering is harmless, but one live pull on the operator's Mac must confirm the names before the
fetcher is relied on. Whether ETFs and recent listings appear in the file is also unconfirmed.

No run of 6 or more digits appears below (the account-number guard in tests/test_config_invariants.py).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from market_data_ingest.sec_company_map import fetch_company_exchanges, parse_company_exchanges

FIELDS = ["cik", "name", "ticker", "exchange"]


def doc(*rows, fields=None):
    return {"fields": fields or FIELDS, "data": [list(r) for r in rows]}


def test_rows_map_each_ticker_to_the_exchange_the_sec_records():
    parsed = parse_company_exchanges(doc((1, "Alpha Corp", "AAA", "Nasdaq"), (2, "Bravo Inc", "BBB", "NYSE")))
    assert parsed == {"AAA": "Nasdaq", "BBB": "NYSE"}


def test_columns_are_found_by_name_so_a_reordering_is_harmless():
    reordered = doc(("Nasdaq", "AAA", 1, "Alpha Corp"), fields=["exchange", "ticker", "cik", "name"])
    assert parse_company_exchanges(reordered) == {"AAA": "Nasdaq"}


def test_tickers_are_uppercased_and_a_share_class_dash_becomes_a_dot():
    assert parse_company_exchanges(doc((3, "Charlie", "brk-b", "NYSE"))) == {"BRK.B": "NYSE"}


def test_the_exchange_is_kept_as_written_but_trimmed():
    assert parse_company_exchanges(doc((1, "Alpha", "AAA", "  OTC  "))) == {"AAA": "OTC"}


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_missing_exchange_is_none_not_an_empty_string(blank):
    assert parse_company_exchanges(doc((1, "Alpha", "AAA", blank))) == {"AAA": None}


def test_the_same_ticker_repeated_with_the_same_exchange_is_fine():
    assert parse_company_exchanges(doc((1, "A", "AAA", "Nasdaq"), (1, "A", "AAA", "Nasdaq"))) == {"AAA": "Nasdaq"}


def test_the_same_ticker_on_two_different_exchanges_is_refused_and_named():
    with pytest.raises(ValueError, match="AAA"):
        parse_company_exchanges(doc((1, "A", "AAA", "Nasdaq"), (2, "B", "AAA", "NYSE")))


def test_an_empty_data_list_is_an_empty_map():
    assert parse_company_exchanges(doc()) == {}


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(["not", "an", "object"], id="not_an_object"),
        pytest.param({"data": []}, id="fields_missing"),
        pytest.param({"fields": "cik,name", "data": []}, id="fields_not_a_list"),
        pytest.param({"fields": ["cik", "name", "exchange"], "data": []}, id="ticker_column_missing"),
        pytest.param({"fields": ["cik", "name", "ticker"], "data": []}, id="exchange_column_missing"),
        pytest.param({"fields": FIELDS}, id="data_missing"),
        pytest.param({"fields": FIELDS, "data": "rows"}, id="data_not_a_list"),
        pytest.param({"fields": FIELDS, "data": ["not a row"]}, id="row_not_a_list"),
        pytest.param({"fields": FIELDS, "data": [[1, "A", "AAA"]]}, id="row_too_short"),
        pytest.param({"fields": FIELDS, "data": [[1, "A", 7, "Nasdaq"]]}, id="ticker_not_a_string"),
        pytest.param({"fields": FIELDS, "data": [[1, "A", "", "Nasdaq"]]}, id="ticker_blank"),
        pytest.param({"fields": FIELDS, "data": [[1, "A", "AAA", 5]]}, id="exchange_not_a_string"),
    ],
)
def test_a_malformed_document_is_refused_not_skipped(document):
    with pytest.raises(ValueError):
        parse_company_exchanges(document)


@patch("market_data_ingest.sec_company_map._get_json")
def test_fetch_reads_the_sec_exchange_file_and_parses_it(mock_get):
    mock_get.return_value = doc((1, "Alpha", "AAA", "Nasdaq"))
    assert fetch_company_exchanges(user_agent="Acme Research ops@acme.example") == {"AAA": "Nasdaq"}
    assert mock_get.call_args.args[0] == "https://www.sec.gov/files/company_tickers_exchange.json"


@patch("market_data_ingest.sec_company_map._get_json")
def test_a_404_is_not_swallowed(mock_get):
    mock_get.side_effect = LookupError("404")  # the file itself missing is an error, not "no exchanges"
    with pytest.raises(LookupError):
        fetch_company_exchanges(user_agent="Acme Research ops@acme.example")


def test_the_fetcher_refuses_a_blank_user_agent_before_any_request(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with patch("market_data_ingest.edgar_fundamentals.get_json_object") as network:
        with pytest.raises(ValueError):
            fetch_company_exchanges(user_agent="  ")
    network.assert_not_called()
