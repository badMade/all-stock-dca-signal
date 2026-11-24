"""Synthetic close rows for the factor_signals tests. None of this is market data.

Closes are built from chosen daily log returns (close_t = close_{t-1} * exp(r_t)), so the
log returns the model recovers equal the ones chosen here to float precision, and every
expected beta can be written down in closed form.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

START = date(2025, 1, 1)


def day(i: int) -> date:
    """Calendar day i after START. Weekends are ignored on purpose: the model's session
    calendar is the market symbol's own close dates, so any date sequence works."""
    return START + timedelta(days=i)


def market_and_sector_returns(n: int, seed: int = 7) -> tuple[list[float], list[float], list[float]]:
    """(market, sector, sector_noise) returns of length n. sector = 1.2 * market + noise."""
    rng = random.Random(seed)
    m = [rng.gauss(0.0004, 0.01) for _ in range(n)]
    u = [rng.gauss(0.0, 0.006) for _ in range(n)]
    s = [1.2 * mi + ui for mi, ui in zip(m, u, strict=True)]
    return m, s, u


def closes_from_returns(ticker: str, returns: list[float], start_price: float = 100.0,
                        first_day: int = 0) -> list[dict]:
    """One row per day, first_day holding start_price and each later day one return on."""
    rows = [{"ticker": ticker, "date": day(first_day), "close": start_price}]
    price = start_price
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append({"ticker": ticker, "date": day(first_day + i), "close": price})
    return rows


def as_of_after(rows: list[dict]) -> date:
    """The day after the newest row, so every row is final by date."""
    return max(r["date"] for r in rows) + timedelta(days=1)
