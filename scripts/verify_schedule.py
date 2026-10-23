#!/usr/bin/env python3
"""Daily per-slot audit: did each live roster slot leave same-day scheduled evidence?

Why this exists (2026-09-22): the console's Schedule page showed every elapsed
live slot as ``elapsed_unverified`` forever, and nothing anywhere turned that
into "verified" or "missing". Checking the artifact tree by hand for that day
found the live agents firing -- each slot's dated run directory existed -- but
stopping before Gate 9 wrote ``run_state.json``, and hand-writing
``latest_<stream>_run.json`` pointers in a private shape (``slot``/``run_dir``/
``outcome``) that ``same_day_pointer.validate_pointer`` rejects as malformed.
So there was no evidence to verify, and no check that would have said so.

This module answers one question per ``dca_runner.SLOTS`` entry for one ET
trading day, from the slot's OWN dated run directory rather than the stream's
latest pointer. The directory is slot-specific (``intraday_dca/<day>_1330ET``),
whereas intraday's single ``latest_intraday_run.json`` is overwritten by each of
its three slots and cannot say which one wrote it.

Statuses, most to least settled:

  * ``verified``           -- ``run_state.json`` exists, names this trading day
    and this slot's ``scheduled_for_et``/stream, and passes
    ``same_day_pointer.derive_valid_scheduled_evidence`` (scheduled, non-manual,
    no contract-failure or evidence-denied marker). Proof the slot FIRED and
    finalized -- not proof any order filled.
  * ``evidence_incomplete`` -- the run directory exists (the slot started) but
    its ``run_state.json`` is missing, unreadable, or not scheduled evidence
    for this slot. This is the 2026-09-22 failure.
  * ``no_evidence``        -- past the grace deadline with no run directory.
  * ``awaiting_evidence``  -- the slot's time has passed but its grace deadline
    (acceptance-window end + ``GRACE_MINUTES``) has not; a run may still be
    finalizing.
  * ``not_due``            -- before the slot's nominal time.
  * ``market_closed``      -- Gate 0 says the slot ends silently today (closed
    exchange, or a >= 13:00 slot on an early close) and nothing was written.

Read-only: nothing here writes, locks, or constructs an executor or broker.
:func:`classify_slot_evidence` is pure so the local console can feed it state
read through its own ``BoundedReader`` instead of this module's plain reads.

CLI (exit 0 when every due slot is verified or market_closed, 1 otherwise)::

    python -m scripts.verify_schedule                       # today, ET
    python -m scripts.verify_schedule --date 2026-09-22 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from dca_paths import cron_tracking_root
from scripts.same_day_pointer import derive_valid_scheduled_evidence

ET = ZoneInfo("America/New_York")

# Minutes after a slot's acceptance-window END before a missing artifact is
# reported as a failure instead of "awaiting". The window already absorbs the
# scheduler's jitter (dca_runner.SlotSpec); a full live run was observed taking
# 26 minutes end to end (10:30 -> 10:56 on 2026-09-22), so a slot that fired
# at the very end of its window still gets half an hour to write Gate 9.
GRACE_MINUTES = 30

STATUS_VERIFIED = "verified"
STATUS_EVIDENCE_INCOMPLETE = "evidence_incomplete"
STATUS_NO_EVIDENCE = "no_evidence"
STATUS_AWAITING = "awaiting_evidence"
STATUS_NOT_DUE = "not_due"
STATUS_MARKET_CLOSED = "market_closed"
# Triage Key (Notion "DCA Issues") for a slot that never started: past its grace
# deadline with no run directory at all and no failed-start marker in its window. Deliberately NOT applied to
# evidence_incomplete (the slot fired and stopped before Gate 9) or awaiting_evidence
# (still inside grace) -- loosening that boundary would hide real misses.
FAILURE_KEY_SLOT_DID_NOT_FIRE = "slot_did_not_fire"
# Statuses that do not fail the daily audit.
OK_STATUSES = frozenset({STATUS_VERIFIED, STATUS_NOT_DUE, STATUS_AWAITING, STATUS_MARKET_CLOSED})

# slot id -> (run_classification stream, run-directory parent relative to the
# cron-tracking root). The dated folder is always "<YYYYMMDD>_<slot>ET". These
# mirror each executor profile's state_root_stream/folder_suffix and
# run_classification.scheduled_run_dir(); tests/test_verify_schedule.py pins
# both so this table cannot drift from the writers. Kept as data (not an
# executor import) so the read-only console can use it without loading the
# executor modules' import-time state.
SLOT_EVIDENCE_LAYOUT: Dict[str, tuple[str, str]] = {
    "0935": ("market_open", "market_open_dca"),
    "1030": ("morning", "morning_dca"),
    "1130": ("intraday", "intraday_dca"),
    "1230": ("midday", "midday_dca"),
    "1330": ("intraday", "intraday_dca"),
    "1430": ("intraday", "intraday_dca"),
    "1530": ("afternoon", "ce16ba6b/scheduled_runs"),
}


def slot_run_dir_relative(slot_id: str, day: date) -> Path:
    """The slot's dated run directory, relative to the cron-tracking root."""
    _, parent = SLOT_EVIDENCE_LAYOUT[slot_id]
    return Path(parent) / f"{day.strftime('%Y%m%d')}_{slot_id}ET"


