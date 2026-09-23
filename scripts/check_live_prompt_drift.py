#!/usr/bin/env python3
"""Compare the LIVE Cowork scheduled-task bodies against their repo mirrors.

Why this exists (2026-09-22): the 2:30 PM run found its live task text telling
it to save order history as a bare JSON array -- which
``scripts/same_day_top_up_gate.py`` rejects, needing ``{"account_number",
"trading_day", "pages"}`` -- and quoting a trading-window end date that no
longer matched ``config/trading_window.json``. The committed
``claude/dca-executor-prompts/live-mirror/`` copies were already right on both
counts. The drift was between the live bodies in
``~/Claude/Scheduled/<task>/SKILL.md`` and those mirrors, and nothing compared
them. The same day the 10:30 slot placed 11 orders repeating tickers 9:35 had
just bought, which is what a live body without the same-day top-up guard
produces.

``claude/dca-executor-prompts/README.md`` names the live SKILL.md as the source
of truth and the mirror as a diffable copy whose only permitted divergence is
the account literal (placeholder in the mirror). This script does that diff
for all seven slots and names the specific failures that have cost real runs:

* ``missing_top_up_gate`` -- the live body never runs
  ``scripts.same_day_top_up_gate``;
* ``bare_array_order_pages`` -- it runs the gate but never wraps the saved
  pages as ``{"account_number", "trading_day", "pages"}``;
* ``stale_window_end_date`` -- it hardcodes a window end date that disagrees
  with ``config/trading_window.json``;
* ``missing_finalize_evidence`` -- Gate 9 never runs
  ``scripts.finalize_run_evidence``, so the slot leaves no verifiable
  run_state or pointer (2026-09-22: all seven slots fired, none verified).

Read-only. It never edits a live task (that stays a human's
``update_scheduled_task`` full-body replacement) and never prints an
account-shaped number: the configured account value and any other run of 6+
digits are masked in every line it prints.

Usage (on the machine that runs the Cowork tasks)::

    python3 -m scripts.check_live_prompt_drift            # summary
    python3 -m scripts.check_live_prompt_drift --diff     # plus masked diffs
    python3 -m scripts.check_live_prompt_drift --json

Exit status: 0 when every slot matches with no findings, 1 when any slot
drifted or is missing, 2 when the check itself cannot run: the scheduled-task
directory is missing, the mirror directory does not hold exactly one mirror
per roster slot, or no window end date can be resolved. Each of those fails
closed rather than reporting a clean result it never checked (Copilot review).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIRROR_DIR = REPO_ROOT / "claude" / "dca-executor-prompts" / "live-mirror"
ACCOUNT_PLACEHOLDER = "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"
TOP_UP_GATE_MODULE = "scripts.same_day_top_up_gate"
# The one Gate 9 command that writes verifiable evidence (2026-09-22 schedule
# audit: every slot fired, none left a run_state.json or a valid pointer).
FINALIZE_EVIDENCE_MODULE = "scripts.finalize_run_evidence"
# The seven roster slots, as each mirror's filename prefix (<HHMM>ET-<task>.txt).
EXPECTED_SLOTS = ("0935ET", "1030ET", "1130ET", "1230ET", "1330ET", "1430ET", "1530ET")
# Every key same_day_top_up_gate requires on its --orders-pages envelope.
ORDER_PAGES_ENVELOPE_KEYS = ('"account_number"', '"trading_day"', '"pages"')

_HEADER_TASK_RE = re.compile(r"MIRROR of the LIVE scheduled task `([^`]+)`")
_FILENAME_TASK_RE = re.compile(r"^\d{3,4}ET-(.+)\.txt$")
_DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
# A date within this many characters after an end-date phrase is read as a
# hardcoded window end. Kept short so a date elsewhere in the same paragraph
# (an incident citation, say) is not mistaken for one.
_END_DATE_CONTEXT_CHARS = 80
_END_DATE_PHRASE_RE = re.compile(r"end[_ ]date|window end|trading window ends?", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\d{6,}")


def default_scheduled_dir() -> Path:
    """``$CLAUDE_SCHEDULED_DIR``, else ``~/Claude/Scheduled`` (the README's location)."""
    override = os.environ.get("CLAUDE_SCHEDULED_DIR")
    return Path(override).expanduser() if override else Path.home() / "Claude" / "Scheduled"


def task_id_for(mirror: Path, text: Optional[str] = None) -> Optional[str]:
    """The live task id a mirror file copies: its header, else its filename.

    Six mirrors open with ``# MIRROR of the LIVE scheduled task `<id>```; the
    1230 one lost that header in a rewrite, but every mirror is still named
    ``<HHMM>ET-<task id>.txt``.
    """
    text = mirror.read_text(encoding="utf-8") if text is None else text
    header = _HEADER_TASK_RE.search(text[:2000])
    if header:
        return header.group(1)
    by_name = _FILENAME_TASK_RE.match(mirror.name)
    return by_name.group(1) if by_name else None


def _mirror_body(text: str) -> List[str]:
    """Mirror lines minus its leading ``#`` comment preamble, which the live body never had.

    Only the contiguous block of ``#``/blank lines at the TOP is mirror
    metadata. A ``#`` line after the first instruction is prompt content --
    the 1230 mirror carries Python comments inside its code snippets -- and
    must still be compared, or a live body missing it would read as a match
    (Copilot review: "Strip only explicitly delimited mirror metadata").
    """
    lines = text.splitlines()
    start = 0
    while start < len(lines) and (lines[start].startswith("#") or not lines[start].strip()):
        start += 1
    return lines[start:]


def _normalize_live(text: str, account_number: Optional[str]) -> List[str]:
    if account_number:
        text = text.replace(account_number, ACCOUNT_PLACEHOLDER)
    return text.splitlines()


def mask(line: str, account_number: Optional[str] = None) -> str:
    """Mask the account value and any other account-shaped digit run."""
    if account_number:
        line = line.replace(account_number, "[ACCOUNT]")
    return _LONG_DIGITS_RE.sub("[REDACTED]", line)


def _stale_window_dates(text: str, window_end_date: str) -> List[str]:
    found: List[str] = []
    for phrase in _END_DATE_PHRASE_RE.finditer(text):
        context = text[phrase.end(): phrase.end() + _END_DATE_CONTEXT_CHARS]
        for date in _DATE_RE.findall(context):
            if date != window_end_date and date not in found:
                found.append(date)
    return found


def findings_for(live_text: str, window_end_date: Optional[str]) -> List[Dict[str, str]]:
    """The named failure modes present in one live body."""
    findings: List[Dict[str, str]] = []
    if TOP_UP_GATE_MODULE not in live_text:
        findings.append({
            "code": "missing_top_up_gate",
            "detail": f"never runs {TOP_UP_GATE_MODULE}: a repeat buy would be sized as a first allocation",
        })
    elif any(key not in live_text for key in ORDER_PAGES_ENVELOPE_KEYS):
        findings.append({
            "code": "bare_array_order_pages",
            "detail": 'runs the gate but never wraps saved pages as {"account_number", "trading_day", "pages"}; '
            "the gate rejects a bare array",
        })
    if FINALIZE_EVIDENCE_MODULE not in live_text:
        findings.append({
            "code": "missing_finalize_evidence",
            "detail": f"never runs {FINALIZE_EVIDENCE_MODULE}: the slot fires but leaves no verifiable run_state "
            "or pointer, so scripts.verify_schedule reports it evidence_incomplete",
        })
    if window_end_date:
        stale = _stale_window_dates(live_text, window_end_date)
        if stale:
            findings.append({
                "code": "stale_window_end_date",
                "detail": f"hardcodes {', '.join(stale)} as the window end; config/trading_window.json says "
                f"{window_end_date}",
            })
    return findings


def check(
    *,
    mirror_dir: Path,
    scheduled_dir: Path,
    window_end_date: Optional[str],
    account_number: Optional[str],
    only: Optional[Iterable[str]] = None,
    with_diff: bool = False,
) -> Dict[str, Any]:
    """Compare every mirror in ``mirror_dir`` with its live SKILL.md."""
    wanted = set(only) if only is not None else None
    slots: List[Dict[str, Any]] = []
    for mirror in sorted(Path(mirror_dir).glob("*.txt")):
        # Dotfiles (.template.txt) are authoring scaffolds, not live slots --
        # the same exclusion tests/test_config_invariants.py applies.
        if mirror.name.startswith("."):
            continue
        if wanted is not None and mirror.name not in wanted:
            continue
        text = mirror.read_text(encoding="utf-8")
        task = task_id_for(mirror, text)
        slot: Dict[str, Any] = {"mirror": mirror.name, "task": task, "findings": []}
        live_path = Path(scheduled_dir) / str(task) / "SKILL.md"
        if not task or not live_path.is_file():
            slot["status"] = "live_missing"
            slots.append(slot)
            continue
        live_text = live_path.read_text(encoding="utf-8")
        mirror_lines = _mirror_body(text)
        live_lines = _normalize_live(live_text, account_number)
        # Compare with blank lines dropped: a repaste routinely adds or trims
        # them, and they carry no instruction.
        same = [line for line in mirror_lines if line.strip()] == [line for line in live_lines if line.strip()]
        slot["status"] = "match" if same else "differs"
        slot["findings"] = findings_for(live_text, window_end_date)
        if not same:
            diff = list(difflib.unified_diff(mirror_lines, live_lines, "mirror", "live", lineterm="", n=0))
            slot["changed_lines"] = sum(
                1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))
            )
            if with_diff:
                slot["diff"] = [mask(line, account_number) for line in diff]
        slots.append(slot)
    drifted = sum(1 for s in slots if s["status"] != "match" or s["findings"])
    return {"scheduled_dir": str(scheduled_dir), "window_end_date": window_end_date,
            "drifted_slots": drifted, "slots": slots}


