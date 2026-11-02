"""Point-in-time daily log returns from caller-supplied close rows.

ADR rule 1 (docs/adr-comovement-signals.md): clusters are built from a trailing window of
daily returns using only data dated on or before the as-of date (and a bar ON the as-of
date only once the caller marks it final). This module is where
that promise is kept, so it is deliberately strict:

  * a row dated after ``as_of``, or on it unless marked final, is dropped before anything
    else looks at it, which is the whole lookahead guard -- nothing downstream ever sees a
    future (or still-forming) price;
  * a row flagged ``interpolated`` is dropped too. Robinhood's daily historicals fill the
    days before a listing with bars flagged ``interpolated: true`` (seen for SNDK, RVI,
    VCX and others in the 2026-11-02 co-movement research), and those are not prices;
  * a malformed row raises ``ComovementInputError`` instead of being skipped, because a
    silently skipped bad row would quietly change the clusters. The suggesters turn that
    error into a fail-closed "publish nothing" outcome.

Pure: no I/O, no network, no clock. Stdlib only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

# ADR question 1: a 252-session (about one trading year) trailing window.
DEFAULT_WINDOW_SESSIONS = 252
# The 2026-11-02 research used "minimum 120 overlapping days" for every pairwise
# correlation; a ticker with fewer returns than this in the window is a singleton.
DEFAULT_MIN_SESSIONS = 120


class ComovementInputError(ValueError):
    """A caller-supplied input is malformed. Raised, never skipped (fail closed)."""


@dataclass(frozen=True)
class ReturnsMatrix:
    """Daily log returns over the window, keyed ticker -> session date -> return.

    Attributes:
        as_of: the as-of date; no row after it was read.
        sessions: the window's return dates, oldest first. A "session" is a date on
            which at least half of the tickers listed that day have a usable close
            (_session_calendar), so the calendar comes from the data itself and needs no
            exchange-calendar dependency.
        returns: per-ticker returns. A ticker with a missing close on a session has no
            return on that session or the next one (never a multi-day return).
        eligible: tickers with at least ``min_sessions`` returns, sorted. Only these are
            clustered.
        singletons: tickers with some price history but too few returns, sorted. Each is
            its own cluster.
        latest_price_date: the newest close date used, or None if there was none. The
            suggesters compare it with ``as_of`` for the staleness check.
    """

    as_of: date
    sessions: tuple[date, ...]
    returns: Mapping[str, Mapping[date, float]]
    eligible: tuple[str, ...]
    singletons: tuple[str, ...]
    latest_price_date: date | None


def _parse_date(value: object) -> date:
    if isinstance(value, date):
        # datetime is a date subclass; keep only the calendar day.
        return date(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ComovementInputError(f"unparseable date {value!r}") from exc
    raise ComovementInputError(f"date must be a date or ISO string, got {type(value).__name__}")


def _bool_flag(row: Mapping[str, object], name: str) -> bool:
    value = row.get(name, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        # A string "false" is truthy; refusing non-bools avoids guessing what it meant.
        raise ComovementInputError(f"{name} flag must be a bool: {row!r}")
    return value


def _clean_rows(close_rows: Iterable[Mapping[str, object]], as_of: date) -> dict[str, dict[date, float]]:
    """Validate every real row, then keep the closes that were final before as_of.

    Which rows count (review round 1 on PR #2106 tightened the first two):

      * dated before as_of: kept;
      * dated ON as_of: kept only when the caller marks it ``final: True``. At an
        intraday slot today's daily bar is still forming, and treating it as a close would
        leak the rest of the day into the clusters. An end-of-day caller marks it final;
      * dated after as_of: dropped (the lookahead guard);
      * flagged ``interpolated``: dropped before its close is even looked at, so a null or
        junk close on a pre-listing fill bar is skipped rather than failing the run --
        those bars are ignored anyway. The flag itself must still be a bool.

    Every real row is validated, including future ones: a malformed future row is still a
    malformed feed and should fail the run rather than be ignored because of its date.
    Duplicates are checked among real rows only.
    """
    closes: dict[str, dict[date, float]] = {}
    seen: set[tuple[str, date]] = set()
    for row in close_rows:
        if not isinstance(row, Mapping):
            raise ComovementInputError(f"row must be a mapping, got {type(row).__name__}")
        if _bool_flag(row, "interpolated"):
            continue
        final = _bool_flag(row, "final")
        try:
            ticker, raw_date, raw_close = row["ticker"], row["date"], row["close"]
        except KeyError as exc:
            raise ComovementInputError(f"row missing ticker/date/close: {row!r}") from exc
        if not isinstance(ticker, str) or not ticker:
            raise ComovementInputError(f"ticker must be a non-empty string: {row!r}")
        d = _parse_date(raw_date)
        key = (ticker, d)
        if key in seen:
            raise ComovementInputError(f"duplicate row for {ticker} on {d.isoformat()}")
        seen.add(key)
        if isinstance(raw_close, bool) or not isinstance(raw_close, (int, float)):
            raise ComovementInputError(f"close must be a number: {row!r}")
        close = float(raw_close)
        if not math.isfinite(close) or close <= 0.0:
            raise ComovementInputError(f"close must be finite and positive: {row!r}")
        if d > as_of or (d == as_of and not final):
            continue
        closes.setdefault(ticker, {})[d] = close
    return closes


# A date is a session when at least this share of the tickers listed on it (first close
# <= date <= last close) has a close that day. Review round 1: with a plain union of dates,
# one stray row for one ticker (a weekend print, a bad vendor date) became a "session"
# that every other ticker was missing, so it broke the adjacent-day return for all of
# them. Counting only listed tickers keeps a new listing's early dates from being judged
# against names that did not exist yet.
DEFAULT_CALENDAR_MIN_SHARE = 0.5


def _session_calendar(closes: Mapping[str, Mapping[date, float]], min_share: float) -> list[date]:
    spans = {t: (min(by_date), max(by_date)) for t, by_date in closes.items() if by_date}
    candidates = sorted({d for by_date in closes.values() for d in by_date})
    sessions = []
    for d in candidates:
        listed = sum(1 for first, last in spans.values() if first <= d <= last)
        trading = sum(1 for by_date in closes.values() if d in by_date)
        if listed and trading >= min_share * listed:
            sessions.append(d)
    return sessions


def returns_matrix(
    close_rows: Iterable[Mapping[str, object]],
    as_of: date,
    window_sessions: int = DEFAULT_WINDOW_SESSIONS,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    calendar_min_share: float = DEFAULT_CALENDAR_MIN_SHARE,
) -> ReturnsMatrix:
    """Build the point-in-time returns matrix.

    Args:
        close_rows: mappings with ``ticker`` (str), ``date`` (date or ISO string),
            ``close`` (positive number) and optionally ``interpolated`` and ``final``
            (bools; see _clean_rows for exactly which rows count).
        as_of: the as-of date. Rows after it, and non-final rows on it, are ignored.
        window_sessions: number of return sessions to keep (the newest ones).
        min_sessions: minimum returns in the window for a ticker to be clustered.
        calendar_min_share: see DEFAULT_CALENDAR_MIN_SHARE. A close on a date that is not
            a session is ignored.

    Returns:
        A ReturnsMatrix.

    Raises:
        ComovementInputError: on any malformed row or invalid parameter.
    """
    if window_sessions < 1 or min_sessions < 1:
        raise ComovementInputError("window_sessions and min_sessions must be at least 1")
    if not (math.isfinite(calendar_min_share) and 0.0 < calendar_min_share <= 1.0):
        raise ComovementInputError("calendar_min_share must be in (0, 1]")
    closes = _clean_rows(close_rows, as_of)
    calendar = _session_calendar(closes, calendar_min_share)
    # One extra leading date so the oldest kept session still has a prior close.
    window_dates = calendar[-(window_sessions + 1):]
    sessions = tuple(window_dates[1:])

    returns: dict[str, dict[date, float]] = {}
    for ticker in sorted(closes):
        by_date = closes[ticker]
        series: dict[date, float] = {}
        for prev, cur in zip(window_dates, window_dates[1:], strict=False):  # pairwise neighbours
            # Both closes must exist on adjacent calendar sessions; a gap yields no
            # return rather than a silently multi-day one.
            if prev in by_date and cur in by_date:
                series[cur] = math.log(by_date[cur] / by_date[prev])
        returns[ticker] = series

    eligible = tuple(t for t in sorted(returns) if len(returns[t]) >= min_sessions)
    singletons = tuple(t for t in sorted(returns) if len(returns[t]) < min_sessions)
    return ReturnsMatrix(
        as_of=as_of,
        sessions=sessions,
        returns=returns,
        eligible=eligible,
        singletons=singletons,
        latest_price_date=calendar[-1] if calendar else None,
    )
