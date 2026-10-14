#!/usr/bin/env python3
"""Fill the three private placeholders in the Perplexity backup prompts from values kept LOCALLY.

The six prompts committed under ``claude/dca-executor-prompts/perplexity/`` carry three placeholders so the repo
never holds the operator's values:

* ``${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}``: the last four digits of the account the run may trade.
* ``${PPLX_RUN_STATE_DIR}``: the run-state folder, one per task (``<state root>/<task id>``).
* ``${DCA_ALERT_EMAIL_ADDRESS}``: where failure alerts are sent.

This script renders a private, filled copy of every prompt for pasting into Perplexity. Nothing it writes is
meant to be committed, so it refuses any output directory inside this repo checkout, writes owner-only files,
and never prints a value.

Where the values come from (environment first, then the gitignored local ``.env`` that ``dca_env`` already
loads; see ``docs/local_env_file.md``):

* last four digits: ``ROBINHOOD_AGENTIC_ACCOUNT_LAST4`` if set, otherwise the last four digits of
  ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (the variable the rest of the repo already uses, so no new secret).
* alert email: ``DCA_ALERT_EMAIL_ADDRESS`` (the same variable the executors use).
* state folder: ``--state-root`` (default ``cron_tracking``) plus a folder name per prompt. Perplexity names each
  task's folder after its automation UUID (the ``automationId`` in the task URL), which is not the repo task id,
  so pass ``--folder <task id>=<automation UUID>`` once per prompt. Without any ``--folder`` the repo task id is
  used; with at least one, every prompt must be mapped (fail closed, so a typo cannot silently split state).

Fail closed and all-or-nothing: a missing or malformed value, a prompt without a task header or one of the three
placeholders, or any other ``${NAME}`` left over after filling writes NOTHING and exits 2.

Usage::

    python3 -m scripts.fill_perplexity_backup_prompts --out ~/perplexity-filled
    python3 -m scripts.fill_perplexity_backup_prompts --out ~/perplexity-filled \\
        --folder 4412c3c4=<automation UUID> --folder e028361e=<automation UUID> ...

Standard library only; no network.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import MutableMapping, Optional, Sequence

import dca_env

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "claude" / "dca-executor-prompts" / "perplexity"
DEFAULT_STATE_ROOT = "cron_tracking"

LAST4_VAR = "ROBINHOOD_AGENTIC_ACCOUNT_LAST4"
ACCOUNT_VAR = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
EMAIL_VAR = "DCA_ALERT_EMAIL_ADDRESS"

TOKEN_LAST4 = "${ROBINHOOD_AGENTIC_ACCOUNT_LAST4}"
TOKEN_STATE_DIR = "${PPLX_RUN_STATE_DIR}"
TOKEN_EMAIL = "${DCA_ALERT_EMAIL_ADDRESS}"

_PROMPT_GLOB = "*-backup.txt"
_LAST4 = re.compile(r"\d{4}")
_ACCOUNT = re.compile(r"\d{4,20}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_TASK_HEADER = re.compile(r"^Task ([0-9a-f]{8})\.", re.MULTILINE)
_ANY_PLACEHOLDER = re.compile(r"\$\{[A-Za-z0-9_]+\}")
_STATE_ROOT = re.compile(r"[A-Za-z0-9_./-]+")
_FOLDER_ARG = re.compile(r"([0-9a-f]{8})=([A-Za-z0-9_-]{8,64})")

EXIT_OK = 0
EXIT_FAILED = 2


class FillError(Exception):
    """A reason the prompts were not filled. Messages name variables and files, never the values themselves."""


def resolve_values(environ: MutableMapping[str, str]) -> tuple[str, str]:
    """Return ``(last4, email)`` from ``environ`` (after loading the local ``.env``), or raise ``FillError``.

    Args:
        environ: The mapping to read; the allowlisted keys of the local ``.env`` are exported into it first and
            never override a variable that is already set.

    Raises:
        FillError: A value is missing or malformed, or the local ``.env`` is rejected by the loader.
    """
    try:
        dca_env.load_dotenv(environ=environ)
    except ValueError as error:
        raise FillError(f"the local .env was rejected: {error}") from error

    if LAST4_VAR in environ:
        last4 = environ[LAST4_VAR]
        if not _LAST4.fullmatch(last4):
            raise FillError(f"{LAST4_VAR} must be exactly four digits")
    else:
        account = environ.get(ACCOUNT_VAR)
        if not account:
            raise FillError(f"missing: set {LAST4_VAR}, or {ACCOUNT_VAR} (its last four digits are used)")
        if not _ACCOUNT.fullmatch(account):
            raise FillError(f"{ACCOUNT_VAR} must be all digits and at least four long")
        last4 = account[-4:]

    email = environ.get(EMAIL_VAR)
    if not email:
        raise FillError(f"missing: set {EMAIL_VAR}")
    if not _EMAIL.fullmatch(email):
        raise FillError(f"{EMAIL_VAR} is not a plain address (one @, a dotted domain, no whitespace)")
    return last4, email


def _checked_state_root(state_root: str) -> str:
    root = state_root.rstrip("/") or state_root
    if not _STATE_ROOT.fullmatch(root) or ".." in root.split("/"):
        raise FillError("--state-root may only use letters, digits, '_', '.', '-' and '/', with no '..'")
    return root


def _checked_out_dir(src: Path, out: Path) -> Path:
    """The resolved output directory, refused if it could end up committed or overwrite the sources."""
    target = out.expanduser().resolve()
    source = src.resolve()
    if target == ROOT or ROOT in target.parents:
        raise FillError("refusing to write inside the repo checkout: filled copies hold private values and must stay out")
    if target == source or source in target.parents:
        raise FillError("--out must not be the source directory or inside it")
    return target


def parse_folders(pairs: Sequence[str]) -> dict[str, str]:
    """``TASK=FOLDER`` arguments as a dict.

    Raises:
        FillError: An argument is malformed or a task id is given twice.
    """
    folders: dict[str, str] = {}
    for pair in pairs:
        match = _FOLDER_ARG.fullmatch(pair)
        if match is None:
            raise FillError("--folder must look like <8-hex task id>=<folder name of letters, digits, '-' or '_'>")
        if match.group(1) in folders:
            raise FillError(f"--folder given twice for task {match.group(1)}")
        if match.group(2) in folders.values():
            raise FillError("--folder: two tasks may not share one folder (they would share a state record)")
        folders[match.group(1)] = match.group(2)
    return folders


def render(
    text: str, *, name: str, last4: str, email: str, state_root: str, folders: Optional[dict[str, str]] = None
) -> str:
    """One prompt with its three placeholders filled.

    Raises:
        FillError: The prompt has no task header, lacks one of the three placeholders, or still contains any
            ``${NAME}`` after filling.
    """
    header = _TASK_HEADER.search(text)
    if header is None:
        raise FillError(f"{name}: no 'Task <id>.' header line, so its state folder cannot be derived")
    task = header.group(1)
    if folders:
        if task not in folders:
            raise FillError(f"{name}: no --folder given for task {task}")
        state_dir = f"{state_root}/{folders[task]}"
    else:
        state_dir = f"{state_root}/{task}"
    for token in (TOKEN_LAST4, TOKEN_STATE_DIR, TOKEN_EMAIL):
        if token not in text:
            raise FillError(f"{name}: expected placeholder {token} is not present")
    filled = text.replace(TOKEN_LAST4, last4).replace(TOKEN_STATE_DIR, state_dir).replace(TOKEN_EMAIL, email)
    leftover = sorted(set(_ANY_PLACEHOLDER.findall(filled)))
    if leftover:
        raise FillError(f"{name}: unfilled placeholder(s) remain: {', '.join(leftover)}")
    return filled


def _stage_private(path: Path, text: str) -> str:
    """Write ``text`` to an owner-only temp file beside ``path`` and return its name (not yet in place).

    The whole payload is written (``os.write`` may stop short, so the file object's ``write`` is used) and
    flushed before returning, so a truncated prompt can never be moved into place.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o600)  # mkstemp is already 0600; stated so the guarantee does not depend on it
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return tmp_name


