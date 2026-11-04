"""Hand-built toy inputs for the co-movement tests.

Nothing here is market data. Every close, return and dollar amount is invented to make
the expected outputs computable by hand, and the comment next to each test shows that
arithmetic. Tickers are placeholders (A1, B2, ...) on purpose, so no reader mistakes a
toy number for a real quote.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

# Day 0 of every toy calendar. Consecutive calendar days are used as "sessions": the
# functions under test only care about ordering, not about weekends.
D0 = date(2026, 1, 1)


def day(i: int) -> date:
    return D0 + timedelta(days=i)


def rows_from_closes(ticker: str, closes, start: int = 0, interpolated: bool | None = None) -> list[dict]:
    """One row per close, dated day(start), day(start + 1), ..."""
    rows = []
    for i, close in enumerate(closes):
        row = {"ticker": ticker, "date": day(start + i), "close": close}
        if interpolated is not None:
            row["interpolated"] = interpolated
        rows.append(row)
    return rows


def rows_from_returns(ticker: str, log_returns, start: int = 0, first_close: float = 100.0) -> list[dict]:
    """Closes chosen so that ln(close[t] / close[t-1]) == log_returns[t-1]."""
    closes = [first_close]
    for r in log_returns:
        closes.append(closes[-1] * math.exp(r))
    return rows_from_closes(ticker, closes, start=start)


# Three 8-session Walsh patterns. Each has mean zero, and every pair is orthogonal, so the
# Pearson correlation between any two patterns is exactly 0 and between a pattern and any
# positive multiple of itself is exactly 1 (up to float round-off from exp/log).
WALSH_P = (1, -1, 1, -1, 1, -1, 1, -1)
WALSH_Q = (1, 1, -1, -1, 1, 1, -1, -1)
WALSH_S = (1, 1, 1, 1, -1, -1, -1, -1)


def three_block_rows() -> list[dict]:
    """Nine tickers in three blocks of three. Block A follows WALSH_P scaled by 1%, 2% and
    1.5% per session; block B follows WALSH_Q; block C follows WALSH_S. Within a block the
    correlation is 1 (distance 0); across blocks it is 0 (distance 1). 9 closes on
    day(0)..day(8) give 8 returns on day(1)..day(8)."""
    rows: list[dict] = []
    for block, pattern in (("A", WALSH_P), ("B", WALSH_Q), ("C", WALSH_S)):
        for k, scale in enumerate((0.01, 0.02, 0.015), start=1):
            rows += rows_from_returns(f"{block}{k}", [scale * x for x in pattern])
    return rows


THREE_BLOCKS = (("A1", "A2", "A3"), ("B1", "B2", "B3"), ("C1", "C2", "C3"))
