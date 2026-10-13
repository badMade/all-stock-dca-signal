"""Read-only post-slot audit of refusal text and the reported Gate 9 counts.

This is a submission-completeness check, not a settlement check:
``counts.submitted`` must equal ``eligible_count`` minus the three allowed drops.
``counts.submitted`` counts every order row the producer recorded, including
``pending``, ``unconfirmed`` and ``potentially_submitted`` ones, so a pass proves
every eligible buy was sent or accounted for, not that each one filled.
It never contacts a broker or changes run artifacts.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

from dca_console_reader import BoundedReader
from dca_paths import cron_tracking_root
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative

DROP_REASONS = ("price_recovered", "spread_over_1pct", "daily_cap")
REPORT_SUFFIXES = frozenset({".json", ".txt", ".md", ".log", ".csv"})


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def evaluate(state: object, report_text: str) -> list[str]:
    """Return deterministic failures for a reported run, refusing incomplete counts."""
    failures = []
    if "PLATFORM REFUSAL" in report_text.upper():
        failures.append("PLATFORM REFUSAL found in run reports")
    if not isinstance(state, dict):
        return sorted([*failures, "run_state.json must be an object"])
    counts = state.get("counts")
    report = state.get("gate9_report")
    if not isinstance(counts, dict) or not isinstance(report, dict):
        return sorted([*failures, "run_state.json requires counts and gate9_report objects"])
    filled = counts.get("submitted")
    eligible = report.get("eligible_count")
    dropped = report.get("dropped")
    if not _count(filled):
        failures.append("counts.submitted must be a nonnegative integer")
    if not _count(eligible):
        failures.append("gate9_report.eligible_count must be a nonnegative integer")
    if (
        not isinstance(dropped, dict)
        or set(dropped) != set(DROP_REASONS)
        or not all(_count(value) for value in dropped.values())
    ):
        failures.append("gate9_report.dropped requires all three allowed nonnegative integer counts")
    elif isinstance(filled, int) and isinstance(eligible, int) and _count(filled) and _count(eligible):
        expected = eligible - sum(dropped.values())
        if expected < 0 or filled != expected:
            failures.append(f"submitted {filled} != eligible {eligible} minus dropped {sum(dropped.values())}")
    return sorted(failures)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def main(argv: Sequence[str] | None = None) -> int:
    """Audit one dated slot directory; return 0 on success, 1 on unavailable/failed evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=sorted(SLOT_EVIDENCE_LAYOUT), required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    failures: list[str] = []
    root = (args.root if args.root is not None else Path(cron_tracking_root())).resolve()
    reader = BoundedReader(root, failures)
    relative = slot_run_dir_relative(args.slot, args.date)
    text = reader.text(relative / "run_state.json")
    state = None
    if text is None:
        failures.append("run_state.json is missing or unreadable")
    else:
        try:
            state = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            failures.append("run_state.json is malformed")
    if isinstance(state, dict) and state.get("trading_day") != args.date.isoformat():
        failures.append("run_state.json trading_day does not match --date")
    reports = []
    for entry in sorted(reader.entries(relative)):
        if entry.suffix.lower() not in REPORT_SUFFIXES:
            continue
        content = reader.text(entry)
        if content is None:
            failures.append("A run report is unreadable or incomplete")
        else:
            reports.append(content)
    failures.extend(evaluate(state, "\n".join(reports)))
    failures = sorted(set(failures))
    if args.json:
        print(json.dumps({"ok": not failures, "failures": failures}, sort_keys=True))
    elif failures:
        for failure in failures:
            print(f"FAIL: {failure}")
    else:
        print("PASS: reported slot counts balance; no PLATFORM REFUSAL found")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
