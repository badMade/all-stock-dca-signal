"""Point-in-time price index and the per-decision view a candidate is allowed to read.

The harness's point-in-time rule (docs/adr-cost-basis-backtest-harness.md, "Point in
time"): a decision for session t sees closes dated strictly before t, and events whose
``known_at`` is strictly before t. The fill itself happens at the close of t, which the
candidate never sees. That is stricter than the live 3:30 PM slot (which sees a near-close
quote for t), on purpose: it can only understate a signal, never flatter it.

Every accessor that takes a date checks it. A request for t or later is recorded on
``PointInTimeView.violations`` *and* raises LookaheadError, so a candidate that catches the
error and carries on is still caught: the engine rejects the whole run if any violation was
recorded (review of the peeking-suggester test, which swallows the error on purpose).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime


class BacktestInputError(ValueError):
    """Malformed harness input. The run fails closed before any decision is made."""


class LookaheadError(RuntimeError):
    """A candidate asked for data dated on or after its decision session."""


def parse_day(value: object, where: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise BacktestInputError(f"{where}: unparseable date {value!r}") from exc
    raise BacktestInputError(f"{where}: date must be a date or ISO string, got {type(value).__name__}")


def finite(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise BacktestInputError(f"{where}: must be a finite number, got {value!r}")
    return float(value)


def event_known_at(event: object, where: str) -> date:
    """``known_at`` of an event given as an object attribute (e.g. residual_reversal's
    CorporateEvent, which this package may not import) or a mapping key. For an 8-K this is
    the SEC filed_date, never the event or period date."""
    raw = event.get("known_at") if isinstance(event, Mapping) else getattr(event, "known_at", None)
    if raw is None:
        raise BacktestInputError(f"{where}: event has no known_at")
    if isinstance(raw, datetime) or not isinstance(raw, date):
        # A datetime would let an intraday filing compare equal to its own session; require
        # the session date so the strict "< t" rule is unambiguous.
        raise BacktestInputError(f"{where}: known_at must be a date, got {type(raw).__name__}")
    return raw


class PriceIndex:
    """Validated daily closes, indexed per ticker and on one session calendar.

    Rows are ``{"ticker", "date", "close"}`` plus optional bool ``interpolated`` (dropped,
    as everywhere else in dca-sizing-signals). Closes must already be split/dividend
    adjusted *as known at the start of the backtest* -- an adjusted history re-pulled later
    rewrites past returns, so snapshot it (residual_reversal ADR, "Point in time").

    The calendar is the union of every row's date. A ticker with no close on a session has
    no fill and no forward return there (zero quote tolerance, same as the ledger's
    OUTCOME_QUOTE_TOLERANCE_SESSIONS default).
    """

    def __init__(self, close_rows: Sequence[Mapping[str, object]]) -> None:
        if isinstance(close_rows, (str, bytes)) or not isinstance(close_rows, Sequence):
            raise BacktestInputError("close_rows must be a sequence of row mappings")
        by_ticker: dict[str, dict[date, float]] = {}
        for i, row in enumerate(close_rows):
            where = f"close_rows[{i}]"
            if not isinstance(row, Mapping):
                raise BacktestInputError(f"{where}: must be a mapping")
            flag = row.get("interpolated", False)
            if flag is not None and not isinstance(flag, bool):
                raise BacktestInputError(f"{where}: interpolated must be a bool")
            if flag:
                continue
            ticker = row.get("ticker")
            if not isinstance(ticker, str) or not ticker:
                raise BacktestInputError(f"{where}: ticker must be a non-empty string")
            d = parse_day(row.get("date"), where)
            close = finite(row.get("close"), f"{where} close")
            if close <= 0.0:
                raise BacktestInputError(f"{where}: close must be positive")
            closes = by_ticker.setdefault(ticker, {})
            if d in closes:
                raise BacktestInputError(f"{where}: duplicate close for {ticker} on {d.isoformat()}")
            closes[d] = close
        if not by_ticker:
            raise BacktestInputError("close_rows has no usable rows")
        self._closes = by_ticker
        self._dates = {t: sorted(c) for t, c in by_ticker.items()}
        self.sessions: tuple[date, ...] = tuple(sorted({d for c in by_ticker.values() for d in c}))
        self._session_pos = {d: i for i, d in enumerate(self.sessions)}

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._closes))

    def close_on(self, ticker: str, d: date) -> float | None:
        return self._closes.get(ticker, {}).get(d)

    def last_close_on_or_before(self, ticker: str, d: date) -> float | None:
        dates = self._dates.get(ticker)
        if not dates:
            return None
        i = bisect.bisect_right(dates, d)
        return self._closes[ticker][dates[i - 1]] if i else None

    def session_offset(self, d: date, offset: int) -> date | None:
        """The calendar session ``offset`` sessions after (or before) session ``d``."""
        pos = self._session_pos.get(d)
        if pos is None:
            return None
        j = pos + offset
        return self.sessions[j] if 0 <= j < len(self.sessions) else None

    def closes_before(self, ticker: str, before: date, lookback_sessions: int | None) -> list[tuple[date, float]]:
        dates = self._dates.get(ticker, [])
        hi = bisect.bisect_left(dates, before)
        lo = 0
        if lookback_sessions is not None:
            start_pos = bisect.bisect_left(self.sessions, before) - lookback_sessions
            if start_pos > 0:
                lo = bisect.bisect_left(dates, self.sessions[start_pos])
        closes = self._closes.get(ticker, {})
        return [(d, closes[d]) for d in dates[lo:hi]]


class PointInTimeView:
    """What a candidate may read when deciding session ``asof`` (call it t): closes dated
    before t and events known before t. Nothing else about the backtest is reachable from
    here -- not the baseline's future buys, not the fill price, not the forward returns."""

    def __init__(self, index: PriceIndex, events_by_ticker: Mapping[str, Sequence[object]], asof: date) -> None:
        self._index = index
        self._events = events_by_ticker
        self._asof = asof
        self.violations: list[str] = []

    @property
    def asof(self) -> date:
        return self._asof

    def _check(self, d: date, what: str) -> None:
        if d >= self._asof:
            message = f"{what} dated {d.isoformat()} requested for decision {self._asof.isoformat()}"
            self.violations.append(message)
            raise LookaheadError(message)

    def sessions(self, lookback: int | None = None) -> tuple[date, ...]:
        """Calendar sessions before t (the newest ``lookback`` of them, if given)."""
        hi = bisect.bisect_left(self._index.sessions, self._asof)
        lo = 0 if lookback is None else max(0, hi - lookback)
        return self._index.sessions[lo:hi]

    def tickers(self) -> tuple[str, ...]:
        return self._index.tickers

    def close(self, ticker: str, d: date) -> float | None:
        self._check(d, f"close for {ticker}")
        return self._index.close_on(ticker, d)

    def closes(self, ticker: str, lookback_sessions: int | None = None) -> list[tuple[date, float]]:
        return self._index.closes_before(ticker, self._asof, lookback_sessions)

    def close_rows(self, tickers: Sequence[str] | None = None,
                   lookback_sessions: int | None = None) -> list[dict[str, object]]:
        """Plain close rows, the shape comovement_signals and residual_reversal read. Every
        row is a completed past session, so each is marked ``final``. ``lookback_sessions``
        bounds the window by calendar sessions; without it every past row is copied, which
        is quadratic over a long backtest."""
        names = self._index.tickers if tickers is None else tuple(tickers)
        rows: list[dict[str, object]] = []
        for ticker in names:
            for d, close in self._index.closes_before(ticker, self._asof, lookback_sessions):
                rows.append({"ticker": ticker, "date": d, "close": close, "final": True})
        return rows

    def events(self, ticker: str) -> tuple[object, ...]:
        """Events for ``ticker`` with ``known_at`` strictly before t. An 8-K filed on t is
        not visible to the decision for t (data up to t-1 close only)."""
        return tuple(e for e in self._events.get(ticker, ()) if event_known_at(e, ticker) < self._asof)

    def events_by_ticker(self, tickers: Sequence[str]) -> dict[str, tuple[object, ...]]:
        return {t: self.events(t) for t in tickers}
