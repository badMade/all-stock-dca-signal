"""Seeded synthetic closes and baseline cash flows for the harness tests. Not market data.

An ETF random walk, and tickers built as beta * ETF return + increments of an
Ornstein-Uhlenbeck residual, so residual reversal is genuinely present and the
residual_reversal suggester has something to fit."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

START = date(2024, 1, 1)  # a Monday
ETF = "XLK"


def sessions(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def universe(n_sessions: int = 260, tickers: tuple[str, ...] = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
             seed: int = 7, b: float = 0.85, resid_sigma: float = 0.012,
             beta: float = 1.0) -> tuple[list[dict], list[date]]:
    rng = random.Random(seed)
    dates = sessions(n_sessions)
    etf_r = [rng.gauss(0.0003, 0.01) for _ in range(n_sessions - 1)]
    rows = []
    price = 100.0
    rows.append({"ticker": ETF, "date": dates[0].isoformat(), "close": price})
    for d, r in zip(dates[1:], etf_r, strict=True):
        price *= math.exp(r)
        rows.append({"ticker": ETF, "date": d.isoformat(), "close": price})
    for ticker in tickers:
        x_prev, price = 0.0, 50.0 + 10 * rng.random()
        rows.append({"ticker": ticker, "date": dates[0].isoformat(), "close": price})
        for d, r in zip(dates[1:], etf_r, strict=True):
            x = b * x_prev + resid_sigma * rng.gauss(0.0, 1.0)
            price *= math.exp(beta * r + (x - x_prev))
            x_prev = x
            rows.append({"ticker": ticker, "date": d.isoformat(), "close": price})
    return rows, dates


def graded_baseline(rows: list[dict], dates: list[date], tickers: tuple[str, ...], first: int = 120,
                    usd: float = 10.0) -> dict:
    """A stand-in for the graded baseline: $usd per ticker per session from ``first`` on,
    doubled when the ticker's previous close was below the one before (a crude dip ladder
    that only reads the past, so baseline dollars vary)."""
    closes = closes_by(rows)
    out = {}
    for i in range(first, len(dates)):
        out[dates[i]] = {
            t: usd * (2.0 if closes[(t, dates[i - 1])] < closes[(t, dates[i - 2])] else 1.0) for t in tickers
        }
    return out


def closes_by(rows: list[dict]) -> dict[tuple[str, date], float]:
    return {(r["ticker"], date.fromisoformat(r["date"])): r["close"] for r in rows}
