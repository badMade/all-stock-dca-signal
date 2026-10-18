"""PriceRouter: primary, then fallback, then raise.

Only a ``ProviderError`` (which includes ``StaleDataError`` and ``IncompleteBatchError``) is a
reason to fail over. Anything else is a bug and propagates unchanged, so a programming error
in the primary can never be hidden behind a quietly-working fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from typing import Any

from .errors import ProviderError, RouterExhaustedError
from .models import CorporateAction, DailyBar, Quote
from .provider import PriceProvider

logger = logging.getLogger(__name__)


class PriceRouter:
    """A ``PriceProvider`` that tries ``primary`` and, on ``ProviderError``, ``fallback``."""

    def __init__(self, primary: PriceProvider, fallback: PriceProvider) -> None:
        self.primary = primary
        self.fallback = fallback

    def __repr__(self) -> str:
        return f"PriceRouter(primary={self.primary!r}, fallback={self.fallback!r})"

    def _route(self, method: str, *args: Any) -> Any:
        try:
            return getattr(self.primary, method)(*args)
        except ProviderError as primary_error:
            logger.warning(
                "primary %s.%s failed (%s: %s); trying fallback",
                type(self.primary).__name__,
                method,
                type(primary_error).__name__,
                primary_error.code,
            )
            try:
                result = getattr(self.fallback, method)(*args)
            except ProviderError as fallback_error:
                logger.error(
                    "fallback %s.%s failed (%s: %s); no source left",
                    type(self.fallback).__name__,
                    method,
                    type(fallback_error).__name__,
                    fallback_error.code,
                )
                raise RouterExhaustedError(primary_error, fallback_error) from fallback_error
            logger.warning("served %s from fallback %s", method, type(self.fallback).__name__)
            return result

    def daily_bars(self, session: date) -> list[DailyBar]:
        return self._route("daily_bars", session)

    def corporate_actions(self, since: date) -> list[CorporateAction]:
        return self._route("corporate_actions", since)

    def last_trade(self, symbols: Sequence[str]) -> dict[str, Quote]:
        return self._route("last_trade", symbols)
