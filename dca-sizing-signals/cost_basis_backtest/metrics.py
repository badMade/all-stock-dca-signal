"""Pure metric functions over replayed fills. No I/O, no state.

Conventions used throughout (docs/adr-cost-basis-backtest-harness.md, "Metrics"):

* ``d`` = candidate dollars minus baseline dollars for one (session, ticker) fill: the tilt
  dollars. A budget-neutral candidate's ``d`` sums to zero each session.
* Basis points are 1e4 x a fraction. Positive is always "candidate better".
* Forward returns start at the fill close on session t and end at the close exactly H
  calendar sessions later. A ticker with no close on that session has no forward return
  there (zero quote tolerance, as in the ledger); it is skipped, never filled from an older
  close.
* Forward returns from daily decisions overlap (a 20-session return is sampled every
  session), so naive t-stats on them are overstated. Only the cost-basis t-stat, which is
  across tickers, is reported.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from cost_basis_backtest.view import PriceIndex


@dataclass(frozen=True)
class FillRecord:
    """One (session, ticker) replayed in both arms at the same close."""

    session: date
    ticker: str
    price: float
    base_usd: float
    cand_usd: float
    base_shares: float
    cand_shares: float
    multiplier: float
    score: float | None
    hedge: tuple[str, float] | None

    @property
    def tilt_usd(self) -> float:
        return self.cand_usd - self.base_usd


def _bp(x: float) -> float:
    return x * 1e4


def rankdata(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = rankdata(xs), rankdata(ys)
    try:
        return statistics.correlation(rx, ry)
    except statistics.StatisticsError:  # a constant input
        return None


# ------------------------------------------------------------------------- cost basis


def cost_basis(fills: Iterable[FillRecord]) -> dict[str, object]:
    """Paired per-ticker cost-basis improvement, the primary metric.

    Cost basis = dollars spent (one-way costs included) / shares bought, per arm. A ticker
    the candidate bought no shares of (every multiplier 0) has no cost basis and is listed
    under ``excluded``. Improvement = (baseline - candidate) / baseline in bp.
    """
    usd: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for f in fills:
        acc = usd[f.ticker]
        acc[0] += f.base_usd
        acc[1] += f.base_shares
        acc[2] += f.cand_usd
        acc[3] += f.cand_shares
    per_ticker: dict[str, dict[str, float]] = {}
    excluded: list[str] = []
    for ticker in sorted(usd):
        b_usd, b_sh, c_usd, c_sh = usd[ticker]
        if b_sh <= 0.0 or c_sh <= 0.0:
            excluded.append(ticker)
            continue
        cb_b, cb_c = b_usd / b_sh, c_usd / c_sh
        per_ticker[ticker] = {
            "baseline_cost_basis": cb_b,
            "candidate_cost_basis": cb_c,
            "improvement_bp": _bp((cb_b - cb_c) / cb_b),
            "baseline_usd": b_usd,
            "candidate_usd": c_usd,
        }
    improvements = [v["improvement_bp"] for v in per_ticker.values()]
    weights = [v["baseline_usd"] for v in per_ticker.values()]
    aggregate: dict[str, object] = {"n_tickers": len(improvements)}
    if improvements:
        aggregate["mean_bp"] = statistics.fmean(improvements)
        aggregate["median_bp"] = statistics.median(improvements)
        aggregate["dollar_weighted_mean_bp"] = math.fsum(i * w for i, w in zip(improvements, weights,
                                                                               strict=True)) / math.fsum(weights)
        aggregate["share_improved"] = sum(1 for i in improvements if i > 0.0) / len(improvements)
        if len(improvements) >= 2:
            sd = statistics.stdev(improvements)
            aggregate["t_stat"] = (aggregate["mean_bp"] / (sd / math.sqrt(len(improvements)))) if sd > 0 else None
    return {"per_ticker": per_ticker, "aggregate": aggregate, "excluded": excluded}


def terminal_wealth(fills: Sequence[FillRecord], index: PriceIndex, end: date) -> dict[str, float | None]:
    """Value at ``end`` (last close on or before it) of the shares bought, per dollar spent."""
    b_val = c_val = b_usd = c_usd = 0.0
    for f in fills:
        p = index.last_close_on_or_before(f.ticker, end)
        if p is None:  # cannot happen for a filled ticker; kept explicit
            continue
        b_val += f.base_shares * p
        c_val += f.cand_shares * p
        b_usd += f.base_usd
        c_usd += f.cand_usd
    b = b_val / b_usd if b_usd > 0 else None
    c = c_val / c_usd if c_usd > 0 else None
    return {"baseline": b, "candidate": c, "difference_bp": _bp(c - b) if b is not None and c is not None else None}


# ------------------------------------------------------------------------- tilt dollars


def _forward_price(index: PriceIndex, ticker: str, session: date, horizon: int | None, end: date) -> float | None:
    if horizon is None:
        return index.last_close_on_or_before(ticker, end)
    target = index.session_offset(session, horizon)
    return None if target is None else index.close_on(ticker, target)


def tilt_forward_return(fills: Sequence[FillRecord], index: PriceIndex, horizon: int | None, end: date,
                        buy_cost_bps: float) -> dict[str, float | int | None]:
    """Net return of the tilt dollars over ``horizon`` sessions (None = to ``end``), per
    gross tilt dollar: sum(d * R) / sum(|d|). R includes the one-way buy cost, so an extra
    dollar pays it and a removed dollar saves it."""
    keep = 1.0 - buy_cost_bps / 1e4
    num = den = 0.0
    n = 0
    for f in fills:
        d = f.tilt_usd
        if d == 0.0:
            continue
        p = _forward_price(index, f.ticker, f.session, horizon, end)
        if p is None:
            continue
        num += d * (keep * p / f.price - 1.0)
        den += abs(d)
        n += 1
    return {"return_bp": _bp(num / den) if den > 0 else None, "n_tilts": n, "gross_tilt_usd": den}


def hit_rate(fills: Sequence[FillRecord], index: PriceIndex, horizon: int) -> dict[str, float | int | None]:
    """Share of tilts whose sign matches the ticker's forward return relative to that
    session's equal-weight mean forward return of every filled ticker. Relative, because a
    budget-neutral tilt can only win by picking which names, not by market timing."""
    by_day: dict[date, list[tuple[FillRecord, float]]] = defaultdict(list)
    for f in fills:
        target = index.session_offset(f.session, horizon)
        p = None if target is None else index.close_on(f.ticker, target)
        if p is not None:
            by_day[f.session].append((f, p / f.price - 1.0))
    hits = n = 0
    for rows in by_day.values():
        mean = statistics.fmean(r for _, r in rows)
        for f, r in rows:
            d, excess = f.tilt_usd, r - mean
            if d == 0.0 or excess == 0.0:
                continue
            n += 1
            hits += (d > 0.0) == (excess > 0.0)
    return {"rate": hits / n if n else None, "n": n}


def turnover(fills: Sequence[FillRecord]) -> dict[str, float | None]:
    """``gross_tilt_fraction``: sum |d| / sum baseline dollars (share of the budget moved).
    ``mean_multiplier_change``: mean |m_t - m_prev| over tickers filled on two consecutive
    decision sessions -- how much the tilt itself churns."""
    base = math.fsum(f.base_usd for f in fills)
    gross = math.fsum(abs(f.tilt_usd) for f in fills)
    by_day: dict[date, dict[str, float]] = defaultdict(dict)
    for f in fills:
        by_day[f.session][f.ticker] = f.multiplier
    days = sorted(by_day)
    changes = [abs(by_day[cur][t] - by_day[prev][t])
               for prev, cur in zip(days, days[1:], strict=False) for t in by_day[cur] if t in by_day[prev]]
    return {
        "gross_tilt_fraction": gross / base if base > 0 else None,
        "mean_multiplier_change": statistics.fmean(changes) if changes else None,
    }


# ------------------------------------------------------------------------- score buckets


def bucket_label(lo: float, hi: float) -> str:
    def fmt(x: float) -> str:
        return "-inf" if x == -math.inf else "inf" if x == math.inf else f"{x:g}"
    return f"[{fmt(lo)}, {fmt(hi)})"


def forward_residuals(fills: Sequence[FillRecord], index: PriceIndex, horizon: int) -> list[tuple[float, float]]:
    """(score, forward residual log return) for every scored fill with forward data.

    With a hedge (benchmark, beta): ln(P'/P) - beta * ln(B'/B) over the same two sessions.
    Without one: ln(P'/P) minus the mean over that session's other unhedged scored tickers
    (needs at least two of them)."""
    out: list[tuple[float, float]] = []
    unhedged: dict[date, list[tuple[float, float]]] = defaultdict(list)
    for f in fills:
        if f.score is None:
            continue
        target = index.session_offset(f.session, horizon)
        p = None if target is None else index.close_on(f.ticker, target)
        if p is None:
            continue
        log_r = math.log(p / f.price)
        if f.hedge is not None:
            bench, beta = f.hedge
            b0, b1 = index.close_on(bench, f.session), index.close_on(bench, target)
            if b0 is None or b1 is None:
                continue
            out.append((f.score, log_r - beta * math.log(b1 / b0)))
        else:
            unhedged[f.session].append((f.score, log_r))
    for rows in unhedged.values():
        if len(rows) < 2:
            continue
        mean = statistics.fmean(r for _, r in rows)
        out.extend((s, r - mean) for s, r in rows)
    return out


def score_buckets(points: Sequence[tuple[float, float]], edges: Sequence[float], min_count: int,
                  score_sign: int) -> dict[str, object]:
    """Mean forward residual return (bp) per score bucket, and whether the means are
    strictly monotone in the expected direction (``score_sign`` -1: falling as the score
    rises, the residual-reversal hypothesis). ``monotone`` is None when any bucket has
    fewer than ``min_count`` points -- too thin to pass, which is not the same as failing."""
    bounds = [-math.inf, *edges, math.inf]
    buckets = []
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        vals = [r for s, r in points if lo <= s < hi]
        buckets.append({"bucket": bucket_label(lo, hi), "n": len(vals),
                        "mean_bp": _bp(statistics.fmean(vals)) if vals else None})
    monotone: bool | None
    if any(b["n"] < min_count for b in buckets):
        monotone = None
    else:
        means = [b["mean_bp"] for b in buckets]
        pairs = list(zip(means, means[1:], strict=False))
        monotone = all((b < a) if score_sign < 0 else (b > a) for a, b in pairs)
    rho = spearman([s for s, _ in points], [r for _, r in points])
    return {"buckets": buckets, "monotone": monotone, "spearman": rho, "n": len(points)}


# ------------------------------------------------------------------------- active returns


def worst_rolling(active: Sequence[tuple[date, float, float]], window: int) -> float | None:
    """Worst ``window``-session relative return of candidate vs baseline, in bp:
    min over windows of prod(1 + r_c) / prod(1 + r_b) - 1. ``active`` rows are
    (session, r_baseline, r_candidate). None if the series is shorter than the window."""
    if len(active) < window:
        return None
    cum = [0.0]
    for _, rb, rc in active:
        cum.append(cum[-1] + math.log1p(rc) - math.log1p(rb))
    return _bp(math.expm1(min(cum[i + window] - cum[i] for i in range(len(cum) - window))))


def lookahead_ic(fills: Sequence[FillRecord], index: PriceIndex) -> dict[str, dict[str, float | int | None]]:
    """Canary for a candidate that sees returns it could not have known.

    Per decision session t with at least three filled tickers and a non-constant tilt:
    Spearman IC of (multiplier - 1) against the return into the fill (close t-1 to close t,
    unknown at the decision) and the return after it (close t to t+1). A genuine daily
    equity signal has a mean IC of a few hundredths; an oracle is near 1. A peek that goes
    through the view is rejected structurally; this catches one that does not (a closure
    over the full dataset)."""
    out: dict[str, dict[str, float | int | None]] = {}
    for name, (a, b) in {"into_fill": (-1, 0), "after_fill": (0, 1)}.items():
        by_day: dict[date, list[tuple[float, float]]] = defaultdict(list)
        for f in fills:
            sa, sb = index.session_offset(f.session, a), index.session_offset(f.session, b)
            pa = None if sa is None else index.close_on(f.ticker, sa)
            pb = None if sb is None else index.close_on(f.ticker, sb)
            if pa is not None and pb is not None:
                by_day[f.session].append((f.multiplier - 1.0, pb / pa - 1.0))
        ics = [ic for rows in by_day.values()
               if (ic := spearman([m for m, _ in rows], [r for _, r in rows])) is not None]
        out[name] = {"mean_ic": statistics.fmean(ics) if ics else None, "n_days": len(ics)}
    return out


def select(fills: Sequence[FillRecord], keep: Callable[[date], bool]) -> list[FillRecord]:
    return [f for f in fills if keep(f.session)]
