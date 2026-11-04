#!/usr/bin/env python3
"""Bring the LIVE Cowork scheduled-task bodies up to date with their repo mirrors.

``scripts/check_live_prompt_drift.py`` reports which live ``SKILL.md`` bodies lag
``claude/dca-executor-prompts/live-mirror/``; this is the repaste it asks for,
done by script instead of by hand across seven files (2026-10-26: 7 of 7 behind).

A faithful repaste is not a blind copy (see ``claude/dca-executor-prompts/README.md``):
the live file's YAML frontmatter is kept byte for byte, the mirror's leading ``#``
preamble is left out, and ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}`` is replaced with the
live account.

* The default is a dry run. ``--write`` backs each changed ``SKILL.md`` up as
  ``SKILL.md.bak-<UTC timestamp>`` and then replaces it; a slot whose body already
  matches is neither rewritten nor backed up, so a second write changes nothing.
* All or nothing: every slot is validated first, and one slot that cannot be handled
  (no ``SKILL.md``, a different account) means no slot is written.
* A mirror carrying the ``RESOLVED ACCOUNT FOR NATIVE CALLS`` rule (the 9:35 prompt) keeps the placeholder; only
  the mirrors without it get the account written in.
* The account is ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``, else the single value every
  live slot already carries. A live task is never repointed to another account, and no
  account-shaped number is ever printed.

Usage::

    python3 -m scripts.sync_live_prompts            # dry run
    python3 -m scripts.sync_live_prompts --write

Exit status: 0 on success, 2 when the sync cannot run or refuses.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from scripts import check_live_prompt_drift as drift


def _split_frontmatter(live_text: str) -> tuple[str, str]:
    """``(frontmatter, body)`` of a live file; frontmatter is "" when there is no closed ``---`` block."""
    lines = live_text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                head = "".join(lines[: index + 1])
                return (head if head.endswith("\n") else head + "\n"), "".join(lines[index + 1 :])
    return "", live_text


def _nonblank(text: str) -> List[str]:
    return [line for line in text.splitlines() if line.strip()]


def _plan(mirror_dir: Path, scheduled_dir: Path, account: Optional[str]) -> tuple[List[Dict[str, object]], List[str]]:
    """Per-slot plans and the reasons any slot cannot be handled."""
    plans: List[Dict[str, object]] = []
    problems: List[str] = []
    for mirror in sorted(mirror_dir.glob("*.txt")):
        if mirror.name.startswith("."):
            continue
        text = mirror.read_text(encoding="utf-8")
        task = drift.task_id_for(mirror, text)
        live_path = scheduled_dir / str(task) / "SKILL.md"
        if not task or not live_path.is_file():
            problems.append(f"{task or mirror.name}: no live SKILL.md")
            continue
        live_bytes = live_path.read_bytes()
        live_text = live_bytes.decode("utf-8")
        references = set(drift._ACCOUNT_REFERENCE_RE.findall(live_text))
        if account and references and references != {account}:
            problems.append(f"{task}: live task account references do not match; refusing to repoint it")
            continue
        mirror_lines = drift._mirror_body(text)
        if not any(line.strip() for line in mirror_lines):
            problems.append(f"{task}: mirror has no prompt body")
            continue
        body = "\n".join(mirror_lines) + "\n"
        # A mirror that resolves the account at run time keeps the placeholder: the number is written into no file
        # for it (2026-11-04). Every other mirror still gets the literal, because its native tool calls expand no
        # shell variable and would send the placeholder text to the broker.
        if account and not drift.mirror_resolves_account(text):
            body = body.replace(drift.ACCOUNT_PLACEHOLDER, account)
        frontmatter, live_body = _split_frontmatter(live_text)
        plans.append(
            {
                "task": task,
                "path": live_path,
                "old": live_bytes,
                "new": (frontmatter + body).encode("utf-8"),
                "changed": _nonblank(body) != _nonblank(live_body),
            }
        )
    return plans, problems


def _write_like(target: Path, data: bytes, mode: int) -> None:
    """Create ``target`` owner-only, then give it ``mode``.

    A live SKILL.md can be owner-only (it carries the account number). Creating the file 0600 first means the
    data is never readable by anyone else, not even for the instant before the final chmod, and the
    exclusive create refuses to overwrite a backup that already exists.
    """
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(target, mode)


def _stage(path: Path, data: bytes) -> Path:
    mode = path.stat().st_mode & 0o777
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # a leftover from an interrupted run must not block the exclusive create
    try:
        _write_like(tmp, data, mode)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _replace(path: Path, staged: Path) -> None:
    os.replace(staged, path)


def _remove_staged(staged: Sequence[Path]) -> None:
    for path in staged:
        path.unlink(missing_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mirror-dir", type=Path, default=drift.DEFAULT_MIRROR_DIR)
    parser.add_argument("--scheduled-dir", type=Path, default=None, help="Default: $CLAUDE_SCHEDULED_DIR, else ~/Claude/Scheduled")
    parser.add_argument("--write", action="store_true", help="Back up and replace the live bodies (default: dry run)")
    args = parser.parse_args(argv)

    scheduled_dir = args.scheduled_dir or drift.default_scheduled_dir()
    if not scheduled_dir.is_dir():
        print(f"scheduled-task directory not found: {scheduled_dir}", file=sys.stderr)
        return 2
    problem = drift.mirror_set_problem(args.mirror_dir)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    account = os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER") or drift.shared_live_account(
        args.mirror_dir, scheduled_dir
    )
    if not account:
        print("cannot determine the live account: set $ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", file=sys.stderr)
        return 2
    plans, problems = _plan(args.mirror_dir, scheduled_dir, account)
    if problems:
        for line in problems:
            print(drift.mask(line, account), file=sys.stderr)
        print("nothing written", file=sys.stderr)
        return 2

    changed = [p for p in plans if p["changed"]]
    if args.write:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        staged: List[Path] = []
        try:
            for plan in changed:
                path = plan["path"]
                assert isinstance(path, Path)
                staged.append(_stage(path, plan["new"]))  # type: ignore[arg-type]
        except OSError:
            _remove_staged(staged)
            print("could not stage live prompt replacements; nothing written", file=sys.stderr)
            return 2

        try:
            for plan in changed:
                path = plan["path"]
                assert isinstance(path, Path)
                _write_like(
                    path.with_name(f"SKILL.md.bak-{stamp}"),
                    plan["old"],  # type: ignore[arg-type]
                    path.stat().st_mode & 0o777,
                )
        except OSError:
            _remove_staged(staged)
            print("could not back up live prompts; nothing written", file=sys.stderr)
            return 2

        replaced: List[Dict[str, object]] = []
        try:
            for plan, temporary in zip(changed, staged, strict=True):
                _replace(plan["path"], temporary)  # type: ignore[arg-type]
                replaced.append(plan)
        except OSError:
            rollback_failed = False
            for plan in reversed(replaced):
                path = plan["path"]
                assert isinstance(path, Path)
                rollback: Optional[Path] = None
                try:
                    rollback = _stage(path, plan["old"])  # type: ignore[arg-type]
                    os.replace(rollback, path)
                except OSError:
                    rollback_failed = True
                finally:
                    if rollback is not None:
                        try:
                            rollback.unlink(missing_ok=True)
                        except OSError:
                            rollback_failed = True
            _remove_staged(staged)
            message = (
                "replacement failed and rollback was incomplete"
                if rollback_failed
                else "replacement failed; prior replacements were restored"
            )
            print(f"{message}; sync refused", file=sys.stderr)
            return 2

        for plan in plans:
            print(f"- {plan['task']}: {'updated' if plan['changed'] else 'already matches'}")
        print(f"{len(changed)} of {len(plans)} slots updated (written)")
    else:
        for plan in plans:
            print(f"- {plan['task']}: {'would change' if plan['changed'] else 'already matches'}")
        print(f"{len(changed)} of {len(plans)} slots need a sync (dry run, nothing written; pass --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
