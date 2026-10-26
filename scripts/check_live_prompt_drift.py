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
* ``missing_oversized_order_pages_helper`` -- the live body calls
  ``get_equity_orders`` but never runs ``scripts.assemble_order_pages``, so an
  oversized page would be hand-transcribed instead of assembled and proven
  complete (2026-09-09: the 12:30 slot hand-transcribed a page, two passes
  disagreed on the row count, and Gate 6 aborted -- the incident this helper
  exists to prevent; see ``scripts/assemble_order_pages.py``). Live drift on
  this finding is exactly what would let that incident recur even though the
  repo's own mirror already carries the fix (2026-09-24 audit: all seven live
  bodies had drifted from the mirror on this exact paragraph).
* ``missing_shell_unavailable_fallback`` -- the live body runs Gate 2's repo
  probes but never carries the distinct abort reason "executor shell tool
  unavailable, repo probes could not run" (2026-09-08: a dead sandboxed shell
  made Gate 2's probes look identical to an unreachable repo, so three
  consecutive slots reported "repo access" with the wrong reconnect-the-folder
  remedy -- one of them burning roughly a third of its window on retries
  first).
* ``missing_bridge_only_abort`` -- the live body runs Gate 2's repo probes
  but never carries the distinct abort reason "repo readable via device
  bridge, not mounted in executor shell" (2026-09-29 2:30 PM: shell alive,
  all four probes empty, repo readable only through the device file bridge,
  so the run took the generic repo-access abort and its reconnect-the-folder
  remedy for a repo that exists).
* ``email_draft_not_send`` -- the live body still tells the operator to
  DRAFT the Gate 9 alert email rather than send it (2026-08-26:
  ``docs/alert_digest.md`` counted roughly fifty undelivered draft alerts
  sitting in Gmail before this was fixed to require EMAIL SENT).

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
# The helper that replaces hand-transcribing an oversized get_equity_orders
# page (2026-09-09 incident; see scripts/assemble_order_pages.py).
ORDER_PAGES_HELPER_MODULE = "scripts.assemble_order_pages"
GET_EQUITY_ORDERS_CALL = "get_equity_orders"
# A Gate 2 repo-probe command; its presence means the live body actually runs
# the probes the shell-unavailable fallback below must guard (2026-09-08
# incident: see check_live_prompt_drift's module docstring).
GATE_2_PROBE_MARKER = "ls -d /sessions"
SHELL_UNAVAILABLE_ABORT_REASON = "executor shell tool unavailable, repo probes could not run"
# Same probe marker, different failure: the shell works and finds no checkout,
# but the repo is readable through a device file bridge (2026-09-29 2:30 PM).
BRIDGE_ONLY_ABORT_REASON = "repo readable via device bridge, not mounted in executor shell"
# The pre-2026-08-26 alert wording this checker flags as a regression back to
# draft-only delivery (docs/alert_digest.md: ~50 undelivered drafts).
EMAIL_DRAFT_PHRASE = "EMAIL DRAFT"
# 2026-10-12 owner direction retired the yfinance fallback from every scheduled run; the
# generators now reject a yahoo_finance_yfinance source (tests/test_yfinance_retired.py). A
# live body still carrying the old merge paragraph, or the short-lived 9:35 PREFETCH-FIRST
# rule that made the prefetched file primary, aborts Gate 5 instead of scoring. Any slot.
RETIRED_YFINANCE_MARKERS = ("PREFETCHED YFINANCE FALLBACK", "PREFETCH-FIRST")
# 2026-10-17 (DCA Issues key hand_reimplemented): Gate 5 must say signals come only from the slot's repo
# generator and are never recomputed by hand. A live body without this sentence lets a failed data path end in
# hand-written signal logic instead of a not_submitted_error abort.
NO_HAND_SIGNALS_RULE = "Never recompute signals, sizes or candidates by hand"
# 2026-10-22 11:30 (ref_id_format_rejected): the per-order placement sentence still named the text key
# dca-runner-<slot>-<date>-<TICKER> right after "DETERMINISTIC ref_id", with the UUID override ~110 lines
# earlier in the NATIVE ORDER-ID RULE. One session in five sent the text key, place_equity_order rejected
# it (UUID only) and the slot placed nothing. The reworded mirrors put only the validate_native_placement
# UUID in the ref_id position; a live body still carrying this phrase needs a repaste.
TEXT_KEY_REF_ID_WORDING = "DETERMINISTIC ref_id with every order (sent as the UUID"
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
_ACCOUNT_REFERENCE_RE = re.compile(
    r"(?:--account(?:\s*=\s*|\s+)|[\"']account_number[\"']\s*:\s*[\"']?|"
    r"\baccount(?:_number|\s+number)?\s*(?:[:=]\s*|\s+(?:is\s+)?)[\"']?)(\d{6,})(?!\d)",
    re.IGNORECASE,
)


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


