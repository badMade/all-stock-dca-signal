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

Validated ingest (2026-10-05, five of seven slots): the same hand-transcription failure
hit the order history when the response was NOT oversized, so no saved file existed and
the session re-keyed it. The 10:30 envelope kept about 12 of the ~19 raw fields (no
``ref_id``) and the 14:30 one carried ids like ``transcribed-00-AAPL``; the top-up guard
returned PROCEED on both. So this script is the one validated path in, and it also:

* refuses a placeholder order id (real broker ids are canonical UUIDs) and a condensed
  capture (a row missing a field fixed when the order was created, so present on every raw
  row, filled or not), BEFORE the pagination replay, so a hand-built capture is refused for
  what it is;
* refuses a non-finite or boolean ``dollar_based_amount``;
* with ``--expect-count`` / ``--expect-total-usd``, cross-checks the history against the
  response's own count and dollar total (decimal arithmetic, a difference below half a cent
  is a match) and refuses a mismatch;
* reads one page from stdin when a page file is ``-``;
* records an ``ingest`` block in the envelope: provenance (``spilled_file``, or the weaker
  ``stdin_paste`` when any page came from stdin, derived never asserted), order count and
  dollar total, per-page and payload sha256, and the cross-check outcome.

The pages themselves stay byte-for-byte what the session supplied. The account number is
still written into the envelope: the Gate 6.5 guard binds on it. Redacting it is a separate
step.

Usage::

    python3 -m scripts.assemble_order_pages --account "$ACCT" --trading-day 2026-09-09 \\
        --out "$ROOT/orders_pages_2026-09-09_1230.json" [--expect-count N --expect-total-usd X] \\
        page1.txt page2.txt

Per-state slices (2026-11-03): when even ONE page is too large for the session to show or
move (the 11-03 12:30 weekly page was 61k characters and its saved file was cloud-side,
unreachable from the repo shell), fetch the window once per order state instead --
``get_equity_orders`` takes a ``state`` filter and no other way to shrink a page -- and pass
every page of every state with ``--slice STATE=PAGE_FILE`` (repeat per page, in fetch order
within a state)::

    python3 -m scripts.assemble_order_pages --account "$ACCT" --trading-day 2026-11-03 \\
        --out "$ROOT/orders_pages_2026-11-03_1230.json" \\
        --slice filled=filled_p1.json --slice filled=filled_p2.json --slice cancelled=cancelled_p1.json \\
        ... one or more pages for EVERY state in ``ORDER_STATES``

Each state's pages are proven a complete chain on their own, every row must carry the state
its slice names, and ALL of ``ORDER_STATES`` must be present (a state with no orders is an
``{"orders": []}`` page, not an omitted flag). The slices are then merged, one row per order
id, into a single terminal page, so the envelope looks to every downstream gate like one
complete history; its ``ingest.mode`` is ``state_slices`` with per-state counts and hashes.
Residual risk, stated plainly: a state the broker adds later is invisible to this partition,
which is why the list is pinned by a test and an unknown row state is refused.

Exit 0 prints ``{"ok": true, "page_count", "order_count", "out", "payload_sha256",
"provenance"}``; every refusal exits 2, prints ``{"ok": false, "reason", "detail"}``, and
writes nothing (an existing ``--out`` file is left untouched).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from dca_config import InvalidAccountNumberError, MissingConfigEnvError, _reject_duplicate_keys, resolve_order_account
from dca_fileio import atomic_write_text
from dca_live_adapters import (
    _is_provable_orders_page,
    _orders_in_settled_layer_or_nested,
    _unwrap_seeking_order_evidence,
)
from scripts.broker_reconciliation import same_day_order_snapshot
from scripts.same_day_top_up_gate import _PageReplayer

TOOL = "scripts.assemble_order_pages"

