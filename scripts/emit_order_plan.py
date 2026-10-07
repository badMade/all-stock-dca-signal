#!/usr/bin/env python3
"""Write the Claude slot's order plan to disk BEFORE Gate 7 places anything.

WHAT THIS IS FOR
----------------
The seven live prompts decide what to buy in prose-driven gates and then place orders one at
a time. If placement stops midway, nothing on disk says what the slot meant to buy, so the
delayed Perplexity backup has to re-derive it (``docs/adr-plan-first-backup-executor.md``).
This script is the missing step: it takes the resolved buy list (every Gate 6.5/6.7 survivor
with ``buy_usd > 0``) and writes it as a validated plan file the backup can read.

WHAT THIS IS NOT
----------------
* It never places, cancels, or modifies an order and imports no broker code. The word
  "emit" is literal: it writes one JSON file.
* A plan is input, never evidence of a purchase. Broker order history stays the only evidence.
* It does not size anything. ``buy_usd`` is taken exactly as Gate 6.5 resolved it.
* A failed emit does not block the primary's placement. The prompts report it in Gate 9 and
  carry on, because only the backup depends on the plan.

Usage (from the repo root)::

    python3 -m scripts.emit_order_plan --trading-day 2026-10-07 --slot 1130 \\
        --candidates $ROOT/order_plan_input_2026-10-07_1130.json

``--candidates`` is a JSON array of survivor rows (``ticker``, ``buy_usd``, ``multiplier``,
``drop_pct``, ``current_price``, ``prior_close``, ``prior_low``; other fields are ignored).
Prints one JSON report. Exit 0 when the plan was written (a plan with zero orders is valid),
2 on any failure, in which case NOTHING is written and an earlier plan file is left untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import dca_config
import dca_order_plan
from dca_order_plan import PlanError
from dca_runner import ET, SLOTS

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _report(status: str, *, path: Path | None = None, order_count: int = 0,
            errors: tuple[PlanError, ...] = (), plan_sha256: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "path": None if path is None else str(path),
        "order_count": order_count,
        "plan_sha256": plan_sha256,
        "errors": [{"code": e.code, "detail": e.detail} for e in errors],
    }


def _failed(*errors: PlanError) -> int:
    print(json.dumps(_report("FAILED", errors=errors), indent=2))
    return 2


def _read_survivors(path: Path) -> tuple[list[Any] | None, PlanError | None]:
    """The survivors array, or the structured reason it cannot be used."""
    if not path.is_file():
        return None, PlanError("inputs_missing", path.name)
    try:
        # Duplicate keys are refused the same way config loading refuses them.
        data = json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)
    except (OSError, ValueError):
        return None, PlanError("inputs_corrupt", path.name)
    if not isinstance(data, list):
        return None, PlanError("inputs_corrupt", f"{path.name}: expected a JSON array")
    return data, None


def _parse_now(text: str | None) -> datetime | None:
    if text is None:
        return datetime.now(ET)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
    parser.add_argument("--slot", required=True, help="Slot id: one of " + ", ".join(SLOTS))
    parser.add_argument("--candidates", required=True, type=Path, help="JSON array of resolved survivor rows.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Plan file to write (default: dca_order_plan.order_plan_path).")
    parser.add_argument("--now", default=None,
                        help="Timezone-aware ISO timestamp recorded in the plan (default: the current ET time).")
    parser.add_argument("--config-dir", type=Path, default=None, help="Config directory (default: the repo's).")
    args = parser.parse_args(argv)

    if not _DAY_RE.match(args.trading_day):
        return _failed(PlanError("bad_trading_day", "expected YYYY-MM-DD"))
    try:
        trading_day = date.fromisoformat(args.trading_day)
    except ValueError:
        return _failed(PlanError("bad_trading_day", "not a calendar date"))
    if args.slot not in SLOTS:
        return _failed(PlanError("unknown_slot", args.slot))
    now = _parse_now(args.now)
    if now is None:
        return _failed(PlanError("bad_now", "expected a timezone-aware ISO timestamp"))

    rows, problem = _read_survivors(args.candidates)
    if problem is not None:
        return _failed(problem)

    try:
        config_dir = args.config_dir if args.config_dir is not None else dca_config.get_config_dir()
        config = dca_config.load_strategy_config(config_dir)
        ladder_version = dca_order_plan.current_ladder_version(config_dir)
    except (OSError, ValueError) as exc:
        # Type name only: a loader message can carry paths or config contents.
        return _failed(PlanError("config_unavailable", type(exc).__name__))

    result = dca_order_plan.plan_from_survivors(
        rows, config=config, trading_day=trading_day, slot=args.slot,
        generated_at_et=now, ladder_version=ladder_version,
    )
    if result.errors or result.plan is None:
        return _failed(*result.errors)

    path = args.out if args.out is not None else dca_order_plan.order_plan_path(trading_day, args.slot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        dca_order_plan.write_plan(path, result.plan)
    except OSError as exc:
        return _failed(PlanError("write_failed", type(exc).__name__))

    digest = hashlib.sha256(dca_order_plan.canonical_plan_json(result.plan).encode()).hexdigest()
    print(json.dumps(_report("EMITTED", path=path, order_count=len(result.orders), plan_sha256=digest), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