def _attempt_times(base: Path, stream: str, day: date) -> List[datetime]:
    """ET times of ``stream``'s failed-start markers for ``day`` (``dca_alerts.write_failed_start_marker``).

    Only ``failure_phase == 'before_run_artifact'`` counts: that is the crash-before-any-artifact
    case the marker exists for, while ``after_run_artifact`` belongs to a run that did write one.
    Root resolution mirrors the writer: ``$DCA_ATTEMPT_LOG_ROOT``, else ``<root>/attempt_log``.
    Unreadable or malformed lines are skipped, so a broken log degrades toward the
    ``slot_did_not_fire`` tag (absence of evidence), never toward excusing the slot.
    """
    log_root = Path(os.environ.get("DCA_ATTEMPT_LOG_ROOT") or base / "attempt_log")
    path = log_root / day.strftime("%Y%m%d") / f"{stream}.attempts.jsonl"
    times: List[datetime] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return times
    for line in lines:
        try:
            marker = json.loads(line)
            if marker.get("failure_phase") != "before_run_artifact":
                continue
            stamp = datetime.fromisoformat(str(marker["triggered_at_utc"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, AttributeError, TypeError):
            continue
        if stamp.tzinfo is not None:
            times.append(stamp.astimezone(ET))
    return times


def _clock(hhmm: tuple[int, int]) -> str:
    return "%02d:%02d" % hhmm


def classify_slot_evidence(
    slot_id: str,
    day: date,
    now: datetime,
    *,
    run_dir_present: bool,
    run_state: Optional[Mapping[str, Any]],
    run_state_present: bool,
    market_closed: Optional[bool],
    artifact_errors: Sequence[str] = (),
    attempt_times: Sequence[datetime] = (),
) -> Dict[str, Any]:
    """Classify one slot's evidence from facts the caller already read.

    ``run_state`` is the parsed ``run_state.json`` (None when absent or
    unreadable); ``run_state_present`` distinguishes "absent" from "present but
    unreadable" for the reason text. ``market_closed`` is Gate 0's
    ``slot_should_end_silently`` verdict, or None when the calendar could not
    answer -- treated as open, so an uncovered year reports missing evidence
    rather than silently excusing it.

    ``artifact_errors`` are the run directory's artifact-contract failures
    (``scheduled_artifacts.validate_run_artifacts``, or the console's bounded
    presence check). A valid scheduled state beside a missing summary or CSV
    is an interrupted run, not a verified one (Copilot review, PR #1514).

    ``attempt_times`` are the stream's failed-start marker times (ET). One inside this slot's
    window (window start .. grace deadline) means the slot DID fire and crashed before writing
    anything, so a ``no_evidence`` row is not tagged ``slot_did_not_fire``; it still fails.
    """
    from dca_runner import SLOTS

    spec = SLOTS[slot_id]
    stream, _ = SLOT_EVIDENCE_LAYOUT[slot_id]
    now_et = now.astimezone(ET) if now.tzinfo else now.replace(tzinfo=ET)
    nominal = datetime.combine(day, time(*spec.nominal_et), tzinfo=ET)
    deadline = datetime.combine(day, time(*spec.window_end_et), tzinfo=ET) + timedelta(minutes=GRACE_MINUTES)
    reasons: List[str] = []
    verified = False
    attempted = False

    if run_state_present and not isinstance(run_state, Mapping):
        reasons.append("run_state.json is unreadable or not a JSON object")
    elif isinstance(run_state, Mapping):
        rc = run_state.get("run_classification")
        rc = rc if isinstance(rc, Mapping) else {}
        if str(run_state.get("trading_day", "")).strip() != day.isoformat():
            reasons.append(f"run_state trading_day {run_state.get('trading_day')!r} != {day.isoformat()!r}")
        # The 3:30 builder omits nothing here, but a legacy afternoon state may
        # carry stream=null, which run_classification treats as afternoon.
        state_stream = rc.get("stream") or ("afternoon" if stream == "afternoon" else None)
        if state_stream != stream:
            reasons.append(f"run_classification.stream {rc.get('stream')!r} != {stream!r}")
        if rc.get("scheduled_for_et") != _clock(spec.nominal_et):
            # Pins the SLOT, not just the stream: an 11:30 intraday state copied
            # into the 13:30 folder must not verify the 13:30 slot.
            reasons.append(f"run_classification.scheduled_for_et {rc.get('scheduled_for_et')!r} "
                           f"!= {_clock(spec.nominal_et)!r}")
        if not derive_valid_scheduled_evidence(run_state):
            reasons.append("run_state is not scheduled evidence (manual, unclassified, "
                           "artifact_contract_failure, or scheduled_evidence_denied)")
        reasons.extend(f"artifact contract: {error}" for error in artifact_errors)
        verified = not reasons
    elif run_dir_present:
        reasons.append("run directory exists but run_state.json was never written (Gate 9 did not finalize)")

    if verified:
        status = STATUS_VERIFIED
    elif now_et < nominal:
        status = STATUS_NOT_DUE
    elif market_closed and not run_dir_present:
        status = STATUS_MARKET_CLOSED
    elif now_et < deadline:
        status = STATUS_AWAITING
    elif run_dir_present:
        status = STATUS_EVIDENCE_INCOMPLETE
    else:
        status = STATUS_NO_EVIDENCE
        reasons.append("no run directory for this slot")
        window_start = datetime.combine(day, time(*spec.window_start_et), tzinfo=ET)
        if any(window_start <= stamp <= deadline for stamp in attempt_times):
            attempted = True
            reasons.append("a failed-start marker exists in this slot's window: it fired and crashed "
                           "before writing a run directory")

    state = run_state if isinstance(run_state, Mapping) else {}
    return {
        "slot_id": slot_id,
        "time_et": _clock(spec.nominal_et),
        "stream": stream,
        "status": status,
        "run_dir": slot_run_dir_relative(slot_id, day).as_posix(),
        "run_dir_present": run_dir_present,
        "run_state_present": run_state_present,
        "order_submit_status": state.get("order_submit_status"),
        "orders_submitted_count": state.get("orders_submitted_count"),
        "deadline_et": deadline.strftime("%H:%M"),
        "failure_key": FAILURE_KEY_SLOT_DID_NOT_FIRE if status == STATUS_NO_EVIDENCE and not attempted else None,
        "reasons": reasons,
    }


def market_closed_for_slot(day: date, slot_id: str) -> Optional[bool]:
    from dca_runner import SLOTS, market_day_status

    try:
        return market_day_status(day).slot_should_end_silently(SLOTS[slot_id])
    except Exception:  # noqa: BLE001 - calendar gaps degrade to "unknown", never to "closed"
        return None


def _read_state(path: Path) -> tuple[bool, Optional[Dict[str, Any]]]:
    if not path.is_file():
        return False, None
    try:
        loaded = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return True, None
    return True, loaded if isinstance(loaded, dict) else None


def verify_day(
    day: date, now: datetime, *, root: Optional[Path] = None, order_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Classify every roster slot, optionally reporting artifact claims for order IDs.

    Attribution is informational only and never changes the audit verdict.
    ``root`` defaults to the cron-tracking root.
    """
    base = Path(root) if root is not None else cron_tracking_root()
    slots = []
    for slot_id in SLOT_EVIDENCE_LAYOUT:
        run_dir = base / slot_run_dir_relative(slot_id, day)
        present, state = _read_state(run_dir / "run_state.json")
        artifact_errors: List[str] = []
        if state is not None:
            from scripts.scheduled_artifacts import validate_run_artifacts

            artifact_errors = validate_run_artifacts(run_dir, require_summary=True).errors
        slots.append(classify_slot_evidence(
            slot_id, day, now, run_dir_present=run_dir.is_dir(), run_state=state,
            run_state_present=present, market_closed=market_closed_for_slot(day, slot_id),
            artifact_errors=artifact_errors,
            attempt_times=_attempt_times(base, SLOT_EVIDENCE_LAYOUT[slot_id][0], day),
        ))
    failing = [row["slot_id"] for row in slots if row["status"] not in OK_STATUSES]
    report = {"date": day.isoformat(), "checked_at_et": now.astimezone(ET).isoformat(timespec="minutes"),
              "ok": not failing, "failing_slots": failing, "slots": slots}
    if order_ids is not None:
        from scripts.slot_attribution import attribute_orders

        report["order_attribution"] = attribute_orders(day, order_ids, root=base)
    return report


def _render(report: Mapping[str, Any]) -> str:
    lines = [f"Schedule evidence for {report['date']} (checked {report['checked_at_et']})"]
    for row in report["slots"]:
        detail = "; ".join(row["reasons"]) or (row["order_submit_status"] or "")
        lines.append(f"  {row['time_et']}  {row['stream']:<12} {row['status']:<20} {detail}")
    lines.append("OK" if report["ok"] else f"FAILING: {', '.join(report['failing_slots'])}")
    attribution = report.get("order_attribution")
    if attribution is not None:
        # Reporting only: these lines never feed the OK/FAILING verdict above.
        lines.extend(f"  orders  {oid} -> slot {slot}" for oid, slot in attribution["attributed"].items())
        if attribution["unattributed"]:
            lines.append(f"  orders  unattributed: {', '.join(attribution['unattributed'])}")
        lines.extend(f"  orders  ambiguous: {oid} -> slots {', '.join(slots)}"
                     for oid, slots in attribution["ambiguous"].items())
    return "\n".join(lines)


def _load_order_ids(spec: str) -> List[str]:
    """Read ``--orders-json``: a JSON array of non-empty strings from a file, or stdin for ``-``.

    Fail closed: anything else raises ``ValueError`` so the caller can refuse to
    report, rather than attributing a silently filtered subset of the IDs.
    """
    try:
        raw = sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
        loaded = json.loads(raw)
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError and bad UTF-8
        raise ValueError(f"cannot read order IDs from {spec!r}: {exc}") from exc
    if not isinstance(loaded, list) or not all(isinstance(oid, str) and oid for oid in loaded):
        raise ValueError(f"{spec!r} must be a JSON array of non-empty strings")
    return loaded


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--date", help="ET trading day YYYY-MM-DD (default: today ET)")
    parser.add_argument("--root", help="cron-tracking root (default: $DCA_CRON_TRACKING_ROOT or <checkout>/cron_tracking)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--orders-json", metavar="PATH",
                        help="JSON array of broker order IDs to attribute to slots from each slot's own "
                             "run_state.json ('-' reads stdin); reporting only, never changes the exit status")
    args = parser.parse_args(argv)
    try:
        order_ids = _load_order_ids(args.orders_json) if args.orders_json is not None else None
    except ValueError as exc:
        parser.error(f"--orders-json: {exc}")
    now = datetime.now(ET)
    day = date.fromisoformat(args.date) if args.date else now.date()
    report = verify_day(day, now, root=Path(args.root) if args.root else None, order_ids=order_ids)
    print(json.dumps(report, indent=2) if args.json else _render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
