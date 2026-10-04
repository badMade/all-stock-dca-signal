#!/usr/bin/env python3
"""Apply the three `.env` loader edits (``dotenv-loader.draft.md``) to the LIVE executor prompts,
then re-capture the ``live-mirror/`` copies from the edited live bodies.

Why a script rather than ``update_scheduled_task``: that tool replaces the WHOLE body, which
means retyping ~200 long lines per slot. Three anchored insertions leave every other byte alone.
Why the mirrors are regenerated from live instead of edited in place: the live body is the
source of truth (``README.md``), so a mirror that was reverted, or that never had the 2026-10-03
bridge-shell change, still ends up identical to live and ``scripts.check_live_prompt_drift``
reports 0 of 7 afterwards.

Safety properties:

* dry run by default; ``--write`` is required to change anything;
* all-or-nothing: every result is computed first, and one missing or ambiguous anchor aborts
  before a single file is touched;
* each anchor must occur EXACTLY once per live body (a body that drifted fails loudly);
* idempotent: a live body that already has the ENVIRONMENT CHECK is not edited again, but its
  mirror is still re-captured;
* live files are copied to ``<scheduled-dir>.bak-<UTC timestamp>/`` before being rewritten;
* the account number is masked to ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}`` in every mirror, and
  the run aborts if a mirror would still hold an account-shaped digit run;
* it never prints file contents, so no account number can reach the terminal.

Usage::

    python3 claude/dca-executor-prompts/apply_dotenv_edits.py            # dry run
    python3 claude/dca-executor-prompts/apply_dotenv_edits.py --write    # apply
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_MIRROR_DIR = HERE / "live-mirror"
DEFAULT_SCHEDULED_DIR = Path.home() / "Claude" / "Scheduled"
ACCOUNT_PLACEHOLDER = "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"
# An 8 to 10 digit run is account-shaped; legitimate prompt numbers (180000, 120) are shorter.
ACCOUNT_SHAPED_RE = re.compile(r"(?<![0-9])[0-9]{8,10}(?![0-9])")

# (task id, mirror filename prefix). The 1230 body is structured differently (the account is
# pinned before Gate 4 and Gate 2 closes with a different sentence), so it has its own anchors.
SLOTS: Tuple[Tuple[str, str], ...] = (
    ("935-am-dca-morning-buy", "0935ET"),
    ("1030-dca-recurring-buy", "1030ET"),
    ("1130-mornig-buying-run", "1130ET"),
    ("1230-pm-midday-buying-run", "1230ET"),
    ("130-pm-afternoon-buyiing-run", "1330ET"),
    ("230-pm-afternoon-buying-run", "1430ET"),
    ("330-final-buying-run", "1530ET"),
)

APPLIED_MARKER = "ENVIRONMENT CHECK. Once exactly one checkout survives"

ENVIRONMENT_CHECK = (
    "ENVIRONMENT CHECK. Once exactly one checkout survives, from its root run: "
    "`python3 -c \"import dca_paths, os; print({k: bool(os.environ.get(k)) for k in "
    "('ROBINHOOD_AGENTIC_ACCOUNT_NUMBER', 'DCA_ALERT_EMAIL_ADDRESS')})\"` "
    "(in bridge mode run it as `\"$PY\" -c ...`, per the BRIDGE PROTOCOL). Importing `dca_paths` loads the "
    "operator's gitignored `.env` (`dca_env`; docs/local_env_file.md), so this works even though the executor "
    "shell does not inherit the operator's exports. Print booleans only, never a value (SECRET RULE). "
    "If the import raises ValueError (insecure `.env` permissions, a symlink, wrong owner), abort "
    "`Failing gate: Gate 2` with \"local .env rejected: <the error text>\" -- the text names a rule, never "
    "contents, so it is safe to quote -- and place nothing. "
    "If ROBINHOOD_AGENTIC_ACCOUNT_NUMBER is False, abort `Failing gate: Gate 2` with \"account number environment "
    "variable not visible to the executor shell; create .env per docs/local_env_file.md\" and place nothing: "
    "Gate 7's account pin would otherwise fail on the same condition minutes later. "
    "If DCA_ALERT_EMAIL_ADDRESS is False, do NOT abort: record \"email alert channel has no recipient\" for "
    "Gate 9's report and in the first alert you send, because channel 3 cannot deliver; channels 1 and 2 still run."
)

SECRET_RULE_EDIT = (
    "an alert is never the place to prove which account you used.",
    "an alert is never the place to prove which account you used. The account number and alert address come "
    "from the operator's local `.env` via `dca_paths`; the file's contents are never read, printed or quoted, "
    "and Gate 2 reports only whether each variable is set.",
)


def _pin_replacement(tail: str) -> str:
    return (
        "Resolve it by running, from the repo root, `python3 -c \"import dca_paths, os; "
        "print(os.environ.get('ROBINHOOD_AGENTIC_ACCOUNT_NUMBER', ''))\"` (the import loads the operator's "
        "gitignored `.env`; see Gate 2's ENVIRONMENT CHECK; in bridge mode run it as `\"$PY\" -c ...`)"
        + tail
        + "and use that printed value as the account for every native call, and fail closed"
    )


_GATE_2_CLOSE = "If none of these yields a directory containing config/trading_window.json"
_GATE_2_CLOSE_1230 = "Zero matches, or none of the deduplicated matches is a directory containing config/trading_window.json"

STANDARD_EDITS: Tuple[Tuple[str, str], ...] = (
    (_GATE_2_CLOSE, ENVIRONMENT_CHECK + "\n" + _GATE_2_CLOSE),
    (
        "Resolve it to its actual environment value through the shell that will construct the call, and fail closed",
        _pin_replacement(" "),
    ),
    SECRET_RULE_EDIT,
)

EDITS_1230: Tuple[Tuple[str, str], ...] = (
    (_GATE_2_CLOSE_1230, ENVIRONMENT_CHECK + "\n" + _GATE_2_CLOSE_1230),
    (
        "Resolve it to its actual environment value through the shell that will construct these calls, "
        "before Gate 4's order-history fetch below, and fail closed",
        _pin_replacement(", before Gate 4's order-history fetch below, "),
    ),
    SECRET_RULE_EDIT,
)


class AnchorError(Exception):
    """An anchor sentence is missing or ambiguous, or a safety check failed."""


def apply_edits(text: str, label: str, edits: Sequence[Tuple[str, str]]) -> str:
    """Return ``text`` with every ``(old, new)`` applied; each ``old`` must occur exactly once."""
    for old, new in edits:
        found = text.count(old)
        if found != 1:
            raise AnchorError(f"{label}: anchor {old[:56]!r} occurs {found} times (need exactly 1)")
        text = text.replace(old, new)
    return text


def live_body(text: str) -> str:
    """The live body minus the leading ``---`` frontmatter block (mirrors never carry it)."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "".join(lines[index + 1:]).lstrip("\n")
    return text


