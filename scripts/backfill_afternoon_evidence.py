#!/usr/bin/env python3
"""Backfill run_state.json for afternoon fires that aborted before writing evidence.

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
     contains no unattributed order created inside it. Coverage is declared by the caller with
     ``--orders-covers-from/--orders-covers-through`` and is checked against the window;
     a broker file that does not span the window is refused rather than assumed empty.

Any order inside the window is refused unless the operator names it with the repeatable
``--attribute-order ID`` flag. Only filled buys with a positive USD amount can be attributed;
these are recorded as submitted deployment, without inventing a Gate 6 budget.
Without attribution the written state records ``order_submit_status`` of
``not_submitted_error``, ``error_class`` ``backfill_gate2_abort`` and a
``gate9_report.reconstruction`` block naming the evidence, so a backfilled fire stays
distinguishable from a live one. Writes only under the cron-tracking root; no broker,
no network, no orders. ``--dry-run`` checks everything and writes nothing.

The abort's gate and fire time (2026-11-11): the 2026-11-09 app outage pushed the 14:30 and
15:30 fires to 19:24 ET, where Gate 1 refused them as off-window and
``scripts/record_evidence_free_abort.sh`` wrote an evidence-free abort marker for each. The
backfill used to label every fire a Gate 2 abort at the slot's nominal time, so the operator had
to correct both states by hand. Now, when a marker exists at
``<cron root>/abort_markers/<YYYYMMDD>/<slot>.jsonl``, its gate, ``reason_code`` and
``fired_at_utc`` name the abort (e.g. ``BACKFILL_GATE1_OFF_WINDOW_ZERO_DEPLOYMENT`` /
``backfill_gate1_off_window``) and the fire time becomes ``run_classification.triggered_at_utc``.
The marker file is read strictly: exactly one well-formed pre-broker (Gate 1 or Gate 2) marker
for that day and slot, or the fire is refused. With no marker file the old Gate 2 labels and the
nominal time are kept. The broker history must then cover the acceptance window AND the fire
time plus :data:`FIRE_ORDER_ALLOWANCE`, and an order anywhere in that span needs attribution.

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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from dca_paths import cron_tracking_root
from scripts import finalize_run_evidence
from scripts.abort_markers import MARKER_KIND, marker_path
from scripts.finalize_run_evidence import (
    ET,
    SLOT_EVIDENCE_LAYOUT,
    FinalizeRefused,
    _reject_duplicate_keys,
    acceptance_window,
    finalize,
    normalize_slot,
    validate_orders,
)

AFTERNOON_SLOTS = ("1430", "1530")
# The labels for a fire with NO abort marker: the 2026-10-13..16 Gate 2 aborts this script was built
# for predate the markers (2026-10-29), so their absence still means "assume Gate 2".
OUTCOME = "BACKFILL_GATE2_ABORT_ZERO_DEPLOYMENT"
ATTRIBUTED_OUTCOME = "BACKFILL_GATE2_ABORT_ATTRIBUTED_ORDERS"
ERROR_CLASS = "backfill_gate2_abort"
# Only an evidence-free abort from a gate that runs before Gate 4's broker read is backfilled from a
# marker: the writer is only ever called on those two paths, and a later-gate marker would mean the
# fire got far enough to need a human, not a reconstruction.
MARKER_GATES = ("gate1", "gate2")
# How long after a marker's fire time an order it placed could still appear. A Gate 1/2 abort places
# nothing, but the marker only covers the fire that wrote it, so an off-window fire's own neighbourhood
# is checked as well as the acceptance window (2026-11-11).
FIRE_ORDER_ALLOWANCE = timedelta(hours=1)
_GATE_TOKEN = re.compile(r"^gate[0-9]+$")
_REASON_CODE = re.compile(r"^[A-Z0-9_]+$")
_ALERT_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4})(?:et)?-.*\.txt$", re.IGNORECASE)


class BackfillRefused(Exception):
    """A fire whose deployment cannot be proven; nothing is written for it."""


def _parse_instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackfillRefused(f"{label} {value!r} is not ISO-8601") from exc
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(ET)


@dataclass(frozen=True)
class AbortMarker:
    """The one evidence-free abort marker a fire left, read by :func:`read_abort_marker`."""

    gate: str  # "gate1"
    reason_code: str  # "GATE1_OFF_WINDOW"
    fired_at: datetime  # ET
    path: Path

    def outcome(self, attributed: bool) -> str:
        return f"BACKFILL_{self.reason_code}_{'ATTRIBUTED_ORDERS' if attributed else 'ZERO_DEPLOYMENT'}"

    @property
    def error_class(self) -> str:
        return f"backfill_{self.reason_code.lower()}"


def read_abort_marker(root: Path, day: date, slot: str) -> Optional[AbortMarker]:
    """The fire's abort marker, ``None`` when there is no marker file, or BackfillRefused.

    Deliberately stricter than ``scripts.abort_markers.read_abort_markers``, which SKIPS bad lines so
    the watchdog degrades toward ``slot_did_not_fire``. Here the marker names the gate and time the
    evidence will claim, so a skipped line could hide a different abort: the file must hold exactly one
    line, and that line must be a well-formed pre-broker marker for this day and slot (the same rules
    the kill-switch gap proof applies). Two markers, even identical ones, are two fires and need a human.
    """
    path = marker_path(root, day, slot)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise BackfillRefused(f"{day} {slot}: abort marker {path} unreadable ({type(exc).__name__})") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BackfillRefused(
            f"{day} {slot}: abort marker file {path} holds {len(lines)} marker lines; exactly one is needed"
        )
    try:
        marker = json.loads(lines[0], object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise BackfillRefused(f"{day} {slot}: abort marker {path} is malformed") from exc
    if not isinstance(marker, dict) or marker.get("kind") != MARKER_KIND:
        raise BackfillRefused(f"{day} {slot}: {path} is not an evidence-free abort marker")
    if marker.get("slot_id") != slot or marker.get("trading_day") != day.isoformat():
        raise BackfillRefused(f"{day} {slot}: abort marker {path} is for another slot or day")
    gate, reason = marker.get("gate"), marker.get("reason_code")
    if not isinstance(gate, str) or not _GATE_TOKEN.match(gate) or gate not in MARKER_GATES:
        raise BackfillRefused(f"{day} {slot}: abort marker gate {gate!r} is not a pre-broker {MARKER_GATES} abort")
    if not isinstance(reason, str) or not _REASON_CODE.match(reason) or not reason.startswith(f"{gate.upper()}_"):
        raise BackfillRefused(f"{day} {slot}: abort marker reason_code {reason!r} does not belong to {gate}")
    raw = marker.get("fired_at_utc")
    try:
        fired = datetime.fromisoformat(raw.replace("Z", "+00:00")) if isinstance(raw, str) else None
    except ValueError:
        fired = None
    if fired is None or fired.tzinfo is None:
        raise BackfillRefused(f"{day} {slot}: abort marker has no timezone-aware fired_at_utc")
    fired = fired.astimezone(ET)
    if fired.date() != day:
        raise BackfillRefused(f"{day} {slot}: abort marker fired_at {fired.isoformat()} is not on {day} ET")
    return AbortMarker(gate=gate, reason_code=reason, fired_at=fired, path=path)


def order_span(slot: str, day: date, marker: Optional[AbortMarker]) -> tuple[datetime, datetime]:
    """The span the broker history must cover and show empty: the acceptance window, widened to the fire.

    Without a marker the fire is assumed to have been inside the window, as before. With one, an
    off-window fire (19:24 ET on 2026-11-09) and :data:`FIRE_ORDER_ALLOWANCE` after it are added, so an
    order that fire could have placed is never outside the checked span.
    """
    start, end = acceptance_window(slot, day)
    if marker is None:
        return start, end
    return min(start, marker.fired_at), max(end, marker.fired_at + FIRE_ORDER_ALLOWANCE)


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


def attributed_rows(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert explicitly attributed filled broker buys to finalizer order evidence."""
    rows = []
    for order in orders:
        order_id = order.get("id")
        if order.get("side") != "buy" or order.get("state") != "filled":
            raise BackfillRefused(
                f"order {order_id}: only filled buys can be attributed "
                f"(side={order.get('side')}, state={order.get('state')})"
            )
        amount = order.get("dollar_based_amount")
        if not isinstance(amount, dict) or amount.get("currency_code") != "USD":
            raise BackfillRefused(f"order {order_id}: missing USD dollar_based_amount")
        value = amount.get("amount")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise BackfillRefused(f"order {order_id}: invalid amount")
        try:
            value = float(value)
        except (ValueError, OverflowError) as exc:
            raise BackfillRefused(f"order {order_id}: invalid amount") from exc
        rows.append({
            "ticker": order.get("symbol"), "order_id": order_id,
            "submitted_amount_usd": value, "status": "filled",
        })
    validate_orders(rows)
    return rows


