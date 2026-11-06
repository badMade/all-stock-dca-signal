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

HISTORY_FIELDS = ["date", "ticker", "held", "score", "rank", "universe_size", "vol", "max_dd"]


# ----------------------------------------------------------------------------------------------
# Price series
# ----------------------------------------------------------------------------------------------
class Series:
    """Sorted (date, adjusted close) pairs with bisect lookups."""

    def __init__(self, points):
        merged = {}
        for day, value in points:
            if value is not None and value > 0 and math.isfinite(value):
                merged[day] = value
        self.dates = sorted(merged)
        self.values = [merged[d] for d in self.dates]

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
        return clipped


def _parse_day(text):
    try:
        return dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def load_archive_series(archive_dir, ticker):
    """Read ``<archive>/<TICKER>.csv`` (``BRK.B`` is stored as ``BRK-B``); None when missing."""
    path = Path(archive_dir) / f"{ticker.replace('.', '-')}.csv"
    if not path.exists():
        return None
    points = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = _parse_day(row.get("Date", ""))
            try:
                value = float(row.get("Adj Close", ""))
            except (TypeError, ValueError):
                continue
            if day is not None:
                points.append((day, value))
    return Series(points) if points else None


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


def return_score(annualised):
    return min(max(annualised / RETURN_SCORE_CAP, 0.0), 1.0) * 100.0


def period_score(percentile, total_return, period):
    ann = annualise(total_return, PERIOD_DAYS[period])
    bonus = HURDLE_BONUS if ann >= HURDLE_ANNUALISED else 0.0
    return 0.6 * percentile + 0.4 * return_score(ann) + bonus


def percentiles(values):
    """{key: 100 * (1 - (rank - 1) / (n - 1))} with rank 1 = largest value, ties share the best rank."""
    n = len(values)
    negated = sorted(-v for v in values.values())  # ascending, so bisect_left counts strictly larger values
    out = {}
    for key, value in values.items():
        rank = bisect.bisect_left(negated, -value) + 1
        out[key] = 100.0 if n == 1 else 100.0 * (1.0 - (rank - 1) / (n - 1))
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


def score_universe(series_by_ticker, as_of):
    """Return {ticker: record} for every ticker with a 1y return and risk stats."""
    clipped = {t: s.truncate(as_of) for t, s in series_by_ticker.items() if s}
    fresh = {t: s for t, s in clipped.items() if len(s) and s.last_date >= as_of - dt.timedelta(days=STALE_DAYS)}
    returns = {t: period_returns(s, as_of) for t, s in fresh.items()}
    risk = {t: r for t, r in ((t, risk_stats(s, as_of)) for t, s in fresh.items()) if r}

    pct = {}
    for period in PERIOD_ORDER:
        pool = {t: r[period] for t, r in returns.items() if period in r}
        pct[period] = percentiles(pool) if pool else {}
    low_vol = percentiles({t: -r[0] for t, r in risk.items()}) if risk else {}
    low_dd = percentiles({t: r[1] for t, r in risk.items()}) if risk else {}

    records = {}
    for ticker, rets in returns.items():
        if "1y" not in rets or ticker not in risk:
            continue
        num = den = 0.0
        for period in PERIOD_ORDER:
            if period in rets:
                num += PERIOD_WEIGHTS[period] * period_score(pct[period][ticker], rets[period], period)
                den += PERIOD_WEIGHTS[period]
        base = num / den
        final = BLEND["base"] * base + BLEND["lowvol"] * low_vol[ticker] + BLEND["lowdd"] * low_dd[ticker]
        records[ticker] = {
            "score": final,
            "base_score": base,
            "lowvol_pct": low_vol[ticker],
            "lowdd_pct": low_dd[ticker],
            "vol": risk[ticker][0],
            "max_dd": risk[ticker][1],
            "returns": rets,
            "periods": len(rets),
        }
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


def upsert_history(path, run_date, records, held):
    """Replace any rows for ``run_date`` and append today's; returns the full row list."""
    rows = [r for r in read_history(path) if r["date"] != run_date.isoformat()]
    size = len(records)
    for ticker, rec in records.items():
        rows.append(
            {
                "date": run_date.isoformat(),
                "ticker": ticker,
                "held": "1" if ticker in held else "0",
                "score": f"{rec['score']:.3f}",
                "rank": str(rec["rank"]),
                "universe_size": str(size),
                "vol": f"{rec['vol']:.4f}",
                "max_dd": f"{rec['max_dd']:.4f}",
            }
        )
    rows.sort(key=lambda r: (r["date"], int(r["rank"])))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
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
              "max_dd"] + [f"ret_{p}" for p in PERIOD_ORDER]
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


UNIVERSES = ("sp500", "all")


def build_universe(data_dir, mode="sp500"):
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
    tickers, held, names, sectors = build_universe(data_dir, args.universe)
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
    records, unscored = score_universe(series, as_of)
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
    rows = upsert_history(history_path, as_of, records, held)
    movement = render_movement_md(movement_summary(rows, args.threshold), args.threshold)
    (data_dir / "movement_summary.md").write_text(movement, encoding="utf-8")
    (data_dir / "latest_evaluation_list.md").write_text(evaluation, encoding="utf-8")
    (data_dir / "latest_ranked_all.csv").write_text((run_dir / "ranked_all.csv").read_text(encoding="utf-8"),
                                                   encoding="utf-8")
    stale = sorted(t for t in unscored if t in series)
    missing = sorted(set(tickers) - set(series))
    (run_dir / "data_gaps.json").write_text(
        json.dumps({"as_of": as_of.isoformat(), "universe": args.universe, "no_price_history": missing,
                    "not_scored_or_stale": stale}, indent=2),
        encoding="utf-8",
    )
    above = sum(1 for r in records.values() if r["score"] > args.threshold)
    return {"as_of": as_of.isoformat(), "universe": args.universe, "scored": len(records), "above_threshold": above,
            "threshold": args.threshold, "no_price_history": len(missing), "not_scored_or_stale": len(stale),
            "run_dir": str(run_dir)}


# ----------------------------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------------------------
def cmd_run(args):
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


def cmd_missing(args):
    tickers, _, _, _ = build_universe(args.data_dir, args.universe)
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
    parser.add_argument("--universe", choices=UNIVERSES, default="sp500",
                        help="sp500 (current scope) or all (also holdings and the watchlist)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="score, rank, write the evaluation list, update history and movement")
    run.add_argument("--threshold", type=float, default=80.0)
    run.add_argument("--top", type=int, default=0, help="list the top N instead of score > threshold")
    run.add_argument("--as-of", default="", help="YYYY-MM-DD (default: latest price date)")
    run.set_defaults(func=cmd_run)
    back = sub.add_parser("backfill", help="re-score a date range to seed history")
    back.add_argument("--start", required=True)
    back.add_argument("--end", required=True)
    back.add_argument("--threshold", type=float, default=80.0)
    back.add_argument("--top", type=int, default=0)
    back.set_defaults(func=cmd_backfill)
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
