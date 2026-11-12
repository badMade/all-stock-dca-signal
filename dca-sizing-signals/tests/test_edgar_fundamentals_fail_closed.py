"""Fail-closed tests for market_data_ingest/edgar_fundamentals.py, beyond the TDD spec in
tests/test_edgar_fundamentals.py (left unchanged). Added after the 2026-11-12
implementation: only a 404 or an untagged concept may degrade to []; a malformed document
or entry, a bad CIK or a missing User-Agent must fail closed.
"""

from __future__ import annotations

import copy
from datetime import date
from unittest.mock import patch

import pytest
from market_data_ingest.edgar_fundamentals import (
    COMPANYFACTS_URL,
    USER_AGENT_ENV,
    PointInTimeFact,
    _get_json,
    build_headers,
    fetch_point_in_time_facts,
    select_single_quarter_facts,
)
from tests.test_edgar_fundamentals import AAPL_REVENUE_RESPONSE

GET_JSON = "market_data_ingest.edgar_fundamentals._get_json"
CONCEPT = "RevenueFromContractWithCustomerExcludingAssessedTax"
# Built rather than written out: 8+ digit literals trip the repo-wide account-number scan
# in tests/test_config_invariants.py.
ELEVEN_DIGIT_CIK = "1" * 11
INT_DATE = int(date(2025, 6, 28).strftime("%Y%m%d"))


def _entries(document):
    return document["facts"]["us-gaap"][CONCEPT]["units"]["USD"]


def _document_with_entry(**overrides):
    document = copy.deepcopy(AAPL_REVENUE_RESPONSE)
    _entries(document)[0].update(overrides)
    return document


def _fetch(document, **kwargs):
    with patch(GET_JSON, return_value=document):
        return fetch_point_in_time_facts(cik="320193", concept=CONCEPT, **kwargs)


# ---------------------------------------------------------------- CIK and URL


def test_short_cik_is_zero_padded_into_the_url():
    with patch(GET_JSON, return_value=AAPL_REVENUE_RESPONSE) as get:
        fetch_point_in_time_facts(cik=" 320193 ", concept=CONCEPT)
    assert get.call_args.args[0] == COMPANYFACTS_URL.format(cik="0000320193")


@pytest.mark.parametrize("cik", ["", "CIK320193", "32019a", "-320193", ELEVEN_DIGIT_CIK, "3201 93"])
def test_bad_cik_raises_before_any_request(cik):
    with patch(GET_JSON) as get:
        with pytest.raises(ValueError, match="CIK"):
            fetch_point_in_time_facts(cik=cik, concept=CONCEPT)
    get.assert_not_called()


# ---------------------------------------------------------------- User-Agent


def test_build_headers_rejects_whitespace_only_and_strips():
    with pytest.raises(ValueError):
        build_headers(user_agent="   ")
    headers = build_headers(user_agent="  Acme ops@acme.example \n")
    assert headers["User-Agent"] == "Acme ops@acme.example"
    assert headers["Accept"] == "application/json"


def test_missing_user_agent_fails_before_the_network(monkeypatch):
    monkeypatch.delenv(USER_AGENT_ENV, raising=False)
    with patch("market_data_ingest.edgar_fundamentals.get_json_object") as get:
        with pytest.raises(ValueError, match=USER_AGENT_ENV):
            fetch_point_in_time_facts(cik="320193", concept=CONCEPT)
    get.assert_not_called()


def test_user_agent_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv(USER_AGENT_ENV, "Env Research env@acme.example")
    with patch("market_data_ingest.edgar_fundamentals.get_json_object", return_value={}) as get:
        _get_json("https://data.sec.gov/x", user_agent=None)
    assert get.call_args.kwargs["headers"]["User-Agent"] == "Env Research env@acme.example"


def test_explicit_user_agent_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(USER_AGENT_ENV, "Env Research env@acme.example")
    with patch("market_data_ingest.edgar_fundamentals.get_json_object", return_value={}) as get:
        _get_json("https://data.sec.gov/x", user_agent="Arg Research arg@acme.example")
    assert get.call_args.kwargs["headers"]["User-Agent"] == "Arg Research arg@acme.example"


def test_explicit_blank_user_agent_does_not_fall_back_to_the_environment(monkeypatch):
    # A caller that passes "" made a mistake; quietly using the env value would hide it.
    monkeypatch.setenv(USER_AGENT_ENV, "Env Research env@acme.example")
    with pytest.raises(ValueError):
        _get_json("https://data.sec.gov/x", user_agent="")


# ---------------------------------------------------------------- transport


@pytest.mark.parametrize("exc", [OSError("timed out"), ValueError("malformed body")])
def test_failures_other_than_404_propagate(exc):
    with patch(GET_JSON, side_effect=exc):
        with pytest.raises(type(exc)):
            fetch_point_in_time_facts(cik="320193", concept=CONCEPT)


