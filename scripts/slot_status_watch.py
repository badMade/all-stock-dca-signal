#!/usr/bin/env python3
"""Mark running slots in the slot-status document, then publish it (credit plan step 6, 2026-11-05).

The Mac dispatcher (``scripts/backup_dispatch.py run``) checks a slot 15 minutes after it starts and the cloud
watcher 20 minutes after. On 2026-11-05 the 9:35 run was still placing orders at 9:50 and recorded ``succeeded``
only at 9:55:04, so both would have emailed Perplexity while Claude was mid-run: the live runs never call
``local_heartbeat``, so nothing said "running".

The live runs do write into their dated run folder the whole time (2026-11-05: a file every 30-40 s from 09:36 to
09:55). This module, run by the Mac publish job (launchd, every 2 minutes and on every change), records a slot whose
run folder exists today but has no final status as ``in_progress``, with ``activity_at`` = the newest file time in
that folder, and then publishes the document exactly as ``slot_status publish`` does. A final status (``succeeded``,
``failed``, ``backup_requested``) is never overwritten. The dispatcher and watcher then wait while the activity is
fresh and send once it goes quiet.

Kept out of ``scripts/slot_status.py`` on purpose: that module is bundled into ``backup_planner.pyz`` (the finalizer
imports it), so any change there re-stamps the planner and asks the operator for a Perplexity re-paste. Nothing in
the planner imports this one.

Standard library only; it reads folder timestamps and never a file's contents, so no ticker, amount or order id can
reach the public document.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dca_paths
from scripts import slot_status
from scripts.verify_schedule import slot_run_dir_relative

FINAL_STATUSES = frozenset({"succeeded", "failed", "backup_requested"})


def _newest_activity(run_dir: Path) -> float:
    """Epoch seconds of the newest file in the folder (the folder itself when it holds none)."""
    stamps = [path.stat().st_mtime for path in run_dir.iterdir() if path.is_file()]
    return max(stamps) if stamps else run_dir.stat().st_mtime


def mark_running(root: str | Path, *, now: datetime) -> list[str]:
    """Record every slot whose run folder exists today with no final status as in_progress; return those slots."""
    now_et = now.astimezone(slot_status.ET)
    day = now_et.date()
    activity: dict[str, str] = {}
    for slot in slot_status.SLOTS:
        run_dir = Path(root) / slot_run_dir_relative(slot, day)
        if run_dir.is_dir():
            stamp = datetime.fromtimestamp(_newest_activity(run_dir), tz=timezone.utc).astimezone(slot_status.ET)
            activity[slot] = stamp.isoformat(timespec="seconds")
    if not activity:
        return []  # nothing running: write nothing, so a quiet day leaves no document behind

    marked: list[str] = []

    def change(document: dict[str, Any]) -> None:
        for slot, active_at in activity.items():
            entry = document["slots"].get(slot)
            if isinstance(entry, dict) and entry.get("status") in FINAL_STATUSES:
                continue  # the finalizer (or a sent backup) already decided this slot
            stamped = entry.get("stamped_at") if isinstance(entry, dict) and entry.get("stamped_at") else None
            document["slots"][slot] = {"status": "in_progress", "stamped_at": stamped or slot_status._stamp(now),
                                       "activity_at": active_at}
            marked.append(slot)
        if marked:
            document["updated_at"] = slot_status._stamp(now)

    slot_status._write(day.isoformat(), change)
    return marked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)")
    parser.add_argument("--now", help="offset-aware ISO time; for tests")
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("naive clock")
    except ValueError:
        print(json.dumps({"ok": False, "error": "bad_now"}))
        return 2
    root = Path(args.root) if args.root else Path(dca_paths.cron_tracking_root())
    report: dict[str, Any] = {}
    try:
        marked = mark_running(root, now=now)
    except (OSError, ValueError, TimeoutError) as exc:
        # Marking is best-effort: a finished slot's status must still be published.
        marked, report["mark_error"] = [], type(exc).__name__
    code, published = slot_status.publish_local(slot_status._configured_publisher())
    print(json.dumps({"marked": marked, **report, **published}, sort_keys=True))
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
