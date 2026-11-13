"""Find the newest script-built orders_pages envelope an EARLIER slot saved today (read only).

Why this exists (2026-11-12, 14:30 ET): the 2:30 slot reached Gate 6 with 57 BUY candidates
and aborted ``ABORT_GATE6_HISTORY_NOT_PERSISTABLE`` / ``output_cap_exceeded`` with 0 orders.
By mid-afternoon TODAY's ``get_equity_orders`` page alone is ~35 KB (37 orders), and a session
cannot re-emit a page that size into a file verbatim; ``assemble_order_pages`` rightly refused
the condensed copy it wrote instead. The WEEK BASELINE REUSE rule (2026-11-05) did not help:
it always merges onto the 9:35 week envelope, so at 2:30 its increment is every order since
9:35 -- the same ~35 KB page. The 3:30 slot survived that afternoon only because its session
improvised a merge onto the 1:30 run's day envelope.

That improvisation is sound, because the safety lives in ``scripts.merge_order_pages``, not
in the choice of base: the merge refuses a gap between the base snapshot and the increment,
refuses a base order the increment does not repeat, and re-validates every base row. Any
complete, unmodified envelope from earlier today is therefore a valid base, and the NEWEST one
makes the increment smallest. This script picks it, and nothing else: it never writes, never
copies, and never decides on its own that history is complete.

A candidate is ``<cron root>/<slot run dir>/orders_pages_<day>_<slot>.json`` (``--kind day``)
or ``orders_pages_week_<Monday>_<slot>.json`` (``--kind week``) for every slot EARLIER than
``--slot`` in ``scripts.verify_schedule.SLOT_EVIDENCE_LAYOUT``. It is accepted only when:

* it parses as JSON with no duplicate keys and no NaN/Infinity, and is an envelope with a
  ``pages`` list;
* ``account_number`` is ``--account`` and ``trading_day`` is the day (or, for a week
  envelope, that ISO week's Monday);
* ``ingest.tool`` is ``scripts.assemble_order_pages`` or ``scripts.merge_order_pages`` AND the
  pages still hash to ``ingest.payload_sha256`` -- so a hand-built, hand-condensed or
  hand-edited file is never a base (those are exactly the files the 2026-11-11 morning runs
  left behind with no ``ingest``);
* every row passes ``assemble_order_pages``' row checks (no placeholder id, no condensed row);
* ``snapshot_at`` is offset-aware, falls on the trading day in ET, and is not in the future.

A rejected candidate is listed with its reason and skipped, never fatal: falling back to an
older valid base only widens the increment. No valid candidate is exit 2, and the caller
falls back to the full fetch exactly as before.

Usage::

    python3 -m scripts.find_order_baseline --kind day --slot 1430 --trading-day 2026-11-12 --account "$ACCT"
    python3 -m scripts.find_order_baseline --kind week --slot 1430 --trading-day 2026-11-12 --account "$ACCT"

Exit 0 prints ``{"ok": true, "base", "base_slot", "snapshot_at", "created_at_gte",
"envelope_trading_day", "payload_sha256", "order_count", "rejected"}``, where
``created_at_gte`` is ``snapshot_at`` minus ``--overlap-seconds`` (default 120, the merge's
own default). Exit 2 prints ``{"ok": false, "reason", "detail", "rejected"}``. The account
number is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence
from zoneinfo import ZoneInfo

import dca_paths
from dca_config import _reject_duplicate_keys
from scripts import assemble_order_pages as asm
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative

TOOL = "scripts.find_order_baseline"
ET = ZoneInfo("America/New_York")
# Only these two writers produce an envelope whose page chain was PROVEN complete when it was
# written (assemble replays the chain; merge only ever extends a proven envelope).
TRUSTED_WRITERS = ("scripts.assemble_order_pages", "scripts.merge_order_pages")
KINDS = ("day", "week")


class BaselineRejected(Exception):
    """One candidate is not a usable base; ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _canonical(obj: Any) -> bytes:
    # Byte-identical to assemble_order_pages / merge_order_pages, so the recomputed hash is the
    # one they recorded.
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise BaselineRejected("bad_snapshot_at", "snapshot_at missing or not a string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineRejected("bad_snapshot_at", "snapshot_at is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BaselineRejected("bad_snapshot_at", "snapshot_at has no UTC offset")
    return parsed


def candidate_paths(kind: str, slot: str, day: date, root: Optional[Path] = None) -> List[tuple[str, Path]]:
    """``(slot, path)`` for every slot earlier than ``slot``, oldest first. Paths may not exist."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if slot not in SLOT_EVIDENCE_LAYOUT:
        raise ValueError(f"unknown slot {slot!r}")
    base = Path(root) if root is not None else Path(dca_paths.cron_tracking_root())
    if kind == "day":
        name = "orders_pages_{day}_{slot}.json"
        named_day = day.isoformat()
    else:
        name = "orders_pages_week_{day}_{slot}.json"
        named_day = monday_of(day).isoformat()
    # Slot ids are zero-padded HHMM, so string order is time order.
    earlier = sorted(s for s in SLOT_EVIDENCE_LAYOUT if s < slot)
    return [(s, base / slot_run_dir_relative(s, day) / name.format(day=named_day, slot=s)) for s in earlier]


def check_candidate(path: Path, *, kind: str, account: str, day: date, now: datetime) -> dict:
    """Return ``{"snapshot": datetime, "envelope": dict}`` for a usable base, or raise BaselineRejected."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BaselineRejected("absent") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise BaselineRejected("unreadable", type(exc).__name__) from exc
    try:
        envelope = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=asm._reject_constant)
    except ValueError as exc:  # also duplicate keys and NaN/Infinity, both raised as ValueError
        raise BaselineRejected("not_json", type(exc).__name__) from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("pages"), list):
        raise BaselineRejected("not_an_envelope", "no pages list")
    if envelope.get("account_number") != account:
        raise BaselineRejected("account_mismatch")
    expected_day = (day if kind == "day" else monday_of(day)).isoformat()
    if envelope.get("trading_day") != expected_day:
        raise BaselineRejected("trading_day_mismatch", f"expected {expected_day}")

    ingest = envelope.get("ingest")
    if not isinstance(ingest, dict) or ingest.get("tool") not in TRUSTED_WRITERS:
        raise BaselineRejected("not_script_built", "ingest.tool is not assemble_order_pages or merge_order_pages")
    if hashlib.sha256(_canonical(envelope["pages"])).hexdigest() != ingest.get("payload_sha256"):
        raise BaselineRejected("payload_sha256_mismatch", "pages were changed after the script wrote them")
    try:
        asm._validate_rows(envelope["pages"])
    except asm.AssemblyRefused as exc:
        raise BaselineRejected("bad_rows", exc.reason) from exc

    snapshot = _aware(envelope.get("snapshot_at"))
    if snapshot.astimezone(ET).date() != day:
        raise BaselineRejected("snapshot_not_today", envelope.get("snapshot_at", ""))
    if snapshot > now:
        raise BaselineRejected("snapshot_in_future", envelope.get("snapshot_at", ""))
    return {"snapshot": snapshot, "envelope": envelope}


