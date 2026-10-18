"""Intraday slot policy: which feeds a slot may use and how fresh a trade must be.

Operator decisions (PLAN Q2, Q3):

* Only the 09:35, 10:30, 12:30 and 13:30 ET slots are configured. Any other slot raises
  ``ValueError``: an unknown slot is a wiring mistake, not a reason to guess a policy.
* IEX last trades must be at most 120 s old.
* Delayed SIP is allowed only for slots at or after 10:30 and never at 09:35. It is 15 minutes
  behind by design, so its limit is 15 min + 120 s; every such price is flagged delayed.
* A trade stamped more than a few seconds in the future, or with a non-positive price, is
  treated as stale: it is not plausible, so it is not usable.
"""

from __future__ import annotations

import math
from datetime import datetime

from .errors import StaleDataError
from .models import Quote

# slot (HH:MM ET) -> may fall back to the delayed SIP feed
SLOT_POLICY: dict[str, bool] = {"09:35": False, "10:30": True, "12:30": True, "13:30": True}
MAX_AGE_IEX_S = 120
DELAYED_SIP_LAG_S = 15 * 60
MAX_AGE_DELAYED_S = DELAYED_SIP_LAG_S + MAX_AGE_IEX_S
MAX_FUTURE_SKEW_S = 5
_MAX_AGE_BY_FEED = {"iex": MAX_AGE_IEX_S, "delayed_sip": MAX_AGE_DELAYED_S}


def allows_delayed(slot: str) -> bool:
    try:
        return SLOT_POLICY[slot]
    except KeyError:
        raise ValueError(f"slot {slot!r} is not configured; known slots: {sorted(SLOT_POLICY)}") from None


def check_fresh(quote: Quote, *, now: datetime) -> None:
    """Raise ``StaleDataError`` unless ``quote`` is usable at ``now``."""
    max_age = _MAX_AGE_BY_FEED.get(quote.feed)
    if max_age is None:
        raise ValueError(f"unknown feed {quote.feed!r}")
    if not (isinstance(quote.price, (int, float)) and math.isfinite(quote.price) and quote.price > 0):
        raise StaleDataError(f"{quote.symbol}: implausible price on {quote.feed}", code="implausible_price")
    age = (now - quote.ts).total_seconds()
    if age < -MAX_FUTURE_SKEW_S:
        raise StaleDataError(
            f"{quote.symbol}: {quote.feed} trade is stamped {-age:.0f}s in the future", code="future_timestamp"
        )
    if age > max_age:
        raise StaleDataError(f"{quote.symbol}: {quote.feed} trade is {age:.0f}s old (limit {max_age}s)", code="stale")
