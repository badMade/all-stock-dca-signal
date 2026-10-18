"""The PriceProvider and SessionCalendar contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from .models import CorporateAction, DailyBar, Quote


@runtime_checkable
class PriceProvider(Protocol):
    """A source of raw daily bars, corporate actions, and last trades.

    Every method either returns complete data or raises a ``ProviderError`` subclass. An
    empty list is a real answer (a closed market, no actions since ``since``), never a
    stand-in for a failure.
    """

    def daily_bars(self, session: date) -> list[DailyBar]: ...

    def corporate_actions(self, since: date) -> list[CorporateAction]: ...

    def last_trade(self, symbols: Sequence[str]) -> dict[str, Quote]: ...


@runtime_checkable
class SessionCalendar(Protocol):
    """Market calendar. An outage raises; it never answers "closed"."""

    def is_session(self, d: date) -> bool: ...

    def previous_sessions(self, d: date, n: int) -> list[date]:
        """The ``n`` sessions strictly before ``d``, most recent first."""
        ...