# Real broker order ids are canonical UUIDs; anything else was typed by a person or a model.
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Fields fixed when an order is created, so present on every raw ``get_equity_orders`` row
# whether or not it has filled. Derived from the raw envelopes seen on 2026-10-05 (not a
# documented schema); the 2026-10-05 condensed captures dropped ``ref_id``, ``time_in_force``,
# ``trigger`` and ``market_hours``. The key must exist; its value may be null.
_CREATION_TIME_FIELDS = (
    "ref_id",
    "time_in_force",
    "trigger",
    "market_hours",
    "placed_agent",
    "type",
    "symbol",
    "side",
    "state",
    "created_at",
)

# A total within this of the expected figure is a match: amounts are whole cents, so half a
# cent is the tightest tolerance that still forgives a figure rounded to the cent.
_TOTAL_TOLERANCE = Decimal("0.005")


class AssemblyRefused(Exception):
    """A page set that cannot be proven complete; carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _reject_constant(token: str) -> None:
    # json.loads accepts the bare tokens NaN / Infinity / -Infinity by default.
    raise ValueError(f"non-finite JSON token {token}")


def _parse_json(text: str, source: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
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


def _load_page(path: str) -> Tuple[Any, str, str]:
    """Return ``(page, sha256_of_the_bytes_read, source)`` for one page file, or ``-`` for stdin."""
    if path == "-":
        text = sys.stdin.read()
        raw, source = text.encode("utf-8"), "stdin"
    else:
        try:
            raw = Path(path).read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AssemblyRefused("page_file_unreadable", f"{path}: {type(exc).__name__}") from exc
        source = "file"
    page = _unwrap_text_block(_parse_json(text, path), path)
    if not isinstance(page, dict):
        raise AssemblyRefused("page_not_an_orders_page", f"{path}: top level is {type(page).__name__}")
    return page, hashlib.sha256(raw).hexdigest(), source


def _order_rows(page: dict) -> list:
    rows = _orders_in_settled_layer_or_nested(_unwrap_seeking_order_evidence(page))
    return rows if isinstance(rows, list) else []


def _dollar_amount(row: dict, index: int) -> Decimal:
    """The row's ``dollar_based_amount.amount`` as an exact Decimal; zero for a quantity-based order."""
    dollars = row.get("dollar_based_amount")
    if not isinstance(dollars, dict):
        return Decimal(0)
    amount = dollars.get("amount")
    # bool subclasses int: a JSON ``true`` would otherwise read as $1.
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float)):
        raise AssemblyRefused("non_finite_value", f"order {index}: dollar amount is not a number")
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise AssemblyRefused("non_finite_value", f"order {index}: dollar amount is not a number") from exc
    if not value.is_finite():
        raise AssemblyRefused("non_finite_value", f"order {index}: dollar amount is not finite")
    return value


