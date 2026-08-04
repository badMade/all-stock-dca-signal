#!/usr/bin/env python3
"""Build the active per-stock DCA calibration from daily OHLCV history files."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path

MINIMUM_HISTORY_SESSIONS = 252
# Frequent first buys, then increasingly rare volatility-driven dollar amounts.
PERCENTILE_MULTIPLIERS = ((0.25, 1.0), (0.50, 1.5), (0.75, 2.5), (0.90, 4.0), (0.975, 5.0))
GLOBAL_FALLBACK_TIERS = ((0.1, 1.0), (2.0, 1.5), (4.0, 2.0), (5.0, 2.5), (6.0, 3.0), (6.5, 3.5), (7.0, 4.0), (7.5, 4.5), (8.0, 5.0))


def quantile(values: list[float], percentile: float) -> float:
    """Return a linearly interpolated percentile from a non-empty sample."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def adjusted_low_drops(history_path: Path, start_date: str) -> list[float]:
    """Return split/dividend-adjusted prior-close-to-low falls as percentages."""
    prices = []
    with history_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                if row["Date"] >= start_date:
                    prices.append((float(row["Close"]), float(row["Adj Close"]), float(row["Low"])))
            except (KeyError, ValueError):
                continue
    drops = []
    for (_, prior_adjusted_close, _), (close, adjusted_close, low) in zip(prices, prices[1:], strict=False):
        if min(close, adjusted_close, low, prior_adjusted_close) <= 0:
            continue
        adjusted_low = low * adjusted_close / close
        drop_pct = max(0.0, 100.0 * (1.0 - adjusted_low / prior_adjusted_close))
        if drop_pct <= 50.0:
            drops.append(drop_pct)
    return drops


def tiers_from_drops(drops: list[float]) -> list[dict[str, float]]:
    """Turn daily drop history into precise percentile-based dollar tiers."""
    return [
        {"min_drop_pct": round(max(0.01, quantile(drops, percentile)), 4), "multiplier": multiplier}
        for percentile, multiplier in PERCENTILE_MULTIPLIERS
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    parser.add_argument("--output", type=Path, default=Path("config/per_ticker_sizing.json"))
    parser.add_argument("--lookback-years", type=int, default=3)
    args = parser.parse_args()

    active = json.loads(args.ticker_config.read_bytes())["active"]
    as_of = date.today()
    start_date = f"{as_of.year - args.lookback_years:04d}-{as_of.month:02d}-{as_of.day:02d}"
    tickers = {}
    fallback = []
    for ticker in active:
        path = args.history_dir / f"{ticker}.csv"
        drops = adjusted_low_drops(path, start_date) if path.exists() else []
        if len(drops) >= MINIMUM_HISTORY_SESSIONS:
            tiers = tiers_from_drops(drops)
            source = "three_year_adjusted_daily_low_percentiles"
        else:
            tiers = [{"min_drop_pct": threshold, "multiplier": multiplier} for threshold, multiplier in GLOBAL_FALLBACK_TIERS]
            source = "global_fallback_insufficient_history"
            fallback.append(ticker)
        tickers[ticker] = {
            "base_amount_usd": 1.0,
            "base_amount_below_close_usd": 1.0,
            "tiers": tiers,
            "source": source,
            "sample_sessions": len(drops),
        }

    payload = {
        "version": as_of.isoformat(),
        "status": "active",
        "description": "Per-stock DCA: all positive first orders start at $1.00; later amounts are precise historical-drop tiers.",
        "method": "25th/50th/75th/90th/97.5th percentile adjusted prior-close-to-low drops map to 1x/1.5x/2.5x/4x/5x.",
        "minimum_order_usd": 1.0,
        "fallback_tickers": fallback,
        "tickers": tickers,
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"WROTE {args.output}: calibrated={len(active) - len(fallback)} fallback={len(fallback)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