def mirror_set_problem(mirror_dir: Path) -> Optional[str]:
    """Why ``mirror_dir`` cannot anchor a check, or None when it holds one mirror per slot.

    A missing or empty directory would otherwise loop zero times and report
    ``drifted_slots == 0`` -- a clean result from comparing nothing (Copilot
    review: "Fail when mirror directory or slot mirrors are missing").
    """
    mirror_dir = Path(mirror_dir)
    if not mirror_dir.is_dir():
        return f"mirror directory not found: {mirror_dir}"
    names = [p.name for p in mirror_dir.glob("*.txt") if not p.name.startswith(".")]
    problems = []
    for slot in EXPECTED_SLOTS:
        count = sum(1 for name in names if name.startswith(f"{slot}-"))
        if count != 1:
            problems.append(f"{slot} has {count} mirrors")
    return f"mirror directory {mirror_dir}: " + ", ".join(problems) if problems else None


def _window_end_date_from_config() -> Optional[str]:
    try:
        import dca_config
    except ImportError:
        return None
    # get_config_dir() honours $DCA_CONFIG_DIR, the same source every
    # executor's Gate 3 reads the window from.
    path = Path(dca_config.get_config_dir()) / "trading_window.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["end_date"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _print_text(report: Dict[str, Any]) -> None:
    print(f"live tasks: {report['scheduled_dir']}  window end_date: {report['window_end_date']}")
    for slot in report["slots"]:
        extra = f" ({slot['changed_lines']} changed lines)" if "changed_lines" in slot else ""
        print(f"- {slot['mirror']} -> {slot['task']}: {slot['status']}{extra}")
        for finding in slot["findings"]:
            print(f"    ! {finding['code']}: {finding['detail']}")
        for line in slot.get("diff", []):
            print(f"      {line}")
    print(f"{report['drifted_slots']} of {len(report['slots'])} slots need a repaste from live-mirror/")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mirror-dir", type=Path, default=DEFAULT_MIRROR_DIR)
    parser.add_argument("--scheduled-dir", type=Path, default=None,
                        help="Default: $CLAUDE_SCHEDULED_DIR, else ~/Claude/Scheduled")
    parser.add_argument("--window-end-date", default=None,
                        help="Default: config/trading_window.json's end_date")
    parser.add_argument("--diff", action="store_true", help="Include masked unified diffs")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    scheduled_dir = args.scheduled_dir or default_scheduled_dir()
    if not scheduled_dir.is_dir():
        print(f"scheduled-task directory not found: {scheduled_dir}", file=sys.stderr)
        return 2
    problem = mirror_set_problem(args.mirror_dir)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    window_end_date = args.window_end_date or _window_end_date_from_config()
    if not window_end_date:
        # Without it stale_window_end_date silently never fires, and a
        # matching pair would exit 0 without checking a failure mode this
        # command advertises (Copilot review).
        print("no window end date: pass --window-end-date or fix config/trading_window.json", file=sys.stderr)
        return 2
    report = check(
        mirror_dir=args.mirror_dir,
        scheduled_dir=scheduled_dir,
        window_end_date=window_end_date,
        account_number=os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER") or None,
        with_diff=args.diff,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_text(report)
    return 1 if report["drifted_slots"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
