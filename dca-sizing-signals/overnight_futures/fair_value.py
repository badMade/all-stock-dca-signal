"""Index-futures fair value (docs/adr-overnight-futures-tracker.md, D4 and D9).

CME's published formula, nothing more:

    Fair Value = Cash x [1 + r x (days / 360)] - expected dividend points to expiry

Pure and stdlib-only. Rates and dividend points arrive from the caller, so the choice
of rate (3-month T-bill, first-release vintage) and of dividend estimate (trailing yield
adjusted by known ex-dates) stays with the caller, per the ADR's open questions 5 and 6.
"""

from __future__ import annotations

import math
from numbers import Real

#: CME quotes equity-index carry on an actual/360 basis. 365 would overstate the
#: premium by about 1.4% of the carry, which is a few index points at a 90-day roll.
DAY_COUNT_BASIS = 360


def _finite(value: object) -> bool:
    # bool is a Real in Python; True as a price is a caller bug, not a 1.0.
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def fair_value(
    cash: float | None,
    rate: float | None,
    days_to_expiry: int | None,
    dividend_points: float | None,
) -> float | None:
    """Return the CME fair value of an index future, or None when it cannot be known.

    Args:
        cash: the cash index level at the same instant as the futures price.
        rate: annual financing rate as a decimal (0.057 for 5.7%). May be negative.
        days_to_expiry: calendar days from the observation date to expiry; 0 on expiry day.
        dividend_points: expected index-point dividends going ex before expiry.

    Returns:
        The fair value, or None if any input is missing, non-finite or impossible
        (non-positive cash, negative days or dividends), or if the result is not
        positive. None is the fail-closed answer (D9): the tracker then marks the row
        ``fv_quality = 'missing'`` and forecasts nothing from it. Never raises.
    """
    if not all(_finite(v) for v in (cash, rate, days_to_expiry, dividend_points)):
        return None
    if cash <= 0 or days_to_expiry < 0 or dividend_points < 0:
        return None
    value = cash * (1 + rate * (days_to_expiry / DAY_COUNT_BASIS)) - dividend_points
    # A non-positive fair value cannot anchor the log return the tracker takes, and it
    # can only come from a dividend estimate larger than the index: refuse it.
    return value if value > 0 else None