def _validate_rows(pages: Sequence[dict]) -> Decimal:
    """Refuse placeholder ids, condensed captures and non-finite amounts; return the dollar total.

    Runs BEFORE the pagination replay so a hand-built capture is refused for what it is, not
    for whatever incompleteness the replay happens to notice first. Rows are counted once per
    id, so an order repeated across pages cannot inflate the total.
    """
    rows = [row for page in pages for row in _order_rows(page)]
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AssemblyRefused("page_not_an_orders_page", f"order {index} is not an object")
        order_id = row.get("id")
        if not isinstance(order_id, str) or not _UUID.match(order_id):
            raise AssemblyRefused("placeholder_order_id", f"order {index}: id is not a broker order id")
    for index, row in enumerate(rows):
        missing = [field for field in _CREATION_TIME_FIELDS if field not in row]
        if missing:
            raise AssemblyRefused("condensed_capture", f"order {index} is missing {', '.join(missing)}")
    total = Decimal(0)
    seen = set()
    for index, row in enumerate(rows):
        amount = _dollar_amount(row, index)
        if row["id"].lower() not in seen:
            seen.add(row["id"].lower())
            total += amount
    return total


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    expect_count: Optional[int] = None,
    expect_total_usd: Optional[Decimal] = None,
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
    if list(page_files).count("-") > 1:
        raise AssemblyRefused("multiple_stdin_inputs", "stdin can supply only one page")

    loaded = [_load_page(path) for path in page_files]
    pages: List[Any] = [page for page, _digest, _source in loaded]
    for path, page in zip(page_files, pages, strict=True):
        # Same check the replay applies, surfaced per file so the refusal names
        # the file instead of a generic pagination reason.
        if not _is_provable_orders_page(page):
            raise AssemblyRefused("page_not_an_orders_page", f"{path}: no orders list")

    total_usd = _validate_rows(pages)

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

    order_count = len(snapshot.orders)
    if expect_count is not None and expect_count != order_count:
        raise AssemblyRefused("count_mismatch", f"history has {order_count} orders, expected {expect_count}")
    if expect_total_usd is not None and abs(total_usd - expect_total_usd) >= _TOTAL_TOLERANCE:
        raise AssemblyRefused("total_mismatch", f"history totals {total_usd} USD, expected {expect_total_usd}")

    envelope: dict = {"account_number": account, "trading_day": day}
    if snapshot_at:
        envelope["snapshot_at"] = snapshot_at
    envelope["pages"] = pages
    envelope["ingest"] = {
        "tool": TOOL,
        # Derived, never asserted: any page relayed through stdin makes the whole chain the weaker class.
        "provenance": "stdin_paste" if any(source == "stdin" for _p, _d, source in loaded) else "spilled_file",
        "order_count": order_count,
        "total_usd": float(total_usd),
        "cross_check": {
            "expected_count": expect_count,
            "expected_total_usd": None if expect_total_usd is None else float(expect_total_usd),
            "matched": None if expect_count is None and expect_total_usd is None else True,
        },
        "pages_sha256": [digest for _page, digest, _source in loaded],
        "payload_sha256": hashlib.sha256(_canonical(pages)).hexdigest(),
    }
    return envelope, order_count


# Every value of get_equity_orders' ``state`` filter (its tool description, 2026-11-03). The slice mode demands each one,
# because a partition is only a complete history when no part of it is missing.
ORDER_STATES = (
    "new",
    "queued",
    "confirmed",
    "unconfirmed",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "failed",
    "voided",
)


def assemble_state_slices(
    slices: dict,
    *,
    account_number: str,
    trading_day: str,
    snapshot_at: Optional[str] = None,
) -> tuple[dict, int]:
    """Return ``(envelope, order_count)`` for a complete set of per-state page chains.

    ``slices`` maps each state in :data:`ORDER_STATES` to its page files in fetch order. Raises
    :class:`AssemblyRefused` when a state is missing or unknown, a chain is incomplete, a row's
    state disagrees with its slice, or two slices disagree about the same order id.
    """
    unknown = sorted(set(slices) - set(ORDER_STATES))
    if unknown:
        raise AssemblyRefused("unknown_order_state", ", ".join(unknown))
    missing = [state for state in ORDER_STATES if not slices.get(state)]
    if missing:
        raise AssemblyRefused("state_slice_missing", ", ".join(missing))

    merged: dict = {}
    per_state: dict = {}
    for state in ORDER_STATES:
        envelope, count = assemble(
            slices[state], account_number=account_number, trading_day=trading_day, snapshot_at=snapshot_at
        )
        for row in (row for page in envelope["pages"] for row in _order_rows(page)):
            if row.get("state") != state:
                raise AssemblyRefused("slice_state_mismatch", f"{state} slice holds an order in state {row.get('state')!r}")
            key = row["id"].lower()
            if key in merged and merged[key] != row:
                raise AssemblyRefused("slice_row_conflict", f"order {key} differs between slices")
            merged[key] = row
        per_state[state] = {
            "order_count": count,
            "provenance": envelope["ingest"]["provenance"],
            "pages_sha256": envelope["ingest"]["pages_sha256"],
        }

    # Newest first, as the broker returns them; the sort is stable and keyed on a string every raw row carries.
    rows = sorted(merged.values(), key=lambda row: str(row.get("created_at")), reverse=True)
    pages: List[Any] = [{"orders": rows}]
    total_usd = _validate_rows(pages)
    out: dict = {"account_number": account_number.strip(), "trading_day": _validated_day(trading_day)}
    if snapshot_at:
        out["snapshot_at"] = snapshot_at
    out["pages"] = pages
    out["ingest"] = {
        "tool": TOOL,
        "mode": "state_slices",
        "provenance": "stdin_paste" if any(v["provenance"] == "stdin_paste" for v in per_state.values()) else "spilled_file",
        "order_count": len(rows),
        "total_usd": float(total_usd),
        "cross_check": {"expected_count": None, "expected_total_usd": None, "matched": None},
        "slices": per_state,
        "payload_sha256": hashlib.sha256(_canonical(pages)).hexdigest(),
    }
    return out, len(rows)