def _live_body(lines: List[str]) -> List[str]:
    """Live lines minus the leading ``---`` YAML frontmatter Cowork keeps on every SKILL.md.

    The frontmatter (``name``/``description``) is task metadata the mirror
    never copies, so leaving it in made every slot read as ``differs`` even
    after a faithful repaste (2026-09-22: "7 of 7 slots need a repaste" could
    never reach 0). Only a block opened on line 1 AND closed by a later ``---``
    is stripped; an unclosed opener is left as prompt content so it is still
    compared rather than silently swallowing the whole body.
    """
    if not lines or lines[0].strip() != "---":
        return lines
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[index + 1 :]
    return lines


def _normalize_live(text: str, account_number: Optional[str]) -> List[str]:
    if account_number:
        text = text.replace(account_number, ACCOUNT_PLACEHOLDER)
    return _live_body(text.splitlines())


def mask(line: str, account_number: Optional[str] = None) -> str:
    """Mask the account value and any other account-shaped digit run."""
    if account_number:
        line = line.replace(account_number, "[ACCOUNT]")
    return _LONG_DIGITS_RE.sub("[REDACTED]", line)


def _stale_window_dates(text: str, window_end_date: str) -> List[str]:
    found: List[str] = []
    for phrase in _END_DATE_PHRASE_RE.finditer(text):
        context = text[phrase.end() : phrase.end() + _END_DATE_CONTEXT_CHARS]
        for date in _DATE_RE.findall(context):
            if date != window_end_date and date not in found:
                found.append(date)
    return found


