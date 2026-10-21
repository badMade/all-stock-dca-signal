"""Print the buys a slot's agent may place: ticker, usd and ref_id only, or why there are none.

This is the buying agent's ONLY input (PLAN-plan-first.md). It never prints a price, a
quote or an account number. Prices passing through the model, and the fractional
arithmetic done on them, are the failure this design removes.

CLI::

    python3 -m scripts.read_recommendation --slot 1230 [--now 2026-10-13T12:07:00-04:00]

Output is one compact JSON line:

* ``{"status":"OK","slot":"1230","as_of":"...","age_s":420,"save_to":"<dir>","orders":[{"ticker":"AAPL","usd":3.0,"ref_id":"..."}]}``
  exits 0. It also records which set was handed out (``handed_<slot>.json``) for the fill comparison. An empty ``orders`` list is a valid "buy nothing this slot".
* ``{"status":"NONE","slot":"1230","reason":"stale"}`` exits 3. The agent buys nothing and alerts.
  ``no_file_for_slot_today`` early in a window can mean the courier has not finished its
  first run yet; the reader prompt may retry.
* Exit 2 means a usage or settings error. The agent buys nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from dca_rec_models import SLOT_IDS
from dca_rec_store import RecommendationStore, load_settings

ET = ZoneInfo("America/New_York")
EXIT_OK, EXIT_ERROR, EXIT_NONE = 0, 2, 3


def _now(text: str | None) -> datetime:
    if text is None:
        return datetime.now(ET)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("--now must carry a UTC offset")
    return moment


def _emit(document: dict) -> None:
    print(json.dumps(document, separators=(",", ":"), allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slot", required=True, choices=SLOT_IDS)
    parser.add_argument("--now", help="timezone-aware ISO timestamp (testing and replay only)")
    parser.add_argument("--config-dir")
    parser.add_argument("--root", help="recommendations root (default: <cron root>/recommendations)")
    args = parser.parse_args(argv)
    try:
        now = _now(args.now)
        settings = load_settings(args.config_dir)
    except ValueError as exc:
        print(f"read_recommendation: {exc}", file=sys.stderr)
        return EXIT_ERROR
    selection = RecommendationStore(args.root).select_for(args.slot, now, settings.max_age)
    if selection.status != "OK" or selection.recommendation is None:
        _emit({"status": "NONE", "slot": args.slot, "reason": selection.reason})
        return EXIT_NONE
    rec = selection.recommendation
    # Never emit executable orders from a cached set the CURRENT config would refuse (PR #1815 review).
    from scripts.backup_planner import current_config_reason

    reason = current_config_reason(rec, trading_day=now.astimezone(ET).date(), slot=args.slot,
                                   config_dir=Path(args.config_dir) if args.config_dir else None)
    if reason:
        _emit({"status": "NONE", "slot": args.slot, "reason": f"config_check_failed:{reason}"})
        return EXIT_NONE
    store = RecommendationStore(args.root)
    try:
        store.record_handed(rec)
    except ValueError as exc:
        # The slot already owns a different handed set: never replace it under a reader that may be placing.
        _emit({"status": "NONE", "slot": args.slot, "reason": str(exc)})
        return EXIT_NONE
    # save_to is where the agent writes each raw place_equity_order response as <TICKER>.json;
    # dca_fill_compare reads them there, so the agent never computes or reports a price.
    _emit({
        "status": "OK", "slot": rec.slot, "as_of": rec.as_of.isoformat(), "age_s": round(selection.age_seconds or 0),
        "save_to": str(store.placements_dir(rec.trading_day, rec.slot)),
        "orders": [item.to_json() for item in rec.items],
    })
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