def _decimal_arg(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not value.is_finite():
        raise argparse.ArgumentTypeError(f"not a finite number: {text!r}")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--account",
        default=None,
        help=(
            "Deprecated: leave it out. Defaults to $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER so the number stays off the command line (AGENTS.md secrets policy). "
            "Account number the pages were fetched for."
        ),
    )
    parser.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
    parser.add_argument("--out", required=True, help="Envelope path (the gate's orders_pages_<day>_<slot>.json).")
    parser.add_argument("--snapshot-at", default=None, help="Optional ISO timestamp; the 09:35 envelope carries it.")
    parser.add_argument("--expect-count", type=int, default=None, help="The response's own order count; a mismatch is refused.")
    parser.add_argument(
        "--expect-total-usd", type=_decimal_arg, default=None, help="The response's own dollar total; off by half a cent or more is refused."
    )
    parser.add_argument(
        "--slice",
        action="append",
        default=[],
        metavar="STATE=PAGE_FILE",
        help="Per-state mode: one saved page of the fetch filtered to STATE; repeat per page, in fetch order, for every state.",
    )
    parser.add_argument("page_files", nargs="*", help="Saved page files, in fetch order; '-' reads one page from stdin.")
    args = parser.parse_args(argv)
    # The account comes from the environment (dca_config.resolve_order_account); --account is still accepted for an
    # existing caller. A failure is a usage error that names the VARIABLE and never the value.
    try:
        args.account = resolve_order_account(args.account)
    except (MissingConfigEnvError, InvalidAccountNumberError) as exc:
        parser.error(str(exc))

    try:
        if args.slice:
            if args.page_files or args.expect_count is not None or args.expect_total_usd is not None:
                raise AssemblyRefused("slice_mode_exclusive", "--slice takes no positional pages or --expect-* flags")
            slices: dict = {}
            for item in args.slice:
                state, sep, path = item.partition("=")
                if not sep or not state or not path:
                    raise AssemblyRefused("bad_slice_argument", item)
                slices.setdefault(state, []).append(path)
            envelope, order_count = assemble_state_slices(
                slices, account_number=args.account, trading_day=args.trading_day, snapshot_at=args.snapshot_at
            )
        else:
            envelope, order_count = assemble(
                args.page_files,
                account_number=args.account,
                trading_day=args.trading_day,
                snapshot_at=args.snapshot_at,
                expect_count=args.expect_count,
                expect_total_usd=args.expect_total_usd,
            )
        atomic_write_text(args.out, json.dumps(envelope, indent=2) + "\n")
    except AssemblyRefused as exc:
        print(json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": "out_unwritable", "detail": type(exc).__name__}))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "page_count": len(envelope["pages"]),
                "order_count": order_count,
                "out": args.out,
                "payload_sha256": envelope["ingest"]["payload_sha256"],
                "provenance": envelope["ingest"]["provenance"],
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
