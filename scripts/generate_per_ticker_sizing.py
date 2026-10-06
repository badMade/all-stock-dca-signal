#!/usr/bin/env python3
"""Build the active per-stock DCA calibration from daily OHLCV history files."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dca_fileio import atomic_write_text
from dca_ticker_paths import configured_ticker_history_dir

# 90 calendar days is roughly 63 sessions, so the old 252-session bar (a 3-year window)
# can never be met. 40 keeps a ticker with under about two months of history (e.g. RVII)
# on the global fallback ladder rather than reading percentiles off a handful of days.
LOOKBACK_DAYS = 90
MINIMUM_HISTORY_SESSIONS = 40
# 2026-10-05: per-ticker tiers follow the owner-specified log-scaled curve, one tier per
# percentile from the 5th to the 100th (96 tiers). It matches the global ladder's top
# (config/sizing_ladder.json 2026-10-04 reaches 8.0x) at the 95th percentile, then the
# 96th..100th climb to 10.0x -- the headroom near_close_dca.graded_buy_multiplier's 10x
# ceiling (PR 1602) was raised to allow. The 100th percentile is a ticker's single
# largest adjusted prior-close-to-low drop inside the lookback window. The curve is flat
# at 1.00x through the 10th percentile (a calm day still buys the base amount).
PERCENTILE_MULTIPLIERS = (
    (0.05, 1.00), (0.06, 1.00), (0.07, 1.00), (0.08, 1.00), (0.09, 1.00), (0.10, 1.00),
    (0.11, 1.03), (0.12, 1.06), (0.13, 1.08), (0.14, 1.11), (0.15, 1.14), (0.16, 1.17),
    (0.17, 1.20), (0.18, 1.23), (0.19, 1.26), (0.20, 1.29), (0.21, 1.32), (0.22, 1.36),
    (0.23, 1.39), (0.24, 1.42), (0.25, 1.45), (0.26, 1.49), (0.27, 1.52), (0.28, 1.55),
    (0.29, 1.59), (0.30, 1.62), (0.31, 1.66), (0.32, 1.70), (0.33, 1.73), (0.34, 1.77),
    (0.35, 1.81), (0.36, 1.85), (0.37, 1.89), (0.38, 1.93), (0.39, 1.97), (0.40, 2.01),
    (0.41, 2.05), (0.42, 2.09), (0.43, 2.13), (0.44, 2.18), (0.45, 2.22), (0.46, 2.27),
    (0.47, 2.31), (0.48, 2.36), (0.49, 2.41), (0.50, 2.46), (0.51, 2.51), (0.52, 2.56),
    (0.53, 2.61), (0.54, 2.66), (0.55, 2.72), (0.56, 2.77), (0.57, 2.83), (0.58, 2.89),
    (0.59, 2.95), (0.60, 3.01), (0.61, 3.07), (0.62, 3.14), (0.63, 3.20), (0.64, 3.27),
    (0.65, 3.34), (0.66, 3.41), (0.67, 3.48), (0.68, 3.56), (0.69, 3.64), (0.70, 3.72),
    (0.71, 3.80), (0.72, 3.89), (0.73, 3.98), (0.74, 4.07), (0.75, 4.17), (0.76, 4.27),
    (0.77, 4.37), (0.78, 4.48), (0.79, 4.59), (0.80, 4.71), (0.81, 4.84), (0.82, 4.97),
    (0.83, 5.11), (0.84, 5.25), (0.85, 5.41), (0.86, 5.58), (0.87, 5.76), (0.88, 5.95),
    (0.89, 6.16), (0.90, 6.38), (0.91, 6.63), (0.92, 6.91), (0.93, 7.22), (0.94, 7.58),
    (0.95, 8.00), (0.96, 8.17), (0.97, 8.38), (0.98, 8.66), (0.99, 9.08), (1.00, 10.00),
)
# Mirrors config/sizing_ladder.json's tiers (min_drop_pct, multiplier) -- what a ticker
# without enough history of its own falls back to.
GLOBAL_FALLBACK_TIERS = (
    (0.1, 1.0), (0.5, 1.5), (1.0, 2.0), (1.5, 2.5), (2.0, 3.0), (2.75, 3.5), (3.5, 4.0), (4.5, 4.5),
    (5.5, 5.0), (7.0, 5.5), (8.5, 6.0), (10.5, 6.5), (12.5, 7.0), (14.5, 7.5), (16.0, 8.0),
)

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


def archive_unavailable_reason(history_dir: Path, ticker_config: Path) -> dict[str, Any] | None:
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
#: truncating older ones, and the rolling lookback window used to
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
    new_tickers: dict[str, Any], output: Path, lookback_days: int | None = None
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
    # Session counts are only comparable across publishes that used the same window:
    # the first publish after the 3-year -> 90-day switch (751 -> ~63 sessions) is a
    # deliberate change of window, not a truncated archive.
    if lookback_days is not None and previous.get("lookback_days") != lookback_days:
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
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is not None:
            d_idx = header.index("Date") if "Date" in header else -1
            c_idx = header.index("Close") if "Close" in header else -1
            ac_idx = header.index("Adj Close") if "Adj Close" in header else -1
            l_idx = header.index("Low") if "Low" in header else -1
            for row in reader:
                try:
                    d_val = row[d_idx] if d_idx != -1 and len(row) > d_idx else None
                    c_val = row[c_idx] if c_idx != -1 and len(row) > c_idx else None
                    ac_val = row[ac_idx] if ac_idx != -1 and len(row) > ac_idx else None
                    l_val = row[l_idx] if l_idx != -1 and len(row) > l_idx else None

                    if (
                        d_val is not None
                        and c_val is not None
                        and ac_val is not None
                        and l_val is not None
                        and d_val >= start_date
                    ):
                        prices.append((float(c_val), float(ac_val), float(l_val)))
                except (ValueError, TypeError):
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
    """Turn daily drop history into precise percentile-based dollar tiers.

    Ties (a low-volatility ticker whose adjacent percentiles round to the same drop)
    collapse to the highest multiplier at that threshold -- exactly what the runtime's
    "largest threshold <= drop wins, last of equals wins" lookup would pick anyway.
    """
    by_threshold: dict[float, float] = {}
    for percentile, multiplier in PERCENTILE_MULTIPLIERS:
        threshold = round(max(0.01, quantile(drops, percentile)), 4)
        by_threshold[threshold] = max(multiplier, by_threshold.get(threshold, 0.0))
    return [{"min_drop_pct": threshold, "multiplier": multiplier} for threshold, multiplier in sorted(by_threshold.items())]


_TIER_BLOCK = re.compile(r'\{\s+"min_drop_pct": ([^,\n]+),\s+"multiplier": ([^\n]+)\s+\}')


def render_payload(payload: dict[str, Any]) -> str:
    """Serialize the calibration with each tier on ONE line.

    A 96-tier-per-ticker payload at ``indent=2`` spends four lines per tier (about 22,000 lines
    for the universe); one line per tier keeps the file and its git diffs reviewable
    and parses identically.
    """
    text = json.dumps(payload, indent=2)
    return _TIER_BLOCK.sub(lambda m: f'{{"min_drop_pct": {m.group(1)}, "multiplier": {m.group(2)}}}', text) + "\n"


def build_sizing_payload(
    history_dir: Path,
    ticker_config: Path,
    *,
    lookback_days: int = LOOKBACK_DAYS,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build the complete active-plus-whole-share-universe calibration without
    writing it.

    The daily Claude-only refresh imports this function after it has verified
    that every history file refreshed successfully.  Separating calculation
    from file output prevents it from publishing a partial model.
    """
    ticker_data = json.loads(ticker_config.read_bytes())
    # A whole-share ticker (e.g. BYDDY) is still evaluated by the SAME graded
    # ladder as the active universe (see docs/whole_share_accumulation.md) and
    # must never fall back to config.base_buy_amount_usd's $2 default -- every
    # other ticker's calibration deliberately floors both base amounts at $1
    # (see this module's build_sizing_payload docstring above). Include it in
    # the universe this generator calibrates so a routine regen never drops
    # its entry and silently doubles its sizing (Codex review: "Give BYDDY
    # the configured one-dollar sizing base").
    universe = list(ticker_data["active"]) + [
        ticker for ticker in ticker_data.get("whole_share", []) if ticker not in ticker_data["active"]
    ]
    effective_date = as_of or date.today()
    start_date = (effective_date - timedelta(days=lookback_days)).isoformat()
    tickers = {}
    fallback = []
    for ticker in universe:
        path = history_dir / f"{ticker}.csv"
        drops = adjusted_low_drops(path, start_date) if path.exists() else []
        if len(drops) >= MINIMUM_HISTORY_SESSIONS:
            tiers = tiers_from_drops(drops)
            source = "ninety_day_adjusted_daily_low_percentiles"
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
            # Calibrated tickers' tier points are samples of one smooth curve, so the runtime
            # blends between them; a fallback ticker carries the global ladder and stays stepped.
            **({"interpolation": "linear"} if source != "global_fallback_insufficient_history" else {}),
            "source": source,
            "sample_sessions": len(drops),
        }

    return {
        "version": effective_date.isoformat(),
        "status": "active",
        "description": "Per-stock DCA: all positive first orders start at $1.00; later amounts are precise historical-drop tiers.",
        "method": (
            "Per ticker, the 5th..100th percentiles (96 tiers) of the last 90 days of adjusted prior-close-to-low drops "
            "map to a log-scaled 1.00x..10.00x curve (8.00x at the 95th, 10.00x at the 100th = the window's largest drop)."
        ),
        "lookback_days": lookback_days,
        "minimum_order_usd": 1.0,
        "fallback_tickers": fallback,
        "tickers": tickers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-dir", type=Path, default=configured_ticker_history_dir())
    parser.add_argument("--ticker-config", type=Path, default=Path("config/tickers.json"))
    parser.add_argument("--output", type=Path, default=Path("config/per_ticker_sizing.json"))
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
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
        lookback_days=args.lookback_days,
    )
    # Passing the archive-availability guard only rules out an unmounted or
    # near-empty archive; it says nothing about a single established
    # ticker's own CSV going missing or being truncated by a bad merge,
    # which still satisfies the 50% active-universe coverage bar while
    # silently downgrading that ticker to global_fallback_insufficient_
    # history for this and every future run (Codex review: "Check session
    # regressions in the standalone publisher").
    regressions = session_count_regressions(payload["tickers"], args.output, payload["lookback_days"])
    if regressions:
        print(
            f"REFUSED {args.output}: session count regression: {regressions}",
            file=sys.stderr,
        )
        return 1
    atomic_write_text(args.output, render_payload(payload))
    active = payload["tickers"]
    fallback = payload["fallback_tickers"]
    print(f"WROTE {args.output}: calibrated={len(active) - len(fallback)} fallback={len(fallback)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
