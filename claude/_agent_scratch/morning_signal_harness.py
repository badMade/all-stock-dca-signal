#!/usr/bin/env python3
"""Throwaway harness for the 10:30 AM ET morning-live signal generation slot.

THIS FILE IS NOT PART OF THE SHIPPED PACKAGE (wheel). It lives under claude/_agent_scratch/
purely so a live executor session (which holds real Robinhood MCP access but no
Python/file-execution access of its own) can feed pre-fetched raw MCP responses
through the repo's OWN signal-generation entry point --
``morning_dca.MorningDCAExecutor`` -- instead of a reimplementation, per
scripts/agent_entry_points.py and docs/live_wiring.md ("An external agent
session ... constructs the class and calls .run(now, data, broker=...)").

SAFETY -- read this before trusting the output:

This script calls ``executor.run(now, data_provider, broker=None, manual=True)``.
Reading morning_dca.py's ``_run_impl`` (the function ``run()`` delegates to)
confirms there is exactly ONE call site for ``_place_broker_orders`` -- the only
function in the whole class that ever calls ``broker.place_market_buy``:

    if broker is not None and self.mode == MORNING_LIVE_MODE:
        ...
        broker_outcomes = self._place_broker_orders(...)

With ``broker=None`` that branch's condition is false and the block never runs,
so ``_place_broker_orders``/``place_market_buy`` is provably unreachable from this
invocation -- independent of ``self.trades``, ``self.mode``, or any other executor
state. (There is a SEPARATE early-return that blocks a *scheduled* run with a
missing broker -- ``execution_enabled and mode==LIVE and self.trades and not manual
and broker is None`` -- but it is gated on ``not manual``, so passing
``manual=True`` here means that branch is skipped too and the run instead falls
through as a full "shadow" evaluation: real signals, real hypothetical dollar
amounts, zero orders.) This harness also contains NO order-placement code of its
own -- it only reads the ``run()`` return value and prints/serializes it.

This script performs no network I/O. All market data comes from the JSON file
passed via --data-file, which the caller must populate with real (or synthetic,
for smoke-testing) raw ``get_equity_historicals`` / ``get_equity_quotes`` MCP
tool responses. Although the executor's manual path writes artifacts and may
record cumulative top-up credit, both its artifact root and ticker-history
archive are forcibly redirected to a temporary directory for this process.
Inherited production path environment variables are never used.

------------------------------------------------------------------------------
--data-file JSON schema
------------------------------------------------------------------------------
{
  "historicals": [ <raw get_equity_historicals response>, <raw get_equity_historicals response>, ... ],
  "quotes":      [ <raw get_equity_quotes response>,      <raw get_equity_quotes response>,      ... ]
}

Both lists hold RAW MCP tool responses exactly as returned by the tool call --
each batch may (and for historicals, given the 10-symbols-per-call limit,
normally will) cover MULTIPLE symbols. Do not pre-split them by symbol: just
append every raw response object you got back from each tool call. The loader
merges every batch's "results" list into one big pool and lets
RobinhoodDataProvider's own per-ticker filtering (which it already does
internally) pick out the right symbol for each requested ticker, so batch
boundaries and call order do not matter.

A raw response may or may not be wrapped in {"data": {...}}; both shapes are
accepted (mirrors dca_live_adapters._unwrap).

Expected inner shape (both dca_live_adapters.py and Robinhood's own contract):

  historicals response: {"results": [{"symbol": "AAPL", "bars": [
        {"begins_at": "2026-08-10T13:30:00Z", "open_price": "1.00",
         "high_price": "1.02", "low_price": "0.99", "close_price": "1.01",
         "volume": "1000", "interpolated": false}, ...]}, ...]}

  quotes response: {"results": [{"quote": {"symbol": "AAPL",
        "last_trade_price": "1.01", "has_traded": true, "state": "active",
        "venue_last_trade_time": "2026-08-10T14:29:00Z",
        "previous_close": "1.05", "previous_close_date": "2026-08-07",
        "bid_price": "1.00", "ask_price": "1.02"},
        "close": {"price": "1.05", "date": "2026-08-07"}}, ...]}

(``close``/``previous_close`` is what feeds get_previous_close_bar; without a
usable previous_close/close.price for a ticker it falls back to a daily-bar
historicals lookup, which this harness's ignore-the-time-range historicals_fn
also happens to satisfy since it always returns the full merged pool.)

------------------------------------------------------------------------------
Usage
------------------------------------------------------------------------------
    python3 claude/_agent_scratch/morning_signal_harness.py \\
        --data-file /path/to/prefetched_data.json \\
        --now 2026-08-10T10:30:00 \\
        --out /path/to/candidates.json

``--now`` is parsed as America/New_York if no UTC offset is given (matches the
strategy's own timezone). ``--out`` is optional; the candidate list is always
printed to stdout as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

# Make the repo root importable regardless of the caller's cwd. This file lives
# at <repo>/claude/_agent_scratch/morning_signal_harness.py, so the repo root is
# two parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Manual evaluation still writes run artifacts, observations, and cumulative
# top-up credits.  Redirect every persistent root before importing the DCA
# modules so synthetic harness input can never contaminate a production tree
# inherited through the caller's environment.  Keep the temporary directory
# alive until interpreter shutdown.
_HARNESS_STATE = tempfile.TemporaryDirectory(prefix="morning-signal-harness-")
_HARNESS_ROOT = Path(_HARNESS_STATE.name)
(_HARNESS_ROOT / "ticker_data").mkdir()
os.environ["DCA_CRON_TRACKING_ROOT"] = str(_HARNESS_ROOT / "cron_tracking")
os.environ["DCA_TICKER_HISTORY_DIR"] = str(_HARNESS_ROOT / "ticker_data")

from dca_live_adapters import RobinhoodDataProvider  # noqa: E402
from morning_dca import MORNING_LIVE_MODE, MorningDCAExecutor  # noqa: E402

EASTERN = ZoneInfo("America/New_York")


def _unwrap(payload: Any) -> Dict[str, Any]:
    """Mirror dca_live_adapters._unwrap: pull payload['data'] if it's a dict,
    otherwise treat payload itself as the data dict."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    return data


