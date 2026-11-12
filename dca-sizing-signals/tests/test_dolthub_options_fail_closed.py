"""Fail-closed tests for market_data_ingest/dolthub_options.py, beyond the TDD spec in
tests/test_dolthub_options.py (left unchanged). Added after the 2026-11-12 implementation:
a failed query, a malformed envelope or one malformed row must fail the whole chain, never
return a silently partial one.
"""

from __future__ import annotations

import copy
import urllib.parse
from datetime import date
from unittest.mock import patch

import pytest
from market_data_ingest.dolthub_options import API_URL, _build_query, _run_query, fetch_option_chain
from tests.test_dolthub_options import AAPL_2024_01_05_RESPONSE

RUN_QUERY = "market_data_ingest.dolthub_options._run_query"
ON = date(2024, 1, 5)


def _response_with_row(**overrides):
    response = copy.deepcopy(AAPL_2024_01_05_RESPONSE)
    response["rows"][0].update(overrides)
    return response


def _fetch(response):
    with patch(RUN_QUERY, return_value=response):
        return fetch_option_chain(ticker="AAPL", on_date=ON)


# ---------------------------------------------------------------- envelope


@pytest.mark.parametrize("status", ["Error", "success", "", None])
def test_query_status_other_than_success_raises(status):
    response = copy.deepcopy(AAPL_2024_01_05_RESPONSE)
    response["query_execution_status"] = status
    with pytest.raises(ValueError, match="query did not succeed"):
        _fetch(response)


def test_missing_query_status_raises():
    response = copy.deepcopy(AAPL_2024_01_05_RESPONSE)
    del response["query_execution_status"]
    with pytest.raises(ValueError, match="query did not succeed"):
        _fetch(response)


def test_missing_rows_key_on_success_is_an_empty_chain():
    assert _fetch({"query_execution_status": "Success"}) == []


@pytest.mark.parametrize("rows", [{}, "rows", 1])
def test_rows_that_are_not_a_list_raise(rows):
    with pytest.raises(ValueError, match="rows is not a list"):
        _fetch({"query_execution_status": "Success", "rows": rows})


@pytest.mark.parametrize("row", [None, "row", ["2024-01-05"]])
def test_a_row_that_is_not_an_object_raises(row):
    with pytest.raises(ValueError, match="row is not an object"):
        _fetch({"query_execution_status": "Success", "rows": [row]})


def test_one_bad_row_fails_the_whole_chain():
    response = copy.deepcopy(AAPL_2024_01_05_RESPONSE)
    bad = dict(response["rows"][0], strike="n/a")
    response["rows"].append(bad)
    with pytest.raises(ValueError):
        _fetch(response)


# ---------------------------------------------------------------- row fields


@pytest.mark.parametrize("call_put", ["call", "CALL", "put", "C", "", None])
def test_call_put_outside_exact_casing_raises(call_put):
    with pytest.raises(ValueError, match="call_put"):
        _fetch(_response_with_row(call_put=call_put))


def test_put_is_accepted():
    assert _fetch(_response_with_row(call_put="Put"))[0].call_put == "Put"


@pytest.mark.parametrize("symbol", ["", None, 7])
def test_missing_or_non_string_symbol_raises(symbol):
    with pytest.raises(ValueError, match="act_symbol"):
        _fetch(_response_with_row(act_symbol=symbol))


@pytest.mark.parametrize("column", ["strike", "bid", "ask", "vol", "delta", "gamma", "theta", "vega", "rho"])
def test_every_numeric_column_rejects_a_non_numeric_string(column):
    with pytest.raises(ValueError):
        _fetch(_response_with_row(**{column: "n/a"}))


@pytest.mark.parametrize("value", ["NaN", "nan", "inf", "-Infinity"])
def test_non_finite_numeric_strings_raise(value):
    with pytest.raises(ValueError, match="not finite"):
        _fetch(_response_with_row(strike=value))


@pytest.mark.parametrize("value", [None, True, False, [], {}])
def test_numeric_column_rejects_null_bool_and_containers(value):
    with pytest.raises(ValueError, match="not numeric"):
        _fetch(_response_with_row(bid=value))


def test_a_json_number_is_still_accepted():
    # The live API sends strings, but a future schema fix sending real numbers must not break.
    row = _fetch(_response_with_row(strike=125, vol=0.5))[0]
    assert row.strike == 125.0 and isinstance(row.strike, float)
    assert row.implied_vol == 0.5


def test_missing_numeric_column_raises():
    response = copy.deepcopy(AAPL_2024_01_05_RESPONSE)
    del response["rows"][0]["gamma"]
    with pytest.raises(ValueError, match="gamma"):
        _fetch(response)


@pytest.mark.parametrize("column", ["date", "expiration"])
@pytest.mark.parametrize("value", [None, 20240105, "01/05/2024", "2024-13-01"])
def test_bad_dates_raise(column, value):
    with pytest.raises(ValueError):
        _fetch(_response_with_row(**{column: value}))


# ---------------------------------------------------------------- query and transport


def test_query_selects_the_one_date_and_is_ordered():
    query = _build_query(ticker="AAPL", on_date=ON)
    assert "act_symbol='AAPL'" in query
    assert "date='2024-01-05'" in query
    assert query.rstrip().endswith("ORDER BY expiration, strike, call_put")


def test_run_query_url_encodes_the_sql_into_q():
    query = _build_query(ticker="A'B", on_date=ON)
    with patch("market_data_ingest.dolthub_options.get_json_object", return_value={}) as get:
        _run_query(query)
    url = get.call_args.args[0]
    base, _, qs = url.partition("?")
    assert base == API_URL
    assert urllib.parse.parse_qs(qs) == {"q": [query]}
    assert " " not in url and "'" not in url


def test_transport_failure_propagates_rather_than_returning_empty():
    with patch(RUN_QUERY, side_effect=OSError("connection refused")):
        with pytest.raises(OSError):
            fetch_option_chain(ticker="AAPL", on_date=ON)


def test_a_404_from_dolthub_is_not_treated_as_an_empty_chain():
    # Unlike EDGAR, an absent ticker comes back as Success with no rows, so a 404 means the
    # endpoint itself moved: that must surface, not read as "no options for this ticker".
    with patch(RUN_QUERY, side_effect=LookupError("404")):
        with pytest.raises(LookupError):
            fetch_option_chain(ticker="AAPL", on_date=ON)