def mirror_header(mirror_text: str) -> str:
    """The mirror's contiguous leading ``#``/blank block (its metadata), kept as is."""
    kept: List[str] = []
    for line in mirror_text.splitlines(keepends=True):
        if line.startswith("#") or not line.strip():
            kept.append(line)
        else:
            break
    return "".join(kept)


def _account_value() -> Optional[str]:
    """The configured account number (to mask), loaded the same way the live runs load it."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import dca_env

        dca_env.load_dotenv()
    except (ImportError, ValueError):
        pass
    return os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER") or None


def build_mirror(header: str, new_live_text: str, account: Optional[str], label: str) -> str:
    body = live_body(new_live_text)
    if account:
        body = body.replace(account, ACCOUNT_PLACEHOLDER)
    if ACCOUNT_SHAPED_RE.search(body):
        raise AnchorError(f"{label}: an account-shaped digit run would reach the mirror; set the account "
                          "number in .env (or the environment) so it can be masked, then rerun")
    return header + body


def plan(scheduled_dir: Path, mirror_dir: Path) -> List[Tuple[Path, Optional[str], str]]:
    """Return ``(path, new_text_or_None_when_unchanged, kind)`` for every live file and mirror."""
    account = _account_value()
    targets: List[Tuple[Path, Optional[str], str]] = []
    for task_id, prefix in SLOTS:
        edits = EDITS_1230 if prefix == "1230ET" else STANDARD_EDITS
        mirrors = sorted(mirror_dir.glob(f"{prefix}-*.txt"))
        if len(mirrors) != 1:
            raise AnchorError(f"{prefix}: expected exactly one mirror in {mirror_dir}, found {len(mirrors)}")
        live_path = scheduled_dir / task_id / "SKILL.md"
        if not live_path.is_file():
            raise AnchorError(f"live file not found: {live_path}")
        live_text = live_path.read_text(encoding="utf-8")
        if APPLIED_MARKER in live_text:
            new_live: Optional[str] = None
            final_live = live_text
        else:
            final_live = new_live = apply_edits(live_text, f"live {task_id}/SKILL.md", edits)
        mirror_text = mirrors[0].read_text(encoding="utf-8")
        new_mirror = build_mirror(mirror_header(mirror_text), final_live, account, f"mirror {mirrors[0].name}")
        targets.append((live_path, new_live, "live"))
        targets.append((mirrors[0], None if new_mirror == mirror_text else new_mirror, "mirror"))
    return targets


def _atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dotenv-edit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scheduled-dir", type=Path, default=DEFAULT_SCHEDULED_DIR)
    parser.add_argument("--mirror-dir", type=Path, default=DEFAULT_MIRROR_DIR)
    parser.add_argument("--write", action="store_true", help="apply the edits (default is a dry run)")
    args = parser.parse_args(argv)
    scheduled_dir = args.scheduled_dir.expanduser()

    try:
        targets = plan(scheduled_dir, args.mirror_dir)
    except AnchorError as exc:
        print(f"ABORT, nothing changed: {exc}", file=sys.stderr)
        return 1

    pending = [(path, text, kind) for path, text, kind in targets if text is not None]
    for path, text, kind in targets:
        state = "unchanged" if text is None else ("will write" if not args.write else "writing")
        print(f"{state:11} {kind:6} {path.parent.name}/{path.name}")
    if not pending:
        print("Nothing to do.")
        return 0
    if not args.write:
        print(f"Dry run: {len(pending)} file(s) would change. Re-run with --write to apply.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = scheduled_dir.parent / f"{scheduled_dir.name}.bak-{stamp}"
    for path, _text, kind in pending:
        if kind == "live":
            backup = backup_root / path.parent.name / path.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
    for path, text, _kind in pending:
        assert text is not None
        _atomic_write(path, text)
    print(f"Wrote {len(pending)} file(s). Live backups: {backup_root}")
    print("Next: .venv/bin/python -m scripts.check_live_prompt_drift   (expect 0 of 7 slots need a repaste)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
