"""Mac side of the Perplexity backups' "hybrid" order history: look up a saved week envelope, merge an increment.

Why this exists (2026-11-14). On 2026-11-13 the 9:35 Claude slot did not fire, and the 10:30 slot fell back to a
full-week ``get_equity_orders`` fetch too large to save (output_cap_exceeded, 0 orders). Commit 67d9d617 fixed the
Claude prompts (``find_order_baseline --kind week`` accepts earlier-day envelopes). The Perplexity backups fetch SEVEN
days through their sandbox, which is the same oversized shape, and the saved envelopes exist only on the Mac. So when
the Mac is reachable (``mac_status`` online, ``mac_path_abandoned`` false) the backup procedure now:

1. ``lookup``: finds the newest complete ISO-week envelope with :func:`scripts.find_order_baseline.find_baseline`
   (kind ``week``) and prints the ``created_at_gte`` the sandbox fetches the increment from;
2. the sandbox fetches only that increment and its raw pages are put, unchanged, in the printed ``work_dir``;
3. ``merge``: re-checks the base exactly as the lookup does, merges with :func:`scripts.merge_order_pages.merge`, and
   writes this run's week envelope inside ``work_dir``. Every planner command that reads history (classify, compute,
   finalize, check-placement) then runs ON THE MAC with ``--week-envelope`` against it. Poll 2, the late-start
   recheck and every step 7c per-order repull merge onto the PREVIOUS merged file the same way (``--base`` that file,
   the increment fetched from the ``next_created_at_gte`` its merge printed).

The merged file never moves into the sandbox. Any exit 2 here means the procedure falls back to its full seven-day
sandbox fetch (``week_history_mode full``); nothing is retyped.

Two properties the procedure relies on:

* No account number on any command line or in any output. The account comes from the Mac's own
  ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (``dca_paths`` loads it from the checkout's .env) and must end in
  ``--account-last4``, the last four of the account the backup resolved; otherwise exit 2.
* The days before Monday are NOT sourced (operator decision 2026-11-14, option 1). Last ISO week's newest envelope ends
  before that week's final orders and any weekend order, and get_equity_orders has no ``created_at_lte`` to fetch just
  that seam. The planner instead treats the trailing-7-day kill switch as engaged whenever it reads a week envelope
  (``backup_planner._bind_week_envelope``).

Usage (Mac, from the all-stock-dca checkout)::

    python3 -m scripts.backup_week_history lookup --trading-day D --slot S --account-last4 NNNN
    python3 -m scripts.backup_week_history merge --trading-day D --slot S --account-last4 NNNN --label poll1 \\
        --base <printed base> --created-at-gte <printed created_at_gte> --snapshot-at <instant> page1.json ...

Standard library only. ``lookup`` never writes except creating ``work_dir``; ``merge`` writes one new file in it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import dca_paths
from dca_fileio import atomic_write_text
from scripts import assemble_order_pages as asm
from scripts import find_order_baseline as fob
from scripts import merge_order_pages as mop

TOOL = "scripts.backup_week_history"
ACCOUNT_ENV = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
MODE = "prior_baseline_merge"
OVERLAP_SECONDS = 120  # the merge's own default, the same 120 s every live prompt uses
WORK_SUBDIR = "perplexity_backup"
_LAST4 = re.compile(r"^\d{4}$")
_LABEL = re.compile(r"^[a-z0-9_]{1,24}$")


class Refused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _utc_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _account(last4: str) -> str:
    if not _LAST4.match(last4 or ""):
        raise Refused("bad_account_last4", "expected exactly four digits")
    account = os.environ.get(ACCOUNT_ENV, "").strip()
    if not account:
        raise Refused("account_unset", f"{ACCOUNT_ENV} is not set on this machine")
    if not account.endswith(last4):
        raise Refused("account_last4_mismatch", "the Mac's configured account is not the one this run resolved")
    return account


def _day(text: str) -> date:
    try:
        return date.fromisoformat(asm._validated_day(text))
    except asm.AssemblyRefused as exc:
        raise Refused(exc.reason, exc.detail) from exc


def work_dir(trading_day: date, slot: str, root: Optional[Path] = None) -> Path:
    """This backup run's Mac-side directory: ``<cron root>/perplexity_backup/<YYYYMMDD>_<slot>ET``.

    Outside every slot run directory, so ``find_order_baseline`` never offers a backup's merged file as a Claude
    slot's base, and a backup never writes where a Claude slot writes.
    """
    base = Path(root) if root is not None else Path(dca_paths.cron_tracking_root())
    return base / WORK_SUBDIR / f"{trading_day.strftime('%Y%m%d')}_{slot}ET"


def lookup(*, trading_day: str, slot: str, account_last4: str, now: Optional[datetime] = None,
           root: Optional[Path] = None) -> dict:
    account = _account(account_last4)
    day = _day(trading_day)
    found = fob.find_baseline(kind="week", slot=slot, trading_day=day.isoformat(), account=account, now=now,
                              overlap_seconds=OVERLAP_SECONDS, root=root)
    if not found["ok"]:
        raise Refused(found["reason"], found.get("detail", ""))
    directory = work_dir(day, slot, root)
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "ok": True,
        "tool": TOOL,
        "week_history_mode": MODE,
        "base": found["base"],
        "base_slot": found["base_slot"],
        "base_day": found["base_day"],
        "snapshot_at": found["snapshot_at"],
        # One instant, UTC: the sandbox fetches with this string verbatim and passes it back to merge unchanged.
        "created_at_gte": _utc_z(fob._aware(found["created_at_gte"])),
        "order_count": found["order_count"],
        "work_dir": str(directory),
        "rejected": found["rejected"],
    }


def merge(*, trading_day: str, slot: str, account_last4: str, label: str, base: str, created_at_gte: str,
          snapshot_at: str, page_files: Sequence[str], now: Optional[datetime] = None,
          root: Optional[Path] = None) -> dict:
    account = _account(account_last4)
    day = _day(trading_day)
    if not _LABEL.match(label or ""):
        raise Refused("bad_label", "lowercase letters, digits and underscores, at most 24")
    moment = now if now is not None else datetime.now(timezone.utc)
    # The same acceptance test the lookup ran, on whatever --base names: a Claude week envelope from the lookup, or
    # this run's previous merged file. A hand-built, edited or wrong-week file is refused here, never merged.
    try:
        fob.check_candidate(Path(base), kind="week", account=account, day=day, now=moment)
    except fob.BaselineRejected as exc:
        raise Refused(f"base_{exc.reason}", exc.detail) from exc
    snapshot = mop._aware(snapshot_at, "--snapshot-at")
    if snapshot.astimezone(fob.ET).date() != day:
        raise Refused("snapshot_not_today", snapshot_at)
    if snapshot > moment + timedelta(seconds=OVERLAP_SECONDS):
        raise Refused("snapshot_in_future", snapshot_at)
    directory = work_dir(day, slot, root)
    monday = fob.monday_of(day).isoformat()
    out = directory / f"orders_pages_week_{monday}_backup_{slot}_{label}.json"
    if out.exists():
        # Each merge gets its own label; overwriting one would break the chain later merges cite as --base.
        raise Refused("out_exists", out.name)
    envelope = mop.merge(base, page_files, account_number=account, trading_day=monday, created_at_gte=created_at_gte,
                         snapshot_at=snapshot_at, overlap_seconds=OVERLAP_SECONDS)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(str(out), json.dumps(envelope, indent=2) + "\n")
    ingest = envelope["ingest"]
    return {
        "ok": True,
        "tool": TOOL,
        "week_history_mode": MODE,
        "out": str(out),
        "snapshot_at": envelope["snapshot_at"],
        # The created_at_gte of the NEXT increment merged onto this file (poll 2, step 7c): snapshot minus the overlap.
        "next_created_at_gte": _utc_z(snapshot - timedelta(seconds=OVERLAP_SECONDS)),
        "order_count": ingest["order_count"],
        "increment_order_count": ingest["increment_order_count"],
        "payload_sha256": ingest["payload_sha256"],
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("lookup", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--trading-day", required=True, help="Today, YYYY-MM-DD (America/New_York).")
        p.add_argument("--slot", required=True, choices=sorted(fob.SLOT_EVIDENCE_LAYOUT), help="This run's slot (HHMM).")
        p.add_argument("--account-last4", required=True, help="Last four digits of the account this run resolved.")
        p.add_argument("--cron-root", default=None, help="Cron-tracking root (default: dca_paths). Tests only.")
        p.add_argument("--now", default=None, help="Offset-aware ISO instant instead of the clock. Tests only.")
        if name == "merge":
            p.add_argument("--label", required=True, help="poll1, poll1b, poll2, order1, ...: names the output file.")
            p.add_argument("--base", required=True, help="The lookup's base, or this run's previous merged file.")
            p.add_argument("--created-at-gte", required=True, help="The created_at_gte the increment was fetched with.")
            p.add_argument("--snapshot-at", required=True, help="Offset-aware instant right after the last page.")
            p.add_argument("page_files", nargs="+", help="The increment's raw pages, in fetch order.")
    args = parser.parse_args(argv)
    root = Path(args.cron_root) if args.cron_root else None
    try:
        now = None
        if args.now:
            now = fob._aware(args.now)
        if args.command == "lookup":
            result: dict[str, Any] = lookup(trading_day=args.trading_day, slot=args.slot,
                                            account_last4=args.account_last4, now=now, root=root)
        else:
            result = merge(trading_day=args.trading_day, slot=args.slot, account_last4=args.account_last4,
                           label=args.label, base=args.base, created_at_gte=args.created_at_gte,
                           snapshot_at=args.snapshot_at, page_files=args.page_files, now=now, root=root)
    except Refused as exc:
        print(json.dumps({"ok": False, "week_history_mode": "full", "reason": exc.reason, "detail": exc.detail}))
        return 2
    except (asm.AssemblyRefused, fob.BaselineRejected) as exc:
        print(json.dumps({"ok": False, "week_history_mode": "full", "reason": exc.reason, "detail": exc.detail}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "week_history_mode": "full", "reason": "io_error", "detail": type(exc).__name__}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
