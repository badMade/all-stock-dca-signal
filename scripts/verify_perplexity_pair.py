#!/usr/bin/env python3
"""Check that a Perplexity backup prompt set and its ``backup_planner.pyz`` agree, BEFORE anything is pasted.

Why this exists (2026-10-14): the 9:55 task ran an earlier prompt and planner pair than the one the operator believed
they had pasted. The two halves are edited by hand in two different places in Perplexity (the task's Instructions box
and the project's Files), and nothing compared them until a slot fired. The planner does verify its own stamp at run
time (step 0 of every prompt), but a mismatch there costs a missed slot. This tool runs on the operator's own machine
against the files they are about to paste.

A set agrees when, for every ``*-backup.txt`` prompt:

* the planner stamp the prompt demands (``content_sha256 must equal <hash>``) equals the planner's own
  ``--build-info`` ``content_sha256``;
* the universe hash and symbol count in the prompt header equal what ``backup_planner.pyz universe`` reports; and
* no ``${NAME}`` placeholder is left (a repo copy pasted unfilled fails the account rule at run time). The repo's own
  committed prompts hold placeholders by design, so ``--allow-placeholders`` skips this one check.

Read-only and private-value safe: it never writes a file, and it prints only file names, the first eight hex digits of
a hash, counts and placeholder NAMES, never a filled-in last four, email or state folder.

Usage::

    python3 -m scripts.verify_perplexity_pair --planner backup_planner.pyz --prompts ~/perplexity-filled

Exit codes: 0 every prompt agrees; 2 anything else (fail closed, never a traceback). Standard library only; no network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

_PROMPT_GLOB = "*-backup.txt"
_TIMEOUT_SECONDS = 60

# The two header facts every generated prompt carries (scripts/generate_perplexity_backup_prompts.py).
# (?![0-9A-Za-z]) because step 0 demands EXACT equality: a 65th hex digit (or a stray letter) after the hash is a
# malformed stamp, not the 64-digit hash followed by nothing (review on #1696).
_STAMP = re.compile(r"content_sha256 must equal ([0-9a-f]{64})(?![0-9A-Za-z])")
_UNIVERSE = re.compile(r"Universe hash ([0-9a-f]{64}) \((\d+) symbols\)")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

EXIT_OK = 0
EXIT_FAILED = 2


class PairError(Exception):
    """The planner could not be asked for its facts. Messages never include file contents."""


def _run_planner(planner: Path, *args: str) -> dict:
    """Run the planner zipapp with ``args`` and return its JSON output.

    ``-I`` keeps the interpreter from loading anything from the current directory, and no bytecode is written, so
    asking the planner a question leaves the operator's folder exactly as it was.

    Raises:
        PairError: The file is missing, will not run, times out, exits non-zero or does not print one JSON object.
    """
    if not planner.is_file():
        raise PairError("planner file not found")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell, operator-supplied file by design
            [sys.executable, "-I", str(planner), *args],
            capture_output=True, timeout=_TIMEOUT_SECONDS, env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PairError(f"planner could not be run ({error.__class__.__name__})") from error
    if done.returncode != 0:
        raise PairError(f"planner exited {done.returncode} for {' '.join(args)}")
    # Captured as bytes and decoded here, so output that is not UTF-8 is a PairError rather than a UnicodeDecodeError
    # raised inside subprocess; stderr is never read, so stray bytes on it cannot fail a good run (review on #1696).
    try:
        document = json.loads(done.stdout.decode("utf-8"))
    except ValueError as error:  # UnicodeDecodeError and JSONDecodeError are both ValueErrors
        raise PairError(f"planner did not print JSON for {' '.join(args)}") from error
    if not isinstance(document, dict):
        raise PairError(f"planner printed something other than a JSON object for {' '.join(args)}")
    return document


def planner_facts(planner: Path) -> tuple[str, str, int]:
    """Return ``(content_sha256, universe_hash, symbol_count)`` as the planner itself reports them.

    Raises:
        PairError: The planner cannot be run, or a reported value has the wrong shape.
    """
    build = _run_planner(planner, "--build-info").get("content_sha256")
    universe = _run_planner(planner, "universe")
    universe_hash, count = universe.get("universe_hash"), universe.get("count")
    if not (isinstance(build, str) and _HEX64.fullmatch(build)):
        raise PairError("planner --build-info has no valid content_sha256")
    if not (isinstance(universe_hash, str) and _HEX64.fullmatch(universe_hash)):
        raise PairError("planner universe has no valid universe_hash")
    if not (isinstance(count, int) and not isinstance(count, bool)):
        raise PairError("planner universe has no valid count")
    return build, universe_hash, count


def problems_in(text: str, *, build: str, universe_hash: str, count: int, allow_placeholders: bool) -> list[str]:
    """Every way one prompt disagrees with the planner, as short messages (empty when it agrees).

    Messages carry only the first eight hex digits of a hash, counts and placeholder names, so nothing private can
    reach the terminal even from a prompt that has already been filled in.
    """
    problems: list[str] = []
    stamp = _STAMP.search(text)
    header = _UNIVERSE.search(text)
    if stamp is None:
        problems.append("MISSING planner stamp line")
    elif stamp.group(1) != build:
        problems.append(f"MISMATCH stamp {stamp.group(1)[:8]} in prompt, planner is {build[:8]}")
    if header is None:
        problems.append("MISSING universe header line")
    else:
        if header.group(1) != universe_hash:
            problems.append(f"MISMATCH universe {header.group(1)[:8]} in prompt, planner is {universe_hash[:8]}")
        if int(header.group(2)) != count:
            problems.append(f"MISMATCH symbols {header.group(2)} in prompt, planner lists {count}")
    if not allow_placeholders:
        names = sorted(set(_PLACEHOLDER.findall(text)))
        if names:
            problems.append("UNFILLED placeholder " + ", ".join(names))
    return problems


def verify(planner: Path, prompts: Path, *, allow_placeholders: bool = False) -> tuple[bool, list[str]]:
    """Verify every prompt in ``prompts`` against ``planner``; return ``(all_agree, report_lines)``."""
    sources = sorted(prompts.glob(_PROMPT_GLOB)) if prompts.is_dir() else []
    if not sources:
        return False, [f"no {_PROMPT_GLOB} prompts found in the prompts folder"]
    try:
        build, universe_hash, count = planner_facts(planner)
    except PairError as error:
        return False, [f"planner: {error}"]
    lines = [f"planner {build[:8]}, {count} symbols (universe {universe_hash[:8]})"]
    agree = True
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            agree = False
            lines.append(f"{path.name}: UNREADABLE ({error.__class__.__name__})")
            continue
        found = problems_in(text, build=build, universe_hash=universe_hash, count=count,
                            allow_placeholders=allow_placeholders)
        if found:
            agree = False
            lines.append(f"{path.name}: " + "; ".join(found))
        else:
            lines.append(f"{path.name}: OK")
    return agree, lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: 0 when every prompt agrees with the planner, 2 on any problem."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--planner", type=Path, required=True, help="the backup_planner.pyz you will attach")
    parser.add_argument("--prompts", type=Path, required=True, help="folder holding the six *-backup.txt prompts")
    parser.add_argument("--allow-placeholders", action="store_true",
                        help="do not fail on ${...} placeholders (for the unfilled prompts committed to the repo)")
    args = parser.parse_args(argv)
    agree, lines = verify(args.planner, args.prompts, allow_placeholders=args.allow_placeholders)
    for line in lines:
        print(line)
    if agree:
        print("All prompts agree with the planner. Still read the Instructions header in Perplexity after pasting.")
        return EXIT_OK
    print("Do NOT paste this set: fix the lines above first.", file=sys.stderr)
    return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
