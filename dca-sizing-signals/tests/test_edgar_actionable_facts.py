"""Backtest protocol item 3: fundamentals joined on acceptance time, never period end; proxy forms excluded.

Phase A guard, written BEFORE the implementation (docs/backtest-gap-audit.md row 3,
docs/adr-backtest-protocol.md). The tests ran xfail(strict=True) until
``market_data_ingest.edgar_acceptance`` landed; the marker came off in that change.
``test_no_backtest_code_calls_the_frames_api`` was never marked: it already held and must keep holding.

Today ``first_filed_facts(facts, as_of=<date>)`` admits a fact on its ``filed`` DATE
(edgar_fundamentals.py:182-205). The contract below replaces the date with the acceptance TIMESTAMP:

* ``parse_edgar_accepted("2026-07-30 16:30:28")`` returns an aware datetime in America/New_York (EDGAR
  index pages show "Accepted" in Eastern time with no suffix).
* ``actionable_facts(facts, acceptance, *, decision_at, calendar)`` keeps a fact only when:
  - its form is in ``POINT_IN_TIME_FORMS`` (periodic reports and their amendments, never a proxy);
  - its ``accn`` has a known acceptance time (an unknown accession is DROPPED, fail closed: no
    fallback to ``filed``);
  - ``actionable_at(acceptance[accn], calendar) <= decision_at``.
  Among the kept facts the existing first-filed rule still picks one value per period.

Fixtures: the facts are REAL rows from the live 2026-08-22 companyfacts pull of CIK0000320193 that
tests/test_edgar_fundamentals.py already uses (RevenueFromContractWithCustomerExcludingAssessedTax). The
acceptance times are REAL (tests/fixtures/edgar_acceptance_aapl.json, EDGAR index pages, 2026-11-23). The
one exception is the DEF 14A fact's VALUE: the accession, form and acceptance are real, but the number is
a placeholder, labelled where it is built, because the test is about the form, not the value.
"""

from __future__ import annotations

import importlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from market_data_ingest.edgar_fundamentals import PointInTimeFact

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_JSON = REPO_ROOT / "config" / "market_calendar.json"
ACCEPTANCE = json.loads((Path(__file__).parent / "fixtures" / "edgar_acceptance_aapl.json").read_text())
ACCEPTED_TEXT = {f["accession_number"]: f["accepted"] for f in ACCEPTANCE["filings"]}


# REAL: AAPL fiscal Q3 2026 single-quarter revenue, 10-Q accn 0000320193-26-000020, filed 2026-07-31
# (accepted 06:01:02 ET), from the 2026-08-22 companyfacts pull.
Q3_FY26_REVENUE = PointInTimeFact(
    start=date(2026, 3, 29), end=date(2026, 6, 27), val=109417000000, fy=2026, fp="Q3",
    form="10-Q", filed=date(2026, 7, 31), frame="CY2026Q2", accn="0000320193-26-000020",
)
# REAL accession, form and acceptance (DEF 14A accepted 2026-01-08 16:31:36 ET); PLACEHOLDER value, period
# and concept. Proxy statements carry XBRL (pay-versus-performance), so a form filter is needed.
PROXY_FACT = PointInTimeFact(
    start=date(2024, 9, 29), end=date(2025, 9, 27), val=1.0, fy=2025, fp="FY",
    form="DEF 14A", filed=date(2026, 1, 8), frame=None, accn="0001308179-26-000008",
)


def _mod():
    return importlib.import_module("market_data_ingest.edgar_acceptance")


def _calendar():
    return importlib.import_module("market_data_ingest.exchange_calendar").ExchangeCalendar.from_json(CALENDAR_JSON)


def _acceptance() -> dict[str, datetime]:
    return {accn: _mod().parse_edgar_accepted(text) for accn, text in ACCEPTED_TEXT.items()}


