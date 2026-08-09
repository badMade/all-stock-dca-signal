#!/usr/bin/env python3
"""Build the active per-stock DCA calibration from daily OHLCV history files."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date
from pathlib import Path

from dca_fileio import atomic_write_text
from dca_ticker_paths import configured_ticker_history_dir

MINIMUM_HISTORY_SESSIONS = 252
# Frequent first buys, then increasingly rare volatility-driven dollar amounts.
PERCENTILE_MULTIPLIERS = ((0.25, 1.0), (0.50, 1.5), (0.75, 2.5), (0.90, 4.0), (0.975, 5.0))
GLOBAL_FALLBACK_TIERS = ((0.1, 1.0), (2.0, 1.5), (4.0, 2.0), (5.0, 2.5), (6.0, 3.0), (6.5, 3.5), (7.0, 4.0), (7.5, 4.5), (8.0, 5.0))

#: Minimum fraction of the active universe that must have a present history
#: CSV before a freshly built calibration is trusted enough to publish.
#: Below this, a missing/unmounted ticker_data archive or an empty target
#: directory looks statistically identical to "every active ticker has
#: insufficient history" -- build_sizing_payload cannot tell the difference,
#: so without this check a caller would atomically replace the live model
#: with global fallback tiers for the ENTIRE active universe on a transient
#: mount failure. Shared by this module's own CLI and by
#: daily_per_ticker_calibration.py's event-driven publish path so neither
#: can bypass the guard (Codex review: "Refuse default calibration when the
#: archive is missing" -- direct/legacy invocation of this CLI must not
#: skip the coverage check daily_per_ticker_calibration.py already enforces).
MINIMUM_ACTIVE_HISTORY_COVERAGE = 0.5


def archive_unavailable_reason(history_dir: Path, ticker_config: Path) -> dict[str, object] | None:
    """``None`` when the archive has enough active-symbol coverage to trust a
    calibration built from it; otherwise a dict describing why publishing
    must be refused.
    """
    if not history_dir.is_dir():
        return {"reason": "history_dir_missing", "history_dir": str(history_dir)}
    try:
        active = json.loads(ticker_config.read_bytes())["active"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return {"reason": f"ticker_config_unreadable: {exc}"}
    if not active:
        return None
    missing = sorted(ticker for ticker in active if not (history_dir / f"{ticker}.csv").is_file())
    coverage = 1.0 - (len(missing) / len(active))
    if coverage < MINIMUM_ACTIVE_HISTORY_COVERAGE:
        return {
            "reason": "insufficient_active_history_coverage",
            "active_tickers": len(active),
            "missing_history_tickers": missing,
            "coverage": round(coverage, 4),
            "minimum_required_coverage": MINIMUM_ACTIVE_HISTORY_COVERAGE,
        }
    return None


#: A published ticker's sample_sessions dropping by more than this fraction
#: versus the currently-live config is treated as evidence of a truncated or
#: malformed archive merge for THAT ticker, not a real change in available
#: history: the daily updater's contract is to merge in recent rows without
#: truncating older ones, and the rolling three-year lookback window used to
#: count sessions only advances by about one trading day per nightly run, so
#: a healthy day-to-day recalculation changes the count only marginally. 5%
#: is already generous headroom for calendar/holiday edge effects across a
#: multi-day gap between runs -- a bound anywhere near 50% would still let a
#: ticker's usable history collapse by hundreds of sessions (e.g. 751 to
#: 376) and publish anyway. Shared by this module's own CLI and by
#: daily_per_ticker_calibration.py's event-driven publish path (Codex
#: review: "Check session regressions in the standalone publisher" -- a
#: direct/legacy invocation of this CLI must not skip the same per-ticker
#: regression check daily_per_ticker_calibration.py already enforces).
MAXIMUM_SESSION_COUNT_REGRESSION_FRACTION = 0.05


def session_count_regressions(
    new_tickers: dict[str, object], output: Path
) -> dict[str, dict[str, int]]:
    """Return ``{ticker: {"previous": N, "new": M}}`` for every ticker whose
    ``sample_sessions`` collapsed by more than the allowed fraction versus
    the config currently published at ``output``.

    Reads whatever is currently live at ``output`` rather than requiring a
    caller-supplied baseline, so this catches a corrupted merge regardless of
    which publish path (event-driven or standalone) produced it.
    """
    try:
        previous = json.loads(output.read_bytes())
    except (OSError, json.JSONDecodeError):
        return {}
    previous_tickers = previous.get("tickers")
    if not isinstance(previous_tickers, dict):
        return {}
    regressions: dict[str, dict[str, int]] = {}
    for ticker, entry in new_tickers.items():
        prior_entry = previous_tickers.get(ticker)
        if not isinstance(prior_entry, dict):
            continue
        prior_sessions = prior_entry.get("sample_sessions")
        new_sessions = entry.get("sample_sessions")
        if not isinstance(prior_sessions, int) or not isinstance(new_sessions, int):
            continue
        if prior_sessions <= 0:
            continue
        if new_sessions < prior_sessions * (1 - MAXIMUM_SESSION_COUNT_REGRESSION_FRACTION):
            regressions[ticker] = {"previous": prior_sessions, "new": new_sessions}
    return regressions


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
            except (KeyError, TypeError, ValueError):
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


def build_sizing_payload(
    history_dir: Path,
    ticker_config: Path,
    *,
    lookback_years: int = 3,
    as_of: date | None = None,
) -> dict[str, object]:
    """Build the complete active-universe calibration without writing it.

    The daily Claude-only refresh imports this function after it has verified
    that every history file refreshed successfully.  Separating calculation
    from file output prevents it from publishing a partial model.
    """
    active = json.loads(ticker_config.read_bytes())["active"]
    effective_date = as_of or date.today()
    start_date = (
        f"{effective_date.year - lookback_years:04d}-{effective_date.month:02d}-{effective_date.day:02d}"
    )
    tickers = {}
    fallback = []
    for ticker in active:
        path = history_dir / f"{ticker}.csv"
        drops = adjusted_low_drops(path, start_date) if path.exists() else []
        if len(drops) >= MINIMUM_HISTORY_SESSIONS:
            tiers = tiers_from_drops(drops)
            source = "three_year_adjusted_daily_low_percentiles"
        else:
            tiers = [
                {"min_drop_pct": threshold, "multiplier": multiplier}
                for threshold, multiplier in GLOBAL_FALLBACK_TIERS
            ]
            source = "global_fallback_insufficient_history"
            fallback.append(ticker)
        tickers[ticker] = {
            "base_amount_usd": 1.0,
            "base_amount_below_close_usd": 1.0,
            "tiers": tiers,
            "source": source,
            "sample_sessions": len(drops),
        }

    return {
        "version": effective_date.isoformat(),
        "status": "active",
        "description": "Per-stock DCA: all positive first orders start at $1.00; later amounts are precise historical-drop tiers.",
        "method": "25th/50th/75th/90th/97.5th percentile adjusted prior-close-to-low drops map to 1x/1.5x/2.5x/4x/5x.",
        "minimum_order_usd": 1.0,
        "fallback_tickers": fallback,
        "tickers": tickers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, default=configured_ticker_history_dir())
    parser.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    parser.add_argument("--output", type=Path, default=Path("config/per_ticker_sizing.json"))
    parser.add_argument("--lookback-years", type=int, default=3)
    args = parser.parse_args()

    # A direct or legacy invocation of this CLI has no upstream caller to
    # verify the history refresh actually succeeded first, so it must apply
    # the same archive-availability guard daily_per_ticker_calibration.py's
    # event-driven path enforces: build_sizing_payload cannot distinguish a
    # missing/unmounted archive from "every ticker genuinely lacks history",
    # and would otherwise overwrite the live config with global fallback
    # tiers for the whole active universe (Codex review: "Refuse default
    # calibration when the archive is missing").
    unavailable = archive_unavailable_reason(args.history_dir, args.ticker_config)
    if unavailable is not None:
        print(f"REFUSED {args.output}: archive unavailable: {unavailable}", file=sys.stderr)
        return 1

    payload = build_sizing_payload(
        args.history_dir,
        args.ticker_config,
        lookback_years=args.lookback_years,
    )
    # Passing the archive-availability guard only rules out an unmounted or
    # near-empty archive; it says nothing about a single established
    # ticker's own CSV going missing or being truncated by a bad merge,
    # which still satisfies the 50% active-universe coverage bar while
    # silently downgrading that ticker to global_fallback_insufficient_
    # history for this and every future run (Codex review: "Check session
    # regressions in the standalone publisher").
    regressions = session_count_regressions(payload["tickers"], args.output)
    if regressions:
        print(
            f"REFUSED {args.output}: session count regression: {regressions}",
            file=sys.stderr,
        )
        return 1
    atomic_write_text(args.output, json.dumps(payload, indent=2) + "\n")
    active = payload["tickers"]
    fallback = payload["fallback_tickers"]
    print(f"WROTE {args.output}: calibrated={len(active) - len(fallback)} fallback={len(fallback)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
