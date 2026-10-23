#!/usr/bin/env python3
"""Independent watchdog: alerts when a live roster slot leaves no evidence it fired.

Why this exists (2026-09-25). ``docs/safeguards.md``'s live execution roster
note already names the failure this closes: a scheduled executor session
reads a real or perceived conflict in its own instructions, declines to
place, and ends by asking an absent user to confirm instead of following the
standing rule -- "a decline in a scheduled run is an abort that must alert on
all three channels ... it must never end on a question" -- recorded on
2026-09-14, 09-15, 09-17, 09-22 x2, and again 2026-09-25 (12:30, before
Gate 4). A session that ends that way never reaches its own Gate 9 alert
step, so none of the three channels fires and the day looks clean until
someone checks cron_tracking by hand.

Every existing alert channel (in-app message center, the Gate 9 repo alert
file, email) is written BY the same executor session whose judgment just
failed. That is a single point of failure: whatever caused the session to
skip the "never ask, always alert" rule also skips the alert that is
supposed to report it. This module does not depend on that session at all.
It is meant to run on its OWN schedule, external to the seven executor
tasks (see "Deploying this" below), and answers one question from evidence
that is either durably on disk or is not:
:func:`scripts.verify_schedule.verify_day` -- already tested, already the
thing the console's Schedule page reads -- says whether each due slot left
valid ``run_state.json`` evidence. This module adds nothing to that
detection logic; it only alerts on what verify_schedule already finds,
through its own durable-alert-file writer (:func:`write_watchdog_alert_files`,
deliberately not :func:`dca_alerts.write_alert_file` -- see that function's
docstring for why), plus an optional Slack webhook.

Deploying this. Like the seven executor tasks and every other reliability
script in this repo, registering a recurring invocation lives outside this
repo (the external scheduler/Cowork, see docs/live_wiring.md and
CLAUDE.md's "library driven from outside" framing) -- editing this file
changes nothing about what runs on a schedule until someone wires it up.
Running it too soon after nominal fires early and misses the window: a slot
stays ``awaiting_evidence`` (excluded from :func:`failing_rows`, so nothing
alerts) until :func:`scripts.verify_schedule.verify_day`'s own deadline --
that slot's acceptance-window end (``dca_runner.SLOTS[...].window_end_et``,
25-30 minutes past nominal on this roster) plus
``scripts.verify_schedule.GRACE_MINUTES`` (30) -- so the earliest a fire can
usefully run is roughly 55-60 minutes after nominal, and this script adds a
few more minutes of margin for its own scheduling jitter on top of that (say
65-75 minutes after nominal, or run once per day well after the last slot's
deadline, whichever the scheduler prefers) -- ``--json`` output and the exit
code make it equally usable either way. If the invocation is itself an
agent session rather than a bare cron job, that session's ONLY job should be
to run this command and relay its result: there is no decision left for it
to make (the ABORT/alert-or-not verdict is already decided by this script's
exit code), which is deliberately the opposite of the failure mode this
module exists to catch.

CLI (exit 0 when every due slot verifies or the watchdog itself cannot run,
never silently; exit 1 when any slot is missing or incomplete evidence)::

    python -m scripts.schedule_watchdog
    python -m scripts.schedule_watchdog --date 2026-09-25 --json
    python -m scripts.schedule_watchdog --slack --slack-webhook-env DCA_WATCHDOG_SLACK_WEBHOOK
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text
from dca_net_utils import (
    BlockedFileHandler,
    BlockedFTPHandler,
    PinnedHTTPHandler,
    PinnedHTTPSHandler,
)
from dca_net_utils import (
    UnsafeDestinationError as _UnsafeDestinationError,
)
from dca_net_utils import (
    assert_safe_destination as _assert_safe_destination,
)
from dca_net_utils import (
    sanitize_exception as _sanitize_exception,
)
from dca_paths import cron_tracking_root
from scripts.verify_schedule import OK_STATUSES, verify_day

ET = ZoneInfo("America/New_York")

# Primary env var, falling back to the two Slack webhooks dca_alerts.py and
# scripts/drift_detection.py already document -- an operator who has either
# configured gets watchdog alerts with zero new setup, matching the fallback
# chain dca_alerts.FAILURE_WEBHOOK_ENV already establishes.
PRIMARY_SLACK_WEBHOOK_ENV = "DCA_WATCHDOG_SLACK_WEBHOOK"
FALLBACK_SLACK_WEBHOOK_ENVS = ("DCA_ALERT_SLACK_WEBHOOK", "DCA_DRIFT_SLACK_WEBHOOK")

# The gate token this module writes into every durable alert filename
# (<day>-<slot>-<gate>.txt, matching the shape scripts/alert_digest.py's
# _ALERT_FILENAME regex already parses). Deliberately distinct from any
# executor-reported gate name -- these alerts are detected from ABSENT
# evidence by an independent process, not self-reported by the run that
# failed -- and deliberately not the bare word "watchdog": alert_digest.py's
# scripts/alert_digest._REASON_CODES must list this exact token (added
# alongside GATE5_DISABLED/GATE4_EXPECTED_OVERLAP) for detect_gate_evidence
# to recognize it and for the BY GATE grouping to bucket it correctly rather
# than reading gate=None; a bare "watchdog" would also risk matching the
# unrelated historical mentions of the removed 3:40 PM watchdog task that
# already appear in this repo's own docs and alert prose.
WATCHDOG_GATE_TOKEN = "SCHEDULE_WATCHDOG"

_DEFAULT_TIMEOUT_SECONDS = 10.0


def failing_rows(report: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The slots in ``report`` (from :func:`verify_day`) that failed the audit."""
    return [row for row in report["slots"] if row["status"] not in OK_STATUSES]


