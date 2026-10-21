#!/usr/bin/env python3
"""Backfill zero-deployment run_state.json for afternoon fires that aborted before writing evidence.

Why this exists (2026-10-20): the 14:30 and 15:30 fires of 2026-10-13..16 died at Gate 2
("bridge shell has no Python 3.11") before ``finalize_run_evidence`` could run, so no
``run_state.json`` exists for them. The 12:30 slot's trailing-7-day afternoon check
requires one validated state per expected ``(market day, 14:30|15:30)`` pair and fails
closed on a gap, which blocked every 12:30 buy for a week.

This writes the missing evidence the honest way, through ``finalize_run_evidence`` (so
the classification, schema and pointers are the ones a real fire stamps) and only when
BOTH of these are proven for a fire:

  1. a durable Gate alert for that exact day and slot exists in the stream's alerts
     directory (the fire happened and aborted), unless ``--allow-no-alert`` names it
     explicitly, in which case the reconstruction block records ``alert_found: false``;
  2. the supplied broker order history COVERS the slot's whole acceptance window and
     contains no order created inside it. Coverage is declared by the caller with
     ``--orders-covers-from/--orders-covers-through`` and is checked against the window;
     a broker file that does not span the window is refused rather than assumed empty.

Any order inside the window is refused: attributing it to this slot is a human decision,
not something to reconstruct. The written state records ``order_submit_status`` of
``not_submitted_error``, ``error_class`` ``backfill_gate2_abort`` and a
``gate9_report.reconstruction`` block naming the evidence, so a backfilled fire stays
distinguishable from a live one. Writes only under the cron-tracking root; no broker,
no network, no orders. ``--dry-run`` checks everything and writes nothing.

Usage (from the repo root)::

    python -m scripts.backfill_afternoon_evidence --fire 2026-10-13:1430 --fire 2026-10-13:1530 \\
        --orders-json broker_orders.json \\
        --orders-covers-from 2026-10-13T00:00:00Z --orders-covers-through 2026-10-20T23:59:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dca_paths import cron_tracking_root
from scripts.finalize_run_evidence import (
    ET,
    SLOT_EVIDENCE_LAYOUT,
    FinalizeRefused,
    acceptance_window,
    finalize,
    normalize_slot,
)

AFTERNOON_SLOTS = ("1430", "1530")
OUTCOME = "BACKFILL_GATE2_ABORT_ZERO_DEPLOYMENT"
ERROR_CLASS = "backfill_gate2_abort"
_ALERT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4})(?:et)?-.*\.txt$", re.IGNORECASE)


class BackfillRefused(Exception):
    """A fire that cannot be proven zero-deployment; nothing is written for it."""


def _parse_instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackfillRefused(f"{label} {value!r} is not ISO-8601") from exc
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(ET)


def parse_fire(value: str) -> tuple[date, str]:
    try:
        day_text, slot_text = value.split(":")
        day = date.fromisoformat(day_text)
    except ValueError as exc:
        raise BackfillRefused(f"--fire {value!r} must look like YYYY-MM-DD:1430") from exc
    slot = normalize_slot(slot_text)
    if slot not in AFTERNOON_SLOTS:
        raise BackfillRefused(f"--fire slot {slot} is not an afternoon slot {AFTERNOON_SLOTS}")
    return day, slot


def stream_dir(root: Path, slot: str) -> Path:
    """The slot's own stream tree, mirroring where finalize_run_evidence writes it."""
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot]
    return root / ("ce16ba6b/scheduled_runs" if stream == "afternoon" else "intraday_dca")


def find_alerts(root: Path, day: date, slot: str) -> List[Path]:
    alerts = stream_dir(root, slot) / "alerts"
    if not alerts.is_dir():
        return []
    found = []
    for path in sorted(alerts.iterdir()):
        match = _ALERT_NAME.match(path.name)
        if match and match.group(1) == day.isoformat() and match.group(2) == slot:
            found.append(path)
    return found


