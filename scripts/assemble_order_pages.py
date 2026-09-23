"""Assemble saved ``get_equity_orders`` page files into an ``orders_pages`` envelope.

Why this exists (DCA Issues ``output_cap_exceeded``): on 2026-09-09 the 12:30
slot fetched a ``get_equity_orders`` page larger than the session's tool-output
cap. The session transcribed the rows out of the saved output BY HAND, two
transcription passes disagreed on the array length, and Gate 6 failed closed --
correctly, but the slot was lost. Chunking the fetch does not help: the page
size is fixed server-side and every live prompt already follows each cursor.
What was missing is a way to get an oversized page into the gates WITHOUT a
human-style transcription step in between.

This script takes the saved page files in fetch order, parses each one as JSON
(rejecting duplicate keys, the same fail-closed rule as ``dca_config``),
proves the pages form one complete pagination chain by replaying them through
``broker_reconciliation.same_day_order_snapshot`` -- the exact replay
``scripts/same_day_top_up_gate.py`` already trusts, so there is no second copy
of the cursor rules to drift -- and only then writes::

    {"account_number": ..., "trading_day": ..., ["snapshot_at": ...,] "pages": [...]}

atomically to ``--out``. Pages are written verbatim (the parsed JSON, not a
reshaped view), so every downstream consumer sees the same raw responses it
would have seen had the page fitted in the session's output.

The saved-output file format was NOT verified against a live session when this
was written. Two shapes are accepted: the raw JSON response, and the one wrapper
MCP itself defines -- a list holding exactly one ``{"type": "text", "text": ...}``
block. Anything else is refused, so an unexpected format degrades to today's
behaviour (the slot aborts and alerts), never to a silently partial history.

Usage::

    python3 -m scripts.assemble_order_pages --account "$ACCT" --trading-day 2026-09-09 \\
        --out "$ROOT/orders_pages_2026-09-09_1230.json" page1.txt page2.txt

Exit 0 prints ``{"ok": true, "page_count", "order_count", "out"}``; every refusal
exits 2, prints ``{"ok": false, "reason", "detail"}``, and writes nothing (an
existing ``--out`` file is left untouched).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, List, Optional, Sequence

from dca_config import _reject_duplicate_keys
from dca_fileio import atomic_write_text
from dca_live_adapters import _is_provable_orders_page
from scripts.broker_reconciliation import same_day_order_snapshot
from scripts.same_day_top_up_gate import _PageReplayer


class AssemblyRefused(Exception):
    """A page set that cannot be proven complete; carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _parse_json(text: str, source: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        # A truncated save is the failure mode this script exists for: it must
        # read as unparseable evidence, never as a shorter-but-valid page.
        raise AssemblyRefused("page_file_unparseable", f"{source}: {type(exc).__name__}") from exc


def _unwrap_text_block(payload: Any, source: str) -> Any:
    """Unwrap MCP's single text content block; leave every other shape alone.

    Only the exact one-block shape is unwrapped. A bare list of order rows is
    also a list, and must stay one so the orders-page check below refuses it
    rather than this helper guessing at what it meant.
    """
    if (
        isinstance(payload, list)
        and len(payload) == 1
        and isinstance(payload[0], dict)
        and payload[0].get("type") == "text"
        and isinstance(payload[0].get("text"), str)
    ):
        return _parse_json(payload[0]["text"], source)
    return payload


def _load_page(path: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AssemblyRefused("page_file_unreadable", f"{path}: {type(exc).__name__}") from exc
    page = _unwrap_text_block(_parse_json(text, path), path)
    if not isinstance(page, dict):
        raise AssemblyRefused("page_not_an_orders_page", f"{path}: top level is {type(page).__name__}")
    return page


def _validated_day(value: str) -> str:
    day = value.strip()
    try:
        # fromisoformat also accepts compact forms on 3.11+; the envelope and
        # every consumer's day-binding compare the dashed string, so require it.
        if len(day) != 10 or date.fromisoformat(day).isoformat() != day:
            raise ValueError(day)
    except ValueError as exc:
        raise AssemblyRefused("invalid_trading_day", repr(value)) from exc
    return day


def assemble(
    page_files: Sequence[str],
    *,
    account_number: str,
    trading_day: str,
    snapshot_at: Optional[str] = None,
) -> tuple[dict, int]:
    """Return ``(envelope, order_count)`` for a provably complete page chain.

    Raises :class:`AssemblyRefused` for anything short of that.
    """
    account = account_number.strip()
    if not account:
        raise AssemblyRefused("missing_account")
    day = _validated_day(trading_day)
    if not page_files:
        raise AssemblyRefused("no_page_files")

    pages: List[Any] = [_load_page(path) for path in page_files]
    for path, page in zip(page_files, pages, strict=True):
        # Same check the replay applies, surfaced per file so the refusal names
        # the file instead of a generic pagination reason.
        if not _is_provable_orders_page(page):
            raise AssemblyRefused("page_not_an_orders_page", f"{path}: no orders list")

    replayer = _PageReplayer(pages)
    snapshot = same_day_order_snapshot(
        replayer,
        account_number=account,
        created_at_gte=day,
        max_pages=max(len(pages) + 1, 2),
    )
    if not replayer.fully_consumed:
        raise AssemblyRefused("unconsumed_saved_order_pages", "a page before the last has no continuation pointer")
    if not snapshot.pagination_complete:
        raise AssemblyRefused("history_incomplete", ",".join(snapshot.incomplete_reasons) or "pagination_incomplete")

    envelope: dict = {"account_number": account, "trading_day": day}
    if snapshot_at:
        envelope["snapshot_at"] = snapshot_at
    envelope["pages"] = pages
    return envelope, len(snapshot.orders)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, help="Account number the pages were fetched for.")
    parser.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
    parser.add_argument("--out", required=True, help="Envelope path (the gate's orders_pages_<day>_<slot>.json).")
    parser.add_argument("--snapshot-at", default=None, help="Optional ISO timestamp; the 09:35 envelope carries it.")
    parser.add_argument("page_files", nargs="*", help="Saved page files, in fetch order.")
    args = parser.parse_args(argv)

    try:
        envelope, order_count = assemble(
            args.page_files,
            account_number=args.account,
            trading_day=args.trading_day,
            snapshot_at=args.snapshot_at,
        )
        atomic_write_text(args.out, json.dumps(envelope, indent=2) + "\n")
    except AssemblyRefused as exc:
        print(json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": "out_unwritable", "detail": type(exc).__name__}))
        return 2

    print(json.dumps({"ok": True, "page_count": len(envelope["pages"]), "order_count": order_count, "out": args.out}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
