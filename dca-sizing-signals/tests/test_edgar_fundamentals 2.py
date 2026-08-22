"""Tests for the not-yet-written market_data_ingest/edgar_fundamentals.py,
written first per Section 3.1 (TDD-First). These are RED right now --
edgar_fundamentals.py does not exist yet. Do not implement it until these
are reviewed and approved.

Fixtures below are trimmed from a live pull of
https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json on
2026-08-22, not fabricated. Two things the live pull surfaced that a
naive implementation would get wrong, both covered below:

1. Field names and types are exactly: start, end, val (int, dollars),
   fy, fp, form, filed, frame (nullable string), accn -- confirmed live,
   not assumed from documentation.
2. A single filing reports BOTH a single-quarter window (start to end
   spans ~3 months, usually carries a frame like "CY2025Q2") and a
   cumulative year-to-date window (start to end spans ~9 months, frame is
   null) for the same concept, same filed date, same accn. Picking the
   wrong one silently corrupts any quarter-over-quarter growth
   calculation -- test_selects_single_quarter_not_ytd exists specifically
   because this would be easy to get wrong without having looked at real
   data.

Recommended implementation note (not enforced by these tests): use
stdlib urllib.request for _get_json rather than adding a requests
dependency -- both EDGAR and DoltHub are plain GET/JSON, no new
third-party HTTP client is needed.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from market_data_ingest.edgar_fundamentals import (
    PointInTimeFact,
    build_headers,
    fetch_point_in_time_facts,
    select_single_quarter_facts,
)

# Trimmed from the live 2026-08-22 pull of CIK0000320193's
# RevenueFromContractWithCustomerExcludingAssessedTax concept. Real
# values, not invented.
AAPL_REVENUE_RESPONSE = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "label": "Revenue from Contract with Customer, Excluding Assessed Tax",
                "units": {
                    "USD": [
                        {
                            "start": "2024-09-29", "end": "2025-06-28",
                            "val": 313695000000, "fy": 2026, "fp": "Q3",
                            "form": "10-Q", "filed": "2026-07-31",
                            "frame": None, "accn": "0000320193-26-000020",
                        },
                        {
                            "start": "2025-03-30", "end": "2025-06-28",
                            "val": 94036000000, "fy": 2026, "fp": "Q3",
                            "form": "10-Q", "filed": "2026-07-31",
                            "frame": "CY2025Q2", "accn": "0000320193-26-000020",
                        },
                        {
                            "start": "2025-09-28", "end": "2026-06-27",
                            "val": 364357000000, "fy": 2026, "fp": "Q3",
                            "form": "10-Q", "filed": "2026-07-31",
                            "frame": None, "accn": "0000320193-26-000020",
                        },
                        {
                            "start": "2026-03-29", "end": "2026-06-27",
                            "val": 109417000000, "fy": 2026, "fp": "Q3",
                            "form": "10-Q", "filed": "2026-07-31",
                            "frame": "CY2026Q2", "accn": "0000320193-26-000020",
                        },
                    ]
                },
            }
        }
    },
}


@patch("market_data_ingest.edgar_fundamentals._get_json")
def test_fetch_returns_one_fact_per_entry(mock_get):
    mock_get.return_value = AAPL_REVENUE_RESPONSE
    facts = fetch_point_in_time_facts(
        cik="0000320193",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    assert len(facts) == 4
    assert all(isinstance(f, PointInTimeFact) for f in facts)


@patch("market_data_ingest.edgar_fundamentals._get_json")
def test_filed_and_end_are_kept_distinct(mock_get):
    """The entire point-in-time discipline depends on filed != end being
    preserved through parsing -- collapsing them to one date silently
    reintroduces lookahead bias."""
    mock_get.return_value = AAPL_REVENUE_RESPONSE
    facts = fetch_point_in_time_facts(
        cik="0000320193",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    fact = facts[0]
    assert fact.end == date(2025, 6, 28)
    assert fact.filed == date(2026, 7, 31)
    assert fact.end != fact.filed


@patch("market_data_ingest.edgar_fundamentals._get_json")
def test_selects_single_quarter_not_ytd(mock_get):
    """Given the same filing carries both a 3-month and a 9-month window
    for the same concept, select_single_quarter_facts must keep only the
    ~3-month entries. This directly reproduces what the live 2026-08-22
    pull showed for AAPL's fiscal Q3 filing."""
    mock_get.return_value = AAPL_REVENUE_RESPONSE
    facts = fetch_point_in_time_facts(
        cik="0000320193",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    quarterly = select_single_quarter_facts(facts)
    assert len(quarterly) == 2
    for fact in quarterly:
        span_days = (fact.end - fact.start).days
        assert 80 <= span_days <= 100  # roughly one quarter, not roughly a year
    assert {f.val for f in quarterly} == {94036000000, 109417000000}


@patch("market_data_ingest.edgar_fundamentals._get_json")
def test_unknown_concept_returns_empty_not_raise(mock_get):
    """A filer that simply doesn't tag a requested concept (e.g. an older
    tag a newer filer never used) is a normal, expected case -- it must
    degrade to an empty list, matching fundamentals_data_available =
    False on the MarketDataSnapshot side, never raise."""
    mock_get.return_value = AAPL_REVENUE_RESPONSE
    facts = fetch_point_in_time_facts(
        cik="0000320193",
        concept="SomeConceptThisFilerNeverUsed",
    )
    assert facts == []


@patch("market_data_ingest.edgar_fundamentals._get_json")
def test_404_returns_empty_not_raise(mock_get):
    """A CIK with no filings on record must degrade gracefully -- ticker
    universe membership is ticker-advisor/'s job, not this module's, so
    this must never crash the caller over it."""
    mock_get.side_effect = LookupError("404")
    facts = fetch_point_in_time_facts(cik="0000000000", concept="Revenues")
    assert facts == []


def test_requires_explicit_user_agent():
    """SEC blocks or rate-limits requests without a descriptive User-Agent
    identifying the requester (name + contact) -- this must fail fast and
    explicitly rather than send a blank or generic header and get
    silently throttled. The placeholder used during verification
    ("MarketDataIngestVerification research@example.com") must NOT ship
    as a default; this test forces a real value to be configured before
    this module is usable outside development."""
    with pytest.raises(ValueError):
        build_headers(user_agent=None)
    with pytest.raises(ValueError):
        build_headers(user_agent="")

    headers = build_headers(user_agent="Acme Research ops@acme.example")
    assert "Acme Research ops@acme.example" in headers["User-Agent"]
