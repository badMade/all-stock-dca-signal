"""Shadow-only comparison for the proposed 09:32 market-open fire (docs/market_open_0932_move.md).

Today's market-open slot decides on the completed 09:30 five-minute bar, which does not exist
before 09:35. The proposed 09:32 fire would decide on the live quote instead. Before that can
size a live order, the operator wants evidence of how far the two decision prices diverge.

This module only *records* that divergence. It never reads or writes sizing, config, state or
orders, and a recording failure must never affect a run (callers treat it as best-effort).
One JSON line per ticker is appended to ``<cron root>/market_open_quote_shadow/<YYYY>.jsonl``
and nothing is ever rewritten or deleted.

Standard library only; imports only the dependency-free :mod:`dca_paths`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import dca_paths

SCHEMA_VERSION = 1
SHADOW_DIR_NAME = "market_open_quote_shadow"


def shadow_root() -> Path:
    # Resolved at call time so $DCA_CRON_TRACKING_ROOT changes (and tests) are honoured.
    return Path(dca_paths.stream_root(SHADOW_DIR_NAME))


def _positive(value: Any) -> Optional[float]:
    """Finite, strictly positive float or None (bool is rejected, it is an int subclass)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _pct_vs(price: Optional[float], reference: Optional[float]) -> Optional[float]:
    if price is None or reference is None:
        return None
    return round((price - reference) / reference * 100.0, 4)


def compare_ticker(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Compare the live-quote decision price with the 09:30 five-minute bar close for one ticker.

    ``drop_vs_prev_close_pct`` is signed (negative = below the previous close) for both prices,
    which is the quantity the graded ladder tiers on. Missing inputs yield ``None`` fields, never
    a guess, so an unusable ticker stays visible in the record.
    """
    prev_bar = data.get("previous_close_bar") or {}
    quote = data.get("quote") or {}
    bars = data.get("intraday_bars") or []
    prev_close = _positive(prev_bar.get("close"))
    quote_price = _positive(quote.get("price"))
    bar_close = _positive(bars[0].get("close")) if bars and isinstance(bars[0], Mapping) else None
    quote_drop = _pct_vs(quote_price, prev_close)
    bar_drop = _pct_vs(bar_close, prev_close)
    return {
        "previous_close": prev_close,
        "quote_price": quote_price,
        "quote_as_of": quote.get("as_of"),
        "bar_close": bar_close,
        "quote_drop_vs_prev_close_pct": quote_drop,
        "bar_drop_vs_prev_close_pct": bar_drop,
        "quote_minus_bar_pct": _pct_vs(quote_price, bar_close),
        "drop_gap_pct_points": (
            round(quote_drop - bar_drop, 4) if quote_drop is not None and bar_drop is not None else None
        ),
    }


def build_records(
    market_data: Mapping[str, Mapping[str, Any]],
    *,
    trading_day: date,
    run_id: str,
    recorded_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    stamp = (recorded_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    records = []
    for ticker in sorted(market_data):
        row = compare_ticker(market_data[ticker] or {})
        records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "trading_day": trading_day.isoformat(),
                "run_id": run_id,
                "recorded_at": stamp,
                "ticker": ticker,
                **row,
            }
        )
    return records


def record_shadow(
    market_data: Mapping[str, Mapping[str, Any]],
    *,
    trading_day: date,
    run_id: str,
    root: Optional[Path] = None,
    recorded_at: Optional[datetime] = None,
) -> Path:
    """Append one JSON line per ticker; returns the file written. Never raises on empty input."""
    records = build_records(market_data, trading_day=trading_day, run_id=run_id, recorded_at=recorded_at)
    base = Path(root) if root is not None else shadow_root()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{trading_day.year}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m market_open_quote_shadow --market-data payload.json --trading-day YYYY-MM-DD``.

    Reads the same Gate 5 payload the candidate generator takes (``sources.robinhood``). Always
    exits 0 on a recording problem so a wrapper step can never block a live run; errors go to stderr.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market-data", type=Path, required=True)
    parser.add_argument("--trading-day", type=date.fromisoformat, required=True)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.market_data.read_text(encoding="utf-8"))
        market_data = payload["sources"]["robinhood"]
        run_id = args.run_id or f"market_open_live_{args.trading_day:%Y%m%d}_0935ET"
        path = record_shadow(market_data, trading_day=args.trading_day, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 -- shadow-only; must never block a live run
        print(f"quote-shadow not recorded: {exc}", file=sys.stderr)
        return 0
    print(f"quote-shadow recorded to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
