"""Seeded synthetic price paths for the kalman_signals tests.

Nothing here is market data. Tickers are placeholders (MKT, S1, ...) so no reader mistakes
a synthetic number for a quote. Each stock follows r = beta * r_market + idiosyncratic noise,
split into an overnight and an intraday part, so the filter has a known beta to recover.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from datetime import date, timedelta

from kalman_signals.residual_dip import ResidualDipConfig

D0 = date(2023, 1, 2)  # a Monday
MKT = "MKT"
#: Small but realistic test config: 250-session window, 40-session burn-in.
SMALL = ResidualDipConfig(market_ticker=MKT, window_sessions=250, burn_in=40, min_train=80)


def weekdays(n: int, start: date = D0) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def generate(
    n_sessions: int,
    betas: Mapping[str, float],
    seed: int = 7,
    market_sd: float = 0.01,
    idio_sd: float = 0.015,
    beta_path: Mapping[str, list[float]] | None = None,
    common: Mapping[str, str] | None = None,
    common_sd: float = 0.0,
) -> tuple[list[date], list[dict]]:
    """(sessions, rows). ``common`` maps ticker -> group name; members of a group share an
    extra common shock with sd ``common_sd`` (a co-movement cluster)."""
    rng = random.Random(seed)
    sessions = weekdays(n_sessions)
    prices = {t: 100.0 for t in [MKT, *betas]}
    rows: list[dict] = []
    for i, d in enumerate(sessions):
        if i == 0:
            for t, p in prices.items():
                rows.append({"ticker": t, "date": d, "open": p, "close": p, "final": True})
            continue
        m_on, m_in = rng.gauss(0, market_sd * 0.5), rng.gauss(0, market_sd * math.sqrt(0.75))
        shocks = {g: (rng.gauss(0, common_sd * 0.5), rng.gauss(0, common_sd * math.sqrt(0.75)))
                  for g in set((common or {}).values())}
        moves = {MKT: (m_on, m_in)}
        for t, beta in betas.items():
            b = beta_path[t][i] if beta_path and t in beta_path else beta
            g = (common or {}).get(t)
            c_on, c_in = shocks[g] if g else (0.0, 0.0)
            moves[t] = (b * m_on + c_on + rng.gauss(0, idio_sd * 0.5),
                        b * m_in + c_in + rng.gauss(0, idio_sd * math.sqrt(0.75)))
        for t, (on, intra) in moves.items():
            opened = prices[t] * math.exp(on)
            prices[t] = opened * math.exp(intra)
            rows.append({"ticker": t, "date": d, "open": opened, "close": prices[t], "final": True})
    return sessions, rows


def replace_bar(rows: list[dict], ticker: str, d: date, *, open_ret: float, intraday_ret: float) -> list[dict]:
    """Rewrite ticker's bar on ``d`` as prior close * exp(open_ret), then * exp(intraday_ret)."""
    prior = max((r for r in rows if r["ticker"] == ticker and r["date"] < d), key=lambda r: r["date"])
    out = []
    for r in rows:
        if r["ticker"] == ticker and r["date"] == d:
            opened = prior["close"] * math.exp(open_ret)
            r = {**r, "open": opened, "close": opened * math.exp(intraday_ret)}
        out.append(r)
    return out
