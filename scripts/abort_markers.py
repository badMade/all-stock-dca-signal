"""Read the evidence-free abort markers ``scripts/record_evidence_free_abort.sh`` writes.

Why this exists (2026-10-29): a slot that fires and aborts before any run evidence can exist -- Gate 1
off-window, or Gate 2 with no Python 3.11 and no python3 to run the 3.10 evidence path -- leaves no run
directory, and ``scripts.verify_schedule`` correctly reports ``no_evidence``. The watchdog then tagged
it ``slot_did_not_fire``, telling the operator the scheduler never ran a slot that did run, and sending
them to the scheduler's run history instead of to the abort reason. The live prompts now write a small
marker FIRST on those paths, through a shell script that needs no Python 3.11, and this module turns
it into a distinct triage key.

A marker is never run evidence. It does not verify a slot or excuse a failing one -- the row's status
stays ``no_evidence`` and still alerts -- and nothing that sizes, caps or allocates reads it as a dollar
figure. The one reader outside triage (2026-11-10) is ``scripts.kill_switch_gap_proof``, the 12:30 Gate 5
kill-switch coverage check: there a clean Gate-1 marker is only a PRECONDITION for asking the broker,
and the $0 proof is a replayed, complete order history showing no order that afternoon -- see
``docs/kill_switch_evidence_gap_proof.md``. Here it only changes the triage key, and only for a marker whose fire time falls inside the slot's acceptance
window through its grace deadline (the same interval ``verify_schedule`` already credits a
failed-start marker with). An off-window marker -- a manual night-time "Run now", or a scheduler fire
hours late -- keeps ``slot_did_not_fire``: the slot's scheduled fire is still unaccounted for, and a
reason line records the off-window attempt so the two are not confused.

Applied by ``scripts.schedule_watchdog.audit_day``, not inside ``verify_schedule.classify_slot_evidence``,
for the reason ``audit_day``'s own docstring gives: ``scripts/verify_schedule.py`` ships inside the
Perplexity backup planner, so changing it forces a rebuild and a repaste of every backup prompt.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKER_DIRNAME = "abort_markers"
MARKER_KIND = "evidence_free_abort"
# Triage Key (Notion "DCA Issues") for a slot that fired inside its window and aborted before any
# evidence could be written. Distinct from verify_schedule.FAILURE_KEY_SLOT_DID_NOT_FIRE on purpose.
FAILURE_KEY_FIRED_ABORTED = "slot_fired_aborted_before_evidence"


def marker_path(base: Path, day: date, slot_id: str) -> Path:
    """``<cron root>/abort_markers/<YYYYMMDD>/<slot>.jsonl`` -- the writer's path."""
    return Path(base) / MARKER_DIRNAME / day.strftime("%Y%m%d") / f"{slot_id}.jsonl"


def read_abort_markers(base: Path, day: date, slot_id: str) -> List[Dict[str, Any]]:
    """Every well-formed marker for ``slot_id`` on ``day``, each with a parsed ``fired_at_et``.

    Malformed lines, markers for another slot or day, and unparseable timestamps are skipped, so a
    broken file degrades toward ``slot_did_not_fire`` (absence of evidence), never toward excusing it.
    """
    try:
        lines = marker_path(base, day, slot_id).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    markers: List[Dict[str, Any]] = []
    for line in lines:
        try:
            marker = json.loads(line)
            if not isinstance(marker, dict) or marker.get("kind") != MARKER_KIND:
                continue
            if marker.get("slot_id") != slot_id or marker.get("trading_day") != day.isoformat():
                continue
            stamp = datetime.fromisoformat(str(marker["fired_at_utc"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        if stamp.tzinfo is None:
            continue
        markers.append({**marker, "fired_at_et": stamp.astimezone(ET)})
    return markers


def _in_window(slot_id: str, day: date, deadline_hhmm: str, stamp: datetime) -> bool:
    # Imported here: finalize_run_evidence imports verify_schedule, which the watchdog already loads.
    from scripts.finalize_run_evidence import acceptance_window

    window_start, _ = acceptance_window(slot_id, day)
    deadline = datetime.combine(day, time.fromisoformat(deadline_hhmm), tzinfo=ET)
    # A deadline past midnight never happens on this roster, but keep the comparison honest if it did.
    if deadline < window_start:
        deadline += timedelta(days=1)
    return window_start <= stamp <= deadline


def apply_abort_markers(report: Mapping[str, Any], *, root: Path) -> None:
    """Retag ``no_evidence`` rows in a ``verify_day`` report that have an in-window abort marker.

    Mutates the rows in place: ``failure_key`` becomes :data:`FAILURE_KEY_FIRED_ABORTED`, a reason
    names the gate and reason code, and ``abort_markers`` lists what was read. Status is unchanged.
    """
    from scripts.verify_schedule import STATUS_NO_EVIDENCE

    day = date.fromisoformat(report["date"])
    for row in report["slots"]:
        if row["status"] != STATUS_NO_EVIDENCE:
            continue
        markers = read_abort_markers(root, day, row["slot_id"])
        if not markers:
            continue
        row_out: MutableMapping[str, Any] = row
        row_out["abort_markers"] = [
            {"gate": m.get("gate"), "reason_code": m.get("reason_code"),
             "fired_at_et": m["fired_at_et"].isoformat(timespec="minutes")}
            for m in markers
        ]
        in_window = [m for m in markers if _in_window(row["slot_id"], day, row["deadline_et"], m["fired_at_et"])]
        if in_window:
            first = in_window[0]
            row_out["failure_key"] = FAILURE_KEY_FIRED_ABORTED
            row_out["reasons"].append(
                f"an evidence-free abort marker shows it fired at {first['fired_at_et']:%H:%M} ET and aborted "
                f"at {first.get('gate')} ({first.get('reason_code')}) before any evidence could be written")
        else:
            stamps = ", ".join(f"{m['fired_at_et']:%H:%M}" for m in markers)
            row_out["reasons"].append(
                f"an evidence-free abort marker exists only outside this slot's window ({stamps} ET); "
                f"not counted as its scheduled fire")
