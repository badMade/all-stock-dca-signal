#!/usr/bin/env python3
"""Pre-fetch a labeled yfinance fallback payload for the DCA market-data
source policy's fallback (``config/scheduled_tasks.json``'s
``market_data_source_policy``: "yfinance is a fallback used ONLY if Finance
connector data is unavailable, stale, incomplete, or unresolved").

WHAT THIS IS FOR
----------------
``scripts/generate_buy_candidates.py`` (the 1530ET final-buy slot) and
``scripts/stream_candidates.py`` (the 0935ET/1030ET/1130-1330-1430ET/1230ET
slots) both accept an already-labeled ``sources.yahoo_finance_yfinance``
block in their ``--market-data`` payload and substitute it for a Finance
fetch that is unavailable, errored, or incomplete. Neither module fetches
that data itself -- by design, this repo's core makes no network calls and
has zero third-party runtime dependencies (see the root ``CLAUDE.md``).

Historically the only place instructed to populate that block was the live
executor prompt itself, fetching yfinance evidence mid-run with whatever
tools that session happens to have. When that session has no real path to
Yahoo Finance (no network, no ``yfinance`` package installed), the fallback
this repo's own code already supports can never actually be populated, and a
Finance outage at 12:30 or 3:30 has no way to recover.

This script closes that gap as a small, standalone, run-before-the-slot
fetch step: given a ticker list, a trading day, and a slot's decision time
and bar interval, it downloads previous-close bars, intraday bars, and a
current quote from Yahoo Finance via ``yfinance`` and writes them in the
exact per-ticker shape ``scripts/generate_buy_candidates.py``'s own module
docstring documents for ``sources.yahoo_finance_yfinance``. A caller (an
executor prompt, or a scheduled task that runs a few minutes ahead of a
live slot) merges this script's output into its own ``--market-data``
payload; this script never calls either candidate generator itself and
never places an order.

Like ``ticker-advisor/``, ``ml-forecaster/``, ``wondrous-octopus/``, and
``ticker-market-breadth/``, this is a self-contained, third-party-dependency
subproject with its own ``requirements.txt`` and ``tests/``, excluded from
the core repo's stdlib-only ruff and coverage gates.

OUTPUT SHAPE
------------
A JSON object shaped to be merged directly into an executor's own
``--market-data`` payload::

    {
      "sources": {
        "yahoo_finance_yfinance": {
          "<TICKER>": {"previous_close_bar": <bar>, "intraday_bars": [<bar>, ...], "quote": <quote>}, ...
        }
      },
      "source_errors": [],
      "ticker_errors": {"yahoo_finance_yfinance": ["<TICKER>", ...]}
    }

A ``<bar>`` is ``{"timestamp": "<ISO 8601, tz-aware>", "open", "high",
"low", "close", "volume"}``, one straight-through consumer of Yahoo's own
OHLCV columns -- unlike the live Robinhood adapter's quote-derived previous-
close bar, this is a real daily bar, so ``low`` is always the genuine prior
session low, never the ``low<=0`` "unknown" sentinel.

A ticker whose fetch raises for ANY reason (network error, an unrecognized
symbol, a malformed response) is recorded under ``ticker_errors`` and
omitted from ``sources.yahoo_finance_yfinance`` entirely, rather than
letting a partial/garbled payload for it reach either candidate generator --
both of those already treat a ``ticker_errors`` entry as authoritative
evidence of a fetch exception (never the benign "not covered" case).

No network calls happen at import time; ``yfinance`` is imported lazily
inside each fetch function so ``--help`` and unit tests that monkeypatch
these functions never require it to be installed or reachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

#: The label the two candidate generators (scripts/generate_buy_candidates.py,
#: scripts/stream_candidates.py) both expect this evidence under.
SOURCE_KEY = "yahoo_finance_yfinance"


def _bar_from_row(timestamp: datetime, row: Any) -> Dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat(),
        "open": float(row["Open"]),
        "high": float(row["High"]),
        "low": float(row["Low"]),
        "close": float(row["Close"]),
        "volume": float(row["Volume"]),
    }


def fetch_previous_close_bar(ticker: str, trading_day: date) -> Optional[Dict[str, Any]]:
    """Return the prior completed regular-session daily bar for ``ticker``.

    Stamped at 16:00 ET on the prior session's own calendar date, matching
    the shape every other market-data source in this repo already produces
    for ``previous_close_bar`` (the timestamp
    ``scripts/generate_buy_candidates.py``'s ``_StaticMarketData`` requires
    to fall strictly before the trading day being evaluated). Returns
    ``None`` when Yahoo has no row strictly before ``trading_day`` -- e.g. a
    brand-new listing, or a symbol Yahoo does not recognize returning an
    empty frame rather than raising.
    """
    import yfinance as yf

    frame = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=False)
    if frame is None or frame.empty:
        return None
    frame = frame[frame.index.date < trading_day]
    if frame.empty:
        return None
    session_date = frame.index[-1].date()
    return _bar_from_row(datetime.combine(session_date, dtime(16, 0), tzinfo=ET), frame.iloc[-1])


def fetch_intraday_bars(
    ticker: str,
    trading_day: date,
    decision_time_et: dtime,
    interval_minutes: int,
) -> List[Dict[str, Any]]:
    """Return completed intraday bars for ``trading_day`` from 09:30 ET
    through the last bar at or before ``decision_time_et``.

    A bar timestamped strictly after the decision moment is still-forming
    (or, for a bar fetched slightly ahead of real time, not yet real) and is
    omitted -- every live-mirror executor prompt requires the same "omit the
    still-open decision-time bar" for Finance's own 15-minute bars, and this
    fallback must not hand the candidate generators a bar neither of them
    would ever see from a real completed-bar fetch.
    """
    import yfinance as yf

    frame = yf.Ticker(ticker).history(
        start=datetime.combine(trading_day, dtime(9, 30), tzinfo=ET),
        end=datetime.combine(trading_day, dtime(16, 0), tzinfo=ET),
        interval=f"{interval_minutes}m",
        auto_adjust=False,
    )
    if frame is None or frame.empty:
        return []
    decision_dt = datetime.combine(trading_day, decision_time_et, tzinfo=ET)
    bars: List[Dict[str, Any]] = []
    for timestamp, row in frame.iterrows():
        ts_et = timestamp.tz_convert(ET) if timestamp.tzinfo is not None else timestamp.tz_localize(ET)
        if ts_et > decision_dt:
            continue
        bars.append(_bar_from_row(ts_et.to_pydatetime(), row))
    return bars


def fetch_quote(ticker: str) -> Optional[Dict[str, Any]]:
    """Return a best-effort current quote as ``{"price", "as_of"}``.

    Optional evidence, matching every other source's own optional ``quote``
    field: a candidate generator falls back to the completed decision bar's
    close when no quote is present, so a missing/unreadable quote here is
    tolerated rather than treated as a fetch failure.
    """
    import yfinance as yf

    fast_info = yf.Ticker(ticker).fast_info
    price = None
    for key in ("last_price", "lastPrice", "regular_market_price"):
        try:
            price = fast_info[key]
        except (KeyError, TypeError):
            price = getattr(fast_info, key, None)
        if price:
            break
    if not price:
        return None
    return {"price": float(price), "as_of": datetime.now(tz=ET).isoformat()}


def fetch_ticker_payload(
    ticker: str,
    trading_day: date,
    decision_time_et: dtime,
    interval_minutes: int,
) -> Dict[str, Any]:
    """Fetch one ticker's full ``sources.yahoo_finance_yfinance`` payload.

    ``previous_close_bar``/``quote`` are omitted (not stamped as null) when
    unavailable, matching the optional-key shape the candidate generators
    already expect (an absent ``previous_close_bar`` is treated as
    "incomplete" exactly the same whether the key is missing or its value is
    ``None`` -- but a caller reusing this document verbatim should never see
    a literal ``null`` where a bar object is otherwise expected).
    """
    payload: Dict[str, Any] = {
        "intraday_bars": fetch_intraday_bars(ticker, trading_day, decision_time_et, interval_minutes),
    }
    previous_close_bar = fetch_previous_close_bar(ticker, trading_day)
    if previous_close_bar is not None:
        payload["previous_close_bar"] = previous_close_bar
    quote = fetch_quote(ticker)
    if quote is not None:
        payload["quote"] = quote
    return payload


def build_fallback_document(
    tickers: List[str],
    trading_day: date,
    decision_time_et: dtime,
    interval_minutes: int,
) -> Dict[str, Any]:
    """Fetch every ticker and return the labeled fallback block.

    A per-ticker fetch exception is recorded under ``ticker_errors`` and
    that ticker is left out of ``sources.yahoo_finance_yfinance`` entirely
    -- never a partial/garbled payload for it -- so a caller merging this
    document into its own ``--market-data`` payload gets the same
    "provider_error=True" treatment for that ticker that a genuine live
    fetch exception on Finance/Robinhood already gets.
    """
    per_ticker: Dict[str, Any] = {}
    errored_tickers: List[str] = []
    for ticker in tickers:
        try:
            per_ticker[ticker] = fetch_ticker_payload(ticker, trading_day, decision_time_et, interval_minutes)
        except Exception:  # noqa: BLE001 -- a fetch exception here is per-ticker evidence, not a caller bug
            errored_tickers.append(ticker)
    document: Dict[str, Any] = {
        "sources": {SOURCE_KEY: per_ticker},
        "source_errors": [],
        "ticker_errors": {},
    }
    if errored_tickers:
        document["ticker_errors"][SOURCE_KEY] = sorted(errored_tickers)
    return document


def _parse_decision_time(raw: str) -> dtime:
    try:
        hour_str, minute_str = raw.split(":", 1)
        return dtime(int(hour_str), int(minute_str))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"--decision-time-et must be HH:MM, got {raw!r}") from exc


def _parse_tickers(raw: str) -> List[str]:
    tickers = [entry.strip().upper() for entry in raw.split(",") if entry.strip()]
    if not tickers:
        raise argparse.ArgumentTypeError("--tickers must list at least one ticker symbol")
    return tickers


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tickers", required=True, type=_parse_tickers, help="comma-separated ticker symbols")
    parser.add_argument("--trading-day", required=True, type=date.fromisoformat)
    parser.add_argument(
        "--decision-time-et",
        required=True,
        type=_parse_decision_time,
        help="the slot's decision time, HH:MM 24-hour ET (e.g. 12:30, 15:30)",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        help="bar interval to fetch -- 15 to stand in for Finance (the default), 5 to stand in for Robinhood",
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    document = build_fallback_document(args.tickers, args.trading_day, args.decision_time_et, args.interval_minutes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "out": str(args.out),
        "tickers_fetched": sorted(document["sources"][SOURCE_KEY]),
        "ticker_errors": document["ticker_errors"].get(SOURCE_KEY, []),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
