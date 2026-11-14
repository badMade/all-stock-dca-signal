"""Find the newest script-built orders_pages envelope saved before this slot (read only).

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

The 2026-11-13 10:30 miss exposed the week analogue: 9:35 did not fire, so no current-day week
envelope existed and the session fell back to a 135,748-character full-week fetch it could not
persist. A previous trading day's week envelope is equally safe because merge rechecks the gap,
payload and every row; week lookup therefore searches Monday through yesterday as well.

A candidate is ``<cron root>/<slot run dir>/orders_pages_<day>_<slot>.json`` (``--kind day``)
or ``orders_pages_week_<Monday>_<slot>.json`` (``--kind week``). Day candidates are from slots
EARLIER than ``--slot`` today; week candidates also include every slot on each earlier trading
day of this ISO week. It is accepted only when:

* it parses as JSON with no duplicate keys and no NaN/Infinity, and is an envelope with a
  ``pages`` list;
* ``account_number`` is ``--account`` and ``trading_day`` is the day (or, for a week
  envelope, that ISO week's Monday);
* ``ingest.tool`` is ``scripts.assemble_order_pages`` or ``scripts.merge_order_pages`` AND the
  pages still hash to ``ingest.payload_sha256`` -- so a hand-built, hand-condensed or
  hand-edited file is never a base (those are exactly the files the 2026-11-11 morning runs
  left behind with no ``ingest``);
* every row passes ``assemble_order_pages``' row checks (no placeholder id, no condensed row);
* ``snapshot_at`` is offset-aware, falls today in ET for day envelopes or between Monday and
  today for week envelopes, and is not in the future.

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

``--kind plan`` (2026-11-12 review of PR 2246) is what the live prompts run. It does both
lookups and makes the two decisions a session must never make by eye:

* **One fetch boundary, set by the DAY base.** An earlier draft told the session to fetch from
  the EARLIER of the two ``created_at_gte`` values. Then a stale week base (say only the 9:35
  file survived) dragged the shared fetch back to 9:35, which is again the whole day's ~35 KB
  page, and both kinds fell back to the full fetch: the same abort. Now the boundary is the day
  base's ``created_at_gte`` (the week base's only when there is no day base). A week base older
  than that boundary is NOT merged; the week alone falls back to its full fetch, so it can never
  enlarge the day increment.
* **One instant, normalized to UTC.** The two bases can carry different offsets (``Z`` from one
  writer, ``-05:00`` from another), and comparing them as text gets the order wrong. The script
  compares parsed instants and prints ``fetch_created_at_gte`` once, as UTC with a ``Z``; the
  prompt uses that string verbatim for both the fetch and every merge's ``--created-at-gte``.

It prints ``{"ok": true, "fetch_created_at_gte", "boundary_kind", "merge": {kind: {"base",
"envelope_trading_day", ...}}, "fallback_full": [kinds], "lookups": {"day": ..., "week": ...}}``.
A kind in ``fallback_full`` is fetched in full exactly as before. No usable base for either kind
is exit 2.
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
PLAN = "plan"


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


def candidate_paths(kind: str, slot: str, day: date, root: Optional[Path] = None) -> List[tuple[str, date, Path]]:
    """Return eligible slot/day paths in oldest-first order; paths may not exist."""
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
    if kind == "day":
        return [(s, day, base / slot_run_dir_relative(s, day) / name.format(day=named_day, slot=s)) for s in earlier]
    result = [(s, day, base / slot_run_dir_relative(s, day) / name.format(day=named_day, slot=s)) for s in earlier]
    monday = monday_of(day)
    # 2026-11-13: a missed 9:35 run left no same-day week file; prior-day slots are valid bases
    # because merge_order_pages independently refuses any history gap and revalidates each row.
    for prior_day in (monday + timedelta(days=i) for i in range((day - monday).days)):
        for prior_slot in sorted(SLOT_EVIDENCE_LAYOUT):
            result.append((prior_slot, prior_day, base / slot_run_dir_relative(prior_slot, prior_day) / name.format(day=named_day, slot=prior_slot)))
    return result


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
    snapshot_day = snapshot.astimezone(ET).date()
    earliest = monday_of(day) if kind == "week" else day
    if not earliest <= snapshot_day <= day:
        raise BaselineRejected("snapshot_outside_window" if kind == "week" else "snapshot_not_today", envelope.get("snapshot_at", ""))
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
    best: Optional[tuple[datetime, str, date, Path, dict]] = None
    for cand_slot, cand_day, path in candidates:
        try:
            found = check_candidate(path, kind=kind, account=account, day=day, now=moment)
        except BaselineRejected as exc:
            if exc.reason != "absent":  # a slot that saved nothing is normal, not news
                item = {"slot": cand_slot, "path": str(path), "reason": exc.reason}
                if kind == "week":
                    item["day"] = cand_day.isoformat()
                rejected.append(item)
            continue
        # Newest snapshot wins; on a tie the later slot (iterated later) wins.
        if best is None or found["snapshot"] >= best[0]:
            best = (found["snapshot"], cand_slot, cand_day, path, found["envelope"])

    if best is None:
        return {
            "ok": False,
            "reason": "no_valid_baseline",
            "detail": (f"no usable {kind} envelope from a slot earlier than {slot} today" if kind == "day"
                       else f"no usable {kind} envelope from an eligible earlier slot/day for {slot}"),
            "rejected": rejected,
        }
    snapshot, cand_slot, cand_day, path, envelope = best
    rows = [row for page in envelope["pages"] for row in asm._order_rows(page)]
    result = {
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
    if kind == "week":
        result["base_day"] = cand_day.isoformat()
    return result


def _utc_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_plan(
    *,
    slot: str,
    trading_day: str,
    account: str,
    now: Optional[datetime] = None,
    overlap_seconds: int = 120,
    root: Optional[Path] = None,
) -> dict:
    """Both lookups, the single UTC fetch boundary, and which kinds may merge from it."""
    lookups = {
        kind: find_baseline(
            kind=kind, slot=slot, trading_day=trading_day, account=account,
            now=now, overlap_seconds=overlap_seconds, root=root,
        )
        for kind in KINDS
    }
    usable = {kind: result for kind, result in lookups.items() if result["ok"]}
    if not usable:
        return {
            "ok": False,
            "reason": "no_valid_baseline",
            "detail": f"no usable day or week envelope from a slot earlier than {slot} today",
            "fallback_full": list(KINDS),
            "lookups": lookups,
        }
    # The day base sets the boundary: it is the increment that must stay small. Parse, never
    # compare the printed strings -- their offsets can differ.
    boundary_kind = "day" if "day" in usable else "week"
    boundary = _aware(usable[boundary_kind]["created_at_gte"])
    merge: dict = {}
    fallback: List[str] = []
    for kind in KINDS:
        result = usable.get(kind)
        # merge_order_pages refuses a fetch that starts later than base snapshot minus overlap,
        # i.e. it needs boundary <= this base's created_at_gte. An older base fails that, so it
        # falls back on its own instead of widening the shared fetch.
        if result is not None and boundary <= _aware(result["created_at_gte"]):
            merge[kind] = {
                key: result[key]
                for key in ("base", "base_slot", "envelope_trading_day", "snapshot_at", "payload_sha256", "order_count")
            }
        else:
            fallback.append(kind)
    return {
        "ok": True,
        "tool": TOOL,
        "kind": PLAN,
        "fetch_created_at_gte": _utc_z(boundary),
        "boundary_kind": boundary_kind,
        "overlap_seconds": overlap_seconds,
        "merge": merge,
        "fallback_full": fallback,
        "lookups": lookups,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--kind", required=True, choices=(*KINDS, PLAN),
        help="day: today's envelope; week: the ISO week's; plan: both, plus the one fetch boundary (live prompts).",
    )
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
    if args.kind == PLAN:
        result = fetch_plan(
            slot=args.slot, trading_day=args.trading_day, account=args.account,
            now=now, overlap_seconds=args.overlap_seconds,
        )
        print(json.dumps(result))
        return 0 if result["ok"] else 2
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
