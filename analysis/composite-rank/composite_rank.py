#!/usr/bin/env python3
"""Daily composite ranking: weighted multi-period ROI, low-volatility tilt, and rank-movement tracking.

READ-ONLY research tool. It never places orders, never touches config/ or any live strategy
module, and writes only under its own data directory (default ``artifacts/composite_rank/``, which
is gitignored). Standard library only.

Score (per ticker, all numbers as of the latest price date):

  period score = 0.6 * percentile(total return in the period, across the whole pool)
               + 0.4 * return score  (annualised ROI / 40%, clamped to 0..100)
               + 5   if annualised ROI >= 20%
  base score   = weighted average of the period scores that exist
                 (1y 6, 1m 5, 1w 4, 3m 3, 6m 2, 5y 1)
  final score  = 0.70 * base + 0.20 * low-volatility percentile + 0.10 * shallow-drawdown percentile

Volatility and drawdown use the trailing 365 days. Returns use dividend-adjusted closes
(the ``Adj Close`` column of the ticker archive), so reinvested dividends count as profit.

Daily flow (see RUNBOOK.md): the scheduled agent writes ``inputs/holdings.json`` (and
``inputs/ext_prices.json`` for tickers the archive lacks), then runs ``run``. Every run appends the
day's ranks to ``history.csv`` and rebuilds ``movement_summary`` from that history.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import html
import json
import math
import os
import re
import statistics
import sys
from pathlib import Path

PERIOD_DAYS = {"1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365, "5y": 1826}
PERIOD_WEIGHTS = {"1y": 6, "1m": 5, "1w": 4, "3m": 3, "6m": 2, "5y": 1}
PERIOD_ORDER = ("1y", "1m", "1w", "3m", "6m", "5y")
RISK_WINDOW_DAYS = 365
MIN_RISK_OBS = 120
MAX_GAP_DAYS = 7  # a period start price may sit at most this many days before the target date
STALE_DAYS = 5  # a series whose last bar is older than as_of - STALE_DAYS is excluded
BLEND = {"base": 0.70, "lowvol": 0.20, "lowdd": 0.10}
HURDLE_ANNUALISED = 0.20
RETURN_SCORE_CAP = 0.40
HURDLE_BONUS = 5.0
BAND_EDGES = (10, 25, 50, 100, 200)  # rank bands used by the movement summary

# Deliberately no "held" column: the long-term log is pure market data. Holdings live in inputs/holdings.json only.
HISTORY_FIELDS = ["date", "ticker", "score", "rank", "universe_size", "vol", "max_dd", "eligible"]

# The daily trade list: everything above the evaluate threshold, plus holdings above the held threshold.
# The ``reason`` column carries the only trace of holdings (held names between the two thresholds).
TRADE_LIST_FIELDS = ["date", "ticker", "rank", "score", "reason", "params_version"]

DEFAULT_PARAMS = {
    "version": "builtin",
    "period_weights": dict(PERIOD_WEIGHTS),
    "blend": dict(BLEND),
    "percentile_share": 0.6,
    "hurdle_annualised": HURDLE_ANNUALISED,
    "hurdle_bonus": HURDLE_BONUS,
    "return_score_cap": RETURN_SCORE_CAP,
    "evaluate_threshold": 80.0,
    "held_threshold": 50.0,
    # Share of the final score taken from each forward-looking model's percentile. ZERO = shadow mode: the signal is
    # logged and measured (shadow_octopus_history.csv) but cannot change the score or the list.
    "forward_weights": {"octopus": 0.0, "ml": 0.0},
    # Liquidity hygiene, applied only with --universe archive. Everything is still ranked; names that fail are flagged
    # ``eligible = 0`` and cannot enter the buy list (held names keep their held-floor rule). Percentiles are computed
    # against the ELIGIBLE pool so thousands of illiquid names do not stretch the scale. Floors follow the pool in
    # docs/adr-dynamic-universe.md (price > $5, 60-day MEDIAN dollar volume > $20M). That ADR also asks for market cap
    # > $2B, fractional eligibility and 252 days of history; this tool has no market-cap or tradability feed, so those
    # are not applied here (risk stats already need ~120 bars).
    "liquidity": {"min_median_dollar_volume": 20_000_000.0, "min_price": 5.0, "window": 60},
}
SHADOW_FIELDS = ["origin_date", "ticker", "exp_ret", "p10_ret", "p90_ret", "expected_target_date", "fwd_pct", "model"]
FORWARD_SIGNALS = ("octopus", "ml")
SHADOW_ML_FIELDS = ["date", "ticker", "tilt", "fwd_pct", "status", "model_version"]
ML_HORIZON_DAYS = 7  # ml-forecaster targets the forward 5 trading days, about 7 calendar days
PARAMS_PATH = Path(__file__).resolve().with_name("params.json")


def load_params(path=None):
    """Defaults overlaid with ``params.json`` (or ``path``); a bad file raises instead of silently scoring wrong."""
    params = json.loads(json.dumps(DEFAULT_PARAMS))
    path = Path(path) if path else PARAMS_PATH
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        for key, value in raw.items():
            if key in ("period_weights", "blend", "forward_weights", "liquidity"):
                params[key].update(value)
            elif key in params:
                params[key] = value
    if set(params["period_weights"]) != set(PERIOD_ORDER) or any(w < 0 for w in params["period_weights"].values()):
        raise ValueError("period_weights must cover exactly " + ", ".join(PERIOD_ORDER) + " with weights >= 0")
    blend_total = sum(params["blend"].values())
    if sum(params["period_weights"].values()) <= 0 or not math.isclose(blend_total, 1.0, abs_tol=1e-6):
        raise ValueError("period_weights must not all be zero and blend must sum to 1")
    fw = params["forward_weights"]
    if set(fw) != set(FORWARD_SIGNALS) or any(w < 0 for w in fw.values()) or sum(fw.values()) > 1.0:
        raise ValueError("forward_weights must cover " + ", ".join(FORWARD_SIGNALS)
                         + " with weights >= 0 summing to <= 1")
    liq = params["liquidity"]
    if liq["min_median_dollar_volume"] < 0 or liq["min_price"] < 0 or int(liq["window"]) < 1:
        raise ValueError("liquidity floors must be >= 0 and window >= 1")
    if params["held_threshold"] > params["evaluate_threshold"]:
        raise ValueError("held_threshold must not exceed evaluate_threshold")
    return params


# ----------------------------------------------------------------------------------------------
# Price series
# ----------------------------------------------------------------------------------------------
class Series:
    """Sorted (date, adjusted close) pairs with bisect lookups."""

    def __init__(self, points, liquidity=None):
        merged = {}
        for day, value in points:
            if value is not None and value > 0 and math.isfinite(value):
                merged[day] = value
        self.dates = sorted(merged)
        self.values = [merged[d] for d in self.dates]
        self.liquidity = liquidity or {}  # {date: (raw close, dollar volume)}; empty when the source has no volume

    def liquidity_stats(self, window):
        """(median dollar volume, last raw close) over the last ``window`` bars that carry volume, else None."""
        bars = [self.liquidity[d] for d in self.dates[-window:] if d in self.liquidity]
        if not bars:
            return None
        return statistics.median(dv for _, dv in bars), bars[-1][0]

    def __len__(self):
        return len(self.dates)

    @property
    def last_date(self):
        return self.dates[-1]

    @property
    def last_value(self):
        return self.values[-1]

    def value_at(self, day, max_gap=MAX_GAP_DAYS):
        idx = bisect.bisect_right(self.dates, day) - 1
        if idx < 0 or (day - self.dates[idx]).days > max_gap:
            return None
        return self.values[idx]

    def window(self, start):
        idx = bisect.bisect_left(self.dates, start)
        return self.values[idx:]

    def truncate(self, as_of):
        """Series with every bar after ``as_of`` removed (the archive may already hold a newer, partial day)."""
        idx = bisect.bisect_right(self.dates, as_of)
        clipped = Series([])
        clipped.dates = self.dates[:idx]
        clipped.values = self.values[:idx]
        clipped.liquidity = self.liquidity
        return clipped


def _parse_day(text):
    try:
        day = dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None
    return day if day.year >= 1900 else None  # a yearless "04-27" can parse as year 1 in some archive files


def load_archive_series(archive_dir, ticker):
    """Read ``<archive>/<TICKER>.csv`` (``BRK.B`` is stored as ``BRK-B``); None when missing."""
    path = Path(archive_dir) / f"{ticker.replace('.', '-')}.csv"
    if not path.exists():
        return None
    points, liquidity = [], {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = _parse_day(row.get("Date", ""))
            try:
                value = float(row.get("Adj Close", ""))
            except (TypeError, ValueError):
                continue
            if day is not None:
                points.append((day, value))
                try:  # raw close x share volume: adjusted closes would understate old dollar volume
                    close, volume = float(row.get("Close", "")), float(row.get("Volume", ""))
                    if close > 0 and volume >= 0 and math.isfinite(close * volume):
                        liquidity[day] = (close, close * volume)
                except (TypeError, ValueError):
                    pass
    return Series(points, liquidity) if points else None


def load_ext_series(ext_prices, ticker):
    raw = ext_prices.get(ticker)
    if not raw:
        return None
    points = []
    for day, value in raw:
        parsed = _parse_day(day)
        try:
            points.append((parsed, float(value)))
        except (TypeError, ValueError):
            continue
    points = [(d, v) for d, v in points if d is not None]
    return Series(points) if points else None


def load_series(archive_dir, ext_prices, ticker):
    """Archive first; fall back to (or prefer, when newer) the externally supplied bars."""
    arch = load_archive_series(archive_dir, ticker)
    ext = load_ext_series(ext_prices, ticker)
    if arch and ext:
        return ext if ext.last_date > arch.last_date + dt.timedelta(days=3) else arch
    return arch or ext


# ----------------------------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------------------------
def annualise(total_return, days):
    if total_return <= -0.999:
        return -1.0
    return (1.0 + total_return) ** (365.0 / days) - 1.0


def return_score(annualised, cap=RETURN_SCORE_CAP):
    return min(max(annualised / cap, 0.0), 1.0) * 100.0


def period_score(percentile, total_return, period, params=None):
    p = params or DEFAULT_PARAMS
    ann = annualise(total_return, PERIOD_DAYS[period])
    bonus = p["hurdle_bonus"] if ann >= p["hurdle_annualised"] else 0.0
    share = p["percentile_share"]
    return share * percentile + (1.0 - share) * return_score(ann, p["return_score_cap"]) + bonus


def percentiles(values, reference=None):
    """{key: 100 * (1 - (rank - 1) / (n - 1))} with rank 1 = largest value, ties share the best rank.

    With ``reference`` (a list of values) each key is ranked against that pool instead of ``values`` itself and the
    result is clamped to 0..100; used to place ineligible names on the eligible pool's scale.
    """
    pool = list(values.values()) if reference is None else list(reference)
    n = len(pool)
    negated = sorted(-v for v in pool)  # ascending, so bisect_left counts strictly larger values
    out = {}
    for key, value in values.items():
        rank = bisect.bisect_left(negated, -value) + 1
        pct = 100.0 if n <= 1 else 100.0 * (1.0 - (rank - 1) / (n - 1))
        out[key] = min(max(pct, 0.0), 100.0)
    return out


def pooled_percentiles(values, eligible):
    """Percentiles computed on the eligible names only; ineligible names are ranked against that pool."""
    pool = {k: v for k, v in values.items() if eligible.get(k, True)}
    rest = {k: v for k, v in values.items() if k not in pool}
    if not pool or not rest:
        return percentiles(values)
    out = percentiles(pool)
    out.update(percentiles(rest, reference=list(pool.values())))
    return out


def liquidity_flags(series_by_ticker, liquidity):
    """{ticker: (eligible, median dollar volume or None)}. ``liquidity`` None disables the filter (all eligible).

    A series with no volume data (e.g. external bars) cannot prove liquidity, so it is ineligible when the filter is on.
    """
    out = {}
    for ticker, series in series_by_ticker.items():
        stats = series.liquidity_stats(int(liquidity["window"])) if liquidity else None
        if not liquidity:
            out[ticker] = (True, stats[0] if stats else None)
        else:
            ok = bool(stats) and stats[0] >= liquidity["min_median_dollar_volume"] and stats[1] >= liquidity["min_price"]
            out[ticker] = (ok, stats[0] if stats else None)
    return out


def period_returns(series, as_of):
    out = {}
    last = series.last_value
    for period, days in PERIOD_DAYS.items():
        start = series.value_at(as_of - dt.timedelta(days=days))
        if start:
            out[period] = last / start - 1.0
    return out


def risk_stats(series, as_of):
    values = series.window(as_of - dt.timedelta(days=RISK_WINDOW_DAYS))
    if len(values) < MIN_RISK_OBS:
        return None
    logs = [math.log(b / a) for a, b in zip(values, values[1:], strict=False)]
    vol = statistics.stdev(logs) * math.sqrt(252)
    peak, worst = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return vol, worst


def apply_forward(historical, signals, weights):
    """Blend forward-model percentiles into the historical score. With zero weights (shadow mode) this returns
    ``historical`` unchanged. A ticker missing a signal keeps that signal's weight on the historical score."""
    used = {k: w for k, w in weights.items() if w > 0 and signals and k in signals}
    if not used:
        return historical
    return (1.0 - sum(used.values())) * historical + sum(w * signals[k] for k, w in used.items())


