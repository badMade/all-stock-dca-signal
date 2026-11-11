"""Check that Claude is available before each live DCA slot.

The seven live slots depend on Claude Desktop's scheduled tasks. 2026-11-09:
the app was closed from 22:24 ET the prior day until 19:12 ET, so every slot
missed its window and caught up too late. This independent launchd check opens
Claude and records an alert before a slot can be silently missed. It never
places orders or stops the app.

Public API: :func:`check` accepts injected process, launch, sleep and alert
functions; :func:`main` implements the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time as time_module
from datetime import datetime, time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text
from dca_paths import cron_tracking_root
from scripts.alert_digest import is_trading_day
from scripts.schedule_watchdog import notify_macos

ET = ZoneInfo("America/New_York")
SLOTS = ("09:35", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30")


def _pgrep_running() -> bool:
    """Return whether the exact Claude process name is present.

    ``-a`` matters (2026-11-09 review): BSD/macOS pgrep silently drops its own
    ancestors from the match list. Run from any shell a Claude session started,
    Claude *is* an ancestor, so a plain ``pgrep -x Claude`` exits 1 while the
    app is open, and this check would report a running Claude as down.
    """
    return subprocess.run(["pgrep", "-a", "-x", "Claude"], check=False, capture_output=True).returncode == 0


def _launch_claude() -> None:
    """Ask Launch Services to open Claude; never signal an existing process."""
    subprocess.run(["open", "-a", "Claude"], check=False, capture_output=True)


def _next_slots(now: datetime) -> list[str]:
    current = now.timetz().replace(tzinfo=None)
    return [slot for slot in SLOTS if time.fromisoformat(slot) >= current]


def check(
    now: datetime,
    *,
    root: Optional[Path] = None,
    is_running: Callable[[], bool] = _pgrep_running,
    launch: Callable[[], Any] = _launch_claude,
    sleep: Callable[[float], Any] = time_module.sleep,
    wait_s: int = 90,
    notify: Optional[Callable[[str, str], Any]] = None,
) -> dict[str, Any]:
    """Check/reopen Claude, persisting one JSONL check and an idempotent alert.

    ``now`` is interpreted in ET (naive values are assumed ET). ``root`` is a
    test override; production always resolves the shared root with
    :func:`dca_paths.cron_tracking_root`.
    """
    checked = now.replace(tzinfo=ET) if now.tzinfo is None else now.astimezone(ET)
    stamp = checked.isoformat(timespec="seconds")
    day = checked.date()
    if not is_trading_day(day):
        print(f"skip: market_closed {stamp}")
        return {"status": "market_closed", "checked_at": stamp, "waited_s": 0, "exit_code": 0}

    base = Path(root) if root is not None else Path(cron_tracking_root())
    if is_running():
        status, waited = "claude_running", 0
        print(f"ok: claude_running {stamp}")
    else:
        launch()
        waited = 0
        status = "still_down"
        for second in range(max(0, wait_s)):
            sleep(1)
            waited = second + 1
            if is_running():
                status = "reopened"
                break
        print(f"{'ok' if status == 'reopened' else 'error'}: {status} {stamp}")

        hhmm = checked.strftime("%H%M")
        alert_path = base / "claude_liveness" / "alerts" / f"{day.isoformat()}-{hhmm}-CLAUDE_NOT_RUNNING.txt"
        future_slots = _next_slots(checked)
        risk = ", ".join(f"{slot} ET" for slot in future_slots) if future_slots else "no later slots today"
        body = (
            f"Claude was not running at {stamp}.\nOutcome: {status} (waited {waited}s).\n"
            f"Next slot(s) at risk: {risk}.\n"
            "Remedy: open Claude. A slot whose window has passed must not be placed late; "
            "check Robinhood order history before any manual placement because the Perplexity backup may have bought.\n"
        )
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(alert_path, body)
        if notify is not None:
            try:
                notify("Claude is not running", f"Claude liveness: {status}. At risk: {risk}.")
            except Exception as exc:  # Notification is secondary to the durable alert.
                print(f"warning: notification failed ({type(exc).__name__})", file=sys.stderr)

    row = {"checked_at": stamp, "status": status, "waited_s": waited}
    log_path = base / "claude_liveness" / f"checks_{day.isoformat()}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    return {**row, "exit_code": 1 if status == "still_down" else 0}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the command-line liveness check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notify", action="store_true", help="raise a macOS notification on a down outcome")
    parser.add_argument("--now", help="test clock in ISO-8601 ET (defaults to the current time)")
    parser.add_argument("--wait", type=int, default=int(os.environ.get("CLAUDE_LIVENESS_WAIT", "90")))
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(ET)
    except ValueError:
        parser.error("--now must be an ISO-8601 timestamp")
    if args.wait < 0:
        parser.error("--wait must be non-negative")
    result = check(now, wait_s=args.wait, notify=notify_macos if args.notify else None)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