def check_fire(
    root: Path,
    day: date,
    slot: str,
    orders: List[Dict[str, Any]],
    covers_from: datetime,
    covers_through: datetime,
    allow_no_alert: bool,
    attributed_order_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """Return the reconstruction block for one fire, or raise BackfillRefused."""
    return _check_fire(
        root, day, slot, orders, covers_from, covers_through, allow_no_alert, attributed_order_ids,
        marker=read_abort_marker(root, day, slot),
    )


def _check_fire(
    root: Path,
    day: date,
    slot: str,
    orders: List[Dict[str, Any]],
    covers_from: datetime,
    covers_through: datetime,
    allow_no_alert: bool,
    attributed_order_ids: Sequence[str],
    *,
    marker: Optional[AbortMarker],
) -> Dict[str, Any]:
    window_start, window_end = acceptance_window(slot, day)
    start, end = order_span(slot, day, marker)
    state = stream_dir(root, slot) / f"{day:%Y%m%d}_{slot}ET" / "run_state.json"
    if state.exists():
        raise BackfillRefused(f"{day} {slot}: run_state.json already exists ({state}); never overwritten")
    if not (covers_from <= start and covers_through >= end):
        raise BackfillRefused(
            f"{day} {slot}: broker history covers {covers_from.isoformat()}..{covers_through.isoformat()}, "
            f"which does not span {start.strftime('%H:%M')}-{end.strftime('%H:%M')} ET "
            f"(the acceptance window{' and the marker fire time' if marker else ''})"
        )
    inside = orders_in_window(orders, start, end)
    unattributed = [o for o in inside if o.get("id") not in attributed_order_ids]
    if unattributed:
        ids = ", ".join(str(o.get("id")) for o in unattributed)
        raise BackfillRefused(
            f"{day} {slot}: broker shows {len(unattributed)} unattributed order(s) in the window ({ids}); needs a human"
        )
    if attributed_order_ids:
        attributed_rows(inside)
    alerts = find_alerts(root, day, slot)
    if not alerts and not allow_no_alert:
        raise BackfillRefused(f"{day} {slot}: no durable alert found; pass --allow-no-alert {day}:{slot} to accept")
    if marker is None:
        abort: Dict[str, Any] = {
            "reason": "Gate 2 abort before finalize_run_evidence could run (bridge shell had no Python 3.11)",
            "abort_source": "assumed_gate2_no_marker",
        }
    else:
        abort = {
            "reason": (
                f"{marker.gate.capitalize()} {marker.reason_code} abort at {marker.fired_at:%H:%M} ET, before "
                "finalize_run_evidence could run (from the fire's evidence-free abort marker)"
            ),
            "abort_source": "abort_marker",
            "abort_marker_file": str(marker.path.relative_to(root)),
            "abort_gate": marker.gate,
            "abort_reason_code": marker.reason_code,
            "fired_at_et": marker.fired_at.isoformat(),
        }
    return {
        "backfilled": True,
        **abort,
        "alert_found": bool(alerts),
        "alert_files": [p.name for p in alerts],
        "broker_orders_in_window": len(inside),
        "broker_history_covers": [covers_from.isoformat(), covers_through.isoformat()],
        "window_et": [window_start.isoformat(), window_end.isoformat()],
        "broker_checked_et": [start.isoformat(), end.isoformat()],
        **({"attributed_order_ids": [o["id"] for o in inside], "attributed_by": "operator"} if inside else {}),
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


@contextmanager
def _stamped_trigger_time(fired_at: Optional[datetime]) -> Iterator[None]:
    """For one finalize() call, stamp ``fired_at`` as ``run_classification.triggered_at_utc``.

    finalize() names the run, passes its window check, and builds the classification from one
    ``started``; a 19:24 ET start is rightly refused as off-window, so the backfill passes the nominal
    time and swaps only the trigger time in here. This wraps finalize's own ``build_classification``
    instead of adding a ``fired_at`` keyword to finalize_run_evidence because that file ships inside the
    Perplexity ``backup_planner.pyz``: changing it forces a planner rebuild and a repaste of every
    backup prompt (scripts/tests/test_build_backup_planner.py). Wrapping, not editing the state after
    the write, keeps run_summary.json, the pointer and finalize's own verification on the same value.
    """
    if fired_at is None:
        yield
        return
    original = finalize_run_evidence.build_classification

    def stamped(slot: str, started: datetime) -> Dict[str, Any]:
        classification = original(slot, started)
        classification["triggered_at_utc"] = finalize_run_evidence._utc_iso(fired_at)
        return classification

    finalize_run_evidence.build_classification = stamped
    try:
        yield
    finally:
        finalize_run_evidence.build_classification = original


def write_fire(
    root: Path, day: date, slot: str, reconstruction: Dict[str, Any],
    orders: Optional[List[Dict[str, Any]]] = None,
    marker: Optional[AbortMarker] = None,
) -> int:
    pointer = pointer_path(root, slot)
    before = pointer.read_bytes() if pointer.is_file() else None
    # The nominal time names the run and passes finalize's window check; a marker's real fire time
    # becomes triggered_at_utc (_stamped_trigger_time). dca_risk.run_timestamp_et floors
    # that at the nominal time, so a pre-window fire still counts and an off-window 19:24 one stays
    # afternoon evidence. Keep the actual write clock in generated_at/finalized_at as well as the
    # reconstruction audit trail.
    start = nominal_start(slot, day)
    written_at = datetime.now(ET)
    reconstruction = {**reconstruction, "backfilled_at": written_at.isoformat()}
    try:
        with _stamped_trigger_time(marker.fired_at if marker else None):
            result = finalize(
                slot,
                start,
                outcome=marker.outcome(bool(orders)) if marker else (ATTRIBUTED_OUTCOME if orders else OUTCOME),
                reconstructed_orders=bool(orders),
                order_submit_status="submitted" if orders else "not_submitted_error",
                orders=orders,
                budget={"budget_evaluated": False},
                error_class=marker.error_class if marker else ERROR_CLASS,
                error_detail=f"backfilled {'operator-attributed' if orders else 'zero-deployment'} evidence for {day} {slot} ET",
                report={"reconstruction": reconstruction},
                root=root,
                now=written_at,
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
    parser.add_argument("--attribute-order", action="append", default=[], help="filled buy ID attributed by the operator")
    parser.add_argument("--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)")
    parser.add_argument("--dry-run", action="store_true", help="check every fire, write nothing")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(cron_tracking_root())
    try:
        orders = json.loads(Path(args.orders_json).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(orders, list) or not all(isinstance(order, dict) for order in orders):
            raise BackfillRefused("--orders-json must be a JSON array of objects")
        covers_from = _parse_instant(args.orders_covers_from, "--orders-covers-from")
        covers_through = _parse_instant(args.orders_covers_through, "--orders-covers-through")
        fires = [parse_fire(f) for f in args.fire]
        allowed = {parse_fire(f) for f in args.allow_no_alert}
        if len(set(fires)) != len(fires):
            raise BackfillRefused("duplicate --fire")
        # Read once per fire: the marker proven below is the one written, never a re-read.
        markers = {(day, slot): read_abort_marker(root, day, slot) for day, slot in fires}
        ids = [order.get("id") for order in orders if order.get("id") is not None]
        if any(not isinstance(order_id, str) or not order_id.strip() for order_id in ids) or len(set(ids)) != len(ids):
            raise BackfillRefused("broker order IDs must be nonblank and unique")
        if len(set(args.attribute_order)) != len(args.attribute_order):
            raise BackfillRefused("duplicate --attribute-order")
        for order_id in args.attribute_order:
            if order_id not in ids:
                raise BackfillRefused(f"order {order_id}: not in the order file")
            order = next(o for o in orders if o.get("id") == order_id)
            if not any(
                orders_in_window([order], *order_span(slot, day, markers[(day, slot)]))
                for day, slot in fires
            ):
                raise BackfillRefused(f"order {order_id}: not inside this fire's window")
        # All-or-nothing: prove every fire before writing any of them.
        plans = [
            (day, slot, _check_fire(
                root, day, slot, orders, covers_from, covers_through, (day, slot) in allowed, args.attribute_order,
                marker=markers[(day, slot)],
            ))
            for day, slot in fires
        ]
    except (BackfillRefused, FinalizeRefused, OSError, ValueError) as exc:
        print(json.dumps({"written": False, "refused": str(exc)}, indent=2))
        return 2
    if args.dry_run:
        print(json.dumps({
            "written": False, "dry_run": True, "fires": [f"{d}:{s}" for d, s, _ in plans],
            "attributed_order_ids": args.attribute_order,
        }, indent=2))
        return 0
    worst = 0
    for day, slot, reconstruction in plans:
        attributed = [
            order for order in orders if order.get("id") in reconstruction.get("attributed_order_ids", [])
        ]
        worst = max(worst, write_fire(
            root, day, slot, reconstruction, attributed_rows(attributed) or None, markers[(day, slot)],
        ))
    return worst


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
