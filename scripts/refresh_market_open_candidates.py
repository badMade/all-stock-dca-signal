#!/usr/bin/env python3
"""Re-size 0935ET candidates from placement-boundary, post-anchor quotes.

Unlike the Gate 5 evaluator, this entry point deliberately does not resolve a
signal-time price from bars.  It consumes the explicit quote supplied by the
caller and applies the executor's canonical base and ladder functions to it.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_open_dca import MARKET_OPEN_LIVE_MODE, MarketOpenDCAExecutor, load_market_open_config
from near_close_dca import select_first_allocation_base_usd
from scripts.stream_candidates import _calibration_fields

MAX_REFRESH_QUOTE_AGE = timedelta(minutes=5)
MAX_REFRESH_QUOTE_FUTURE_SKEW = timedelta(seconds=30)
MARKET_TIMEZONE = ZoneInfo("America/New_York")


def refresh_market_open_candidates(
    candidates: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    config,
    *,
    trading_day: date,
    placement_time: datetime,
) -> dict[str, Any]:
    """Return candidates canonically re-sized at their supplied fresh quotes."""
    if placement_time.tzinfo is None:
        raise ValueError("placement_time must be timezone-aware")
    placement_time = placement_time.astimezone(MARKET_TIMEZONE)
    if placement_time.date() != trading_day:
        raise ValueError("placement_time must belong to trading_day in America/New_York")
    executor = MarketOpenDCAExecutor(config=config, mode=MARKET_OPEN_LIVE_MODE)
    refreshed: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker", "")).upper()
        quote = quotes.get(ticker)
        if not ticker or not isinstance(quote, Mapping):
            dropped.append({"ticker": ticker, "reason": "missing_refresh_quote"})
            continue
        price = quote.get("price")
        as_of_raw = quote.get("as_of")
        if isinstance(price, bool):
            dropped.append({"ticker": ticker, "reason": "invalid_refresh_quote"})
            continue
        try:
            price = float(price)
            as_of = datetime.fromisoformat(str(as_of_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            dropped.append({"ticker": ticker, "reason": "invalid_refresh_quote"})
            continue
        if not math.isfinite(price) or price <= 0 or as_of.tzinfo is None:
            dropped.append({"ticker": ticker, "reason": "invalid_refresh_quote"})
            continue
        as_of_market = as_of.astimezone(MARKET_TIMEZONE)
        quote_age = placement_time - as_of_market
        if (
            as_of_market.date() != trading_day
            or quote_age > MAX_REFRESH_QUOTE_AGE
            or quote_age < -MAX_REFRESH_QUOTE_FUTURE_SKEW
        ):
            dropped.append({"ticker": ticker, "reason": "stale_refresh_quote"})
            continue
        try:
            prior_close = float(candidate["prior_close"])
            prior_low_raw = candidate.get("prior_low")
            prior_low = None if prior_low_raw is None else float(prior_low_raw)
        except (KeyError, TypeError, ValueError):
            dropped.append({"ticker": ticker, "reason": "invalid_price_reference"})
            continue
        if not math.isfinite(prior_close) or prior_close <= 0:
            dropped.append({"ticker": ticker, "reason": "invalid_price_reference"})
            continue

        drop_fraction = (prior_close - price) / prior_close
        multiplier = executor.multiplier_for_drop(drop_fraction, ticker)
        base, _ = select_first_allocation_base_usd(
            price, prior_close, prior_low, executor.sizing_config, ticker
        )
        try:
            authorized_base = float(candidate["selected_base_amount_usd"])
        except (KeyError, TypeError, ValueError):
            dropped.append({"ticker": ticker, "reason": "invalid_authorized_base"})
            continue
        if not math.isfinite(authorized_base) or authorized_base <= 0:
            dropped.append({"ticker": ticker, "reason": "invalid_authorized_base"})
            continue
        if base < authorized_base:
            dropped.append({"ticker": ticker, "reason": "base_no_longer_supported"})
            continue
        desired = round(base * multiplier, 2)
        if desired <= 0:
            dropped.append({"ticker": ticker, "reason": "no_longer_ladder_eligible"})
            continue
        try:
            authorized_desired = float(candidate["desired_usd"])
        except (KeyError, TypeError, ValueError):
            dropped.append({"ticker": ticker, "reason": "invalid_authorized_amount"})
            continue
        if not math.isfinite(authorized_desired) or authorized_desired <= 0:
            dropped.append({"ticker": ticker, "reason": "invalid_authorized_amount"})
            continue
        if desired > authorized_desired:
            dropped.append({"ticker": ticker, "reason": "refresh_would_increase_size"})
            continue
        refreshed.append(
            {
                **candidate,
                "current_price": price,
                "quote_as_of": as_of.isoformat(),
                "desired_usd": desired,
                "selected_base_amount_usd": base,
                "multiplier": multiplier,
                "drop_pct": drop_fraction * 100.0,
                **_calibration_fields(ticker, multiplier, base),
            }
        )
    return {"cands": refreshed, "dropped": dropped}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--quotes", required=True, type=Path)
    parser.add_argument("--trading-day", required=True, type=date.fromisoformat)
    parser.add_argument("--placement-time", required=True, type=datetime.fromisoformat)
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args(argv)
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    quotes = json.loads(args.quotes.read_text(encoding="utf-8"))
    if not isinstance(candidates, list) or not all(isinstance(candidate, Mapping) for candidate in candidates):
        parser.error("--candidates must contain a JSON list of objects")
    if not isinstance(quotes, Mapping):
        parser.error("--quotes must contain a JSON object")
    config = load_market_open_config(args.config_dir, allow_unpinned_account=True)
    result = refresh_market_open_candidates(
        candidates,
        quotes,
        config,
        trading_day=args.trading_day,
        placement_time=args.placement_time,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
