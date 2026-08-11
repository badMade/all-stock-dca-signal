#!/usr/bin/env python3
"""Throwaway harness for the 12:30 PM ET midday-live signal generation slot.

THIS FILE IS NOT PART OF THE SHIPPED PACKAGE (wheel). It lives under claude/_agent_scratch/
purely so a live executor session (which holds real Robinhood MCP access but no
Python/file-execution access of its own) can feed pre-fetched raw MCP responses
through the repo's OWN signal-generation entry point --
``midday_dca.MiddayDCAExecutor`` -- instead of a reimplementation, per
scripts/agent_entry_points.py and docs/live_wiring.md ("An external agent
session ... constructs the class and calls .run(now, data, broker=...)").

Modeled directly on claude/_agent_scratch/morning_signal_harness.py (the 10:30 AM
morning-slot harness), swapped to MiddayDCAExecutor/MIDDAY_LIVE_MODE so the
midday stream's own afternoon 0x->5x ladder, midday_signals.csv identity, and
same-day top-up hooks are used instead of the morning executor's.

SAFETY -- read this before trusting the output: identical analysis to
morning_signal_harness.py applies verbatim. ``executor.run(now, data_provider,
broker=None, manual=True)`` makes ``_place_broker_orders``/``place_market_buy``
provably unreachable (the only call site is gated on ``broker is not None``),
and ``manual=True`` skips the separate scheduled-run-with-no-broker early return.
This harness contains no order-placement code of its own.

This script performs no network I/O. All market data comes from the JSON file
passed via --data-file (see morning_signal_harness.py's module docstring for the
exact schema: {"historicals": [...], "quotes": [...]}, both raw MCP responses).

Usage:
    python3 claude/_agent_scratch/midday_signal_harness.py \\
        --data-file /path/to/prefetched_data.json \\
        --now 2026-08-10T12:30:00 \\
        --out /path/to/candidates.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import os  # noqa: E402

os.environ.setdefault(
    "DCA_CRON_TRACKING_ROOT", str(REPO_ROOT / "cron_tracking")
)

from dca_live_adapters import RobinhoodDataProvider  # noqa: E402
from midday_dca import MIDDAY_LIVE_MODE, MiddayDCAExecutor  # noqa: E402

EASTERN = ZoneInfo("America/New_York")


def _unwrap(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    return data


def _merge_results(batches: List[Any]) -> Dict[str, Any]:
    merged: List[Any] = []
    for batch in batches:
        data = _unwrap(batch)
        results = data.get("results")
        if isinstance(results, list):
            merged.extend(results)
    return {"results": merged}


def build_data_provider(data_file: Path) -> RobinhoodDataProvider:
    payload = json.loads(data_file.read_text())
    historicals_batches = payload.get("historicals") or []
    quotes_batches = payload.get("quotes") or []
    if not isinstance(historicals_batches, list):
        raise ValueError("--data-file 'historicals' must be a JSON array")
    if not isinstance(quotes_batches, list):
        raise ValueError("--data-file 'quotes' must be a JSON array")

    merged_historicals = _merge_results(historicals_batches)
    merged_quotes = _merge_results(quotes_batches)

    def historicals_fn(**kwargs: Any) -> Dict[str, Any]:
        return merged_historicals

    def quotes_fn(**kwargs: Any) -> Dict[str, Any]:
        return merged_quotes

    return RobinhoodDataProvider(historicals_fn, quotes_fn)


def parse_now(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt.astimezone(EASTERN)


def extract_candidates(run_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    artifacts = run_result.get("artifacts") or {}
    rows = artifacts.get("signal_rows") or []
    candidates = []
    for row in rows:
        execution_status = row.get("execution_status")
        candidates.append(
            {
                "ticker": row.get("ticker"),
                "action": row.get("action"),
                "current_price": row.get("current_price"),
                "previous_close": row.get("previous_close"),
                "previous_low": row.get("previous_low"),
                "drop_pct": row.get("drop_pct"),
                "multiplier": row.get("multiplier"),
                "selected_base_amount_usd": row.get("selected_base_amount_usd"),
                "hypothetical_amount_usd": row.get("hypothetical_amount_usd"),
                "execution_status": execution_status,
                "auto_buy_eligible": execution_status == "auto_buy_eligible",
                "manual_review_candidate": execution_status == "manual_review_candidate",
                "trend_validated": row.get("trend_validated"),
                "reason": row.get("reason"),
                "warnings": row.get("warnings"),
            }
        )
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-file", required=True, type=Path)
    parser.add_argument("--now", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    now = parse_now(args.now)
    data_provider = build_data_provider(args.data_file)

    executor = MiddayDCAExecutor(mode=MIDDAY_LIVE_MODE)

    result = executor.run(now, data_provider, broker=None, manual=True)

    candidates = extract_candidates(result)
    candidates.sort(key=lambda c: (-(c["multiplier"] or 0.0), c["ticker"] or ""))

    output = {
        "now": now.isoformat(),
        "run_id": (result.get("artifacts") or {}).get("run_id"),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    print(json.dumps(output, indent=2, default=str))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(output, indent=2, default=str))
        print(f"\nWrote {len(candidates)} candidate rows to {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