# ---------------------------------------------------------------- document shape


@pytest.mark.parametrize("document", [{}, {"facts": None}, {"facts": []}, {"facts": "x"}])
def test_document_without_a_facts_object_raises(document):
    with pytest.raises(ValueError, match="no facts object"):
        _fetch(document)


def test_missing_taxonomy_is_empty():
    assert _fetch({"facts": {"dei": {}}}) == []


def test_concept_present_but_unit_missing_is_empty():
    assert _fetch(AAPL_REVENUE_RESPONSE, unit="EUR") == []


def test_concept_without_units_is_empty():
    assert _fetch({"facts": {"us-gaap": {CONCEPT: {"label": "x"}}}}) == []


@pytest.mark.parametrize("entries", [{}, "x", 1])
def test_unit_entries_that_are_not_a_list_raise(entries):
    with pytest.raises(ValueError, match="not a list"):
        _fetch({"facts": {"us-gaap": {CONCEPT: {"units": {"USD": entries}}}}})


@pytest.mark.parametrize("entry", [None, "x", [1]])
def test_an_entry_that_is_not_an_object_raises(entry):
    with pytest.raises(ValueError, match="not an object"):
        _fetch({"facts": {"us-gaap": {CONCEPT: {"units": {"USD": [entry]}}}}})


def test_one_bad_entry_fails_the_whole_concept():
    document = copy.deepcopy(AAPL_REVENUE_RESPONSE)
    _entries(document).append(dict(_entries(document)[0], val="lots"))
    with pytest.raises(ValueError):
        _fetch(document)


# ---------------------------------------------------------------- entry fields


@pytest.mark.parametrize("val", ["94036000000", None, True, False, [1]])
def test_val_must_be_a_real_number(val):
    with pytest.raises(ValueError, match="'val'"):
        _fetch(_document_with_entry(val=val))


def test_float_val_is_kept():
    assert _fetch(_document_with_entry(val=1.25))[0].val == 1.25


@pytest.mark.parametrize("fy", ["2026", 2026.0, True])
def test_fy_must_be_an_integer(fy):
    with pytest.raises(ValueError, match="'fy'"):
        _fetch(_document_with_entry(fy=fy))


@pytest.mark.parametrize("key", ["end", "filed", "form", "accn"])
def test_required_fields_raise_when_missing(key):
    document = copy.deepcopy(AAPL_REVENUE_RESPONSE)
    del _entries(document)[0][key]
    with pytest.raises(ValueError, match=key):
        _fetch(document)


@pytest.mark.parametrize("key", ["form", "accn"])
def test_required_strings_raise_when_empty(key):
    with pytest.raises(ValueError, match=key):
        _fetch(_document_with_entry(**{key: ""}))


@pytest.mark.parametrize("key", ["start", "end", "filed"])
@pytest.mark.parametrize("value", [INT_DATE, "06/28/2025", "2025-02-30"])
def test_bad_dates_raise(key, value):
    with pytest.raises(ValueError):
        _fetch(_document_with_entry(**{key: value}))


@pytest.mark.parametrize("key", ["fp", "frame"])
def test_optional_strings_reject_non_strings(key):
    with pytest.raises(ValueError, match=key):
        _fetch(_document_with_entry(**{key: 3}))


def test_optional_fields_may_be_absent():
    document = copy.deepcopy(AAPL_REVENUE_RESPONSE)
    entry = _entries(document)[1]
    for key in ("start", "fy", "fp", "frame"):
        del entry[key]
    fact = _fetch(document)[1]
    assert (fact.start, fact.fy, fact.fp, fact.frame) == (None, None, None, None)


# ---------------------------------------------------------------- single-quarter selection


def _fact(start, end):
    return PointInTimeFact(
        start=start, end=end, val=1, fy=2026, fp="Q3", form="10-Q",
        filed=date(2026, 7, 31), frame=None, accn="0000320193-26-000020",
    )


def test_instant_facts_are_never_single_quarter():
    assert select_single_quarter_facts([_fact(None, date(2026, 6, 27))]) == []


@pytest.mark.parametrize(
    ("days", "kept"),
    [(79, False), (80, True), (91, True), (98, True), (100, True), (101, False), (182, False), (364, False)],
)
def test_span_band_edges(days, kept):
    end = date(2026, 6, 27)
    start = date.fromordinal(end.toordinal() - days)
    assert (select_single_quarter_facts([_fact(start, end)]) != []) is kept


def test_selection_ignores_frame():
    # EDGAR omits frame on some genuine quarters; selection is by span alone.
    quarter = _fact(date(2026, 3, 29), date(2026, 6, 27))
    assert quarter.frame is None
    assert select_single_quarter_facts([quarter]) == [quarter]