def orders_in_window(orders: List[Dict[str, Any]], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    inside = []
    for order in orders:
        raw = order.get("created_at")
        if not isinstance(raw, str):
            raise BackfillRefused(f"broker order without a created_at timestamp: {order.get('id') or order!r}")
        if start <= _parse_instant(raw, "order created_at") < end:
            inside.append(order)
    return inside


def check_fire(
    root: Path,
    day: date,
    slot: str,
    orders: List[Dict[str, Any]],
    covers_from: datetime,
    covers_through: datetime,
    allow_no_alert: bool,
) -> Dict[str, Any]:
    """Return the reconstruction block for one fire, or raise BackfillRefused."""
    start, end = acceptance_window(slot, day)
    state = stream_dir(root, slot) / f"{day:%Y%m%d}_{slot}ET" / "run_state.json"
    if state.exists():
        raise BackfillRefused(f"{day} {slot}: run_state.json already exists ({state}); never overwritten")
    if not (covers_from <= start and covers_through >= end):
        raise BackfillRefused(
            f"{day} {slot}: broker history covers {covers_from.isoformat()}..{covers_through.isoformat()}, "
            f"which does not span the {start.strftime('%H:%M')}-{end.strftime('%H:%M')} ET window"
        )
    inside = orders_in_window(orders, start, end)
    if inside:
        ids = ", ".join(str(o.get("id")) for o in inside)
        raise BackfillRefused(f"{day} {slot}: broker shows {len(inside)} order(s) in the window ({ids}); needs a human")
    alerts = find_alerts(root, day, slot)
    if not alerts and not allow_no_alert:
        raise BackfillRefused(f"{day} {slot}: no durable alert found; pass --allow-no-alert {day}:{slot} to accept")
    return {
        "backfilled": True,
        "reason": "Gate 2 abort before finalize_run_evidence could run (bridge shell had no Python 3.11)",
        "alert_found": bool(alerts),
        "alert_files": [p.name for p in alerts],
        "broker_orders_in_window": 0,
        "broker_history_covers": [covers_from.isoformat(), covers_through.isoformat()],
        "window_et": [start.isoformat(), end.isoformat()],
    }


def nominal_start(slot: str, day: date) -> datetime:
    """Nominal slot time, which is also the afternoon-evidence floor (14:30) for the 14:30 slot."""
    from dca_runner import SLOTS

    return datetime.combine(day, time(*SLOTS[slot].nominal_et), tzinfo=ET)


def pointer_path(root: Path, slot: str) -> Path:
    """The latest-run pointer finalize_run_evidence refreshes for this slot's stream."""
    if SLOT_EVIDENCE_LAYOUT[slot][0] == "afternoon":
        return root / "ce16ba6b" / "latest_scheduled_run.json"
    return root / "intraday_dca" / "latest_intraday_run.json"


def _pointer_day(raw: Optional[bytes]) -> Optional[date]:
    try:
        return date.fromisoformat(json.loads(raw)["trading_day"][:10]) if raw else None
    except (ValueError, KeyError, TypeError):
        return None


def restore_newer_pointer(path: Path, before: Optional[bytes], written_day: date) -> bool:
    """Put the pointer back if finalize_run_evidence moved it to an OLDER day than it held.

    The finalizer always refreshes the stream pointer to the run it just wrote, which is
    right for a live fire and wrong for a backfill of a past date: on 2026-10-20 it moved
    both afternoon pointers from Oct 20 back to Oct 16. A pointer that held a later day
    (or any day when the finalizer left it unreadable) is restored byte-for-byte.
    """
    held = _pointer_day(before)
    if before is None or held is None or held <= written_day:
        return False
    tmp = path.with_name(path.name + ".backfill-restore")
    tmp.write_bytes(before)
    os.replace(tmp, path)
    return True


def write_fire(root: Path, day: date, slot: str, reconstruction: Dict[str, Any]) -> int:
    pointer = pointer_path(root, slot)
    before = pointer.read_bytes() if pointer.is_file() else None
    start = nominal_start(slot, day)
    # The record describes the FIRE, so generated_at carries the fire's own time (``now=start``):
    # dca_risk.is_afternoon_scheduled_evidence checks that timestamp's time of day against the
    # 14:30 ET floor, so stamping the wall clock rejected every fire backfilled between 00:00 and
    # 14:29 ET (the usual "morning after" run) as non-afternoon evidence. The real write instant is
    # kept in the reconstruction block so the audit trail still says when the backfill ran. This
    # calls finalize() directly, not its CLI, so no flag exists for a live session to forge a time.
    reconstruction = {**reconstruction, "backfilled_at": datetime.now(ET).isoformat()}
    try:
        result = finalize(
            slot,
            start,
            outcome=OUTCOME,
            order_submit_status="not_submitted_error",
            error_class=ERROR_CLASS,
            error_detail=f"backfilled zero-deployment evidence for {day} {slot} ET",
            report={"reconstruction": reconstruction},
            root=root,
            now=start,
        )
    except FinalizeRefused as exc:
        print(json.dumps({"verified": False, "refused": str(exc)}, indent=2))
        code = 2
    else:
        print(json.dumps(result, indent=2))
        code = 0 if result["verified"] else 1
    restore_newer_pointer(pointer, before, day)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fire", action="append", required=True, help="YYYY-MM-DD:1430 or :1530 (repeatable)")
    parser.add_argument("--orders-json", required=True, help="JSON array of broker orders, each with created_at")
    parser.add_argument("--orders-covers-from", required=True, help="ISO instant the order query started at")
    parser.add_argument("--orders-covers-through", required=True, help="ISO instant the order query was run at")
    parser.add_argument("--allow-no-alert", action="append", default=[], help="a --fire value to accept without an alert")
    parser.add_argument("--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)")
    parser.add_argument("--dry-run", action="store_true", help="check every fire, write nothing")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(cron_tracking_root())
    try:
        orders = json.loads(Path(args.orders_json).read_text(encoding="utf-8"))
        if not isinstance(orders, list):
            raise BackfillRefused("--orders-json must be a JSON array")
        covers_from = _parse_instant(args.orders_covers_from, "--orders-covers-from")
        covers_through = _parse_instant(args.orders_covers_through, "--orders-covers-through")
        fires = [parse_fire(f) for f in args.fire]
        allowed = {parse_fire(f) for f in args.allow_no_alert}
        # All-or-nothing: prove every fire before writing any of them.
        plans = [
            (day, slot, check_fire(root, day, slot, orders, covers_from, covers_through, (day, slot) in allowed))
            for day, slot in fires
        ]
    except (BackfillRefused, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"written": False, "refused": str(exc)}, indent=2))
        return 2
    if args.dry_run:
        print(json.dumps({"written": False, "dry_run": True, "fires": [f"{d}:{s}" for d, s, _ in plans]}, indent=2))
        return 0
    worst = 0
    for day, slot, reconstruction in plans:
        worst = max(worst, write_fire(root, day, slot, reconstruction))
    return worst


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
