#!/usr/bin/env python3
"""Restore missing daily-price histories for the configured DCA universe.

This utility is deliberately separate from live DCA data collection.  It writes
only missing CSVs by default, preserving the Drive history archive as the
source for research and sizing analysis.  Yahoo Finance is used as a fallback
historical-data source and never as an execution-data source.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

DEFAULT_HISTORY_DIR = Path("ticker_histories")
DEFAULT_CONFIG = Path("config/tickers.json")
FIELDS = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
USER_AGENT = "Mozilla/5.0 (compatible; AllStockDCAHistoryRefresh/1.0)"


def active_symbols(config_path: Path) -> list[str]:
    """Return the configured active symbols in their declared order."""
    data = json.loads(config_path.read_bytes())
    active = data.get("active")
    if not isinstance(active, list) or not all(isinstance(symbol, str) for symbol in active):
        raise ValueError(f"{config_path} must contain an 'active' list of symbols")
    return active


def fetch_chart(symbol: str) -> dict[str, Any]:
    """Fetch all available daily bars for one symbol from Yahoo's chart feed."""
    # Yahoo's ``range=max`` silently coarsens long histories to monthly bars.
    # Explicit Unix bounds retain the daily granularity required for DCA sizing.
    query = urllib.parse.urlencode(
        {
            "period1": "0",
            "period2": str(int(time.time()) + 2 * 24 * 60 * 60),
            "interval": "1d",
            "events": "div,splits",
        }
    )
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{query}"
    result = subprocess.run(  # nosec B603 - fixed command and HTTPS URL, no shell
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            "30",
            "--user-agent",
            USER_AGENT,
            url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    chart = payload.get("chart", {})
    result = chart.get("result") or []
    if not result:
        raise ValueError(chart.get("error", {}).get("description", "no chart data returned"))
    return result[0]


def chart_rows(chart: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize Yahoo chart JSON into the archive's established CSV columns."""
    timestamps = chart.get("timestamp") or []
    quotes = (chart.get("indicators", {}).get("quote") or [{}])[0]
    adjusted = (chart.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    events = chart.get("events") or {}
    dividends = events.get("dividends") or {}
    splits = events.get("splits") or {}
    rows: list[dict[str, str]] = []

    for index, timestamp in enumerate(timestamps):
        values = {field: (quotes.get(field) or [None] * len(timestamps))[index] for field in ("open", "high", "low", "close", "volume")}
        adj_close = adjusted[index] if index < len(adjusted) else None
        if any(values[field] is None for field in ("open", "high", "low", "close")) or adj_close is None:
            continue
        event_key = str(timestamp)
        split = splits.get(event_key, {})
        rows.append(
            {
                "Date": dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date().isoformat(),
                "Open": str(values["open"]),
                "High": str(values["high"]),
                "Low": str(values["low"]),
                "Close": str(values["close"]),
                "Adj Close": str(adj_close),
                "Volume": str(values["volume"] or 0),
                "Dividends": str((dividends.get(event_key) or {}).get("amount", 0.0)),
                "Stock Splits": str(split.get("numerator", 0.0) / split.get("denominator", 1.0)) if split else "0.0",
            }
        )
    if not rows:
        raise ValueError("chart data contains no complete daily bars")
    return rows


def write_history(path: Path, rows: list[dict[str, str]]) -> None:
    """Write one complete archive CSV atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_HISTORY_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--symbol", action="append", help="Refresh only this symbol; repeatable")
    parser.add_argument("--force", action="store_true", help="Replace an existing history file")
    parser.add_argument("--dry-run", action="store_true", help="Report the intended files without writing")
    parser.add_argument("--pause-seconds", type=float, default=1.0, help="Pause between remote requests")
    args = parser.parse_args()

    configured = active_symbols(args.config)
    requested = args.symbol or configured
    unknown = sorted(set(requested) - set(configured))
    if unknown:
        parser.error(f"symbols are not active in {args.config}: {', '.join(unknown)}")

    failures: list[str] = []
    changed = 0
    for symbol in requested:
        destination = args.history_dir / f"{symbol}.csv"
        if destination.exists() and not args.force:
            print(f"SKIP {symbol}: existing history")
            continue
        if args.dry_run:
            print(f"WOULD_REFRESH {symbol}: {destination}")
            continue
        try:
            rows = chart_rows(fetch_chart(symbol))
            write_history(destination, rows)
            print(f"WROTE {symbol}: {len(rows)} rows ({rows[0]['Date']} through {rows[-1]['Date']})")
            changed += 1
        except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            print(f"FAILED {symbol}: {error}", file=sys.stderr)
            failures.append(symbol)
        time.sleep(max(args.pause_seconds, 0.0))

    print(f"SUMMARY changed={changed} failed={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
