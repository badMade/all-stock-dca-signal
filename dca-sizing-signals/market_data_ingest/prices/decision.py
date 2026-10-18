"""price_for_suggestion: a fresh, recorded intraday price, or an explicit NoSuggestion.

This is the boundary a suggestion engine calls. It is the one place in the package that
turns provider exceptions into a value, and only into ``NoSuggestion`` (reason + error
class), never into a fallback price. It is a library function: nothing in the live DCA
paths calls it, and wiring it in needs its own review and acceptance run.

Order: IEX first; if that fails and the slot allows it, delayed SIP (flagged delayed); then
NoSuggestion. A price is persisted to ``intraday_quotes`` before it is returned, and a price
that cannot be recorded is not used.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import intraday
from .errors import ProviderError, RouterExhaustedError, StaleDataError
from .models import NoSuggestion, PriceOK, Quote

logger = logging.getLogger(__name__)


def _fresh_quote(provider: Any, symbol: str, feed: str, now: datetime) -> Quote:
    quotes = provider.last_trade([symbol], feed=feed)
    quote = quotes.get(symbol)
    if quote is None:
        raise StaleDataError(f"{symbol}: no {feed} print", code="no_print")
    intraday.check_fresh(quote, now=now)
    return quote


def price_for_suggestion(
    symbol: str, *, slot: str, provider: Any, store: Any, clock: Callable[[], datetime], run_id: str
) -> PriceOK | NoSuggestion:
    allow_delayed = intraday.allows_delayed(slot)  # an unknown slot raises: a wiring bug, not a no-suggestion
    now = clock()
    delayed = False
    try:
        quote = _fresh_quote(provider, symbol, "iex", now)
    except ProviderError as iex_error:
        if not allow_delayed:
            logger.warning(
                "%s %s: no usable IEX price (%s: %s); no suggestion",
                slot,
                symbol,
                type(iex_error).__name__,
                iex_error.code,
            )
            return NoSuggestion(symbol=symbol, reason=f"iex: {iex_error}", error_class=type(iex_error).__name__)
        try:
            quote = _fresh_quote(provider, symbol, "delayed_sip", now)
            delayed = True
        except ProviderError as delayed_error:
            exhausted = RouterExhaustedError(iex_error, delayed_error)
            logger.warning("%s %s: %s; no suggestion", slot, symbol, exhausted)
            return NoSuggestion(symbol=symbol, reason=str(exhausted), error_class=type(exhausted).__name__)

    try:
        store.insert_intraday_quote(quote, slot=slot, delayed=delayed, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - fail closed: an unrecorded price is never used
        logger.exception("%s %s: could not record the quote, so it is not used", slot, symbol)
        return NoSuggestion(
            symbol=symbol,
            reason=f"could not record the quote ({type(exc).__name__}); not used",
            error_class=type(exc).__name__,
        )
    return PriceOK(symbol=symbol, price=quote.price, ts=quote.ts, feed=quote.feed, delayed=delayed)