def find_baseline(
    *,
    kind: str,
    slot: str,
    trading_day: str,
    account: str,
    now: Optional[datetime] = None,
    overlap_seconds: int = 120,
    root: Optional[Path] = None,
) -> dict:
    """The JSON result document; ``ok`` is False when no candidate is usable."""
    account = (account or "").strip()
    if not account:
        return {"ok": False, "reason": "missing_account", "detail": "", "rejected": []}
    if overlap_seconds < 0:
        return {"ok": False, "reason": "bad_overlap", "detail": "overlap must not be negative", "rejected": []}
    try:
        day = date.fromisoformat(asm._validated_day(trading_day))
    except asm.AssemblyRefused as exc:
        return {"ok": False, "reason": exc.reason, "detail": exc.detail, "rejected": []}
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return {"ok": False, "reason": "bad_now", "detail": "--now has no UTC offset", "rejected": []}
    try:
        candidates = candidate_paths(kind, slot, day, root)
    except ValueError as exc:
        return {"ok": False, "reason": "bad_argument", "detail": str(exc), "rejected": []}

    rejected: List[dict] = []
    best: Optional[tuple[datetime, str, Path, dict]] = None
    for cand_slot, path in candidates:
        try:
            found = check_candidate(path, kind=kind, account=account, day=day, now=moment)
        except BaselineRejected as exc:
            if exc.reason != "absent":  # a slot that saved nothing is normal, not news
                rejected.append({"slot": cand_slot, "path": str(path), "reason": exc.reason})
            continue
        # Newest snapshot wins; on a tie the later slot (iterated later) wins.
        if best is None or found["snapshot"] >= best[0]:
            best = (found["snapshot"], cand_slot, path, found["envelope"])

    if best is None:
        return {
            "ok": False,
            "reason": "no_valid_baseline",
            "detail": f"no usable {kind} envelope from a slot earlier than {slot} today",
            "rejected": rejected,
        }
    snapshot, cand_slot, path, envelope = best
    rows = [row for page in envelope["pages"] for row in asm._order_rows(page)]
    return {
        "ok": True,
        "tool": TOOL,
        "kind": kind,
        "base": str(path),
        "base_slot": cand_slot,
        "envelope_trading_day": envelope["trading_day"],
        "snapshot_at": envelope["snapshot_at"],
        # Same instant written in the base's own offset, so the merge's gap check compares like for like.
        "created_at_gte": (snapshot - timedelta(seconds=overlap_seconds)).isoformat(),
        "overlap_seconds": overlap_seconds,
        "payload_sha256": envelope["ingest"]["payload_sha256"],
        "order_count": len({str(row.get("id", "")).lower() for row in rows}),
        "rejected": rejected,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", required=True, choices=KINDS, help="day: today's envelope; week: the ISO week's.")
    parser.add_argument("--slot", required=True, choices=sorted(SLOT_EVIDENCE_LAYOUT), help="THIS run's slot (HHMM).")
    parser.add_argument("--trading-day", required=True, help="Today, YYYY-MM-DD (not the Monday, even for --kind week).")
    parser.add_argument("--account", required=True, help="Account number; must match the envelope. Never printed.")
    parser.add_argument("--overlap-seconds", type=int, default=120, help="Overlap subtracted from snapshot_at (default 120).")
    parser.add_argument("--now", help="Offset-aware ISO instant to judge 'not in the future' against (tests only).")
    args = parser.parse_args(argv)

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(json.dumps({"ok": False, "reason": "bad_now", "detail": "--now is not ISO-8601", "rejected": []}))
            return 2
    result = find_baseline(
        kind=args.kind,
        slot=args.slot,
        trading_day=args.trading_day,
        account=args.account,
        now=now,
        overlap_seconds=args.overlap_seconds,
    )
    print(json.dumps(result))
    return 0 if result["ok"] else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
