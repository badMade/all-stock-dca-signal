"""Backtest protocol item 4: an ``actionable_at`` on one ET exchange calendar (next session after acceptance).

Phase A guard, written BEFORE the implementation (docs/backtest-gap-audit.md row 4,
docs/adr-backtest-protocol.md). Every test here is xfail(strict=True) until
``market_data_ingest.exchange_calendar`` exists. Once it does, a passing test turns into a strict XPASS
failure, so the marker has to be removed in the same change that makes the guard real. Run with
``--runxfail`` to see the real failures now.

The contract these tests define:

* ``ExchangeCalendar.from_json(path)`` loads the repo's NYSE calendar (``config/market_calendar.json``,
  the live executors' Gate 0 source of truth, sourced from nyse.com and retrieved 2026-07-28). That is
  the ONE calendar a backtest uses. The research store's Alpaca calendar and the ledger's
  ``market_sessions`` are inputs to reconcile against it, not alternatives.
* ``actionable_at(accepted_at, calendar)`` is the regular-session open (09:30 America/New_York) of the
  first session whose open is STRICTLY after ``accepted_at``. A pre-open acceptance is actionable at
  that day's open. An acceptance during the session, after the close, on a weekend or on a holiday waits
  for the next open. During-session acceptance waits too, on purpose: a backtest cannot know how fast
  the market priced a filing mid-session, so it assumes the slowest case.
* Fail closed: a year the calendar does not cover raises ``CalendarCoverageError``, never a guess
  (the same rule ``dca_runner.market_day_status()`` applies live). A naive datetime is refused.

Fixtures: the EDGAR acceptance timestamps are REAL (tests/fixtures/edgar_acceptance_aapl.json, quoted
from each filing's EDGAR index page on 2026-11-23). The holiday, weekend and early-close cases use the
REAL calendar file with acceptance times chosen for the boundary, labelled ``boundary`` below. No filer
claims them.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=(ImportError, AttributeError, AssertionError, TypeError),
    reason="phase A guard: market_data_ingest.exchange_calendar not implemented yet (backtest protocol item 4)",
)

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
CALENDAR_JSON = REPO_ROOT / "config" / "market_calendar.json"
ACCEPTANCE = json.loads((Path(__file__).parent / "fixtures" / "edgar_acceptance_aapl.json").read_text())
ACCEPTED = {f["accession_number"]: f for f in ACCEPTANCE["filings"]}


def _cal_module():
    return importlib.import_module("market_data_ingest.exchange_calendar")


def _calendar():
    return _cal_module().ExchangeCalendar.from_json(CALENDAR_JSON)


def _et(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def _accepted(accn: str) -> datetime:
    # EDGAR index pages show "Accepted" in Eastern time with no suffix.
    return datetime.strptime(ACCEPTED[accn]["accepted"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)


# --- real EDGAR acceptances -------------------------------------------------------------------------------


def test_after_close_8k_is_actionable_at_the_next_sessions_open():
    # AAPL earnings 8-K, accepted Thu 2026-07-30 16:30:28 ET, after the 16:00 close. Its EDGAR filing
    # date is 2026-07-30, so a date-only rule (``filed <= as_of``) would hand it to a 15:30 decision
    # that day: lookahead.
    accepted = _accepted("0000320193-26-000018")
    assert ACCEPTED["0000320193-26-000018"]["filing_date"] == "2026-07-30"
    assert _cal_module().actionable_at(accepted, _calendar()) == _et(2026, 7, 31, 9, 30)


def test_pre_open_10q_is_actionable_at_that_days_open():
    # AAPL 10-Q, accepted Fri 2026-07-31 06:01:02 ET, before the open.
    accepted = _accepted("0000320193-26-000020")
    assert _cal_module().actionable_at(accepted, _calendar()) == _et(2026, 7, 31, 9, 30)


def test_a_year_the_calendar_does_not_cover_raises_instead_of_guessing():
    # AAPL FY2025 10-K, accepted 2025-10-31 06:01:26 ET. config/market_calendar.json covers 2026-2028
    # only, so the honest answer is "unknown", not 09:30 that day.
    mod = _cal_module()
    with pytest.raises(mod.CalendarCoverageError):
        mod.actionable_at(_accepted("0000320193-25-000079"), _calendar())


# --- boundary acceptances on the real calendar ------------------------------------------------------------


@pytest.mark.parametrize(
    ("accepted", "expected", "why"),
    [
        (_et(2026, 7, 2, 17, 0), _et(2026, 7, 6, 9, 30), "boundary: Thu after close; Fri 2026-07-03 holiday"),
        (_et(2026, 11, 25, 18, 0), _et(2026, 11, 27, 9, 30), "boundary: eve of Thanksgiving 2026-11-26"),
        (_et(2026, 11, 27, 13, 30), _et(2026, 11, 30, 9, 30), "boundary: after the 13:00 early close"),
        (_et(2026, 8, 1, 10, 0), _et(2026, 8, 3, 9, 30), "boundary: Saturday"),
        (_et(2026, 7, 30, 11, 0), _et(2026, 7, 31, 9, 30), "boundary: during the session waits a day"),
        (_et(2026, 7, 30, 9, 30, 0), _et(2026, 7, 31, 9, 30), "boundary: exactly at the open is not before it"),
        (_et(2026, 7, 30, 9, 29, 59), _et(2026, 7, 30, 9, 30), "boundary: one second before the open"),
    ],
    ids=["holiday", "thanksgiving", "early-close", "weekend", "intraday", "at-open", "pre-open"],
)
def test_actionable_at_boundaries(accepted, expected, why):
    assert _cal_module().actionable_at(accepted, _calendar()) == expected, why


def test_actionable_at_is_returned_in_eastern_time():
    got = _cal_module().actionable_at(_accepted("0000320193-26-000018"), _calendar())
    assert got.tzinfo is not None and got.utcoffset() == _et(2026, 7, 31, 9, 30).utcoffset()


def test_an_acceptance_given_in_utc_is_converted_not_misread():
    # 20:30:28 UTC is 16:30:28 EDT: still after the close.
    utc = _accepted("0000320193-26-000018").astimezone(ZoneInfo("UTC"))
    assert _cal_module().actionable_at(utc, _calendar()) == _et(2026, 7, 31, 9, 30)


def test_a_naive_acceptance_is_refused():
    with pytest.raises(ValueError):
        _cal_module().actionable_at(datetime(2026, 7, 30, 16, 30, 28), _calendar())


def test_the_backtest_calendar_is_the_repo_nyse_file():
    cal = _calendar()
    assert cal.timezone == "America/New_York"
    assert not cal.is_session(_et(2026, 7, 3).date())  # Independence Day observed
    assert cal.is_session(_et(2026, 11, 27).date()) and cal.is_early_close(_et(2026, 11, 27).date())