def alert_body(report: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Plain-text Gate-9-style alert body for one failing slot."""
    reasons = "; ".join(row["reasons"]) or "no reason recorded"
    # Additive triage tag: only a slot that never started carries a Key, so the
    # Notion reconciler can tell it from a late or half-finished run.
    key_line = f"Key: {row['failure_key']}\n" if row.get("failure_key") else ""
    return (
        f"Schedule watchdog: slot {row['time_et']} ET ({row['stream']}) "
        f"left no valid scheduled evidence for {report['date']}.\n"
        f"Status: {row['status']}\n"
        f"{key_line}"
        f"Reasons: {reasons}\n"
        f"Expected run directory: {row['run_dir']}\n"
        f"Detected independently of the executor session by "
        f"scripts.schedule_watchdog at {report['checked_at_et']} -- this "
        f"fires whether or not that session alerted on its own."
    )


def _watchdog_alert_path(root: Path, day: date, slot_id: str) -> Path:
    return root / "schedule_watchdog" / "alerts" / f"{day.isoformat()}-{slot_id}-{WATCHDOG_GATE_TOKEN}.txt"


def write_watchdog_alert_files(report: Mapping[str, Any], *, root: Optional[Path] = None) -> List[Path]:
    """Write one durable alert file per failing slot. Never raises; skips a failed write.

    Deliberately NOT :func:`dca_alerts.write_alert_file`, for two reasons a
    review of the first version of this module caught:

    1. That writer resolves its own root from ``$DCA_CRON_TRACKING_ROOT`` /
       :func:`dca_paths.cron_tracking_root`, independently of whatever
       ``--root`` this invocation was given -- so a custom root would audit
       one tree while writing alerts into another. This function takes
       ``root`` explicitly and writes under it (falling back to
       :func:`dca_paths.cron_tracking_root` only when ``root`` is omitted),
       matching exactly what :func:`scripts.verify_schedule.verify_day` was
       just asked to check.
    2. That writer numbers a collision (``.2.txt``, ``.3.txt``, ...) because
       each EXECUTOR alert is a distinct event worth keeping. A recurring
       watchdog invocation reporting the SAME still-unresolved (day, slot)
       absence is not a new event each time -- it is the same one -- so this
       writer overwrites the one file for that key instead, keeping
       scripts/alert_digest.py's per-slot counts and streaks from being
       inflated by how often this script happens to run.
    """
    from near_close_dca import _redacted_diagnostic_text

    base = Path(root) if root is not None else Path(cron_tracking_root())
    day = date.fromisoformat(report["date"])
    written: List[Path] = []
    for row in failing_rows(report):
        path = _watchdog_alert_path(base, day, row["slot_id"])
        body = _redacted_diagnostic_text(alert_body(report, row)).rstrip("\n") + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, body)
        except OSError as exc:
            print(f"warning: alert file for slot {row['slot_id']} could not be written "
                  f"({path}): {_sanitize_exception(exc)}", file=sys.stderr)
            continue
        written.append(path)
    return written


def _resolve_slack_webhook(explicit: Optional[str], env_var: str) -> Optional[str]:
    if explicit:
        return explicit.strip() or None
    candidates = [env_var, *FALLBACK_SLACK_WEBHOOK_ENVS] if env_var == PRIMARY_SLACK_WEBHOOK_ENV else [env_var]
    for candidate_env in candidates:
        value = os.environ.get(candidate_env)
        if value and value.strip():
            return value.strip()
    return None


def send_slack_alert(
    report: Mapping[str, Any],
    *,
    webhook: Optional[str] = None,
    env_var: str = PRIMARY_SLACK_WEBHOOK_ENV,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """POST a summary of every failing slot to a Slack incoming webhook.

    Returns False when no webhook is configured or there is nothing to
    report. Raises on HTTP/network failure so the caller can decide whether a
    failed alert should escalate -- mirrors
    scripts.drift_detection.send_slack_alert exactly, since this is the same
    class of opt-in, env-only, no-secrets-in-git webhook path.
    """
    rows = failing_rows(report)
    if not rows:
        return False
    url = _resolve_slack_webhook(webhook, env_var)
    if not url:
        return False

    try:
        _assert_safe_destination(url)
    except _UnsafeDestinationError as exc:
        raise urllib.error.URLError(f"Invalid or unsafe webhook destination: {exc}") from exc

    slots = ", ".join(f"{row['time_et']} ET ({row['stream']}: {row['status']})" for row in rows)
    text = (
        f":rotating_light: DCA schedule watchdog: {len(rows)} slot(s) left no "
        f"valid evidence for {report['date']}\nSlots: {slots}\n"
        f"Detected independently of the executor sessions -- see the repo "
        f"alert file(s) or run `python -m scripts.schedule_watchdog --date "
        f"{report['date']} --json` for full detail."
    )
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(
        _NoRedirectHandler, PinnedHTTPHandler, PinnedHTTPSHandler, BlockedFileHandler, BlockedFTPHandler
    )
    with opener.open(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied webhook
        return 200 <= response.status < 300


def notify_macos(title: str, message: str) -> None:
    """Raise a macOS notification. Text travels as argv, never inside the AppleScript.

    The message can derive from run-directory contents, so the script reads it as data
    (``item N of argv``) rather than as interpolated code, which closes AppleScript
    injection. Bounded by a timeout so a hung notifier cannot hang the watchdog.
    """
    subprocess.run(
        ["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
         "-e", "end run", title, message],
        check=True, capture_output=True, timeout=_DEFAULT_TIMEOUT_SECONDS,
    )


def _render(report: Mapping[str, Any]) -> str:
    lines = [f"Schedule watchdog for {report['date']} (checked {report['checked_at_et']})"]
    rows = failing_rows(report)
    if not rows:
        lines.append("OK -- every due slot verified or was not due")
        return "\n".join(lines)
    for row in rows:
        lines.append(f"  FAILING  {row['time_et']}  {row['stream']:<12} {row['status']:<20} "
                     f"{'; '.join(row['reasons'])}")
    lines.append(f"{len(rows)} slot(s) failing: {', '.join(r['slot_id'] for r in rows)}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--date", help="ET trading day YYYY-MM-DD (default: today ET)")
    parser.add_argument("--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--no-alert-file", action="store_true",
                         help="skip writing the durable repo alert file for each failing slot")
    parser.add_argument("--notify", action="store_true",
                        help="raise a macOS notification when any slot is failing (for the launchd agent)")
    parser.add_argument("--slack", action="store_true", help="send a Slack alert when any slot is failing")
    parser.add_argument("--slack-webhook", help="Slack webhook URL (prefer the env var; no secrets in configs)")
    parser.add_argument(
        "--slack-webhook-env",
        default=PRIMARY_SLACK_WEBHOOK_ENV,
        help=f"Env var holding the Slack webhook (default {PRIMARY_SLACK_WEBHOOK_ENV}, "
             f"falling back to {' then '.join(FALLBACK_SLACK_WEBHOOK_ENVS)})",
    )
    args = parser.parse_args(argv)

    now = datetime.now(ET)
    day = date.fromisoformat(args.date) if args.date else now.date()
    root = Path(args.root) if args.root else None
    report = verify_day(day, now, root=root)
    rows = failing_rows(report)

    if rows and not args.no_alert_file:
        written = write_watchdog_alert_files(report, root=root)
        if len(written) < len(rows):
            print(f"warning: only {len(written)}/{len(rows)} durable alert files were written "
                  f"(see stderr log)", file=sys.stderr)

    if args.notify and rows:
        # Slot ids and the date only: the alert files quote run_state values, a desktop banner must not.
        message = f"{report['date']}: no verified evidence for slots {', '.join(r['slot_id'] for r in rows)}"
        try:
            notify_macos("Schedule watchdog FAILED", message)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"warning: macOS notification failed: {_sanitize_exception(exc)}", file=sys.stderr)

    if args.slack and rows:
        try:
            sent = send_slack_alert(report, webhook=args.slack_webhook, env_var=args.slack_webhook_env)
            if not sent:
                print(
                    f"warning: --slack set but no webhook configured "
                    f"(pass --slack-webhook or set ${args.slack_webhook_env}, "
                    f"{', '.join(FALLBACK_SLACK_WEBHOOK_ENVS)})",
                    file=sys.stderr,
                )
        except (urllib.error.URLError, OSError) as exc:
            print(f"warning: Slack alert failed: {_sanitize_exception(exc)}", file=sys.stderr)

    print(json.dumps(report, indent=2) if args.json else _render(report))
    return 1 if rows else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
