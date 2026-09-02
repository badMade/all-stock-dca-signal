"""Tests for the not-yet-written market_data_ingest/dolthub_options.py,
written first per Section 3.1 (TDD-First). RED right now -- do not
implement until these are reviewed and approved.

Fixture below is a real row pulled live from DoltHub's public SQL API
(https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master)
on 2026-08-22, querying AAPL options dated 2024-01-05. Not fabricated.
Two things the live pull surfaced that a naive implementation would get
wrong, both covered below:

1. Every numeric column (strike, bid, ask, vol, delta, gamma, theta,
   vega, rho) comes back as a JSON STRING ("125.00"), not a JSON number,
   even though the API's own schema block reports them as decimal types.
   Code that trusts the schema type over the actual payload type will
   crash or silently store strings where floats are expected.
2. call_put values are "Call" / "Put" (capitalized), not lowercase --
   filtering on the wrong case silently returns nothing.

vol is this table's implied volatility column, as decimal (0.6993 =
69.93% IV), not trading volume -- confirmed against a real, plausible
deep-ITM AAPL call quote in the fixture below.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from market_data_ingest.dolthub_options import (
    OptionChainRow,
    _build_query,
    fetch_option_chain,
)

# Real response envelope and one real row, pulled live 2026-08-22.
AAPL_2024_01_05_RESPONSE = {
    "query_execution_status": "Success",
    "query_execution_message": "",
    "repository_owner": "post-no-preference",
    "repository_name": "options",
    "schema": [
        {"columnName": "date", "columnType": "date"},
        {"columnName": "act_symbol", "columnType": "varchar(64)"},
        {"columnName": "expiration", "columnType": "date"},
        {"columnName": "strike", "columnType": "decimal(7,2)"},
        {"columnName": "call_put", "columnType": "varchar(64)"},
        {"columnName": "bid", "columnType": "decimal(7,2)"},
        {"columnName": "ask", "columnType": "decimal(7,2)"},
        {"columnName": "vol", "columnType": "decimal(5,4)"},
        {"columnName": "delta", "columnType": "decimal(5,4)"},
        {"columnName": "gamma", "columnType": "decimal(5,4)"},
        {"columnName": "theta", "columnType": "decimal(5,4)"},
        {"columnName": "vega", "columnType": "decimal(5,4)"},
        {"columnName": "rho", "columnType": "decimal(5,4)"},
    ],
    "rows": [
        {
            "date": "2024-01-05", "act_symbol": "AAPL",
            "expiration": "2024-01-19", "strike": "125.00",
            "call_put": "Call", "bid": "55.65", "ask": "56.95",
            "vol": "0.6993", "delta": "0.9974", "gamma": "0.0003",
            "theta": "-0.0255", "vega": "0.0028", "rho": "0.0476",
        },
    ],
}


@patch("market_data_ingest.dolthub_options._run_query")
def test_numeric_fields_are_coerced_to_float_not_left_as_string(mock_query):
    mock_query.return_value = AAPL_2024_01_05_RESPONSE
    rows = fetch_option_chain(ticker="AAPL", on_date=date(2024, 1, 5))
    row = rows[0]
    assert isinstance(row.strike, float)
    assert isinstance(row.implied_vol, float)
    assert row.strike == 125.00
    assert row.implied_vol == pytest.approx(0.6993)


@patch("market_data_ingest.dolthub_options._run_query")
def test_call_put_matches_exact_api_casing(mock_query):
    mock_query.return_value = AAPL_2024_01_05_RESPONSE
    rows = fetch_option_chain(ticker="AAPL", on_date=date(2024, 1, 5))
    assert rows[0].call_put == "Call"


@patch("market_data_ingest.dolthub_options._run_query")
def test_parses_dates_not_left_as_string(mock_query):
    mock_query.return_value = AAPL_2024_01_05_RESPONSE
    rows = fetch_option_chain(ticker="AAPL", on_date=date(2024, 1, 5))
    assert rows[0].date == date(2024, 1, 5)
    assert rows[0].expiration == date(2024, 1, 19)


@patch("market_data_ingest.dolthub_options._run_query")
def test_ticker_not_in_universe_returns_empty_not_raise(mock_query):
    """Not all ~2,098 symbols DoltHub covers include every ticker in this
    repo's universe -- an absent ticker is a normal, expected case, not
    an error."""
    mock_query.return_value = {
        "query_execution_status": "Success",
        "schema": AAPL_2024_01_05_RESPONSE["schema"],
        "rows": [],
    }
    rows = fetch_option_chain(ticker="ZZZZNOTREAL", on_date=date(2024, 1, 5))
    assert rows == []


def test_query_string_escapes_ticker():
    """Ticker symbols land inside a raw SQL string in the query parameter
    -- a single quote in the input must come out doubled (the standard
    SQL escape), never passed through raw, even though the API is
    read-only and the practical injection risk is low here. Defence in
    depth, not optional."""
    query = _build_query(ticker="A'B", on_date=date(2024, 1, 5))
    assert "act_symbol='A''B'" in query
