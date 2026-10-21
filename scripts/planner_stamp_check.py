#!/usr/bin/env python3
"""Tell the operator when the ``backup_planner.pyz`` they pasted into Perplexity is stale.

Why this exists (2026-10-21). Step 0 of every backup prompt demands the planner's ``content_sha256``, and that
hash moves whenever any file inside the planner changes (it went ``2d4cadaf`` -> ``c1d28d01`` -> ``00ecf680`` ->
``d274f82e`` in one afternoon, each time for a change unrelated to the one being shipped). A stale planner costs a
missed slot, and nothing flagged it. A cloud routine cannot do this check: a scheduled session has no repo or GitHub
credentials (confirmed 2026-10-21), so it runs on the operator's Mac next to the checkout and next to the very
``backup_planner.pyz`` that was attached.

How it decides, with no stored state:

* **Baseline** is the stamp INSIDE the operator's own ``backup_planner.pyz`` (its ``BUILD_INFO.json``), so there is
  no baseline file to forget to update after a repaste.
* **Fresh stamp** is a planner built from ``origin/main`` after ``git fetch``. The ref is exported with
  ``git archive`` into a temporary directory and built there, so the checkout's working tree, branch and any
  uncommitted edits are never touched.

Exit codes: 0 unchanged (silent), 1 stale, 2 could not check. With ``--notify`` both 1 and 2 raise a macOS
notification: a failed check that stays silent looks exactly like "all good", which is the worst outcome.

The JSON report names hashes, a commit and a status only; it carries no filesystem path, account value or e-mail.

CLI::

    python -m scripts.planner_stamp_check --planner backup_planner.pyz --notify

Deployment: ``deploy/launchd/com.all-stock-dca.planner-stamp-check.plist.template`` (see
``docs/schedule_verification.md``). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from scripts.schedule_watchdog import notify_macos

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REF = "origin/main"
_HASH = re.compile(r"[0-9a-f]{64}")
_TIMEOUT = 120


class StampError(Exception):
    """The attached or current planner's stamp could not be established."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StampError("duplicate planner metadata field")
        result[key] = value
    return result


def pasted_stamp(path: Path) -> str:
    """Read validated build metadata without executing the attached planner."""
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist().count("BUILD_INFO.json") != 1:
                raise StampError("planner build metadata is missing or ambiguous")
            if archive.getinfo("BUILD_INFO.json").file_size > 1024 * 1024:
                raise StampError("planner build metadata is too large")
            info = json.loads(archive.read("BUILD_INFO.json"), object_pairs_hook=_unique_object)
            if not isinstance(info, dict):
                raise StampError("planner build metadata is not an object")
            files = info.get("files")
            if not isinstance(files, list) or not files or any(not isinstance(name, str) for name in files):
                raise StampError("planner file inventory is invalid")
            names = archive.namelist()
            if len(names) != len(set(names)) or not {"__main__.py", *files}.issubset(names):
                raise StampError("planner archive is incomplete or ambiguous")
            if archive.testzip() is not None:
                raise StampError("planner archive is corrupt")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, RecursionError) as exc:
        raise StampError("cannot read planner build metadata") from exc
    stamp = info.get("content_sha256")
    if not isinstance(stamp, str) or not _HASH.fullmatch(stamp):
        raise StampError("planner content stamp is invalid")
    return stamp


def _git(repo: Path, *args: str, stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], stdout=stdout, stderr=subprocess.PIPE,
            check=True, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Git errors can contain remote credentials and local paths. Never forward them to the report.
        raise StampError("git operation failed") from exc


def export_ref(repo: Path, ref: str, destination: Path) -> Path:
    """Export committed files to a new directory, leaving the branch and worktree untouched."""
    try:
        destination.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryFile() as stream:
            _git(repo, "archive", "--format=zip", ref, stdout=stream)
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                for member in archive.infolist():
                    path = PurePosixPath(member.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise StampError("unsafe path in git export")
                    # Unrelated symlinks are not planner sources; never follow one during extraction.
                    if stat.S_ISLNK(member.external_attr >> 16) or path.parts[:2] == (".github", "agents"):
                        continue
                    target = destination.joinpath(*path.parts)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("xb") as output:
                            shutil.copyfileobj(source, output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise StampError("cannot export planner sources") from exc
    return destination


def fresh_stamp(repo: Path, ref: str) -> tuple[str, str]:
    """Fetch the requested origin branch and build its exact commit in temporary storage."""
    if not ref.startswith("origin/") or not ref.removeprefix("origin/"):
        raise StampError("expected an origin branch")
    _git(repo, "fetch", "--no-tags", "origin", f"refs/heads/{ref.removeprefix('origin/')}")
    commit = _git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.decode("ascii").strip()
    try:
        with tempfile.TemporaryDirectory(prefix="planner-stamp-") as temporary:
            root = Path(temporary)
            exported = export_ref(repo, commit, root / "source")
            output = root / "backup_planner.pyz"
            # Use the fetched builder as well as its sources: its bootstrap is part of the stamp.
            subprocess.run(
                [sys.executable, "-I", str(exported / "scripts/build_backup_planner.py"),
                 "--out", str(output), "--source-commit", commit],
                cwd=exported, check=True, capture_output=True, timeout=_TIMEOUT,
            )
            return pasted_stamp(output), commit
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise StampError("cannot build current planner") from exc


def main(argv: list[str] | None = None) -> int:
    """Emit one privacy-safe JSON verdict; return 0 current, 1 stale, or 2 unavailable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--planner", type=Path, default=REPO / "backup_planner.pyz")
    parser.add_argument("--repo", type=Path, default=REPO, help="checkout to read (default: this one)")
    parser.add_argument("--ref", default=DEFAULT_REF, help="origin branch to compare (default: origin/main)")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args(argv)
    try:
        pasted = pasted_stamp(args.planner)
        latest, commit = fresh_stamp(args.repo, args.ref)
        code = int(pasted != latest)
        report = {
            "status": "STALE" if code else "UP_TO_DATE",
            "pasted": pasted, "latest": latest, "latest_commit": commit,
        }
        message = f"Planner {pasted[:8]} is stale; rebuild and repaste the latest planner ({latest[:8]})."
    except StampError:
        code = 2
        report = {"status": "ERROR", "error": "Could not check planner freshness."}
        message = "Could not check planner freshness. Run the check manually before the next slot."
    if args.notify and code:
        try:
            notify_macos("DCA planner stamp check", message)
        except (OSError, subprocess.SubprocessError):
            code = 2
            report = {"status": "ERROR", "error": "Could not deliver the planner notification."}
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
