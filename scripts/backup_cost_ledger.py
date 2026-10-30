#!/usr/bin/env python3
"""The operator's local log of Perplexity credits per backup fire, and a report against the daily target.

Perplexity credits are not exposed to this repo (docs/cost_tracking.md), so the operator reads each fire's credits
off Perplexity's usage page and records them here. 2026-10-30 baseline: about 2,500 credits a trading day across the
seven backup fires; target under 200. The ledger is what each change (the slot-status signal, the short prompt, a
cheaper model) is measured against.

The CSV lives under the gitignored cron root (``$DCA_CRON_TRACKING_ROOT``), never in the repo: billing figures do not
belong in a public repository. Output is one JSON document; exit 0 when usable, 2 on bad input or a corrupt ledger.

    python3 -m scripts.backup_cost_ledger add --day 2026-10-29 --task 4412c3c4 --credits 312 \\
        [--status stood_down_local_active]
    python3 -m scripts.backup_cost_ledger report [--since 2026-10-27]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import dca_paths
from scripts.backup_procedure import BACKUPS

TARGET_PER_DAY = 200  # operator target, 2026-10-30
FIELDS = ("trading_day", "task_id", "credits", "status", "recorded_at")
TASK_IDS = tuple(backup.task_id for backup in BACKUPS)
# The STEP 8 state-record statuses of the served procedure (scripts/backup_procedure.py), one vocabulary.
STATUSES = (
    "placed", "partial", "nothing_to_place", "shadow_only", "stood_down_unrecognized_order",
    "stood_down_local_active", "stood_down_local_unknown", "skipped_market_closed", "skipped_outside_window",
    "failed_unexpected_error",
)
UNRECORDED = "unrecorded"


class LedgerError(ValueError):
    """A refused input or an unreadable ledger; ``code`` is the stable error name printed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def ledger_path() -> Path:
    return Path(dca_paths.cron_tracking_root()) / "backup_cost_ledger.csv"


def _credits(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise LedgerError("bad_credits") from None
    if not math.isfinite(value) or value < 0:
        raise LedgerError("bad_credits")
    return value


def _day(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise LedgerError("bad_day") from None


def add(day: str, task: str, credits: str, status: str | None = None) -> dict[str, Any]:
    row = {"trading_day": _day(day), "task_id": task, "credits": _credits(credits), "status": status or "",
           "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if task not in TASK_IDS:
        raise LedgerError("unknown_task")
    if status is not None and status not in STATUSES:
        raise LedgerError("unknown_status")
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)
    return row


def _rows() -> list[dict[str, Any]]:
    path = ledger_path()
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise LedgerError("ledger_corrupt")
        for raw in reader:
            try:
                rows.append({"trading_day": _day(raw["trading_day"] or ""), "task_id": raw["task_id"],
                             "credits": _credits(raw["credits"] or ""), "status": raw["status"] or UNRECORDED})
            except (LedgerError, TypeError):
                raise LedgerError("ledger_corrupt") from None  # never echo the row: it is the operator's data
    return rows


def _means(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row["credits"])
    return {name: {"fires": len(values), "mean_credits": round(sum(values) / len(values), 2)}
            for name, values in sorted(groups.items())}


def report(since: str | None = None) -> dict[str, Any]:
    rows = _rows()
    if since:
        cutoff = _day(since)
        rows = [row for row in rows if row["trading_day"] >= cutoff]
    totals: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        totals[row["trading_day"]].append(row["credits"])
    days = [{"trading_day": day, "credits": round(sum(values), 2), "fires": len(values),
             "over_target": sum(values) > TARGET_PER_DAY} for day, values in sorted(totals.items())]
    return {"target_per_day": TARGET_PER_DAY, "days": days, "by_status": _means(rows, "status"),
            "by_task": _means(rows, "task_id")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser = sub.add_parser("add", help="Record one fire's credits.")
    add_parser.add_argument("--day", required=True)
    add_parser.add_argument("--task", required=True)
    add_parser.add_argument("--credits", required=True)
    add_parser.add_argument("--status")
    report_parser = sub.add_parser("report", help="Daily totals against the target, means by status and task.")
    report_parser.add_argument("--since")
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            out: dict[str, Any] = {"ok": True, "row": add(args.day, args.task, args.credits, args.status)}
        else:
            out = {"ok": True, **report(args.since)}
    except LedgerError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "ledger_unavailable", "detail": type(exc).__name__}))
        return 2
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
