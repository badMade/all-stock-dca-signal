"""Read-only comparison of the independent sizing suggesters' recorded recommendations.

``sizing_suggestion_history`` keeps every run of every suggester (``ml_forecaster``, ``octopus``, ...).
This module is the "another function" that scores them side by side against what the market then did,
so a human can decide -- after at least six months of evidence -- which suggester, or which blend,
deserves a place in live sizing. It only READS: the history files and the ticker-history archive. It
never writes ``config/ml_sizing_adjustment.json`` or anything else live sizing consumes; its one output
is a report under ``<history root>/comparisons/``.

Method (deliberately simple, so a reviewer can check it by hand):

* A suggestion is ``multiplier`` for ``ticker`` from one run. Its **tilt** is ``multiplier - 1`` (>0 = buy
  more than the ladder says, <0 = buy less). A ticker a run does not mention has tilt 0.
* Its **outcome** is the forward return of the ticker's adjusted close, ``horizon`` TRADING days after the
  last close on or before the run's ``as_of`` date (the day it was recorded -- or its ``generated_at`` when
  ``--as-of generated_at``). A run whose horizon has not elapsed yet is *pending*, never scored.
* Each run is judged against the **universe baseline**: the mean forward return over the universe tickers
  on that date. A suggester that only ever tilts up (octopus Tier C) must beat "the average ticker",
  not zero.
* Per suggester: ``tilt_weighted_excess`` = sum(tilt * (r - baseline)) / sum(|tilt|) over scored
  suggestions; ``hit_rate`` = share of non-zero tilts whose sign matched the sign of (r - baseline); and
  ``mean_ic`` = the mean over runs of the correlation between the tilt vector (missing = 0, across the
  universe) and (r - baseline) -- the usual information coefficient.
* ``blend_equal`` is a synthetic suggester: per date, the mean tilt of every real suggester's latest run
  (missing = 0). It answers "is a combination better than either alone" with no tuning, so it cannot
  overfit.

A score is evidence, not a verdict: with few matured runs the numbers are noise, which is why the report
carries ``n_runs_scored`` and ``n_runs_pending`` next to every metric, and why promotion into live sizing
stays a manual edit after the review period.

Standard library only; imports :mod:`dca_paths`-based history helpers and :mod:`dca_ticker_paths`.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from bisect import bisect_right
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import dca_ticker_paths
from sizing_suggestion_history import history_root, read_history

REPORT_SCHEMA_VERSION = 1
BLEND_NAME = "blend_equal"
DEFAULT_HORIZON_TRADING_DAYS = 5
COMPARISONS_DIRNAME = "comparisons"

Prices = Tuple[List[date], List[float]]


def load_prices(ticker: str, history_dir: Optional[Path] = None) -> Optional[Prices]:
    """``(dates, adjusted closes)`` for ``ticker`` from the archive CSV, sorted, or None if absent.

    Parses ``Date`` strictly as ``YYYY-MM-DD`` and drops any row that does not -- the archive is known to
    hold a stray yearless ``04-27`` row (CBU.csv) that must not become year 0001 -- plus non-positive or
    non-finite prices. ``Adj Close`` is preferred (dividends), ``Close`` the fallback.
    """
    path = (history_dir or dca_ticker_paths.configured_ticker_history_dir()) / f"{ticker.upper()}.csv"
    if not path.is_file():
        return None
    rows: Dict[date, float] = {}
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                day = date.fromisoformat((row.get("Date") or "").strip()[:10])
                raw = row.get("Adj Close") or row.get("Close") or ""
                price = float(raw)
            except ValueError:
                continue
            if day.year < 1970 or not math.isfinite(price) or price <= 0:
                continue
            rows[day] = price
    if not rows:
        return None
    days = sorted(rows)
    return days, [rows[d] for d in days]


def forward_return(prices: Prices, as_of: date, horizon: int) -> Optional[float]:
    """Return over ``horizon`` trading days from the last close on/before ``as_of``; None if not matured."""
    days, closes = prices
    idx = bisect_right(days, as_of) - 1  # last trading day <= as_of
    if idx < 0 or idx + horizon >= len(days):
        return None
    start, end = closes[idx], closes[idx + horizon]
    return end / start - 1.0


def _as_of(record: Mapping[str, Any], basis: str) -> Optional[date]:
    raw = record.get("generated_at" if basis == "generated_at" else "recorded_at")
    try:
        return datetime.fromisoformat(str(raw)).astimezone(timezone.utc).date()
    except (TypeError, ValueError):
        return None


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)


def latest_runs_by_date(records: Iterable[Mapping[str, Any]], basis: str = "recorded_at") -> Dict[date, Mapping[str, Any]]:
    """One run per as-of date: the last one appended that day. Blocked runs are kept (see ``status``).

    A suggester that runs twice in a day is judged on its final word for that day, not double counted.
    """
    latest: Dict[date, Mapping[str, Any]] = {}
    for record in records:
        day = _as_of(record, basis)
        if day is not None:
            latest[day] = record  # history is in append order, so later lines win
    return latest


def build_tilts(runs: Mapping[date, Mapping[str, Any]]) -> Dict[date, Dict[str, float]]:
    """``{as_of: {TICKER: multiplier - 1}}`` for each run."""
    return {
        day: {str(t).upper(): float(m) - 1.0 for t, m in (rec.get("adjustments") or {}).items()}
        for day, rec in runs.items()
    }


def blend_tilts(per_suggester: Mapping[str, Mapping[date, Mapping[str, float]]]) -> Dict[date, Dict[str, float]]:
    """Equal-weight mean tilt across every suggester that has a run that day (missing ticker = 0)."""
    days = sorted({day for tilts in per_suggester.values() for day in tilts})
    blended: Dict[date, Dict[str, float]] = {}
    for day in days:
        present = [tilts[day] for tilts in per_suggester.values() if day in tilts]
        tickers = {t for tilt in present for t in tilt}
        blended[day] = {t: sum(tilt.get(t, 0.0) for tilt in present) / len(present) for t in tickers}
    return blended


def score_tilts(
    tilts_by_day: Mapping[date, Mapping[str, float]],
    universe: Sequence[str],
    prices: Mapping[str, Prices],
    horizon: int,
) -> Dict[str, Any]:
    """Score one suggester's (or the blend's) per-date tilts against realized forward returns."""
    weighted_num = weighted_den = 0.0
    hits = signed = 0
    ics: List[float] = []
    scored_days = pending_days = 0
    for day in sorted(tilts_by_day):
        returns = {t: r for t in universe if t in prices and (r := forward_return(prices[t], day, horizon)) is not None}
        if len(returns) < 3:
            pending_days += 1
            continue
        baseline = sum(returns.values()) / len(returns)
        excess = {t: r - baseline for t, r in returns.items()}
        tilt = tilts_by_day[day]
        scored_days += 1
        for ticker, value in tilt.items():
            if ticker not in excess or value == 0.0:
                continue
            weighted_num += value * excess[ticker]
            weighted_den += abs(value)
            signed += 1
            hits += (value > 0) == (excess[ticker] > 0)
        ic = _pearson([tilt.get(t, 0.0) for t in excess], [excess[t] for t in excess])
        if ic is not None:
            ics.append(ic)
    return {
        "n_runs_scored": scored_days,
        "n_runs_pending": pending_days,
        "n_scored_suggestions": signed,
        "tilt_weighted_excess": weighted_num / weighted_den if weighted_den else None,
        "hit_rate": hits / signed if signed else None,
        "mean_ic": sum(ics) / len(ics) if ics else None,
    }


def compare(
    suggesters: Sequence[str],
    universe: Sequence[str],
    *,
    horizon: int = DEFAULT_HORIZON_TRADING_DAYS,
    basis: str = "recorded_at",
    history_dir: Optional[Path] = None,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Score every suggester and their equal-weight blend; return the report dict (no I/O besides reads)."""
    if horizon < 1:
        raise ValueError("horizon must be >= 1 trading day")
    if basis not in ("recorded_at", "generated_at"):
        raise ValueError("basis must be 'recorded_at' or 'generated_at'")
    tickers = sorted({t.upper() for t in universe})
    prices = {t: p for t in tickers if (p := load_prices(t, history_dir)) is not None}
    per_suggester = {name: build_tilts(latest_runs_by_date(read_history(name, root=root), basis)) for name in suggesters}
    results = {name: score_tilts(tilts, tickers, prices, horizon) for name, tilts in per_suggester.items()}
    live = {name: tilts for name, tilts in per_suggester.items() if tilts}
    if len(live) >= 2:
        results[BLEND_NAME] = score_tilts(blend_tilts(live), tickers, prices, horizon)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "horizon_trading_days": horizon,
        "as_of_basis": basis,
        "universe_size": len(tickers),
        "universe_with_prices": len(prices),
        "suggesters": results,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    def cell(value: Any, pct: bool = False) -> str:
        if value is None:
            return "-"
        return f"{value * 100:.2f}%" if pct else (f"{value:.3f}" if isinstance(value, float) else str(value))

    lines = [
        f"# Sizing suggester comparison ({report['generated_at'][:10]})",
        "",
        f"Forward return over {report['horizon_trading_days']} trading days, "
        f"as-of basis `{report['as_of_basis']}`, universe {report['universe_with_prices']}/"
        f"{report['universe_size']} tickers with prices. Evidence, not a verdict: read the run counts.",
        "",
        "| suggester | runs scored | runs pending | suggestions | tilt-weighted excess | hit rate | mean IC |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in report["suggesters"].items():
        lines.append(
            f"| {name} | {row['n_runs_scored']} | {row['n_runs_pending']} | {row['n_scored_suggestions']} "
            f"| {cell(row['tilt_weighted_excess'], True)} | {cell(row['hit_rate'], True)} | {cell(row['mean_ic'])} |"
        )
    return "\n".join(lines) + "\n"


def _universe_from_config() -> List[str]:
    config = Path(__file__).resolve().parent / "config" / "tickers.json"
    return list(json.loads(config.read_bytes()).get("active", []))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare sizing suggesters' recorded recommendations (read-only).")
    parser.add_argument("--suggester", action="append", help="stream to include (default: ml_forecaster, octopus)")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_TRADING_DAYS, help="forward trading days")
    parser.add_argument("--as-of", choices=("recorded_at", "generated_at"), default="recorded_at")
    parser.add_argument("--write", action="store_true", help="also write the report under <history root>/comparisons/")
    args = parser.parse_args(argv)

    report = compare(
        args.suggester or ["ml_forecaster", "octopus"],
        _universe_from_config(),
        horizon=args.horizon,
        basis=args.as_of,
    )
    sys.stdout.write(render_markdown(report))
    if args.write:
        out = history_root() / COMPARISONS_DIRNAME
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{report['generated_at'][:10]}_h{args.horizon}"
        (out / f"{stem}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
