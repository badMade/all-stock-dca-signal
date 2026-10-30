"""Draft the GitHub issue that tells the operator to repaste the Perplexity prompts after the planner changed.

Why (2026-10-28): the 9:35 backup failed at step 0 because the prompt pasted into Perplexity carried the ``d5ad2719``
stamp while the attached ``backup_planner.pyz`` was ``6e3b2c3b`` and ``main`` had moved on to ``f0b3fa93``. Three
rebuilds landed in about a day and nothing told the operator to repaste. ``scripts/planner_stamp_check.py`` covers the
Mac, but only when the Mac is on and only for the attached file. This runs on GitHub when a push to ``main`` changes
``backup_planner.pyz``, and the issue it feeds reaches the operator wherever they are.

Usage (from ``.github/workflows/planner-repaste-notice.yml``)::

    python -m scripts.planner_repaste_notice --previous old.pyz --current backup_planner.pyz \\
        --commit "$GITHUB_SHA" --out-dir "$RUNNER_TEMP/notice"

Writes ``title.txt``, ``body.md`` and ``kind.txt`` into ``--out-dir`` only when the planner's ``content_sha256``
changed (or the previous planner is missing or unreadable, which is reported as "unknown" rather than skipped).
``kind.txt`` is ``needed`` or ``optional``: the bundled files inside the two planners are compared, and a change that
only touches comments or docstrings (the Python AST is unchanged once docstrings are dropped) is ``optional``. Of 29
hash changes between 2026-10-14 and 2026-10-29, 25 touched code or JSON and 4 touched only comments. The hash itself
stays strict; only the wording of the notice differs. Unknown is never downgraded. Exit 0 either way;
exit 2 when the CURRENT planner cannot be read, so a broken build shows as a failed run and not as "all good".
Nothing here carries a private value: the issue is posted to a repository and holds hashes, a count and commands only.
Standard library only; no network.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from scripts.planner_stamp_check import StampError, pasted_stamp
from scripts.verify_perplexity_pair import PairError, planner_facts

EXIT_OK = 0
EXIT_FAILED = 2

# The label the workflow uses to find the one open issue to update, so a burst of rebuilds stays one thread.
LABEL = "planner-repaste"

# The file table is bounded so a sweeping change cannot produce an unreadable issue.
MAX_LISTED_FILES = 30
# One bundled file is ~1.7 MB at most today; a member far above that is not a planner this tool should expand.
_MAX_MEMBER_BYTES = 8 * 1024 * 1024
_BOOTSTRAP = "__main__.py"


@dataclass(frozen=True)
class Notice:
    """The issue to post: ``title`` is stable per new stamp and kind, ``body`` is Markdown.

    ``kind`` is ``needed`` (repaste before the next slot) or ``optional`` (comment or docstring changes only).
    """

    title: str
    body: str
    kind: str


@dataclass(frozen=True)
class FileChange:
    """One bundled file that differs between two planners; ``kind`` is code, comment-only, added or removed."""

    path: str
    kind: str


def _code_form(path: str, data: bytes) -> bytes:
    """The bytes that decide whether a file changed behaviour.

    A ``.py`` file is reduced to its AST with docstrings dropped, so comments, blank lines and docstring text do
    not count. Anything else (JSON, the bootstrap) is compared as written, and so is a file that does not parse:
    a syntax error cannot be proven comment-only.
    """
    if not path.endswith(".py") or path == _BOOTSTRAP:
        return data
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError):
        return data
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.dump(tree).encode()


def _bundled(path: Path) -> dict[str, bytes]:
    """The bootstrap and every inventoried file of a planner (``pasted_stamp`` has already validated it)."""
    with zipfile.ZipFile(path) as archive:
        names = [_BOOTSTRAP, *json.loads(archive.read("BUILD_INFO.json"))["files"]]
        members = {}
        for name in names:
            if archive.getinfo(name).file_size > _MAX_MEMBER_BYTES:
                raise ValueError("bundled file too large")
            members[name] = archive.read(name)
    return members


def changed_files(previous: Path, current: Path) -> Optional[list[FileChange]]:
    """Compare the bundled files of two planners, or return ``None`` when that is not possible.

    ``None`` means "unknown", which the caller treats as a needed repaste.
    """
    try:
        before, after = _bundled(previous), _bundled(current)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RecursionError):
        return None
    changes = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before:
            changes.append(FileChange(name, "added"))
        elif name not in after:
            changes.append(FileChange(name, "removed"))
        elif before[name] != after[name]:
            same_code = _code_form(name, before[name]) == _code_form(name, after[name])
            changes.append(FileChange(name, "comment-only" if same_code else "code"))
    return changes


def _table(changes: Optional[list[FileChange]]) -> list[str]:
    """Markdown lines naming the changed files (never their contents)."""
    if changes is None:
        return ["The previous planner could not be read, so the changed files are unknown. Treat this as a repaste.", ""]
    if not changes:
        return ["No bundled file differs; the stamp moved for another reason. Treat this as a repaste.", ""]
    lines = ["### What changed inside the planner", "", "| File | Change |", "| --- | --- |"]
    for change in changes[:MAX_LISTED_FILES]:
        label = f"{change.path} (bootstrap)" if change.path == _BOOTSTRAP else change.path
        lines.append(f"| `{label}` | {change.kind} |")
    if len(changes) > MAX_LISTED_FILES:
        lines.append(f"| and {len(changes) - MAX_LISTED_FILES} more | |")
    return [*lines, ""]


def _kind(changes: Optional[list[FileChange]]) -> str:
    """``optional`` only when every change is comment-only; unknown or empty is never downgraded."""
    if changes and all(change.kind == "comment-only" for change in changes):
        return "optional"
    return "needed"


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
    changes = changed_files(previous, current) if previous is not None else None
    kind = _kind(changes)
    if kind == "optional":
        title = f"Repaste optional: backup planner {new[:8]} (comment-only change)"
        lead = (f"`backup_planner.pyz` changed on `main` at commit `{commit[:7]}`, but only comments or docstrings "
                "changed. The code is the same, so a pair you already pasted still behaves the same. Repaste when "
                "convenient, for example with your next real repaste.")
    else:
        title = f"Repaste needed: backup planner {new[:8]}"
        lead = (f"`backup_planner.pyz` changed on `main` at commit `{commit[:7]}`. Every Perplexity backup task must "
                "be repasted before its next slot, or step 0 stops the run with `failed_unexpected_error` and no "
                "orders.")
    body = "\n".join([
        lead,
        "",
        "| | |",
        "| --- | --- |",
        f"| Previous `content_sha256` | `{old if old else 'unknown'}` |",
        f"| New `content_sha256` | `{new}` |",
        f"| Universe hash | `{universe_hash[:8]}` ({count} symbols) |",
        "",
        *_table(changes),
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
        "6. Copy the attached planner to the path the Mac stamp check reads, then close this issue.",
        "",
        "Do not paste account digits, an email address or any filled prompt into this issue.",
    ])
    return Notice(title=title, body=body + "\n", kind=kind)


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
    (args.out_dir / "kind.txt").write_text(notice.kind, encoding="utf-8")
    print("planner stamp changed; repaste notice written")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
