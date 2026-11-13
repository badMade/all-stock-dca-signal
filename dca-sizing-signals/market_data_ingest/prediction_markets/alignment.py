"""Daily alignment to equities (ADR §6; methods doc §4).

Prediction markets trade 24/7. The value for an equity session is the last *mid* stamped
strictly before 16:00 America/New_York on that date -- not the UTC daily candle close, and
not a trade print. zoneinfo handles DST, so the close is 20:00 UTC in summer and 21:00 UTC in
winter without a hard-coded offset.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
EQUITY_CLOSE = time(16, 0)


def daily_value(quotes: Iterable, session_date: date):
    """The latest quote with a mid in [00:00, 16:00) ET on ``session_date``, or None."""
    open_ = datetime.combine(session_date, time(0, 0), tzinfo=NEW_YORK)
    close = datetime.combine(session_date, EQUITY_CLOSE, tzinfo=NEW_YORK)
    best = None
    for q in quotes:
        if q.ts_utc.tzinfo is None or q.ts_utc.utcoffset() is None:
            raise ValueError("quote ts_utc must be timezone-aware")
        if q.mid is None or not (open_ <= q.ts_utc < close):
            continue
        if best is None or q.ts_utc > best.ts_utc:
            best = q
    return best
