"""Seeded synthetic close rows for the residual-reversal tests. None of this is market
data: an ETF random walk, and tickers built as beta * ETF return + the increments of an
Ornstein-Uhlenbeck residual, so the true b, beta and the final residual shock are known."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

START = date(2025, 1, 6)  # a Monday
FIXED_NOW = datetime(2026, 1, 9, 20, 0, tzinfo=timezone.utc)


def clock():
    return FIXED_NOW


def sessions(n: int) -> list[date]:
    """n weekdays from START (holidays are irrelevant to the math)."""
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def etf_returns(n: int, seed: int = 1, vol: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, vol) for _ in range(n)]


def ou_increments(n: int, b: float = 0.8, sigma: float = 0.01, seed: int = 2,
                  final_shock_sigmas: float = 0.0) -> list[float]:
    """Increments of X[k] = b X[k-1] + sigma * z. The last X is then pushed by
    ``final_shock_sigmas`` equilibrium standard deviations (negative = residually cheap)."""
    rng = random.Random(seed)
    x_prev, out = 0.0, []
    for _ in range(n):
        x = b * x_prev + sigma * rng.gauss(0.0, 1.0)
        out.append(x - x_prev)
        x_prev = x
    out[-1] += final_shock_sigmas * sigma / math.sqrt(1.0 - b * b)
    return out


def close_rows(ticker: str, returns: list[float], dates: list[date], start: float = 100.0) -> list[dict]:
    """Closes on ``dates`` whose log returns are ``returns`` (len(dates) == len(returns) + 1)."""
    assert len(dates) == len(returns) + 1
    rows, price = [{"ticker": ticker, "date": dates[0].isoformat(), "close": start}], start
    for d, r in zip(dates[1:], returns, strict=True):
        price *= math.exp(r)
        rows.append({"ticker": ticker, "date": d.isoformat(), "close": price})
    return rows


def universe(n_returns: int = 130, shocks: dict[str, float] | None = None, beta: float = 1.0,
             b: float = 0.8, etf: str = "XLK") -> tuple[list[dict], list[date]]:
    """One ETF plus one ticker per ``shocks`` entry (ticker -> final shock in sigmas)."""
    shocks = shocks if shocks is not None else {"AAA": -3.0, "BBB": 3.0, "CCC": 0.0, "DDD": 0.0}
    dates = sessions(n_returns + 1)
    rb = etf_returns(n_returns)
    rows = close_rows(etf, rb, dates)
    for i, (ticker, shock) in enumerate(sorted(shocks.items())):
        eps = ou_increments(n_returns, b=b, seed=10 + i, final_shock_sigmas=shock)
        rows += close_rows(ticker, [beta * x + e for x, e in zip(rb, eps, strict=True)], dates)
    return rows, dates
