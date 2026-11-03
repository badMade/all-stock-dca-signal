#!/usr/bin/env python3
"""Redacted per-slot run summary for the daily DCA triage routine's STEP 1 (log reading).

Why this exists (2026-10-31): the triage routine ran as a cloud routine in a fresh
container, where ``$DCA_CRON_TRACKING_ROOT`` is unset and no ``cron_tracking/``
tree exists. It opened the 2026-10-30 and 2026-10-31 digests with "LOGS UNAVAILABLE
in this container" and so missed Friday 2026-10-30's 9:35 late-fire abort
(ABORT_GATE7_WINDOW_INSUFFICIENT, fired 36 min late) and the 3:30
ABORT_GATE5_ALL_PROVIDER_ERRORS abort. Worse, ``verify_schedule --json`` alone
would not have caught them either: it reported ``ok: true`` that day, because both
slots fired and finalized ("verified" proves evidence exists, not that the slot
succeeded). The outcome lives in each run's ``run_state.json`` ``status`` and
``gate9_report.notes``, which nothing in STEP 1 read.

This module is the one command STEP 1 runs wherever the tree IS readable (a local
Mac task with the checkout, or the ``remote-devices`` bridge at
``$HOME/mnt/all-stock-dca``). It joins ``verify_schedule.verify_day`` with each
slot's own run_state fields and the run directory's first/last file times, and
flags the slots that need attention.

``first_file_et`` is the oldest file mtime in the run directory, a lower bound on
when the run started writing, not its fire time: on 2026-10-30 the 9:35 directory's
oldest file is 09:50 while the run's own note says it fired at 10:11. Quote
``gate9_notes`` for the fire time when it gives one.

Redaction: the output carries only the fields listed in :data:`SUMMARY_FIELDS`,
run directories relative to the root (never an absolute path), and every string is
passed through :func:`redact`, which removes the live account number (when
``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` is set) and any other account-shaped digit run.
``broker``/``budget``/``buy_candidates`` blocks are never copied.

Read-only: nothing here writes, locks, or constructs an executor or broker.

CLI (exit 0 when no slot needs attention, 1 otherwise, 2 when the root is unreadable)::

    python -m scripts.triage_log_summary                    # latest trading day on/before today ET
    python -m scripts.triage_log_summary --date 2026-10-30 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from dca_paths import cron_tracking_root
from scripts.verify_schedule import ET, OK_STATUSES, verify_day

# order_submit_status values that are not failures (config/run_classification.json's
# non-failing set, the same three the end-of-day digest's CLEAN rule accepts).
CLEAN_SUBMIT_STATUSES = frozenset({"submitted", "not_submitted_no_candidates", "not_submitted_budget_exhausted"})

# The only per-slot fields the summary emits. Anything else in run_state.json
# (broker snapshot, budget detail, candidate lists) stays on the Mac.
SUMMARY_FIELDS = (
    "slot_id", "time_et", "stream", "run_dir", "evidence_status", "outcome", "order_submit_status",
    "orders_submitted_count", "error_class", "error_detail", "gate9_notes", "first_file_et",
    "first_file_delay_min", "last_file_et", "finalized_at_et", "attention", "attention_reasons",
)

# Same shape test_config_invariants.py treats as account-shaped: a bare 6-12 digit run.
_ACCOUNT_SHAPED = re.compile(r"(?<![\w.])\d{6,12}(?![\w.])")
# ...except YYYYMMDD / YYYYMM dates, which run directories and notes cite constantly.
_DATE_LIKE = re.compile(r"(19|20)\d{4}(\d{2})?")
REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Strip the live account number and any other account-shaped digit run from ``text``."""
    account = os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "").strip()
    if account:
        text = text.replace(account, REDACTED)
    return _ACCOUNT_SHAPED.sub(lambda m: m.group(0) if _DATE_LIKE.fullmatch(m.group(0)) else REDACTED, text)


def latest_trading_day(today: date) -> date:
    """The newest open trading day on or before ``today``.

    The routine runs at 18:15 ET and also on weekends; on 2026-10-31 (a Saturday)
    "today" has no slots, and the day worth reading is Friday. A calendar gap
    falls back to "any weekday", which can only read a day that turns out closed
    (every slot then reports market_closed), never skip a day that traded.
    """
    from dca_runner import market_day_status

    day = today
    for _ in range(14):
        try:
            closed = market_day_status(day).is_closed
        except Exception:  # noqa: BLE001 - calendar coverage gaps degrade to the weekday rule
            closed = day.weekday() >= 5
        if not closed:
            return day
        day -= timedelta(days=1)
    return today


def _file_times(run_dir: Path) -> tuple[Optional[datetime], Optional[datetime]]:
    try:
        stamps = [entry.stat().st_mtime for entry in run_dir.iterdir() if entry.is_file()]
    except OSError:
        return None, None
    if not stamps:
        return None, None
    return (datetime.fromtimestamp(min(stamps), ET), datetime.fromtimestamp(max(stamps), ET))