def findings_for(live_text: str, window_end_date: Optional[str]) -> List[Dict[str, str]]:
    """The named failure modes present in one live body."""
    findings: List[Dict[str, str]] = []
    if TOP_UP_GATE_MODULE not in live_text:
        findings.append(
            {
                "code": "missing_top_up_gate",
                "detail": f"never runs {TOP_UP_GATE_MODULE}: a repeat buy would be sized as a first allocation",
            }
        )
    elif any(key not in live_text for key in ORDER_PAGES_ENVELOPE_KEYS):
        findings.append(
            {
                "code": "bare_array_order_pages",
                "detail": 'runs the gate but never wraps saved pages as {"account_number", "trading_day", "pages"}; '
                "the gate rejects a bare array",
            }
        )
    if FINALIZE_EVIDENCE_MODULE not in live_text:
        findings.append(
            {
                "code": "missing_finalize_evidence",
                "detail": f"never runs {FINALIZE_EVIDENCE_MODULE}: the slot fires but leaves no verifiable run_state "
                "or pointer, so scripts.verify_schedule reports it evidence_incomplete",
            }
        )
    if GET_EQUITY_ORDERS_CALL in live_text and ORDER_PAGES_HELPER_MODULE not in live_text:
        findings.append(
            {
                "code": "missing_oversized_order_pages_helper",
                "detail": f"calls {GET_EQUITY_ORDERS_CALL} but never runs {ORDER_PAGES_HELPER_MODULE}: an oversized "
                "page would be hand-transcribed instead of assembled and proven complete (2026-09-09 incident)",
            }
        )
    if NO_HAND_SIGNALS_RULE not in live_text:
        findings.append(
            {
                "code": "missing_no_hand_signals_rule",
                "detail": f'Gate 5 lacks the rule "{NO_HAND_SIGNALS_RULE}": a failed data path could end in '
                "hand-replicated signal logic instead of a not_submitted_error abort (hand_reimplemented)",
            }
        )
    if TEXT_KEY_REF_ID_WORDING in live_text:
        findings.append(
            {
                "code": "ref_id_names_text_key",
                "detail": "the placement sentence names the dca-runner text key as the ref_id; a session that sends "
                "it is rejected by place_equity_order (UUID only) and places nothing (2026-10-22 11:30)",
            }
        )
    if window_end_date:
        stale = _stale_window_dates(live_text, window_end_date)
        if stale:
            findings.append(
                {
                    "code": "stale_window_end_date",
                    "detail": f"hardcodes {', '.join(stale)} as the window end; config/trading_window.json says "
                    f"{window_end_date}",
                }
            )
    if GATE_2_PROBE_MARKER in live_text and SHELL_UNAVAILABLE_ABORT_REASON not in live_text:
        findings.append(
            {
                "code": "missing_shell_unavailable_fallback",
                "detail": f'runs Gate 2\'s repo probes without the distinct abort reason "{SHELL_UNAVAILABLE_ABORT_REASON}"'
                ": a dead sandboxed shell can misdiagnose as repo-unreachable (2026-09-08 incident)",
            }
        )
    if GATE_2_PROBE_MARKER in live_text and BRIDGE_ONLY_ABORT_REASON not in live_text:
        findings.append({
            "code": "missing_bridge_only_abort",
            "detail": f'runs Gate 2\'s repo probes without the distinct abort reason "{BRIDGE_ONLY_ABORT_REASON}"'
            ": a repo readable only through the device bridge misdiagnoses as a disconnected folder "
            "(2026-09-29 incident)",
        })
    retired = [marker for marker in RETIRED_YFINANCE_MARKERS if marker in live_text]
    if retired:
        findings.append(
            {
                "code": "retired_yfinance_fallback",
                "detail": f"still carries {', '.join(retired)}: the yfinance fallback was retired from every "
                "scheduled run on 2026-10-12 and the generators reject that source, so Gate 5 aborts; re-paste "
                "the mirror",
            }
        )
    if EMAIL_DRAFT_PHRASE in live_text:
        findings.append(
            {
                "code": "email_draft_not_send",
                "detail": "still tells the operator to DRAFT the Gate 9 alert email instead of sending it "
                "(2026-08-26 regression: docs/alert_digest.md counted ~50 undelivered drafts)",
            }
        )
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
            slot["changed_lines"] = sum(1 for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---")))
            if with_diff:
                slot["diff"] = [mask(line, account_number) for line in diff]
        slots.append(slot)
    drifted = sum(1 for s in slots if s["status"] != "match" or s["findings"])
    return {
        "scheduled_dir": str(scheduled_dir),
        "window_end_date": window_end_date,
        "drifted_slots": drifted,
        "slots": slots,
    }


def shared_live_account(mirror_dir: Path, scheduled_dir: Path) -> Optional[str]:
    """The one account referenced by every live slot, when the env var is unset.

    Without ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` the literal-vs-placeholder
    lines were never normalized, so the plain command reported every slot as
    ``differs`` right after a faithful repaste (2026-09-22: 13 false changed
    lines per slot). The live bodies already hold the account in account-specific
    references, so use it only when each slot references exactly one account and
    every slot agrees. Unrelated numbers are ignored. Anything else (a slot
    missing or carrying multiple/different account references) returns None.
    Never printed.
    """
    shared: Optional[str] = None
    for mirror in sorted(Path(mirror_dir).glob("*.txt")):
        if mirror.name.startswith("."):
            continue
        live = Path(scheduled_dir) / str(task_id_for(mirror)) / "SKILL.md"
        if not live.is_file():
            return None
        references = set(_ACCOUNT_REFERENCE_RE.findall(live.read_text(encoding="utf-8")))
        if len(references) != 1:
            return None
        account = next(iter(references))
        if shared is not None and account != shared:
            return None
        shared = account
    return shared


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
        return str(json.loads(path.read_bytes())["end_date"])
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
    parser.add_argument(
        "--scheduled-dir", type=Path, default=None, help="Default: $CLAUDE_SCHEDULED_DIR, else ~/Claude/Scheduled"
    )
    parser.add_argument("--window-end-date", default=None, help="Default: config/trading_window.json's end_date")
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
        account_number=(
            os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER") or shared_live_account(args.mirror_dir, scheduled_dir)
        ),
        with_diff=args.diff,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_text(report)
    return 1 if report["drifted_slots"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
