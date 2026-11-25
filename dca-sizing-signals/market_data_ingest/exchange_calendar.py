"""The ONE exchange calendar a backtest uses, and ``actionable_at`` on it (backtest protocol item 4).

Specified first by tests/test_actionable_at.py (docs/adr-backtest-protocol.md, phase A). The rules:

* The calendar is the repo's NYSE file, ``config/market_calendar.json``: the live executors' Gate 0
  source of truth (sourced from nyse.com). The research store's Alpaca calendar and the ledger's
  ``market_sessions`` are inputs to reconcile against it, not alternatives. The caller passes the
  path, so this package never reaches into the repo root on its own.
* ``actionable_at(accepted_at, calendar)`` is the 09:30 America/New_York open of the first session
  whose open is STRICTLY after ``accepted_at``. A pre-open acceptance is actionable at that day's
  open; one during the session, after the close, on a weekend or on a holiday waits for the next
  open. During-session acceptance waits on purpose: a backtest cannot know how fast the market
  priced a filing mid-session, so it assumes the slowest case.
* Fail closed. A date in a year the file does not cover raises ``CalendarCoverageError`` (the same
  rule ``dca_runner.market_day_status()`` applies live), never a weekday guess. A naive datetime is
  refused with ``ValueError``: it is ambiguous.

Standard library only. Research ingest only: nothing on the live DCA side imports this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
# Longest run of non-sessions to walk before concluding the file is broken. NYSE has never been
# shut more than a handful of consecutive weekdays outside 9/11 and 1914; 14 days is generous and
# keeps a corrupt file from looping until the coverage check finally trips.
_MAX_GAP_DAYS = 14


class CalendarCoverageError(LookupError):
    """The calendar does not cover the date asked about. The honest answer is "unknown"."""


@dataclass(frozen=True)
class ExchangeCalendar:
    """Sessions, holidays and early closes from one calendar file."""

    timezone: str
    years_covered: frozenset[int]
    holidays: frozenset[date]
    early_closes: frozenset[date]
    early_close_time: time

    @classmethod
    def from_json(cls, path: Path | str) -> ExchangeCalendar:
        """Load ``config/market_calendar.json`` (or a file of the same shape). Malformed input raises."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: calendar file is not a JSON object")
        try:
            tz = raw["timezone"]
            years = frozenset(int(y) for y in raw["years_covered"])
            holidays = frozenset(date.fromisoformat(d) for d in raw["holidays_closed"])
            early = frozenset(date.fromisoformat(d) for d in raw["early_close_1300"])
            early_time = time.fromisoformat(raw.get("early_close_time_et", "13:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: malformed calendar file ({type(exc).__name__}: {exc})") from exc
        ZoneInfo(tz)  # an unknown zone name fails here, not on first use
        if not years:
            raise ValueError(f"{path}: calendar covers no years")
        outside = sorted(d for d in holidays | early if d.year not in years)
        if outside:
            raise ValueError(f"{path}: dates outside years_covered: {', '.join(map(str, outside[:5]))}")
        if holidays & early:
            raise ValueError(f"{path}: a date is both a holiday and an early close")
        return cls(tz, years, holidays, early, early_time)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _require_covered(self, day: date) -> None:
        if day.year not in self.years_covered:
            covered = ", ".join(str(y) for y in sorted(self.years_covered))
            raise CalendarCoverageError(f"{day} is outside the calendar's covered years ({covered})")

    def is_session(self, day: date) -> bool:
        """True on a regular or early-close trading day. Raises for an uncovered year."""
        self._require_covered(day)
        return day.weekday() < 5 and day not in self.holidays

    def is_early_close(self, day: date) -> bool:
        self._require_covered(day)
        return day in self.early_closes

    def session_open(self, day: date) -> datetime:
        """The regular-session open of ``day`` in the calendar's zone. ``day`` must be a session."""
        if not self.is_session(day):
            raise ValueError(f"{day} is not a session")
        return datetime.combine(day, REGULAR_OPEN, self.zone)

    def session_close(self, day: date) -> datetime:
        if not self.is_session(day):
            raise ValueError(f"{day} is not a session")
        return datetime.combine(day, self.early_close_time if day in self.early_closes else REGULAR_CLOSE, self.zone)


def actionable_at(accepted_at: datetime, calendar: ExchangeCalendar) -> datetime:
    """The first session open strictly after ``accepted_at``, as an aware datetime in the calendar's zone."""
    if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
        raise ValueError("accepted_at must be timezone-aware")
    local = accepted_at.astimezone(calendar.zone)
    day = local.date()
    for _ in range(_MAX_GAP_DAYS + 1):
        if calendar.is_session(day):
            opened = calendar.session_open(day)
            if opened > local:
                return opened
        day += timedelta(days=1)
    raise CalendarCoverageError(f"no session within {_MAX_GAP_DAYS} days after {local.isoformat()}")