def _merge_results(batches: List[Any]) -> Dict[str, Any]:
    """Flatten a list of raw MCP batch responses into one {"results": [...]}.

    See the module docstring's schema section for why this is safe: both
    RobinhoodDataProvider._parse_bars and ._quote_for already filter the
    "results" list by the requested ticker symbol, so serving the FULL merged
    pool on every call (regardless of which symbols/time-range were actually
    requested by that particular call) is correct and far less error-prone
    than trying to route each call to "the right" batch.
    """
    merged: List[Any] = []
    for batch in batches:
        data = _unwrap(batch)
        results = data.get("results")
        if isinstance(results, list):
            merged.extend(results)
    return {"results": merged}


def build_data_provider(data_file: Path) -> RobinhoodDataProvider:
    payload = json.loads(data_file.read_bytes())
    historicals_batches = payload.get("historicals") or []
    quotes_batches = payload.get("quotes") or []
    if not isinstance(historicals_batches, list):
        raise ValueError(
            "--data-file 'historicals' must be a JSON array of raw "
            "get_equity_historicals responses (see this script's module docstring)"
        )
    if not isinstance(quotes_batches, list):
        raise ValueError(
            "--data-file 'quotes' must be a JSON array of raw "
            "get_equity_quotes responses (see this script's module docstring)"
        )

    merged_historicals = _merge_results(historicals_batches)
    merged_quotes = _merge_results(quotes_batches)

    def historicals_fn(**kwargs: Any) -> Dict[str, Any]:
        # symbols/start_time/end_time/interval/bounds/adjustment_type are all
        # accepted (RobinhoodDataProvider always passes them as keywords) and
        # deliberately ignored -- the caller pre-fetched a window wide enough
        # to cover both the prior close and the 9:30-10:30 trend, and
        # RobinhoodDataProvider filters the returned bars by ticker itself.
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
    """Pull the Gate-7 buy-list view out of the executor's run_state artifact.

    ``run_result["artifacts"]["signal_rows"]`` is exactly what
    MorningDCAExecutor.build_artifacts() writes to morning_signals.csv (one row
    per MorningSignal.to_row()) -- the same data the live artifact set records,
    not a reimplementation.
    """
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
    parser.add_argument(
        "--now",
        required=True,
        help="ISO-8601 timestamp, e.g. 2026-08-10T10:30:00 "
        "(assumed America/New_York if no UTC offset is given)",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    now = parse_now(args.now)
    data_provider = build_data_provider(args.data_file)

    executor = MorningDCAExecutor(mode=MORNING_LIVE_MODE)

    # broker=None, manual=True: see the module docstring above for the exact
    # code path this makes provably unreachable. No order can be placed by
    # this invocation.
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
