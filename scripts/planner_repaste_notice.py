"""Draft the GitHub issue that tells the operator to repaste the Perplexity prompts after the planner changed.

Why (2026-10-28): the 9:35 backup failed at step 0 because the prompt pasted into Perplexity carried the ``d5ad2719``
stamp while the attached ``backup_planner.pyz`` was ``6e3b2c3b`` and ``main`` had moved on to ``f0b3fa93``. Three
rebuilds landed in about a day and nothing told the operator to repaste. ``scripts/planner_stamp_check.py`` covers the
Mac, but only when the Mac is on and only for the attached file. This runs on GitHub when a push to ``main`` changes
``backup_planner.pyz``, and the issue it feeds reaches the operator wherever they are.

Usage (from ``.github/workflows/planner-repaste-notice.yml``)::

    python -m scripts.planner_repaste_notice --previous old.pyz --current backup_planner.pyz \\
        --commit "$GITHUB_SHA" --out-dir "$RUNNER_TEMP/notice"

Writes ``title.txt`` and ``body.md`` into ``--out-dir`` only when the planner's ``content_sha256`` changed (or the
previous planner is missing or unreadable, which is reported as "unknown" rather than skipped). Exit 0 either way;
exit 2 when the CURRENT planner cannot be read, so a broken build shows as a failed run and not as "all good".
Nothing here carries a private value: the issue is posted to a repository and holds hashes, a count and commands only.
Standard library only; no network.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from scripts.planner_stamp_check import StampError, pasted_stamp
from scripts.verify_perplexity_pair import PairError, planner_facts

EXIT_OK = 0
EXIT_FAILED = 2

# The label the workflow uses to find the one open issue to update, so a burst of rebuilds stays one thread.
LABEL = "planner-repaste"


@dataclass(frozen=True)
class Notice:
    """The issue to post: ``title`` is stable per new stamp, ``body`` is Markdown."""

    title: str
    body: str


def _previous_stamp(previous: Optional[Path]) -> Optional[str]:
    """Return the previous planner's stamp, or ``None`` when it is absent or unreadable.

    Over-notifying is the safe direction: a previous build that cannot be read is no reason to skip a repaste.
    """
    if previous is None:
        return None
    try:
        return pasted_stamp(previous)
    except StampError:
        return None


def build_notice(previous: Optional[Path], current: Path, *, commit: str) -> Optional[Notice]:
    """Return the repaste notice, or ``None`` when the planner's stamp did not change.

    Raises:
        StampError: The current planner's build metadata cannot be read.
        PairError: The current planner cannot be run to report its universe.
    """
    new = pasted_stamp(current)
    old = _previous_stamp(previous)
    if old == new:
        return None
    _, universe_hash, count = planner_facts(current)
    title = f"Repaste needed: backup planner {new[:8]}"
    body = "\n".join([
        f"`backup_planner.pyz` changed on `main` at commit `{commit[:7]}`. Every Perplexity backup task must be "
        "repasted before its next slot, or step 0 stops the run with `failed_unexpected_error` and no orders.",
        "",
        "| | |",
        "| --- | --- |",
        f"| Previous `content_sha256` | `{old if old else 'unknown'}` |",
        f"| New `content_sha256` | `{new}` |",
        f"| Universe hash | `{universe_hash[:8]}` ({count} symbols) |",
        "",
        "### Repaste steps",
        "",
        "1. Update your checkout to `main` (or download the current ZIP).",
        "2. Fill the prompts from your local values: `python3 -m scripts.fill_perplexity_backup_prompts --out "
        "<folder outside the repo>`.",
        "3. Check the pair: `python3 -m scripts.verify_perplexity_pair --planner backup_planner.pyz --prompts "
        "<that folder>`. It must exit 0.",
        "4. In each of the six Perplexity tasks, replace the Instructions text with its filled prompt, and replace "
        "the project's attached `backup_planner.pyz` with the one from `main`.",
        "5. Read each task's Instructions header: it must show the universe hash and symbol count above.",
        "6. Close this issue.",
        "",
        "Do not paste account digits, an email address or any filled prompt into this issue.",
    ])
    return Notice(title=title, body=body + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--previous", type=Path, default=None, help="planner from the commit before the push")
    parser.add_argument("--current", type=Path, required=True, help="planner at the pushed commit")
    parser.add_argument("--commit", required=True, help="the pushed commit SHA")
    parser.add_argument("--out-dir", type=Path, required=True, help="where title.txt and body.md are written")
    args = parser.parse_args(argv)
    try:
        notice = build_notice(args.previous, args.current, commit=args.commit)
    except (StampError, PairError) as exc:
        # The exception text is a fixed message by design; local paths are never forwarded.
        print(f"planner_repaste_notice: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if notice is None:
        print("planner stamp unchanged; no repaste notice")
        return EXIT_OK
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "title.txt").write_text(notice.title, encoding="utf-8")
    (args.out_dir / "body.md").write_text(notice.body, encoding="utf-8")
    print("planner stamp changed; repaste notice written")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