def _et(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def test_parse_edgar_accepted_reads_eastern_time():
    got = _mod().parse_edgar_accepted(ACCEPTED_TEXT["0000320193-26-000018"])
    assert got == _et(2026, 7, 30, 16, 30, 28)
    assert got.utcoffset() == _et(2026, 7, 30).utcoffset()  # EDT, UTC-4


@pytest.mark.parametrize("bad", ["", "2026-07-30", "2026-07-30T16:30:28Z", "30/07/2026 16:30:28"])
def test_parse_edgar_accepted_refuses_anything_else(bad):
    with pytest.raises(ValueError):
        _mod().parse_edgar_accepted(bad)


def test_a_10q_is_absent_one_second_before_its_actionable_open_and_present_at_it():
    mod = _mod()
    kept_before = mod.actionable_facts(
        [Q3_FY26_REVENUE], _acceptance(), decision_at=_et(2026, 7, 31, 9, 29, 59), calendar=_calendar()
    )
    kept_at = mod.actionable_facts(
        [Q3_FY26_REVENUE], _acceptance(), decision_at=_et(2026, 7, 31, 9, 30), calendar=_calendar()
    )
    assert kept_before == []
    assert kept_at == [Q3_FY26_REVENUE]


def test_the_date_rule_this_replaces_admits_the_same_fact_before_the_open():
    # Contrast, so the regression is visible: the current date-based rule hands the 10-Q to a decision
    # at 06:00 ET on 2026-07-31, a minute before EDGAR accepted it (06:01:02) and 3.5 h before any trade
    # could act on it. The new rule must not.
    from market_data_ingest.edgar_fundamentals import first_filed_facts

    assert first_filed_facts([Q3_FY26_REVENUE], as_of=date(2026, 7, 31)) == [Q3_FY26_REVENUE]
    assert _mod().actionable_facts(
        [Q3_FY26_REVENUE], _acceptance(), decision_at=_et(2026, 7, 31, 6, 0), calendar=_calendar()
    ) == []


def test_an_unknown_accession_is_dropped_not_admitted_on_its_filed_date():
    kept = _mod().actionable_facts(
        [Q3_FY26_REVENUE], {}, decision_at=_et(2026, 9, 1, 12, 0), calendar=_calendar()
    )
    assert kept == []


def test_proxy_forms_are_excluded_even_when_long_actionable():
    kept = _mod().actionable_facts(
        [PROXY_FACT, Q3_FY26_REVENUE], _acceptance(), decision_at=_et(2026, 9, 1, 12, 0), calendar=_calendar()
    )
    assert kept == [Q3_FY26_REVENUE]


def test_point_in_time_forms_allow_periodic_reports_and_never_proxies():
    forms = _mod().POINT_IN_TIME_FORMS
    assert {"10-K", "10-Q", "10-K/A", "10-Q/A"} <= forms
    assert not {"DEF 14A", "DEFA14A", "PRE 14A", "DEFM14A"} & forms


def test_an_acceptance_in_an_uncovered_year_fails_closed():
    # REAL: the FY2025 10-K accn 0000320193-25-000079 was accepted 2025-10-31, a year the calendar file
    # does not cover. The fact must not be admitted on a guessed open.
    fy25 = PointInTimeFact(
        start=date(2024, 9, 29), end=date(2025, 9, 27), val=1.0, fy=2025, fp="FY",  # PLACEHOLDER value
        form="10-K", filed=date(2025, 10, 31), frame=None, accn="0000320193-25-000079",
    )
    cal_mod = importlib.import_module("market_data_ingest.exchange_calendar")
    with pytest.raises(cal_mod.CalendarCoverageError):
        _mod().actionable_facts([fy25], _acceptance(), decision_at=_et(2026, 9, 1, 12, 0), calendar=_calendar())


def test_no_backtest_code_calls_the_frames_api():
    # The frames API returns one value per period across filers, stamped with the latest filing, so it
    # cannot be point in time. It already holds; this keeps it holding as the harness is written.
    package = Path(__file__).resolve().parents[1]
    offenders = [
        p.relative_to(package).as_posix()
        for sub in ("market_data_ingest", "backtest")
        for p in sorted((package / sub).rglob("*.py"))
        if "api/xbrl/frames" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
