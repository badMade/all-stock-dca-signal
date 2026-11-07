"""Reshape a saved orders_pages envelope into the 12:30 slot's ``orders_week.json`` list.

Why this exists (2026-11-03 and 2026-11-05): the 12:30 live prompt's Gate 4 tells the session
to fetch the whole ISO week of ``get_equity_orders`` and reshape every raw row BY HAND into the
flat list its gate scripts read (``BrokerOrder(**row)`` and ``dca_runner.budget_snapshot``). By
Thursday that week is ~150+ orders, ~184 KB: on 2026-11-03 the 12:30 slot aborted because the
week could not be written out at all, and a hand reshape of 150 rows is the same transcription
risk ``assemble_order_pages`` was written to remove (2026-09-09, 2026-10-05). The other post-9:35
slots now reuse the 9:35 run's script-assembled week envelope and merge only the newer orders
(``scripts.merge_order_pages``); 12:30 could not, because its gates read this reshaped list, not
the envelope. This script is the missing step, so no row is ever retyped.

It reads an envelope in the shape ``assemble_order_pages`` / ``merge_order_pages`` write, binds
it to the requested account and trading day (``same_day_top_up_gate._bind_saved_pages``), re-runs
``assemble_order_pages``' row checks and the same complete-pagination-chain replay it uses
(``broker_reconciliation.same_day_order_snapshot`` over ``same_day_top_up_gate._PageReplayer``; no
second copy of the cursor rules), and writes one entry per order id with exactly the Gate 4 contract keys:

* ``symbol`` -- stripped and upper-cased, ``side`` and ``state`` lower-cased, the same
  normalisation ``backup_planner._to_broker_order`` applies to the same raw rows;
* ``dollar_amount`` -- ``dollar_based_amount.amount``; a quantity-based BUY with no dollar
  amount is valued at quantity x price (``price``, else ``average_price``) or refused, never
  read as zero (``backup_planner._to_broker_order``); a non-buy with none is 0.0;
* ``created_at`` -- verbatim, refused unless offset-aware (every gate calls ``_require_aware``);
* ``placed_agent`` -- verbatim string or null; any other type is refused (the gates treat
  provenance as evidence, so an unreadable value is not quietly nulled);
* ``executed_dollar_amount`` -- only for a dead order: ``executed_notional.amount``, else
  ``cumulative_quantity`` x ``average_price`` when both are positive, else null; null for a live
  order (``backup_planner._to_broker_order``; ``dca_runner.BrokerOrder.counts_toward_budget``
  reads it only on a dead order). "Dead" is ``dca_runner``'s canonical terminal-state set, the
  set the consumer of this file applies, which also contains ``expired``;
* ``client_order_id`` -- the raw ``ref_id`` verbatim, null when absent or empty;
* ``order_id`` -- the first usable value of ``id`` or ``order_id``, verbatim, else null.

It fails CLOSED -- exit 2, ``{"ok": false, "reason", "detail"}``, nothing written (an existing
``--out`` is untouched) -- on any envelope or row it cannot map exactly. Details name the row
index and field only, never a row's values. The caller falls back to the full Monday fetch.

Usage::

    python3 -m scripts.reshape_order_pages --envelope "$ROOT/orders_pages_week_<Monday>_1230.json" \\
        --account "$ACCT" --trading-day <Monday YYYY-MM-DD> --out "$ROOT/orders_week.json"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Sequence

from dca_config import _reject_duplicate_keys
from dca_fileio import atomic_write_text
from dca_runner import _is_dead_order_state
from scripts import assemble_order_pages as asm
from scripts.broker_reconciliation import same_day_order_snapshot
from scripts.same_day_top_up_gate import _bind_saved_pages, _PageReplayer

TOOL = "scripts.reshape_order_pages"

# The 12:30 prompt's Gate 4 reshape contract; tests pin that every row carries exactly these.
CONTRACT_FIELDS = frozenset(
    {
        "symbol",
        "side",
        "dollar_amount",
        "created_at",
        "placed_agent",
        "state",
        "executed_dollar_amount",
        "client_order_id",
        "order_id",
    }
)


def _malformed(index: int, what: str) -> asm.AssemblyRefused:
    return asm.AssemblyRefused("malformed_row", f"order {index}: {what}")


def _money(value: Any, index: int, field: str, *, required: bool = False) -> Optional[Decimal]:
    """A finite Decimal from a JSON number or numeric string; None when absent.

    A present-but-unusable value (a boolean, a non-numeric string, NaN) is refused rather than
    read as absent: absent means "the broker reported nothing", unusable means "we cannot tell".
    """
    if value is None:
        if required:
            raise _malformed(index, f"{field} is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _malformed(index, f"{field} is not a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise _malformed(index, f"{field} is not a number") from exc
    if not number.is_finite():
        raise _malformed(index, f"{field} is not finite")
    return number


def _nested_amount(row: dict, key: str, index: int) -> Optional[Decimal]:
    nested = row.get(key)
    if nested is None:
        return None
    if not isinstance(nested, dict):
        raise _malformed(index, f"{key} is not an object")
    return _money(nested.get("amount"), index, f"{key}.amount")


def _created_at(row: dict, index: int) -> str:
    value = row.get("created_at")
    if not isinstance(value, str):
        raise _malformed(index, "created_at is not a string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise _malformed(index, "created_at is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise asm.AssemblyRefused("naive_timestamp", f"order {index}: created_at has no UTC offset")
    # Verbatim, as Gate 4 requires; the gates' datetime.fromisoformat accepts "Z" on 3.11+.
    return value


def _text(row: dict, key: str, index: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _malformed(index, f"{key} is not a non-empty string")
    return value.strip()


def _optional_text(row: dict, key: str, index: int) -> Optional[str]:
    value = row.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _malformed(index, f"{key} is not a string")
    return value


def reshape_row(row: Any, index: int) -> dict:
    """One raw ``get_equity_orders`` row as a Gate 4 ``orders_week.json`` entry, or refuse."""
    if not isinstance(row, dict):
        raise _malformed(index, "row is not an object")
    symbol = _text(row, "symbol", index).upper()
    side = _text(row, "side", index).lower()
    state = _text(row, "state", index).lower()
    created_at = _created_at(row, index)

    if row.get("dollar_based_amount") is not None:
        # The shared check, so a NaN or boolean amount is refused with the same reason code the
        # envelope's own proof would give.
        amount: Optional[Decimal] = asm._dollar_amount(row, index)
    else:
        amount = None
    if amount is None and side == "buy":
        # A quantity-based buy still commits money; value it or refuse, never read it as zero.
        quantity = _money(row.get("quantity"), index, "quantity")
        price = _money(row.get("price") or row.get("average_price"), index, "price")
        if not (quantity and price and quantity > 0 and price > 0):
            raise _malformed(index, "buy has no dollar amount and no quantity and price")
        amount = quantity * price

    executed: Optional[Decimal] = None
    if _is_dead_order_state(state):
        executed = _nested_amount(row, "executed_notional", index)
        if executed is None:
            cumulative = _money(row.get("cumulative_quantity"), index, "cumulative_quantity")
            average = _money(row.get("average_price"), index, "average_price")
            if cumulative and average and cumulative > 0 and average > 0:
                executed = cumulative * average

    order_id = None
    for key in ("id", "order_id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            order_id = value
            break

    return {
        "symbol": symbol,
        "side": side,
        "dollar_amount": float(amount) if amount is not None else 0.0,
        "created_at": created_at,
        "placed_agent": _optional_text(row, "placed_agent", index),
        "state": state,
        "executed_dollar_amount": float(executed) if executed is not None else None,
        "client_order_id": _optional_text(row, "ref_id", index),
        "order_id": order_id,
    }


def _load_envelope(path: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=asm._reject_constant)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise asm.AssemblyRefused("envelope_unreadable", f"{path}: {type(exc).__name__}") from exc


def reshape(envelope_path: str, *, account_number: str, trading_day: str) -> list:
    """Return the Gate 4 list for a proven envelope, or raise ``AssemblyRefused``."""
    account = account_number.strip()
    if not account:
        raise asm.AssemblyRefused("missing_account")
    day = asm._validated_day(trading_day)
    payload = _load_envelope(envelope_path)
    pages, problem = _bind_saved_pages(payload, account_number=account, trading_day=day)
    if problem is not None or pages is None:
        raise asm.AssemblyRefused(problem or "orders_pages_missing_query_metadata")
    for index, page in enumerate(pages):
        if not asm._is_provable_orders_page(page):
            raise asm.AssemblyRefused("page_not_an_orders_page", f"page {index}: no orders list")
    asm._validate_rows(pages)
    # The complete-chain proof, run on the envelope's pages exactly as ``assemble_order_pages.assemble``
    # runs it on page files: the cursor rules live in ``same_day_order_snapshot`` and the replay in
    # ``_PageReplayer``, so nothing here re-derives them. (Not factored out of ``assemble`` itself:
    # that file is bundled into ``backup_planner.pyz``, and any edit to it forces a planner rebuild
    # and a Perplexity repaste.)
    replayer = _PageReplayer(list(pages))
    snapshot = same_day_order_snapshot(
        replayer, account_number=account, created_at_gte=day, max_pages=max(len(pages) + 1, 2)
    )
    if not replayer.fully_consumed:
        raise asm.AssemblyRefused("unconsumed_saved_order_pages", "a page before the last has no continuation pointer")
    if not snapshot.pagination_complete:
        raise asm.AssemblyRefused("history_incomplete", ",".join(snapshot.incomplete_reasons) or "pagination_incomplete")

    out: list = []
    seen: set = set()
    rows = [row for page in pages for row in asm._order_rows(page)]
    for index, row in enumerate(rows):
        key = row["id"].lower()  # _validate_rows proved every row has a UUID id
        if key in seen:
            continue  # an order repeated across pages must not count twice
        seen.add(key)
        out.append(reshape_row(row, index))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--envelope", required=True, help="Saved orders_pages envelope (assembled or merged).")
    parser.add_argument("--account", required=True, help="Account number; must match the envelope.")
    parser.add_argument("--trading-day", required=True, help="The envelope's trading_day, YYYY-MM-DD (the week's Monday).")
    parser.add_argument("--out", required=True, help="Path of the orders_week.json list to write.")
    args = parser.parse_args(argv)

    try:
        rows = reshape(args.envelope, account_number=args.account, trading_day=args.trading_day)
        text = json.dumps(rows, indent=2) + "\n"
        atomic_write_text(args.out, text)
    except asm.AssemblyRefused as exc:
        print(json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": "out_unwritable", "detail": type(exc).__name__}))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "tool": TOOL,
                "order_count": len(rows),
                "out": args.out,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