def score_universe(series_by_ticker, as_of, params=None, forward=None, liquidity=None):
    """Return {ticker: record} for every ticker with a 1y return and risk stats."""
    params = params or DEFAULT_PARAMS
    weights, blend = params["period_weights"], params["blend"]
    clipped = {t: s.truncate(as_of) for t, s in series_by_ticker.items() if s}
    fresh = {t: s for t, s in clipped.items() if len(s) and s.last_date >= as_of - dt.timedelta(days=STALE_DAYS)}
    returns = {t: period_returns(s, as_of) for t, s in fresh.items()}
    risk = {t: r for t, r in ((t, risk_stats(s, as_of)) for t, s in fresh.items()) if r}
    flags = liquidity_flags(fresh, liquidity)
    elig = {t: ok for t, (ok, _) in flags.items()}

    pct = {}
    for period in PERIOD_ORDER:
        pool = {t: r[period] for t, r in returns.items() if period in r}
        pct[period] = pooled_percentiles(pool, elig) if pool else {}
    low_vol = pooled_percentiles({t: -r[0] for t, r in risk.items()}, elig) if risk else {}
    low_dd = pooled_percentiles({t: r[1] for t, r in risk.items()}, elig) if risk else {}

    records = {}
    for ticker, rets in returns.items():
        if "1y" not in rets or ticker not in risk:
            continue
        num = den = 0.0
        for period in PERIOD_ORDER:
            if period in rets:
                num += weights[period] * period_score(pct[period][ticker], rets[period], period, params)
                den += weights[period]
        if den <= 0:
            continue
        base = num / den
        final = blend["base"] * base + blend["lowvol"] * low_vol[ticker] + blend["lowdd"] * low_dd[ticker]
        final = apply_forward(final, (forward or {}).get(ticker), params["forward_weights"])
        records[ticker] = {
            "score": final,
            "base_score": base,
            "lowvol_pct": low_vol[ticker],
            "lowdd_pct": low_dd[ticker],
            "vol": risk[ticker][0],
            "max_dd": risk[ticker][1],
            "returns": rets,
            "periods": len(rets),
            "eligible": elig[ticker],
            "adv": flags[ticker][1],
        }
    ranked = sorted(records, key=lambda t: (-records[t]["score"], t))
    for position, ticker in enumerate(ranked, 1):
        records[ticker]["rank"] = position
    for position, ticker in enumerate((t for t in ranked if records[t]["eligible"]), 1):
        records[ticker]["elig_rank"] = position
    return records, sorted(set(series_by_ticker) - set(records))


SCORE_MODELS = ("composite", "adr_price")
ADR_WINSOR = 3.0  # docs/adr-dynamic-universe.md: factors are winsorized at plus or minus 3
ADR_MOMENTUM_WINDOWS = (182, 365)  # 6- and 12-month lookbacks, each ending one month before ``as_of`` (skip-month)
ADR_SKIP_DAYS = 30


def _zscores(values, reference=None):
    """{key: z} against the mean/stdev of ``reference`` (default: all values), winsorized at +/- ADR_WINSOR."""
    ref = list((reference if reference is not None else values).values())
    if len(ref) < 2:
        return {k: 0.0 for k in values}
    mean, sd = statistics.fmean(ref), statistics.pstdev(ref)
    if sd <= 0:
        return {k: 0.0 for k in values}
    return {k: max(-ADR_WINSOR, min(ADR_WINSOR, (v - mean) / sd)) for k, v in values.items()}


def score_universe_adr_price(series_by_ticker, as_of, liquidity=None):
    """PRICE-ONLY SURROGATE of the dynamic-universe ADR's composite -- NOT the ADR's ranker.

    Keeps the ADR's two price-derived factors: momentum (average of the 6- and 12-month return, each ending one
    month before ``as_of``) and low volatility (negative 1-year daily volatility), each z-scored and winsorized at
    +/- 3, equal weight. Left out for lack of data here: gross profitability and value (EDGAR), sector neutralisation
    (no SIC codes in the archive) and the market-cap / fractional-eligibility / 252-day-history filters. z-scores are
    taken over the liquidity-eligible pool, so ineligible names are scored on the same scale but never move it.
    Returns the same ``(records, unscored)`` shape as ``score_universe`` so grading and backfill reuse it unchanged.
    """
    clipped = {t: s.truncate(as_of) for t, s in series_by_ticker.items() if s}
    fresh = {t: s for t, s in clipped.items() if len(s) and s.last_date >= as_of - dt.timedelta(days=STALE_DAYS)}
    flags = liquidity_flags(fresh, liquidity)
    skip_day = as_of - dt.timedelta(days=ADR_SKIP_DAYS)
    momentum, lowvol, risk = {}, {}, {}
    for ticker, series in fresh.items():
        recent = series.value_at(skip_day)
        stats = risk_stats(series, as_of)
        if not recent or not stats:
            continue
        legs = [recent / start - 1.0 for start in (series.value_at(as_of - dt.timedelta(days=d))
                                                    for d in ADR_MOMENTUM_WINDOWS) if start]
        if len(legs) != len(ADR_MOMENTUM_WINDOWS):
            continue  # the ADR scores a name only when its factors exist; here both momentum legs are required
        momentum[ticker], lowvol[ticker], risk[ticker] = statistics.fmean(legs), -stats[0], stats
    pool = {t for t in momentum if flags[t][0]} or set(momentum)
    z_mom = _zscores(momentum, {t: momentum[t] for t in pool})
    z_vol = _zscores(lowvol, {t: lowvol[t] for t in pool})
    records = {}
    for ticker in momentum:
        records[ticker] = {"score": 50.0 + 15.0 * (z_mom[ticker] + z_vol[ticker]) / 2.0, "vol": risk[ticker][0],
                           "max_dd": risk[ticker][1], "eligible": flags[ticker][0], "mom_z": z_mom[ticker],
                           "lowvol_z": z_vol[ticker]}
    ranked = sorted(records, key=lambda t: (-records[t]["score"], t))
    for position, ticker in enumerate(ranked, 1):
        records[ticker]["rank"] = position
    return records, sorted(set(series_by_ticker) - set(records))


