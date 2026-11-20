"""Tests for the point-in-time selection helpers in market_data_ingest/edgar_fundamentals.py.

Written BEFORE the implementation (TDD), step 1 of docs/fundamentals_feed_scope.md. They define:

* ``first_filed_facts(facts, as_of)`` -- one fact per reporting period: the value as FIRST reported, and
  only if it was public on ``as_of`` (``filed <= as_of``). A later filing that repeats the same period
  (the prior-year comparative column of the next 10-K, or a restatement) is never substituted: using it
  would let a value nobody knew yet leak into a ranking (lookahead bias).
* ``select_annual_facts(facts)`` -- keep only duration facts spanning a fiscal year (the sibling of the
  existing ``select_single_quarter_facts``).
* ``latest_fact(facts)`` -- the fact whose period ends last.

All facts here are SYNTHETIC; none claims to be a real filer's number.
"""

from __future__ import annotations

from datetime import date

import pytest
from market_data_ingest.edgar_fundamentals import (
    PointInTimeFact,
    first_filed_facts,
    latest_fact,
    select_annual_facts,
)

AS_OF = date(2026, 6, 30)


def fact(
    *,
    end: date,
    filed: date,
    val: float = 1.0,
    start: date | None = None,
    form: str = "10-K",
    accn: str = "0000000000-00-000001",
) -> PointInTimeFact:
    return PointInTimeFact(
        start=start, end=end, val=val, fy=end.year, fp="FY", form=form, filed=filed, frame=None, accn=accn
    )


FY2024 = dict(start=date(2024, 1, 1), end=date(2024, 12, 31))
FY2025 = dict(start=date(2025, 1, 1), end=date(2025, 12, 31))


# --- first_filed_facts ---------------------------------------------------------------------------------


def test_the_original_value_wins_over_a_later_restatement():
    original = fact(**FY2024, filed=date(2025, 2, 10), val=100, accn="A-1")
    restated = fact(**FY2024, filed=date(2026, 2, 12), val=90, accn="A-2")  # next year's 10-K, comparative column
    assert first_filed_facts([restated, original], as_of=AS_OF) == [original]


def test_a_period_first_filed_after_as_of_is_absent_not_replaced():
    # FY2025 became public on 2026-08-01, after AS_OF. Nothing may stand in for it.
    later = fact(**FY2025, filed=date(2026, 8, 1), val=120, accn="B-1")
    earlier = fact(**FY2024, filed=date(2025, 2, 10), val=100, accn="A-1")
    assert first_filed_facts([later, earlier], as_of=AS_OF) == [earlier]


def test_a_restatement_filed_after_as_of_does_not_hide_the_original():
    original = fact(**FY2024, filed=date(2025, 2, 10), val=100, accn="A-1")
    restated = fact(**FY2024, filed=date(2026, 7, 15), val=90, accn="A-2")  # after AS_OF
    assert first_filed_facts([original, restated], as_of=AS_OF) == [original]


def test_the_filed_date_boundary_is_inclusive():
    on_the_day = fact(**FY2025, filed=AS_OF, val=7)
    assert first_filed_facts([on_the_day], as_of=AS_OF) == [on_the_day]
    one_day_late = fact(**FY2025, filed=date(2026, 7, 1), val=7)
    assert first_filed_facts([one_day_late], as_of=AS_OF) == []


def test_different_periods_are_all_kept_oldest_period_first():
    fy25 = fact(**FY2025, filed=date(2026, 2, 11), val=2, accn="B-1")
    fy24 = fact(**FY2024, filed=date(2025, 2, 10), val=1, accn="A-1")
    assert first_filed_facts([fy25, fy24], as_of=AS_OF) == [fy24, fy25]


def test_an_instant_fact_is_keyed_by_its_end_date_alone():
    # Balance-sheet items have no start. The same instant reported again later is the same period.
    first = fact(end=date(2024, 12, 31), filed=date(2025, 2, 10), val=500)
    again = fact(end=date(2024, 12, 31), filed=date(2026, 2, 12), val=480, accn="A-2")
    assert first_filed_facts([again, first], as_of=AS_OF) == [first]


def test_a_quarter_and_a_year_that_share_an_end_date_are_different_periods():
    # Ordered by (end, start): the year starts earlier, so it comes first.
    quarter = fact(start=date(2024, 10, 1), end=date(2024, 12, 31), filed=date(2025, 2, 10), val=1)
    year = fact(**FY2024, filed=date(2025, 2, 10), val=4, accn="A-2")
    assert first_filed_facts([quarter, year], as_of=AS_OF) == [year, quarter]


def test_an_original_beats_an_amendment_filed_the_same_day():
    original = fact(**FY2024, filed=date(2025, 2, 10), val=100, form="10-K", accn="A-9")
    amendment = fact(**FY2024, filed=date(2025, 2, 10), val=101, form="10-K/A", accn="A-1")
    assert first_filed_facts([amendment, original], as_of=AS_OF) == [original]


def test_a_remaining_tie_is_broken_by_the_lowest_accession_number():
    a = fact(**FY2024, filed=date(2025, 2, 10), val=1, accn="0000000000-25-000002")
    b = fact(**FY2024, filed=date(2025, 2, 10), val=2, accn="0000000000-25-000001")
    assert first_filed_facts([a, b], as_of=AS_OF) == [b]
    assert first_filed_facts([b, a], as_of=AS_OF) == [b]  # input order never matters


def test_first_filed_facts_does_not_mutate_its_input_and_handles_empty():
    facts = [fact(**FY2025, filed=date(2026, 2, 11)), fact(**FY2024, filed=date(2025, 2, 10), accn="A-1")]
    snapshot = list(facts)
    first_filed_facts(facts, as_of=AS_OF)
    assert facts == snapshot
    assert first_filed_facts([], as_of=AS_OF) == []


# --- select_annual_facts -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start, end",
    [
        (date(2024, 1, 1), date(2024, 12, 31)),  # calendar year, leap (366 days)
        (date(2025, 1, 1), date(2025, 12, 31)),  # calendar year (365 days)
        (date(2024, 9, 29), date(2025, 9, 27)),  # 52-week fiscal year (363 days)
        (date(2023, 10, 1), date(2024, 9, 29)),  # 52/53-week fiscal year, long
    ],
)
def test_a_fiscal_year_window_is_kept(start, end):
    annual = fact(start=start, end=end, filed=date(2025, 11, 1))
    assert select_annual_facts([annual]) == [annual]


def test_quarters_year_to_date_windows_and_instants_are_dropped():
    quarter = fact(start=date(2025, 10, 1), end=date(2025, 12, 31), filed=date(2026, 2, 1))
    nine_months = fact(start=date(2025, 4, 1), end=date(2025, 12, 31), filed=date(2026, 2, 1))
    instant = fact(end=date(2025, 12, 31), filed=date(2026, 2, 1))
    assert select_annual_facts([quarter, nine_months, instant]) == []


# --- latest_fact ---------------------------------------------------------------------------------------------


def test_latest_fact_is_the_one_whose_period_ends_last():
    old = fact(**FY2024, filed=date(2025, 2, 10), accn="A-1")
    new = fact(**FY2025, filed=date(2026, 2, 11), accn="B-1")
    assert latest_fact([old, new]) == new
    assert latest_fact([new, old]) == new


def test_latest_fact_of_nothing_is_none():
    assert latest_fact([]) is None