def _read_state(path: Path) -> Optional[Dict[str, Any]]:
    try:
        loaded = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _et_clock(value: Any) -> Optional[str]:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(ET).strftime("%H:%M")


def _clean(value: Any) -> Any:
    # Recursive: gate9_report.notes is a string on some streams and a list of
    # strings on others (2026-10-30 midday/intraday), and both must be redacted.
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def summarize_slot(row: Mapping[str, Any], base: Path, day: date) -> Dict[str, Any]:
    """One slot's redacted summary from its verify_schedule row and its own run directory."""
    run_dir = base / row["run_dir"]
    state = _read_state(run_dir / "run_state.json") or {}
    gate9 = state.get("gate9_report") if isinstance(state.get("gate9_report"), dict) else {}
    first, last = _file_times(run_dir) if run_dir.is_dir() else (None, None)
    hh, mm = int(row["slot_id"][:2]), int(row["slot_id"][2:])
    nominal = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)

    outcome = state.get("status")
    submit = state.get("order_submit_status", row.get("order_submit_status"))
    reasons: List[str] = []
    if row["status"] not in OK_STATUSES:
        reasons.append(f"evidence {row['status']}")
    if row["status"] == "verified" and submit not in CLEAN_SUBMIT_STATUSES:
        reasons.append(f"order_submit_status {submit}")
    # A run can submit orders and still abort (2026-10-30 9:35: a backup placed 32,
    # the slot itself fired late and placed nothing). A cap-exhausted abort is the
    # cap doing its job, not a fault.
    if isinstance(outcome, str) and outcome.startswith("ABORT") and submit != "not_submitted_budget_exhausted":
        reasons.append(f"outcome {outcome}")
    if row.get("failure_key"):
        reasons.append(f"failure_key {row['failure_key']}")

    summary = {
        "slot_id": row["slot_id"],
        "time_et": row["time_et"],
        "stream": row["stream"],
        "run_dir": str(row["run_dir"]),
        "evidence_status": row["status"],
        "outcome": outcome,
        "order_submit_status": submit,
        "orders_submitted_count": state.get("orders_submitted_count", row.get("orders_submitted_count")),
        "error_class": state.get("error_class"),
        "error_detail": state.get("error_detail"),
        "gate9_notes": gate9.get("notes"),
        "first_file_et": first.strftime("%H:%M") if first else None,
        "first_file_delay_min": round((first - nominal).total_seconds() / 60) if first else None,
        "last_file_et": last.strftime("%H:%M") if last else None,
        "finalized_at_et": _et_clock(state.get("finalized_at")) if state.get("finalized_at") else None,
        "attention": bool(reasons),
        "attention_reasons": reasons,
    }
    return {key: _clean(summary[key]) for key in SUMMARY_FIELDS}


def summarize_day(day: date, now: datetime, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """The redacted per-slot summary for ``day``. Raises ``FileNotFoundError`` for an unreadable root."""
    base = Path(root if root is not None else cron_tracking_root())
    if not base.is_dir() or not os.access(base, os.R_OK | os.X_OK):
        raise FileNotFoundError("cron-tracking root is not a readable directory")
    report = verify_day(day, now, root=base)
    slots = [summarize_slot(row, base, day) for row in report["slots"]]
    attention = [slot["slot_id"] for slot in slots if slot["attention"]]
    return {
        "date": report["date"],
        "checked_at_et": report["checked_at_et"],
        "verify_schedule_ok": report["ok"],
        "attention_slots": attention,
        "ok": not attention,
        "slots": slots,
    }


def _render(summary: Mapping[str, Any]) -> str:
    lines = [f"DCA run summary for {summary['date']} (checked {summary['checked_at_et']})"]
    for slot in summary["slots"]:
        late = f" first file {slot['first_file_et']} (+{slot['first_file_delay_min']}m)" if slot["first_file_et"] else ""
        flag = "  <-- " + "; ".join(slot["attention_reasons"]) if slot["attention"] else ""
        lines.append(f"  {slot['time_et']}  {slot['stream']:<12} {slot['outcome'] or slot['evidence_status']:<36} "
                     f"{slot['order_submit_status']} ({slot['orders_submitted_count']}){late}{flag}")
        for label in ("error_detail", "gate9_notes"):
            if slot[label]:
                lines.append(f"         {label}: {slot[label]}")
    lines.append("OK" if summary["ok"] else f"ATTENTION: {', '.join(summary['attention_slots'])}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--date", help="ET trading day YYYY-MM-DD (default: latest trading day on/before today ET)")
    parser.add_argument("--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)")
    parser.add_argument("--json", action="store_true", help="emit the summary as JSON")
    args = parser.parse_args(argv)
    now = datetime.now(ET)
    day = date.fromisoformat(args.date) if args.date else latest_trading_day(now.date())
    try:
        summary = summarize_day(day, now, root=Path(args.root) if args.root else None)
    except FileNotFoundError as exc:
        # The exact phrase STEP 1 of the routine prompt keys on; no path is printed.
        print(f"LOGS UNAVAILABLE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2) if args.json else _render(summary))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