# ----------------------------------------------------------------------------------------------
# History and movement tracking
# ----------------------------------------------------------------------------------------------
def read_history(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def upsert_history(path, run_date, records):
    """Replace any rows for ``run_date`` and append today's; returns the full row list.

    Rows written by older versions carried a ``held`` column; it is dropped on the next write.
    """
    rows = [r for r in read_history(path) if r["date"] != run_date.isoformat()]
    size = len(records)
    for ticker, rec in records.items():
        rows.append(
            {
                "date": run_date.isoformat(),
                "ticker": ticker,
                "score": f"{rec['score']:.3f}",
                "rank": str(rec["rank"]),
                "universe_size": str(size),
                "vol": f"{rec['vol']:.4f}",
                "max_dd": f"{rec['max_dd']:.4f}",
                "eligible": "1" if rec.get("eligible", True) else "0",
            }
        )
    rows.sort(key=lambda r: (r["date"], int(r["rank"])))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return rows


def band_of(rank):
    for edge in BAND_EDGES:
        if rank <= edge:
            low = 1 if edge == BAND_EDGES[0] else BAND_EDGES[BAND_EDGES.index(edge) - 1] + 1
            return f"{low}-{edge}"
    return f"{BAND_EDGES[-1] + 1}+"


def movement_summary(rows, threshold):
    """Per-ticker rank behaviour plus movement by rank band.

    ``move`` is positive when a ticker climbed (its rank number fell). Bands use the ticker's
    rank on the PREVIOUS run, so the table answers "from this position, how far does a name
    typically move next run?".
    """
    by_ticker = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append((row["date"], int(row["rank"]), float(row["score"])))
    dates = sorted({r["date"] for r in rows})
    tickers = {}
    band_moves = {}
    for ticker, series in by_ticker.items():
        series.sort()
        ranks = [r for _, r, _ in series]
        moves = [prev - cur for prev, cur in zip(ranks, ranks[1:], strict=False)]
        for (_, prev_rank, _), move in zip(series, moves, strict=False):
            band_moves.setdefault(band_of(prev_rank), []).append(move)
        above = [s >= threshold for _, _, s in series]
        tickers[ticker] = {
            "days": len(series),
            "last_rank": ranks[-1],
            "avg_rank": statistics.fmean(ranks),
            "rank_std": statistics.pstdev(ranks) if len(ranks) > 1 else 0.0,
            "best": min(ranks),
            "worst": max(ranks),
            "avg_abs_move": statistics.fmean(abs(m) for m in moves) if moves else 0.0,
            "max_abs_move": max((abs(m) for m in moves), default=0),
            "chg_1d": ranks[-2] - ranks[-1] if len(ranks) > 1 else 0,
            "chg_5d": ranks[-6] - ranks[-1] if len(ranks) > 5 else None,
            "chg_20d": ranks[-21] - ranks[-1] if len(ranks) > 20 else None,
            "days_above": sum(above),
            "entries": sum(1 for a, b in zip([False] + above, above, strict=False) if b and not a) if above else 0,
            "exits": sum(1 for a, b in zip(above, above[1:], strict=False) if a and not b),
        }
    bands = {}
    for band, moves in band_moves.items():
        bands[band] = {
            "samples": len(moves),
            "avg_abs_move": statistics.fmean(abs(m) for m in moves),
            "median_abs_move": statistics.median(abs(m) for m in moves),
            "pct_moved_10_plus": 100.0 * sum(1 for m in moves if abs(m) >= 10) / len(moves),
            "avg_signed_move": statistics.fmean(moves),
        }
    return {"dates": dates, "tickers": tickers, "bands": bands}


def _band_sort_key(label):
    return int(label.split("-")[0].rstrip("+"))


def render_movement_md(summary, threshold):
    dates = summary["dates"]
    lines = [f"# Rank movement ({len(dates)} runs: {dates[0] if dates else '-'} to {dates[-1] if dates else '-'})", ""]
    if len(dates) < 2:
        lines.append("Only one run recorded so far. Movement statistics start with the second run.")
        return "\n".join(lines) + "\n"
    lines += [
        "Move = positions climbed (+) or dropped (-) between consecutive runs. "
        "Bands are the ticker's rank on the previous run.",
        "",
        "## Typical movement by starting rank",
        "",
        "| Rank band | Samples | Avg abs move | Median abs move | % moving 10+ | Avg signed move |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for band in sorted(summary["bands"], key=_band_sort_key):
        b = summary["bands"][band]
        lines.append(
            f"| {band} | {b['samples']} | {b['avg_abs_move']:.1f} | {b['median_abs_move']:.1f} | "
            f"{b['pct_moved_10_plus']:.0f}% | {b['avg_signed_move']:+.1f} |"
        )
    lines += [
        "",
        f"## Per ticker (sorted by current rank; threshold {threshold:g})",
        "",
        "| Ticker | Rank | Avg rank | Rank std | Best | Worst | Avg abs move | Max move | Chg 1d | Chg 5d | Chg 20d "
        "| Days >= thr | Entries | Exits |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for ticker, t in sorted(summary["tickers"].items(), key=lambda kv: kv[1]["last_rank"]):
        c5 = "-" if t["chg_5d"] is None else f"{t['chg_5d']:+d}"
        c20 = "-" if t["chg_20d"] is None else f"{t['chg_20d']:+d}"
        lines.append(
            f"| {ticker} | {t['last_rank']} | {t['avg_rank']:.0f} | {t['rank_std']:.1f} | {t['best']} | {t['worst']} "
            f"| {t['avg_abs_move']:.1f} | {t['max_abs_move']} | {t['chg_1d']:+d} | {c5} | {c20} "
            f"| {t['days_above']} | {t['entries']} | {t['exits']} |"
        )
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------------
# Daily trade list, review-first proposal, effectiveness
# ----------------------------------------------------------------------------------------------
def build_trade_list(records, held, params):
    """[(ticker, reason)] best rank first: above the evaluate threshold, or held and above the held threshold."""
    hi, lo = params["evaluate_threshold"], params["held_threshold"]
    out = []
    for ticker in sorted(records, key=lambda t: records[t]["rank"]):
        score = records[ticker]["score"]
        if score > hi and not records[ticker].get("eligible", True) and ticker not in held:
            continue  # illiquid names are ranked but never enter the buy list; a held name keeps the rules below
        if score > hi:
            out.append((ticker, "above_eval_held" if ticker in held else "above_eval"))
        elif ticker in held and score > lo:
            out.append((ticker, "held_above_floor"))
    return out


def write_trade_list(run_dir, history_path, run_date, records, trade, params):
    """Per-run CSV plus an idempotent upsert into the append-only ``trade_list_history.csv``."""
    stamp = params["version"]
    rows = [{"date": run_date.isoformat(), "ticker": t, "rank": records[t]["rank"],
             "score": f"{records[t]['score']:.3f}", "reason": reason, "params_version": stamp} for t, reason in trade]
    for path in (Path(run_dir) / "trade_list.csv", Path(history_path)):
        old = [r for r in read_history(path) if r["date"] != run_date.isoformat()] if path == Path(history_path) else []
        merged = sorted(old + rows, key=lambda r: (r["date"], int(r["rank"])))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADE_LIST_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(merged)
    return rows


def build_proposal(run_date, trade, tickers_config, params):
    """Diff of today's trade list against config/tickers.json. Informational: nothing here edits config."""
    active = list(tickers_config.get("active", []))
    locked = set(tickers_config.get("excluded", [])) | set(tickers_config.get("whole_share", []))
    wanted = [t for t, _ in trade if t not in locked]
    add = [t for t in wanted if t not in set(active)]
    drop = [t for t in active if t not in set(wanted)]
    return {"as_of": run_date.isoformat(), "params_version": params["version"], "trade_list_size": len(trade),
            "active_now": len(active), "add": add, "stop_buying": drop, "keep": [t for t in active if t in set(wanted)],
            "skipped_excluded_or_whole_share": [t for t, _ in trade if t in locked]}


def render_proposal_md(proposal):
    lines = [
        f"# Proposed ticker change for {proposal['as_of']} (REVIEW FIRST: nothing has been applied)",
        "",
        f"- Daily trade list: {proposal['trade_list_size']} names; active universe now: {proposal['active_now']}",
        f"- Would add ({len(proposal['add'])}): {', '.join(proposal['add']) or 'none'}",
        f"- Would stop buying ({len(proposal['stop_buying'])}): {', '.join(proposal['stop_buying']) or 'none'}",
        "",
        "To accept: edit `config/tickers.json` and `near_close_dca.ACTIVE_TICKERS`, run "
        "`python -m scripts.generate_per_ticker_sizing`, regenerate the Perplexity prompts and "
        "`backup_planner.pyz`, update the universe-count tests, and open a draft PR. "
        "Ask Claude to do it with: `apply the 2026-MM-DD proposal`.",
        "",
    ]
    return "\n".join(lines)


DELISTED_GAP_DAYS = 30  # a series that stopped this long before the archive's newest bar is treated as delisted


def forward_returns(series_by_ticker, tickers, start, horizon, delist_loss=None, archive_end=None):
    """{ticker: return from start to start+horizon days} for tickers with bars on both ends.

    With ``delist_loss`` and ``archive_end`` a series whose last bar is more than DELISTED_GAP_DAYS older than the
    archive's newest date, and that ended inside the window, is treated as delisted: its return is the last close over
    the start close with ``delist_loss`` taken off (Shumway: about -30% for performance delistings). Without this,
    names that vanished drop out of every forward pool and the grade is survivorship-biased upward. Cash acquisitions
    look the same as failures here, so the fill is conservative for those.
    """
    end = start + dt.timedelta(days=horizon)
    out = {}
    for t in tickers:
        s = series_by_ticker.get(t)
        if not s:
            continue
        if s.last_date < end:
            delisted = (delist_loss is not None and archive_end is not None and s.last_date > start
                        and (archive_end - s.last_date).days > DELISTED_GAP_DAYS)
            a = s.value_at(start, max_gap=3) if delisted else None
            if a:
                out[t] = (s.last_value / a) * (1.0 - delist_loss) - 1.0
            continue
        a, b = s.value_at(start, max_gap=3), s.value_at(end, max_gap=3)
        if a and b:
            out[t] = b / a - 1.0
    return out


def effectiveness(trade_rows, series_by_ticker, universe, horizon):
    """Per trade-list date: mean forward return of the listed names vs the whole scored universe."""
    by_date = {}
    for r in trade_rows:
        by_date.setdefault(r["date"], []).append(r["ticker"])
    results = []
    for day in sorted(by_date):
        start = dt.date.fromisoformat(day)
        listed = forward_returns(series_by_ticker, by_date[day], start, horizon)
        pool = forward_returns(series_by_ticker, universe, start, horizon)
        if not listed or not pool:
            continue
        results.append({"date": day, "listed": len(listed), "list_return": statistics.fmean(listed.values()),
                        "universe_return": statistics.fmean(pool.values()),
                        "edge": statistics.fmean(listed.values()) - statistics.fmean(pool.values())})
    return results


# ----------------------------------------------------------------------------------------------
# Grading: does the ranking predict forward returns, and what does acting on it cost?
# ----------------------------------------------------------------------------------------------
GRADE_HORIZONS = (5, 20, 60)  # calendar days ahead
DEFAULT_TOP_N = 100
DEFAULT_COST_BPS = 20.0  # placeholder round-trip cost per replaced name; tune from real slippage, not evidence-backed


def scores_from_history(rows):
    """{date: {ticker: score}} and {date: {ticker, ...} eligible} from history.csv rows (blank eligible = eligible)."""
    scores, eligible = {}, {}
    for r in rows:
        scores.setdefault(r["date"], {})[r["ticker"]] = float(r["score"])
        if r.get("eligible", "") != "0":
            eligible.setdefault(r["date"], set()).add(r["ticker"])
    return scores, eligible


def _top_set(scores, eligible, top_n, previous=None, hold_rank=None):
    """The ``top_n`` best-scored eligible names. With ``hold_rank`` and a ``previous`` list, incumbents still ranked
    within ``hold_rank`` stay and the best-ranked non-members fill the remaining slots (a buy/hold buffer in the style
    of ``docs/adr-dynamic-universe.md``'s 75/150 rule; there is no separate entry rank because the list is always
    refilled to ``top_n`` in rank order). A hold_rank of ``top_n`` or less is the plain top-N.
    """
    ranked = sorted((t for t in scores if eligible is None or t in eligible), key=lambda t: (-scores[t], t))
    if not hold_rank or hold_rank <= top_n or not previous:
        return set(ranked[:top_n])
    keep = {t for t in ranked[:hold_rank] if t in previous}
    chosen = set(keep)
    for t in ranked:
        if len(chosen) >= top_n:
            break
        chosen.add(t)
    return chosen


def _quintile_means(pairs):
    """Mean forward return of each fifth of ``pairs`` (sorted ascending by score): Q1 = lowest score, Q5 = highest."""
    n = len(pairs)
    return [statistics.fmean(y for _, y in pairs[i * n // 5:(i + 1) * n // 5]) for i in range(5)]


def grade_scores(scores_by_date, series_by_ticker, horizon, top_n=DEFAULT_TOP_N, cost_bps=DEFAULT_COST_BPS,
                 eligible_by_date=None, min_names=30, delist_loss=None, hold_rank=None, non_overlap=False):
    """Per-date grade of a ranking at one horizon: rank IC, quintile spread, top-N edge vs the eligible pool, and
    turnover against the list ``horizon`` days earlier (a rebalance cadence of one horizon, so costs are not charged
    once per overlapping daily sample). Equal-weighted; no market-cap data exists here for value weighting.
    A date is graded only when its horizon has fully elapsed in the price data and at least ``min_names`` names have
    a forward return.

    ``non_overlap`` grades only dates at least one horizon after the previously graded one, so samples do not overlap
    (the t-stat is then honest) and turnover is exactly the rebalance turnover. ``hold_rank`` applies the buy/hold
    buffer of ``_top_set`` along that same date chain.
    """
    dates = sorted(scores_by_date)
    if non_overlap:
        picked = []
        for d in dates:
            if not picked or (dt.date.fromisoformat(d) - dt.date.fromisoformat(picked[-1])).days >= horizon:
                picked.append(d)
        dates = picked
    archive_end = max((s.last_date for s in series_by_ticker.values() if s), default=None)
    top_sets, previous = {}, None
    for d in dates:
        previous = top_sets[d] = _top_set(scores_by_date[d], (eligible_by_date or {}).get(d), top_n, previous, hold_rank)
    rows = []
    for day in dates:
        scores = scores_by_date[day]
        pool_names = (eligible_by_date or {}).get(day)
        pool = [t for t in scores if pool_names is None or t in pool_names]
        origin = dt.date.fromisoformat(day)
        fwd = forward_returns(series_by_ticker, pool, origin, horizon, delist_loss, archive_end)
        if len(fwd) < min_names:
            continue
        pairs = sorted(((scores[t], fwd[t]) for t in fwd), key=lambda p: p[0])
        k = max(1, len(pairs) // 5)
        top_names = [t for t in top_sets[day] if t in fwd]
        if not top_names:
            continue
        pool_mean = statistics.fmean(fwd.values())
        top_mean = statistics.fmean(fwd[t] for t in top_names)
        prior = [d for d in dates if (dt.date.fromisoformat(day) - dt.date.fromisoformat(d)).days >= horizon]
        turnover = None
        if prior:
            base = top_sets[prior[-1]]
            turnover = 1.0 - len(base & top_sets[day]) / max(1, len(top_sets[day]))
        edge = top_mean - pool_mean
        ended = sum(1 for t in fwd if series_by_ticker[t].last_date < origin + dt.timedelta(days=horizon))
        rows.append({"date": day, "n": len(pairs), "delisted": ended, "ic": spearman(pairs), "top_return": top_mean,
                     "pool_return": pool_mean, "edge": edge,
                     "spread": statistics.fmean(y for _, y in pairs[-k:]) - statistics.fmean(y for _, y in pairs[:k]),
                     "quintiles": _quintile_means(pairs),
                     "turnover": turnover,
                     "net_edge": None if turnover is None else edge - turnover * cost_bps / 10000.0})
    return rows


def summarize_grade(rows):
    """Mean / dispersion of the per-date grade. t-stats ignore overlap between daily samples, so they overstate
    significance whenever the horizon exceeds one day; read them as a ranking aid, not a test."""
    def col(key):
        return [r[key] for r in rows if r[key] is not None]

    ics = col("ic")
    out = {"dates": len(rows), "mean_ic": None, "ic_std": None, "ic_positive_share": None, "ic_tstat": None}
    if ics:
        out["mean_ic"] = statistics.fmean(ics)
        out["ic_positive_share"] = sum(1 for v in ics if v > 0) / len(ics)
        if len(ics) > 1:
            out["ic_std"] = statistics.stdev(ics)
            if out["ic_std"] > 0:
                out["ic_tstat"] = out["mean_ic"] / (out["ic_std"] / math.sqrt(len(ics)))
    out["delisted_fills"] = sum(r.get("delisted", 0) for r in rows)
    out["mean_quintiles"] = [statistics.fmean(r["quintiles"][i] for r in rows) for i in range(5)] if rows else None
    for key in ("top_return", "pool_return", "edge", "spread", "turnover", "net_edge"):
        values = col(key)
        out[f"mean_{key}"] = statistics.fmean(values) if values else None
    return out


def render_grade(label, horizons_summary, top_n, cost_bps, hold_rank=None, non_overlap=False):
    def f(v, pct=False, digits=3):
        return "n/a" if v is None else (f"{v * 100:+.2f}%" if pct else f"{v:+.{digits}f}")

    mode = (f", hold-rank {hold_rank}" if hold_rank else "") + (", non-overlapping dates" if non_overlap else "")
    lines = [f"## {label}: top {top_n} eligible vs eligible pool (equal weight, cost {cost_bps:g} bps round trip{mode})", "",
             "| Horizon | Dates | Mean IC | IC std | IC>0 | IC t* | Top-N | Pool | Edge | Q5-Q1 | Turnover | Net edge |",
             "|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for horizon, sm in horizons_summary.items():
        share = "n/a" if sm["ic_positive_share"] is None else f"{sm['ic_positive_share'] * 100:.0f}%"
        turn = "n/a" if sm["mean_turnover"] is None else f"{sm['mean_turnover'] * 100:.0f}%"
        lines.append(f"| {horizon}d | {sm['dates']} | {f(sm['mean_ic'])} | {f(sm['ic_std'])} | {share} | "
                     f"{f(sm['ic_tstat'], digits=1)} | {f(sm['mean_top_return'], True)} | "
                     f"{f(sm['mean_pool_return'], True)} | {f(sm['mean_edge'], True)} | {f(sm['mean_spread'], True)} | "
                     f"{turn} | {f(sm['mean_net_edge'], True)} |")
    fills = sum(sm.get("delisted_fills", 0) for sm in horizons_summary.values())
    lines += ["", "Mean forward return by score quintile (Q1 = lowest score, Q5 = highest; whole eligible pool):", "",
              "| Horizon | Q1 | Q2 | Q3 | Q4 | Q5 |", "|--:|--:|--:|--:|--:|--:|"]
    for horizon, sm in horizons_summary.items():
        qs = sm.get("mean_quintiles")
        lines.append(f"| {horizon}d | " + (" | ".join(f(q, True) for q in qs) if qs else "n/a | n/a | n/a | n/a | n/a") + " |")
    note = ("samples do not overlap (--non-overlap), so t is a fair guide." if non_overlap else
            "t ignores overlap between daily samples (overstated for horizons above 1 day).")
    lines += ["", f"*{note}", f"Delisting-loss fills used across all rows: {fills}.", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# Shadow logging of forward-looking models (zero weight until they earn it)
# ----------------------------------------------------------------------------------------------
def find_octopus_files(sources):
    """Every ``*_next_day_forecasts.csv`` at or below the given files / directories."""
    found = []
    for source in sources:
        path = Path(source)
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(path.rglob("*_next_day_forecasts.csv")))
    return found


def read_octopus_forecasts(paths):
    """[{origin_date, ticker, exp_ret, p10_ret, p90_ret, expected_target_date, model}] from Octopus CSV exports."""
    rows = []
    for path in paths:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    ref = float(row["ref_close"])
                    exp = float(row["pred_close"]) / ref - 1.0
                    lo, hi = float(row["close_p10"]) / ref - 1.0, float(row["close_p90"]) / ref - 1.0
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    continue
                if not (math.isfinite(exp) and math.isfinite(lo) and math.isfinite(hi)):
                    continue
                rows.append({"origin_date": row.get("origin_date", ""), "ticker": row.get("ticker", ""),
                             "exp_ret": exp, "p10_ret": lo, "p90_ret": hi,
                             "expected_target_date": row.get("expected_target_date", ""),
                             "model": row.get("model", "octopus")})
    rows = [r for r in rows if r["origin_date"] and r["ticker"]]
    # An export also carries forecasts for tickers whose data ended long ago (origin dates years back); only the
    # current batch is a live forecast, so keep origins within a week of the newest one in the export.
    if rows:
        newest = max(r["origin_date"] for r in rows)
        cutoff = (dt.date.fromisoformat(newest) - dt.timedelta(days=7)).isoformat()
        rows = [r for r in rows if r["origin_date"] >= cutoff]
    return rows


def octopus_percentiles(forecasts, tickers, origin_date):
    """{ticker: percentile of expected 1-day return} among ``tickers`` forecast for ``origin_date``."""
    pool = {f["ticker"]: f["exp_ret"] for f in forecasts if f["origin_date"] == origin_date and f["ticker"] in tickers}
    return percentiles(pool) if pool else {}


def upsert_shadow(path, forecasts, pct_by_date):
    """Merge forecasts into ``shadow_octopus_history.csv`` keyed by (origin_date, ticker); returns the row count."""
    path = Path(path)
    merged = {(r["origin_date"], r["ticker"]): r for r in read_history_any(path)}
    for f in forecasts:
        pct = pct_by_date.get(f["origin_date"], {}).get(f["ticker"])
        merged[(f["origin_date"], f["ticker"])] = {
            "origin_date": f["origin_date"], "ticker": f["ticker"], "exp_ret": f"{f['exp_ret']:.5f}",
            "p10_ret": f"{f['p10_ret']:.5f}", "p90_ret": f"{f['p90_ret']:.5f}",
            "expected_target_date": f["expected_target_date"], "fwd_pct": "" if pct is None else f"{pct:.1f}",
            "model": f["model"]}
    rows = [merged[k] for k in sorted(merged)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHADOW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def read_history_any(path):
    return read_history(path)


def default_ml_root():
    """ml-forecaster's history root: ``$DCA_CRON_TRACKING_ROOT``/sizing_suggestions."""
    base = os.environ.get("DCA_CRON_TRACKING_ROOT") or str(Path(__file__).resolve().parents[2] / "cron_tracking")
    return Path(base) / "sizing_suggestions"


def read_ml_runs(root):
    """One entry per ml-forecaster run: {date, status, model_version, tilts{ticker: multiplier - 1}}.

    Reads ``<root>/ml_forecaster/*.jsonl`` (torn or malformed lines are skipped). The date is the run's source-data
    date (``generated_at``). Several runs on one date keep the one recorded last. Tilt is monotonic in the model's
    predicted forward return, so its rank is the forward signal.
    """
    best = {}
    directory = Path(root) / "ml_forecaster"
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
                day = str(rec["generated_at"])[:10]
                dt.date.fromisoformat(day)
                tilts = {str(t).upper(): float(m) - 1.0 for t, m in (rec.get("adjustments") or {}).items()
                         if math.isfinite(float(m))}
            except (ValueError, KeyError, TypeError):
                continue
            if tilts and (day not in best or str(rec.get("recorded_at", "")) >= best[day][0]):
                best[day] = (str(rec.get("recorded_at", "")),
                             {"date": day, "status": str(rec.get("status", "")),
                              "model_version": str(rec.get("model_version") or ""), "tilts": tilts})
    return [best[d][1] for d in sorted(best)]


def ml_percentiles(runs, tickers, as_of, max_age_days=5):
    """{ticker: percentile of tilt} from the newest run dated within ``max_age_days`` before ``as_of``."""
    eligible = [r for r in runs if 0 <= (as_of - dt.date.fromisoformat(r["date"])).days <= max_age_days]
    if not eligible:
        return {}
    pool = {t: v for t, v in eligible[-1]["tilts"].items() if t in tickers}
    return percentiles(pool) if pool else {}


def upsert_shadow_ml(path, runs):
    """Merge runs into ``shadow_ml_history.csv`` keyed by (date, ticker); returns the row count."""
    path = Path(path)
    merged = {(r["date"], r["ticker"]): r for r in read_history(path)}
    for run in runs:
        pcts = percentiles(run["tilts"]) if run["tilts"] else {}
        for ticker, tilt in run["tilts"].items():
            merged[(run["date"], ticker)] = {"date": run["date"], "ticker": ticker, "tilt": f"{tilt:.5f}",
                                             "fwd_pct": f"{pcts[ticker]:.1f}", "status": run["status"],
                                             "model_version": run["model_version"]}
    rows = [merged[k] for k in sorted(merged)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHADOW_ML_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def ml_scorecard(ml_rows, history_rows, series_by_ticker, horizon_days=ML_HORIZON_DAYS):
    """Per run date: rank correlation of ml-forecaster's tilt and of the composite score with the realised return over
    the next ``horizon_days`` calendar days (only dates whose horizon has fully elapsed in the price data)."""
    scores = {}
    for r in history_rows:
        scores.setdefault(r["date"], {})[r["ticker"]] = float(r["score"])
    by_date = {}
    for r in ml_rows:
        by_date.setdefault(r["date"], []).append(r)
    out = []
    for day in sorted(by_date):
        origin = _parse_day(day)
        target = origin + dt.timedelta(days=horizon_days) if origin else None
        pairs, comp = [], []
        for r in by_date[day]:
            series = series_by_ticker.get(r["ticker"])
            if not series or target is None or series.last_date < target:
                continue
            a, b = series.value_at(origin, max_gap=3), series.value_at(target, max_gap=3)
            if not a or not b:
                continue
            pairs.append((float(r["tilt"]), b / a - 1.0))
            if r["ticker"] in scores.get(day, {}):
                comp.append((scores[day][r["ticker"]], b / a - 1.0))
        if len(pairs) >= 5:
            out.append({"date": day, "n": len(pairs), "ml_ic": spearman(pairs), "composite_ic": spearman(comp)})
    return out


def spearman(pairs):
    """Rank correlation of (x, y) pairs; None when fewer than 5 pairs or no variation."""
    if len(pairs) < 5:
        return None
    rx = percentiles({i: x for i, (x, _) in enumerate(pairs)})
    ry = percentiles({i: y for i, (_, y) in enumerate(pairs)})
    xs, ys = [rx[i] for i in range(len(pairs))], [ry[i] for i in range(len(pairs))]
    if statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = statistics.fmean((a - mx) * (b - my) for a, b in zip(xs, ys, strict=False))
    return cov / (statistics.pstdev(xs) * statistics.pstdev(ys))


def shadow_scorecard(shadow_rows, history_rows, series_by_ticker):
    """Per origin date: rank correlation of Octopus's expected return and of the composite score with the realised
    return to the forecast's target date, plus the top-quintile minus bottom-quintile realised spread for Octopus."""
    scores = {}
    for r in history_rows:
        scores.setdefault(r["date"], {})[r["ticker"]] = float(r["score"])
    by_date = {}
    for r in shadow_rows:
        by_date.setdefault(r["origin_date"], []).append(r)
    out = []
    for day in sorted(by_date):
        pairs, comp = [], []
        for r in by_date[day]:
            series = series_by_ticker.get(r["ticker"])
            target = _parse_day(r["expected_target_date"])
            origin = _parse_day(day)
            if not series or target is None or origin is None or series.last_date < target:
                continue
            a, b = series.value_at(origin, max_gap=3), series.value_at(target, max_gap=3)
            if not a or not b:
                continue
            realised = b / a - 1.0
            pairs.append((float(r["exp_ret"]), realised))
            if r["ticker"] in scores.get(day, {}):
                comp.append((scores[day][r["ticker"]], realised))
        if len(pairs) < 5:
            continue
        ordered = sorted(pairs)
        k = max(1, len(ordered) // 5)
        spread = statistics.fmean(y for _, y in ordered[-k:]) - statistics.fmean(y for _, y in ordered[:k])
        out.append({"date": day, "n": len(pairs), "octopus_ic": spearman(pairs), "composite_ic": spearman(comp),
                    "octopus_spread": spread})
    return out


# ----------------------------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------------------------
def _pct(value):
    return "-" if value is None else f"{value * 100:+.0f}%"


def previous_run(rows, run_date):
    earlier = sorted({r["date"] for r in rows if r["date"] < run_date.isoformat()})
    if not earlier:
        return None, {}
    last = earlier[-1]
    return last, {r["ticker"]: (int(r["rank"]), float(r["score"])) for r in rows if r["date"] == last}


def render_evaluation_md(run_date, records, names, held, threshold, top, prev_date, prev, tradability):
    chosen = [t for t in sorted(records, key=lambda x: records[x]["rank"]) if records[t]["score"] > threshold]
    if top:
        chosen = [t for t in sorted(records, key=lambda x: records[x]["rank"])][:top]
    prev_chosen = {t for t, (_, s) in prev.items() if s > threshold} if not top else set()
    entered = [t for t in chosen if prev and t not in prev_chosen] if not top else []
    exited = sorted(prev_chosen - set(chosen)) if prev and not top else []
    label = f"top {top}" if top else f"score > {threshold:g}"
    lines = [
        f"# Evaluation list for {run_date.isoformat()} ({label})",
        "",
        f"- Names scored: {len(records)}; on the list: {len(chosen)} "
        f"({sum(1 for t in chosen if t in held)} held, {sum(1 for t in chosen if t not in held)} not held)",
    ]
    if prev_date:
        lines.append(f"- Compared with the previous run ({prev_date}): {len(entered)} new, {len(exited)} dropped off")
        if entered:
            lines.append(f"- New: {', '.join(entered)}")
        if exited:
            lines.append(f"- Dropped: {', '.join(exited)}")
    lines += [
        "",
        "| # | Ticker | Company | Held | Score | Rank chg | Vol | Max DD | 1y | 1m | 1w | 3m | 6m | 5y | Fractional |",
        "|--:|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|",
    ]
    for position, ticker in enumerate(chosen, 1):
        rec = records[ticker]
        change = "new" if prev_date and ticker not in prev else "-"
        if ticker in prev:
            change = f"{prev[ticker][0] - rec['rank']:+d}"
        r = rec["returns"]
        star = "*" if rec["periods"] < len(PERIOD_ORDER) else ""
        lines.append(
            f"| {position} | {ticker}{star} | {names.get(ticker, '')} | {'Yes' if ticker in held else 'No'} "
            f"| {rec['score']:.1f} | {change} | {rec['vol'] * 100:.0f}% | {rec['max_dd'] * 100:.0f}% | "
            + " | ".join(_pct(r.get(p)) for p in PERIOD_ORDER)
            + f" | {tradability.get(ticker, '?')} |"
        )
    lines += ["", "`*` = fewer than six periods of history; its score is re-weighted over the periods it has.", ""]
    return "\n".join(lines)


def write_ranked_csv(path, records, names, sectors, held):
    fields = ["rank", "ticker", "company", "sector", "held", "score", "base_score", "lowvol_pct", "lowdd_pct", "vol",
              "max_dd"] + [f"ret_{p}" for p in PERIOD_ORDER] + ["eligible", "elig_rank", "median_dollar_volume"]
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for ticker in sorted(records, key=lambda t: records[t]["rank"]):
            rec = records[ticker]
            writer.writerow(
                [rec["rank"], ticker, names.get(ticker, ""), sectors.get(ticker, ""), int(ticker in held),
                 f"{rec['score']:.3f}", f"{rec['base_score']:.3f}", f"{rec['lowvol_pct']:.1f}",
                 f"{rec['lowdd_pct']:.1f}", f"{rec['vol']:.4f}", f"{rec['max_dd']:.4f}"]
                + [("" if p not in rec["returns"] else f"{rec['returns'][p]:.4f}") for p in PERIOD_ORDER]
                + [int(rec.get("eligible", True)), rec.get("elig_rank", ""),
                   "" if rec.get("adv") is None else f"{rec['adv']:.0f}"]
            )


# ----------------------------------------------------------------------------------------------
# Input helpers (the scheduled agent produces these files)
# ----------------------------------------------------------------------------------------------
def parse_sp500_html(text):
    """Extract [(ticker, company, sector)] from a saved Wikipedia 'List of S&P 500 companies' page."""
    start = text.find('id="constituents"')
    if start < 0:
        raise ValueError("constituents table not found")
    end = text.find("</table>", start)
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text[start:end], re.S)[1:]:
        raw_cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in raw_cells]
        if len(cells) >= 3 and cells[0]:
            out.append((cells[0], cells[1], cells[2]))
    if len(out) < 400:
        raise ValueError(f"only {len(out)} constituents parsed; the page layout may have changed")
    return out


def ingest_bars(paths):
    """Convert get_equity_historicals result files into {ticker: [[date, close], ...]}."""
    out = {}
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for result in payload.get("data", {}).get("results", []):
            bars = [
                [b["begins_at"][:10], float(b["close_price"])]
                for b in result.get("bars", [])
                if not b.get("interpolated") and b.get("close_price")
            ]
            if bars:
                out[result["symbol"]] = bars
    return out


def _load_json(path, default):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


UNIVERSES = ("sp500", "all", "archive")


def build_universe(data_dir, mode="sp500", archive_dir=None):
    """Tickers to score, the held set (flag only), and name / sector lookups.

    ``sp500`` (default, the current scope) scores only S&P 500 constituents; holdings that are not
    constituents are ignored but held constituents are flagged. ``all`` also scores every holding and
    every ticker in ``inputs/watchlist.json`` (the planned wider universe).
    """
    if mode not in UNIVERSES:
        raise ValueError(f"unknown universe {mode!r}")
    inputs = Path(data_dir) / "inputs"
    holdings = _load_json(inputs / "holdings.json", {"symbols": []})
    held = set(holdings.get("symbols", []))
    sp = _load_json(inputs / "sp500.json", [])
    watch = set(_load_json(inputs / "watchlist.json", []))
    names = {t: n for t, n, _ in sp}
    sectors = {t: s for t, _, s in sp}
    names.update(holdings.get("names", {}))
    members = {t for t, _, _ in sp}
    if mode == "archive":
        # every ticker with a price file; BRK-B style stems stay as stored, so held/S&P flags use the archive spelling
        stems = {p.stem for p in Path(archive_dir).glob("*.csv")} if archive_dir else set()
        tickers = sorted(stems | held | watch)
    else:
        tickers = sorted(members if mode == "sp500" else members | held | watch)
    return tickers, held, names, sectors


def find_missing(tickers, archive_dir, ext_prices):
    return [t for t in tickers if load_series(archive_dir, ext_prices, t) is None]


def default_as_of(series):
    """The date most series end on, so one early-refreshed ticker cannot move the whole run."""
    counts = {}
    for s in series.values():
        counts[s.last_date] = counts.get(s.last_date, 0) + 1
    return max(counts, key=lambda d: (counts[d], d))


def prepare(args):
    data_dir = Path(args.data_dir)
    tickers, held, names, sectors = build_universe(data_dir, args.universe, args.archive_dir)
    if not tickers:
        raise SystemExit("error: no universe; run ingest-sp500 first (inputs/sp500.json)")
    ext = _load_json(data_dir / "inputs" / "ext_prices.json", {})
    series = {t: load_series(args.archive_dir, ext, t) for t in tickers}
    series = {t: s for t, s in series.items() if s}
    if not series:
        raise SystemExit("error: no price history found")
    return tickers, held, names, sectors, series


def run_once(args, tickers, held, names, sectors, series, as_of):
    """Score one date and write its outputs; returns the summary dict, or None when nothing scores."""
    data_dir = Path(args.data_dir)
    tradability = _load_json(data_dir / "inputs" / "tradability.json", {})
    params = load_params(getattr(args, "params", None))
    threshold = params["evaluate_threshold"] if args.threshold is None else args.threshold
    args.threshold = threshold
    sources = _load_json(data_dir / "inputs" / "octopus_sources.json", [])
    octopus = read_octopus_forecasts(find_octopus_files(sources)) if sources else []
    forward = {}
    for ticker, pct in octopus_percentiles(octopus, set(series), as_of.isoformat()).items():
        forward.setdefault(ticker, {})["octopus"] = pct
    ml_runs = read_ml_runs(getattr(args, "ml_root", None) or default_ml_root())
    for ticker, pct in ml_percentiles(ml_runs, set(series), as_of).items():
        forward.setdefault(ticker, {})["ml"] = pct
    records, unscored = score_universe(series, as_of, params, forward,
                                        params["liquidity"] if args.universe == "archive" else None)
    if not records:
        return None
    run_dir = data_dir / "runs" / as_of.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    history_path = data_dir / "history.csv"
    prev_date, prev = previous_run(read_history(history_path), as_of)

    write_ranked_csv(run_dir / "ranked_all.csv", records, names, sectors, held)
    evaluation = render_evaluation_md(as_of, records, names, held, args.threshold, args.top, prev_date, prev,
                                      tradability)
    (run_dir / "evaluation_list.md").write_text(evaluation, encoding="utf-8")
    rows = upsert_history(history_path, as_of, records)
    movement = render_movement_md(movement_summary(rows, args.threshold), args.threshold)
    (data_dir / "movement_summary.md").write_text(movement, encoding="utf-8")
    (data_dir / "latest_evaluation_list.md").write_text(evaluation, encoding="utf-8")
    (data_dir / "latest_ranked_all.csv").write_text((run_dir / "ranked_all.csv").read_text(encoding="utf-8"),
                                                   encoding="utf-8")
    if octopus:
        # Only tickers Octopus forecast have an octopus value; ML-only tickers have none and must not crash the run.
        pcts = {as_of.isoformat(): {t: f["octopus"] for t, f in forward.items() if "octopus" in f}}
        upsert_shadow(data_dir / "shadow_octopus_history.csv", octopus, pcts)
    if ml_runs:
        upsert_shadow_ml(data_dir / "shadow_ml_history.csv", ml_runs)
    trade = build_trade_list(records, held, params)
    write_trade_list(run_dir, data_dir / "trade_list_history.csv", as_of, records, trade, params)
    tickers_config = _load_json(Path(args.config_dir) / "tickers.json", {})
    proposal = build_proposal(as_of, trade, tickers_config, params)
    (run_dir / "tickers_proposal.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    (run_dir / "tickers_proposal.md").write_text(render_proposal_md(proposal), encoding="utf-8")
    (data_dir / "latest_trade_list.csv").write_text((run_dir / "trade_list.csv").read_text(encoding="utf-8"),
                                                   encoding="utf-8")
    (data_dir / "latest_tickers_proposal.md").write_text(render_proposal_md(proposal), encoding="utf-8")
    stale = sorted(t for t in unscored if t in series)
    missing = sorted(set(tickers) - set(series))
    (run_dir / "data_gaps.json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "universe": args.universe, "no_price_history": missing,
                    "not_scored_or_stale": stale}, indent=2),
        encoding="utf-8",
    )
    above = sum(1 for r in records.values() if r["score"] > args.threshold and r.get("eligible", True))
    summary = {"as_of": as_of.isoformat(), "universe": args.universe, "scored": len(records),
               "above_threshold": above, "threshold": args.threshold, "no_price_history": len(missing),
               "not_scored_or_stale": len(stale), "eligible": sum(1 for r in records.values() if r.get("eligible", True)),
               "trade_list": len(trade), "params_version": params["version"],
               "proposal_add": len(proposal["add"]), "proposal_stop_buying": len(proposal["stop_buying"])}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")  # read by the console page
    return {**summary, "run_dir": str(run_dir)}


# ----------------------------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------------------------
def archive_settling_mtime(archive_dir, quiet_minutes, now=None):
    """Newest mtime among the archive's CSVs if it falls inside the quiet window, else None.

    The daily archive update rewrites thousands of files (and restates whole histories) over a window of minutes.
    A run that reads mid-update mixes old and new bars, so the run refuses to score until the folder has been quiet.
    """
    now = now or dt.datetime.now().timestamp()
    newest = None
    for path in Path(archive_dir).glob("*.csv"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None or now - newest >= quiet_minutes * 60:
        return None
    return newest


def cmd_run(args):
    if not args.allow_active_archive:
        settling = archive_settling_mtime(args.archive_dir, args.quiet_minutes)
        if settling is not None:
            stamp = dt.datetime.fromtimestamp(settling).strftime("%H:%M:%S")
            print(f"error: archive {args.archive_dir} changed at {stamp} (within {args.quiet_minutes} min); "
                  "wait for the daily update to finish, or pass --allow-active-archive", file=sys.stderr)
            return 3
    tickers, held, names, sectors, series = prepare(args)
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else default_as_of(series)
    summary = run_once(args, tickers, held, names, sectors, series, as_of)
    if summary is None:
        print("error: nothing could be scored", file=sys.stderr)
        return 2
    print(json.dumps(summary))
    return 0


def cmd_backfill(args):
    """Re-score past trading days (series are loaded once). Uses today's holdings for the held flag."""
    tickers, held, names, sectors, series = prepare(args)
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    ran, skipped = 0, 0
    day = start
    while day <= end:
        bars_today = sum(1 for s in series.values() if s.value_at(day, max_gap=0) is not None)
        if bars_today >= 0.5 * len(series):  # a real trading day, not a weekend or holiday
            if run_once(args, tickers, held, names, sectors, series, day):
                ran += 1
        else:
            skipped += 1
        day += dt.timedelta(days=1)
    print(json.dumps({"backfilled_runs": ran, "skipped_non_trading_days": skipped}))
    return 0


def cmd_history(args):
    """Print a few compact lines from history.csv so a model (or you) never has to load the whole log."""
    rows = read_history(Path(args.data_dir) / "history.csv")
    if not rows:
        print("no history yet")
        return 0
    dates = sorted({r["date"] for r in rows})
    if args.date:
        if args.date not in dates:
            print(f"no run on {args.date}; recorded runs: {dates[0]} .. {dates[-1]} ({len(dates)} runs)")
            return 1
        day_rows = sorted((r for r in rows if r["date"] == args.date), key=lambda r: int(r["rank"]))
        print(f"{args.date} top {min(args.top, len(day_rows))} of {len(day_rows)}")
        for r in day_rows[: args.top]:
            print(f"#{r['rank']:>3} {r['ticker']:<6} {float(r['score']):5.1f}  vol {float(r['vol']) * 100:3.0f}%  "
                  f"dd {float(r['max_dd']) * 100:4.0f}%")
        return 0
    if not args.ticker:
        print(f"{len(dates)} runs recorded, {dates[0]} .. {dates[-1]}; pass --ticker T [T ...] or --date D --top N")
        return 0
    keep = set(dates[-args.days:])
    for ticker in (t.upper() for t in args.ticker):
        series = sorted((r["date"], int(r["rank"]), float(r["score"])) for r in rows
                        if r["ticker"] == ticker and r["date"] in keep)
        if not series:
            print(f"{ticker}: no history in the last {args.days} runs")
            continue
        ranks = [rank for _, rank, _ in series]
        print(f"{ticker}: {len(series)} runs, rank now {ranks[-1]} (best {min(ranks)}, worst {max(ranks)}, "
              f"avg {statistics.fmean(ranks):.0f})")
        for day, rank, score in series:
            print(f"  {day} #{rank:<3} {score:5.1f}")
    return 0


def cmd_effectiveness(args):
    """How well did past trade lists do versus the whole scored universe over the next N days?"""
    rows = read_history(Path(args.data_dir) / "trade_list_history.csv")
    if not rows:
        print("no trade-list history yet")
        return 0
    tickers, _, _, _, series = prepare(args)
    results = effectiveness(rows, series, tickers, args.horizon)
    if not results:
        print(f"no trade-list date has {args.horizon} days of later prices yet")
        return 0
    stamps = {}
    for r in rows:
        stamps.setdefault(r["date"], r.get("params_version", ""))
    print(f"{args.horizon}-day forward return of each day's trade list vs the scored universe")
    for item in results:
        print(f"  {item['date']} [{stamps.get(item['date'], '')}] {item['listed']:>3} names  "
              f"list {item['list_return'] * 100:+6.2f}%  universe {item['universe_return'] * 100:+6.2f}%  "
              f"edge {item['edge'] * 100:+6.2f}%")
    print(f"  average edge {statistics.fmean(i['edge'] for i in results) * 100:+.2f}% over {len(results)} days")
    return 0


def read_scores_csv(path):
    """{date: {ticker: score}} and eligibility from a generic ``date,ticker,score[,eligible]`` export (e.g. the
    dynamic-universe scorer's ``universe_scores`` table)."""
    return scores_from_history(read_history(path))


def trading_days(series_by_ticker, start, end, min_share=0.5):
    """Dates in [start, end] on which at least ``min_share`` of the series have a bar (weekends/holidays drop out)."""
    counts = {}
    for s in series_by_ticker.values():
        lo, hi = bisect.bisect_left(s.dates, start), bisect.bisect_right(s.dates, end)
        for day in s.dates[lo:hi]:
            counts[day] = counts.get(day, 0) + 1
    floor = min_share * len(series_by_ticker)
    return sorted(d for d, c in counts.items() if c >= floor)


def cmd_backfill_history(args):
    """Point-in-time score history for grading: score every ``--step-days`` trading day from ``--start`` to ``--end``
    using only bars up to that day and the same liquidity pool, and write ONE CSV (not the live history.csv and none
    of the per-run outputs). Holds no forward model. Universe = today's archive tickers, so names that left the archive
    are absent (survivorship); grade with --delist-loss to charge ones whose series ended early."""
    tickers, held, names, sectors, series = prepare(args)
    params = load_params(getattr(args, "params", None))
    days = trading_days(series, dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end))
    chosen, last = [], None
    for day in days:
        if last is None or (day - last).days >= args.step_days:
            chosen.append(day)
            last = day
    liquidity = params["liquidity"] if args.universe == "archive" else None
    rows = []
    for day in chosen:
        if getattr(args, "score_model", "composite") == "adr_price":
            records, _ = score_universe_adr_price(series, day, liquidity)
        else:
            records, _ = score_universe(series, day, params, None, liquidity)
        size = len(records)
        for ticker, rec in records.items():
            rows.append({"date": day.isoformat(), "ticker": ticker, "score": f"{rec['score']:.3f}",
                         "rank": str(rec["rank"]), "universe_size": str(size), "vol": f"{rec['vol']:.4f}",
                         "max_dd": f"{rec['max_dd']:.4f}", "eligible": "1" if rec["eligible"] else "0"})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"dates": len(chosen), "rows": len(rows), "out": str(out), "params_version": params["version"],
                      "score_model": getattr(args, "score_model", "composite")}))
    return 0


def cmd_grade(args):
    """Grade the composite score (history.csv) and optionally a second ranking at several horizons."""
    _, _, _, _, series = prepare(args)
    sources = []
    history_path = Path(args.history) if args.history else Path(args.data_dir) / "history.csv"
    scores, eligible = scores_from_history(read_history(history_path))
    if scores:
        sources.append(("composite_rank", scores, eligible))
    for label_path in args.compare or []:
        label, _, path = label_path.partition("=")
        if not path:
            raise SystemExit("error: --compare expects LABEL=PATH to a date,ticker,score[,eligible] CSV")
        cmp_scores, cmp_elig = read_scores_csv(path)
        sources.append((label, cmp_scores, cmp_elig))
    if not sources:
        print("no ranking history yet (history.csv is empty and no --compare given)")
        return 0
    report, payload = [], {}
    for label, sc, el in sources:
        summaries = {}
        for horizon in args.horizons:
            rows = grade_scores(sc, series, horizon, args.top_n, args.cost_bps, el, args.min_names,
                               args.delist_loss, args.hold_rank, args.non_overlap)
            summaries[horizon] = summarize_grade(rows)
        payload[label] = {str(h): s for h, s in summaries.items()}
        report.append(render_grade(label, summaries, args.top_n, args.cost_bps, args.hold_rank, args.non_overlap))
    text = "# Ranking grade\n\n" + "\n".join(report)
    out_dir = Path(args.data_dir) / "grading"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grade_latest.md").write_text(text, encoding="utf-8")
    (out_dir / "grade_latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(text)
    return 0


def cmd_shadow_octopus(args):
    """Log Octopus next-day forecasts (zero weight) from CSV files or directories into shadow_octopus_history.csv."""
    forecasts = read_octopus_forecasts(find_octopus_files(args.sources))
    if not forecasts:
        print("no Octopus forecasts found in the given paths")
        return 1
    pcts = {}
    for day in {f["origin_date"] for f in forecasts}:
        pcts[day] = octopus_percentiles(forecasts, {f["ticker"] for f in forecasts}, day)
    total = upsert_shadow(Path(args.data_dir) / "shadow_octopus_history.csv", forecasts, pcts)
    print(json.dumps({"forecasts_read": len(forecasts), "shadow_rows": total}))
    return 0


def cmd_shadow_ml(args):
    """Log ml-forecaster's per-ticker tilts (zero weight) into shadow_ml_history.csv."""
    runs = read_ml_runs(args.root or default_ml_root())
    if not runs:
        print("no ml-forecaster runs found (expected <root>/ml_forecaster/*.jsonl)")
        return 1
    total = upsert_shadow_ml(Path(args.data_dir) / "shadow_ml_history.csv", runs)
    print(json.dumps({"runs_read": len(runs), "shadow_rows": total, "latest": runs[-1]["date"]}))
    return 0


def cmd_shadow_scorecard(args):
    """Does Octopus's expected return predict the realised next-day return better than the composite score?"""
    shadow = read_history(Path(args.data_dir) / "shadow_octopus_history.csv")
    ml_rows = read_history(Path(args.data_dir) / "shadow_ml_history.csv")
    if not shadow and not ml_rows:
        print("no shadow history yet")
        return 0
    tickers, _, _, _, series = prepare(args)
    history = read_history(Path(args.data_dir) / "history.csv")
    ml_card = ml_scorecard(ml_rows, history, series) if ml_rows else []
    for item in ml_card:
        ic = "  n/a" if item["ml_ic"] is None else f"{item['ml_ic']:+.2f}"
        cic = "  n/a" if item["composite_ic"] is None else f"{item['composite_ic']:+.2f}"
        print(f"  ml {item['date']} n={item['n']:>3}  ml IC {ic}  composite IC {cic}  (next {ML_HORIZON_DAYS} days)")
    if ml_rows and not ml_card:
        print("  ml: no logged run has its 7-day horizon in the price data yet")
    card = shadow_scorecard(shadow, history, series) if shadow else []
    if not card:
        print("no logged Octopus forecast has a realised target date in the price data yet")
        return 0

    def fmt(value):
        return "  n/a" if value is None else f"{value:+.2f}"

    for item in card:
        print(f"  {item['date']} n={item['n']:>3}  octopus IC {fmt(item['octopus_ic'])}  "
              f"composite IC {fmt(item['composite_ic'])}  "
              f"octopus top-vs-bottom 20% {item['octopus_spread'] * 100:+.2f}%")
    ics = [i["octopus_ic"] for i in card if i["octopus_ic"] is not None]
    if ics:
        print(f"  mean octopus IC {statistics.fmean(ics):+.3f} over {len(ics)} days "
              "(IC = rank correlation with the realised return)")
    return 0


def cmd_missing(args):
    tickers, _, _, _ = build_universe(args.data_dir, args.universe, args.archive_dir)
    ext = _load_json(Path(args.data_dir) / "inputs" / "ext_prices.json", {})
    print(json.dumps(find_missing(tickers, args.archive_dir, ext)))
    return 0


def cmd_ingest_bars(args):
    ext_path = Path(args.data_dir) / "inputs" / "ext_prices.json"
    ext = _load_json(ext_path, {})
    ext.update(ingest_bars(args.files))
    ext_path.parent.mkdir(parents=True, exist_ok=True)
    ext_path.write_text(json.dumps(ext), encoding="utf-8")
    print(json.dumps({"tickers_in_file": len(ext)}))
    return 0


def cmd_ingest_sp500(args):
    constituents = parse_sp500_html(Path(args.html).read_text(encoding="utf-8"))
    out = Path(args.data_dir) / "inputs" / "sp500.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(constituents), encoding="utf-8")
    print(json.dumps({"constituents": len(constituents)}))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--data-dir", default=str(root / "artifacts" / "composite_rank"))
    parser.add_argument("--archive-dir", default=str(root / "ticker_data"))
    parser.add_argument("--params", default="", help="ranking parameters JSON (default: params.json)")
    parser.add_argument("--ml-root", default="", help="ml-forecaster suggestion history root (default: cron root)")
    parser.add_argument("--config-dir", default=str(root / "config"),
                        help="read-only: config/tickers.json is compared with the trade list to build the proposal")
    parser.add_argument("--universe", choices=UNIVERSES, default="sp500",
                        help="sp500 (current scope), all (also holdings and the watchlist) or archive (every archive ticker, "
                         "liquidity-filtered buy list)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="score, rank, write the evaluation list, update history and movement")
    run.add_argument("--threshold", type=float, default=None, help="default: params.json evaluate_threshold")
    run.add_argument("--top", type=int, default=0, help="list the top N instead of score > threshold")
    run.add_argument("--as-of", default="", help="YYYY-MM-DD (default: latest price date)")
    run.add_argument("--quiet-minutes", type=float, default=30,
                     help="refuse to score if any archive CSV changed more recently than this (default 30)")
    run.add_argument("--allow-active-archive", action="store_true",
                     help="score even while the archive is still being updated (not for scheduled runs)")
    run.set_defaults(func=cmd_run)
    back = sub.add_parser("backfill", help="re-score a date range to seed history")
    back.add_argument("--start", required=True)
    back.add_argument("--end", required=True)
    back.add_argument("--threshold", type=float, default=None)
    back.add_argument("--top", type=int, default=0)
    back.set_defaults(func=cmd_backfill)
    hist = sub.add_parser("history", help="compact rank history for chosen tickers, or one day's top list")
    hist.add_argument("--ticker", nargs="+", default=[], help="one or more tickers")
    hist.add_argument("--days", type=int, default=30, help="most recent runs to show (default 30)")
    hist.add_argument("--date", default="", help="YYYY-MM-DD: show that day's top list instead")
    hist.add_argument("--top", type=int, default=20)
    hist.set_defaults(func=cmd_history)
    eff = sub.add_parser("effectiveness", help="forward return of past trade lists vs the universe (tuning input)")
    eff.add_argument("--horizon", type=int, default=20, help="calendar days ahead (default 20)")
    eff.set_defaults(func=cmd_effectiveness)
    bh = sub.add_parser("backfill-history", help="point-in-time score history to one CSV, for grading only")
    bh.add_argument("--start", required=True)
    bh.add_argument("--end", required=True)
    bh.add_argument("--step-days", type=int, default=7, help="minimum calendar days between scored dates")
    bh.add_argument("--out", required=True, help="output CSV (not history.csv)")
    bh.add_argument("--score-model", choices=SCORE_MODELS, default="composite",
                    help="composite (default) or adr_price: a price-only surrogate of the dynamic-universe ADR's "
                         "momentum + low-vol factors, for side-by-side grading only")
    bh.set_defaults(func=cmd_backfill_history)
    gr = sub.add_parser("grade", help="rank IC, quintile spread, top-N edge, turnover and cost drag by horizon")
    gr.add_argument("--horizons", type=int, nargs="+", default=list(GRADE_HORIZONS), help="calendar days ahead")
    gr.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    gr.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS, help="round-trip cost per replaced name")
    gr.add_argument("--history", default="", help="score history CSV to grade (default: <data-dir>/history.csv)")
    gr.add_argument("--delist-loss", type=float, default=None,
                    help="treat series that ended early as delisted with this loss (e.g. 0.30); default: ignore them")
    gr.add_argument("--min-names", type=int, default=30, help="skip dates with fewer names with a forward return")
    gr.add_argument("--hold-rank", type=int, default=None,
                    help="buy/hold buffer: incumbents ranked within this rank stay on the list (e.g. 150 with --top-n 100)")
    gr.add_argument("--non-overlap", action="store_true",
                    help="grade only dates one horizon apart: non-overlapping samples, exact rebalance turnover")
    gr.add_argument("--compare", action="append", metavar="LABEL=PATH",
                    help="also grade another ranking from a date,ticker,score[,eligible] CSV (repeatable)")
    gr.set_defaults(func=cmd_grade)
    so = sub.add_parser("shadow-octopus", help="log Octopus next-day forecasts at zero weight")
    so.add_argument("sources", nargs="+", help="Octopus *_next_day_forecasts.csv files or run directories")
    so.set_defaults(func=cmd_shadow_octopus)
    sm = sub.add_parser("shadow-ml", help="log ml-forecaster tilts at zero weight")
    sm.add_argument("--root", default="", help="sizing_suggestions root (default: <cron root>/sizing_suggestions)")
    sm.set_defaults(func=cmd_shadow_ml)
    sub.add_parser("shadow-scorecard", help="Octopus vs composite score against realised returns").set_defaults(
        func=cmd_shadow_scorecard)
    sub.add_parser("missing", help="print tickers with no price history yet").set_defaults(func=cmd_missing)
    bars = sub.add_parser("ingest-bars", help="merge saved get_equity_historicals results into ext_prices.json")
    bars.add_argument("files", nargs="+")
    bars.set_defaults(func=cmd_ingest_bars)
    sp = sub.add_parser("ingest-sp500", help="parse a saved Wikipedia S&P 500 page into inputs/sp500.json")
    sp.add_argument("html")
    sp.set_defaults(func=cmd_ingest_sp500)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
