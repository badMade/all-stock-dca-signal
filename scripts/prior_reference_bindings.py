#!/usr/bin/env python3
"""Return canonical logged-reference/order bindings from prior-allocation artifacts.

This read-only command deliberately delegates artifact eligibility, recovery
classification, price precedence, conflict poisoning, and cross-root recency to
``near_close_dca.load_prior_same_day_allocations_multi``.  It never guesses an
order ID for legacy or aggregated CSV evidence: such evidence is reported as
unresolved so a live executor can route the ticker to manual review.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, time
from pathlib import Path
from typing import Sequence

from near_close_dca import load_prior_same_day_allocations_multi

__all__ = ["load_reference_bindings", "main"]


def load_reference_bindings(roots: Sequence[str | Path], trading_day: date) -> dict:
    """Load validated per-ticker reference bindings before the 10:00 ET boundary."""
    paths = [Path(root) for root in roots]
    missing = [str(path) for path in paths if not path.is_dir()]
    skipped: list[Path] = []
    allocations = load_prior_same_day_allocations_multi(
        paths,
        trading_day,
        boundary_et=time(10, 0),
        skip_sink=skipped,
    )
    bindings = {}
    for ticker, allocation in sorted(allocations.items()):
        resolved = (
            allocation.amount_usd > 0
            and allocation.reference_price is not None
            and bool(allocation.reference_order_id)
        )
        bindings[ticker] = {
            "amount_usd": allocation.amount_usd,
            "reference_price": allocation.reference_price,
            "order_id": allocation.reference_order_id,
            "resolved": resolved,
        }
    complete = not missing and not skipped
    return {
        "verdict": "PROCEED" if complete else "ABORT",
        "trading_day": trading_day.isoformat(),
        "scan_complete": complete,
        "missing_roots": missing,
        "skipped_artifacts": [str(path) for path in skipped],
        "bindings": bindings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trading-day", required=True, type=date.fromisoformat)
    parser.add_argument("--root", action="append", required=True, dest="roots")
    args = parser.parse_args(argv)
    result = load_reference_bindings(args.roots, args.trading_day)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == "PROCEED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
