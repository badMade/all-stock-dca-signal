"""Fundamentals joined on EDGAR acceptance time, never period end (backtest protocol item 3).

Specified first by tests/test_edgar_actionable_facts.py (docs/adr-backtest-protocol.md, phase A).
``first_filed_facts`` in edgar_fundamentals.py admits a fact on its ``filed`` DATE, which hands an
after-close 8-K or a pre-open 10-Q to a decision made earlier that same day. This module replaces
the date with the acceptance TIMESTAMP and the calendar:

* ``parse_edgar_accepted`` reads the "Accepted" field of an EDGAR filing index page
  (``YYYY-MM-DD HH:MM:SS``, Eastern time, no suffix) into an aware America/New_York datetime.
  Anything else is refused, including an ISO string with its own offset: the source format is
  fixed, so a different shape means a different (unverified) source.
* ``actionable_facts`` keeps a fact only when its form is a periodic report (``POINT_IN_TIME_FORMS``:
  never a proxy, which also carries XBRL), its accession has a known acceptance time (an unknown
  accession is DROPPED, fail closed, with no fallback to ``filed``), and its ``actionable_at`` is at
  or before ``decision_at``. Of the kept facts the existing first-filed rule picks one per period.
* An acceptance in a year the calendar does not cover raises ``CalendarCoverageError`` rather than
  admitting the fact on a guessed open.

Research ingest only: nothing on the live DCA side imports this.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .edgar_fundamentals import PointInTimeFact, first_filed_facts
from .exchange_calendar import ExchangeCalendar, actionable_at

EDGAR_TZ = ZoneInfo("America/New_York")
_ACCEPTED = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

# Periodic reports and their amendments. Proxy statements (DEF 14A and friends) are excluded:
# their pay-versus-performance XBRL is not a company's reported financials. 8-K exhibits are
# excluded too: earnings releases are unaudited and companyfacts rarely carries them.
POINT_IN_TIME_FORMS: frozenset[str] = frozenset(
    form + suffix
    for form in ("10-K", "10-Q", "10-KT", "10-QT", "20-F", "40-F")
    for suffix in ("", "/A")
)


def parse_edgar_accepted(text: str) -> datetime:
    """``"2026-07-30 16:30:28"`` (EDGAR "Accepted", Eastern) -> aware datetime in America/New_York."""
    if not isinstance(text, str) or not _ACCEPTED.fullmatch(text):
        raise ValueError(f"not an EDGAR acceptance timestamp: {text!r}")
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EDGAR_TZ)


def actionable_facts(
    facts: Iterable[PointInTimeFact],
    acceptance: Mapping[str, datetime],
    *,
    decision_at: datetime,
    calendar: ExchangeCalendar,
) -> list[PointInTimeFact]:
    """The facts a decision at ``decision_at`` could have acted on, one per period, first-filed value."""
    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueError("decision_at must be timezone-aware")
    kept = []
    for fact in facts:
        if fact.form not in POINT_IN_TIME_FORMS:
            continue
        accepted = acceptance.get(fact.accn)
        if accepted is None:
            continue  # fail closed: no acceptance time, no admission on the filed date
        if actionable_at(accepted, calendar) <= decision_at:
            kept.append(fact)
    # Every kept fact is already public by construction, so the date filter is a no-op here.
    return first_filed_facts(kept, as_of=date.max)
