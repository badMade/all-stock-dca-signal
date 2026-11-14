"""Merge an overlapping incremental ``get_equity_orders`` fetch into a saved orders_pages envelope.

Why this exists (2026-10-28 and 2026-10-29, 09:35 ET): the live prompt re-read the whole
day's order history before EVERY order. The page grows with each order, so 18 orders cost
roughly 360,000 tokens of tool output, the session was compacted mid-batch on 10-29, and on
both days the run narrowed its own fetches to cope. See ``docs/adr-incremental-order-history.md``.

The incremental form: one full fetch at batch start (saved with ``assemble_order_pages``),
then before each later order a fetch of only the orders created since the last snapshot,
with an overlap. This script merges the saved increment into the base envelope and writes a
new envelope in the same shape, so caps, the Gate 4 rescan and the top-up guard still read
the COMPLETE history. It fails CLOSED -- exit 2, nothing written -- on anything that could
hide an order, because an undercount of spend is the failure that costs money:

* ``--created-at-gte`` is later than the base ``snapshot_at`` minus ``--overlap-seconds``, so
  an order created in the gap could be missing from both views;
* an order the base already holds inside the increment's window is absent from the increment,
  so the two views of the same period disagree;
* the increment is not a provably complete page chain, carries a placeholder id or a
  condensed row, or cannot be parsed (all checked by ``assemble_order_pages``, not re-derived);
* the account or trading day does not match the base, or the new ``--snapshot-at`` is not
  later than the base's.

An order present in both views is kept once and the increment's row wins, because the
increment is the newer observation of its state (``unconfirmed`` -> ``filled``). Rows are
written verbatim. The merged envelope is one page with no continuation pointer, ordered
newest first (``created_at`` descending, then id) so the same inputs always give the same
bytes. If this script refuses, the caller falls back to a full fetch and
``assemble_order_pages``; it never proceeds on a partial merge.

Usage::

    python3 -m scripts.merge_order_pages --base "$ROOT/orders_pages_<day>_0935.json" \\
        --account "$ACCT" --trading-day <day> --created-at-gte <iso> --snapshot-at <iso> \\
        --overlap-seconds 120 --out "$ROOT/orders_pages_<day>_0935.json" page1.json [page2.json ...]

Exit 0 prints ``{"ok": true, ...}``; every refusal exits 2, prints
``{"ok": false, "reason", "detail"}``, and writes nothing (an existing ``--out`` is untouched).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from dca_config import _reject_duplicate_keys
from dca_fileio import atomic_write_text
from scripts import assemble_order_pages as asm

TOOL = "scripts.merge_order_pages"


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _aware(value: Any, what: str) -> datetime:
    """Parse an offset-aware ISO-8601 instant, refusing a naive or malformed one."""
    if not isinstance(value, str):
        raise asm.AssemblyRefused("bad_timestamp", f"{what} is not a string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise asm.AssemblyRefused("bad_timestamp", f"{what} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise asm.AssemblyRefused("bad_timestamp", f"{what} has no UTC offset")
    return parsed


def _load_base(path: str, account: str, day: str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
        envelope = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=asm._reject_constant)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise asm.AssemblyRefused("base_unreadable", f"{path}: {type(exc).__name__}") from exc
    if not isinstance(envelope, dict) or not isinstance(envelope.get("pages"), list):
        raise asm.AssemblyRefused("base_not_an_envelope", "base has no pages list")
    if envelope.get("account_number") != account:
        raise asm.AssemblyRefused("account_mismatch", "base envelope is for a different account")
    if envelope.get("trading_day") != day:
        raise asm.AssemblyRefused("trading_day_mismatch", "base envelope is for a different trading day")
    return envelope


def _by_id(rows: list) -> dict:
    return {row["id"].lower(): row for row in rows}


def _sort_key(row: dict) -> tuple:
    created = _aware(row.get("created_at"), "an order's created_at")
    return (-created.timestamp(), row["id"].lower())


def merge(
    base_path: str,
    page_files: Sequence[str],
    *,
    account_number: str,
    trading_day: str,
    created_at_gte: str,
    snapshot_at: str,
    overlap_seconds: int,
) -> dict:
    """Return the merged envelope, or raise :class:`assemble_order_pages.AssemblyRefused`."""
    account = account_number.strip()
    day = asm._validated_day(trading_day)
    if overlap_seconds < 0:
        raise asm.AssemblyRefused("bad_overlap", "overlap must not be negative")
    base = _load_base(base_path, account, day)

    gte = _aware(created_at_gte, "--created-at-gte")
    new_snapshot = _aware(snapshot_at, "--snapshot-at")
    base_snapshot = _aware(base.get("snapshot_at"), "the base envelope's snapshot_at")
    if new_snapshot < base_snapshot:
        raise asm.AssemblyRefused("snapshot_not_forward", "--snapshot-at is earlier than the base snapshot")
    if gte > base_snapshot - timedelta(seconds=overlap_seconds):
        raise asm.AssemblyRefused(
            "increment_window_too_late",
            "an order created between the base snapshot and --created-at-gte could be missing from both views",
        )

    base_pages = base["pages"]
    asm._validate_rows(base_pages)  # refuses a hand-built or condensed base; the total is not needed
    base_rows = [row for page in base_pages for row in asm._order_rows(page)]

    incoming, _count = asm.assemble(
        page_files, account_number=account, trading_day=day, snapshot_at=snapshot_at
    )
    increment_rows = [row for page in incoming["pages"] for row in asm._order_rows(page)]
    increment = _by_id(increment_rows)

    # Both views cover [gte, base snapshot]; a base order in that window that the increment does
    # not repeat means one of the two fetches is wrong, and which one cannot be known here.
    for row in base_rows:
        if _aware(row.get("created_at"), "a base order's created_at") >= gte and row["id"].lower() not in increment:
            raise asm.AssemblyRefused(
                "base_order_missing_from_increment",
                f"{row.get('symbol', '?')} {row['id']} is inside the increment window but not in the increment",
            )

    merged_by_id = _by_id(base_rows)
    merged_by_id.update(increment)
    merged_rows = sorted(merged_by_id.values(), key=_sort_key)
    merged_pages = [{"orders": merged_rows}]
    total = asm._validate_rows(merged_pages)
    # 2026-11-14 (Perplexity backup hybrid): record which writer built the base, so a reader that must refuse a
    # merge onto a hand-built base (backup_planner --week-envelope) can tell. Copied, never asserted: a base
    # with no ingest block records null. Not covered by payload_sha256 (that hashes the pages only).
    base_ingest = base.get("ingest")
    base_tool = base_ingest.get("tool") if isinstance(base_ingest, dict) else None

    return {
        "account_number": account,
        "trading_day": day,
        "snapshot_at": snapshot_at,
        "pages": merged_pages,
        "ingest": {
            "tool": TOOL,
            "merged": True,
            "provenance": "merged_increment",
            "base_order_count": len({row["id"].lower() for row in base_rows}),
            "increment_order_count": len(increment),
            "order_count": len(merged_rows),
            "total_usd": float(total),
            "created_at_gte": created_at_gte,
            "overlap_seconds": overlap_seconds,
            "base_ingest_tool": base_tool if isinstance(base_tool, str) else None,
            "base_snapshot_at": base.get("snapshot_at"),
            "base_payload_sha256": hashlib.sha256(_canonical(base_pages)).hexdigest(),
            "increment_payload_sha256": incoming["ingest"]["payload_sha256"],
            "payload_sha256": hashlib.sha256(_canonical(merged_pages)).hexdigest(),
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="The saved orders_pages envelope to extend.")
    parser.add_argument("--account", required=True, help="Account number; must match the base envelope.")
    parser.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD; must match the base envelope.")
    parser.add_argument("--created-at-gte", required=True, help="The created_at_gte the increment was fetched with (offset-aware ISO).")
    parser.add_argument("--snapshot-at", required=True, help="Offset-aware ISO instant taken right after the increment's last page.")
    parser.add_argument("--overlap-seconds", type=int, default=120, help="Required overlap with the base snapshot (default 120).")
    parser.add_argument("--out", required=True, help="Merged envelope path (may be the same file as --base).")
    parser.add_argument("page_files", nargs="+", help="Saved increment page files, in fetch order.")
    args = parser.parse_args(argv)

    try:
        envelope = merge(
            args.base,
            args.page_files,
            account_number=args.account,
            trading_day=args.trading_day,
            created_at_gte=args.created_at_gte,
            snapshot_at=args.snapshot_at,
            overlap_seconds=args.overlap_seconds,
        )
        atomic_write_text(args.out, json.dumps(envelope, indent=2) + "\n")
    except asm.AssemblyRefused as exc:
        print(json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": "out_unwritable", "detail": type(exc).__name__}))
        return 2

    ingest = envelope["ingest"]
    print(
        json.dumps(
            {
                "ok": True,
                "order_count": ingest["order_count"],
                "base_order_count": ingest["base_order_count"],
                "increment_order_count": ingest["increment_order_count"],
                "out": args.out,
                "payload_sha256": ingest["payload_sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