def _write_all_private(files: dict[Path, str]) -> None:
    """Write every file or none: stage ALL temp files first, then move them into place.

    A failure while staging (full disk, a destination that is a directory) removes every temp file and leaves
    the output directory as it was. The final ``os.replace`` calls are the only step that could stop part-way;
    they are same-directory renames, and any failure there is reported as ``FillError`` rather than a traceback.

    Raises:
        FillError: Any filesystem failure.
    """
    staged: list[tuple[str, Path]] = []
    try:
        for destination, text in files.items():
            if destination.is_dir():
                raise FillError(f"{destination.name} already exists as a directory in the output directory")
            staged.append((_stage_private(destination, text), destination))
        for tmp_name, destination in staged:
            os.replace(tmp_name, destination)
    except OSError as error:
        raise FillError(f"could not write the output ({error.__class__.__name__})") from error
    finally:
        for tmp_name, _ in staged:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass  # already moved into place


def fill_prompts(
    src: Path,
    out: Path,
    *,
    last4: str,
    email: str,
    state_root: str = DEFAULT_STATE_ROOT,
    folders: Optional[dict[str, str]] = None,
) -> list[Path]:
    """Fill every prompt in ``src`` and write the private copies into ``out``; return the files written.

    All prompts are rendered and checked BEFORE the first file is written, so a bad prompt writes nothing.

    Raises:
        FillError: Any check above fails, ``src`` holds no prompts, or ``out`` is unsafe.
    """
    root = _checked_state_root(state_root)
    target = _checked_out_dir(src, out)
    sources = sorted(Path(src).glob(_PROMPT_GLOB))
    if not sources:
        raise FillError(f"no {_PROMPT_GLOB} files in the source directory")
    rendered = {
        path.name: render(
            path.read_text(encoding="utf-8"),
            name=path.name,
            last4=last4,
            email=email,
            state_root=root,
            folders=folders,
        )
        for path in sources
    }
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise FillError(f"could not create the output directory ({error.__class__.__name__})") from error
    files = {target / name: text for name, text in rendered.items()}
    _write_all_private(files)
    return list(files)


def main(argv: Optional[Sequence[str]] = None, environ: Optional[MutableMapping[str, str]] = None) -> int:
    """CLI entry point: 0 on success, 2 on any failure (nothing written)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="directory of *-backup.txt prompts")
    parser.add_argument("--out", type=Path, required=True, help="private output directory, outside this repo")
    parser.add_argument("--state-root", default=DEFAULT_STATE_ROOT, help="folder holding one state folder per task")
    parser.add_argument("--folder", action="append", default=[], metavar="TASK=FOLDER", help="state folder per task id")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        last4, email = resolve_values(env)
        written = fill_prompts(
            args.src,
            args.out,
            last4=last4,
            email=email,
            state_root=args.state_root,
            folders=parse_folders(args.folder),
        )
    except FillError as error:
        print(f"fill_perplexity_backup_prompts: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Filled {len(written)} prompts into {args.out} (owner-only files). Values are not printed.")
    for path in written:
        print(f"  {path.name}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
