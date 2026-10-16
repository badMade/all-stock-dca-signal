#!/usr/bin/env python3
"""Gate 2 for a cloud run: prove the checkout is the pinned release and pull the evidence store.

A cloud container starts with a fresh checkout and an empty ``cron_tracking``
tree. Before a slot may trade this command proves two things and refuses
otherwise (exit 2, last stdout line ``GATE2_ABORT: <reason>``; success is exit 0
and ``PREFLIGHT_OK <sha>``):

* the code is the release a human pinned. ``$DCA_PINNED_SHA`` (a full lower-case
  40-character commit SHA) lives in the run environment, OUTSIDE the repo,
  because a file inside the checkout cannot vouch for itself. HEAD must equal it,
  the tree must have no modified tracked file, and the commit must be reachable
  from ``origin/main`` (nothing unreviewed trades);
* the evidence store was pulled and is complete enough to trust. The store is a
  private git repository whose ``main`` carries ``MANIFEST.json``. A store that
  is unreachable, has no manifest, or has an unusable one is a refusal, never an
  empty tree: an unreachable store must not look like a new day.

Before touching git it also refuses when the volume holding the evidence root has
less than ``MIN_FREE_BYTES`` free, so a full disk is named as the disk.

On success the stream artifact roots exist (git does not track empty
directories, and ``scripts/prior_reference_bindings.py`` aborts on a missing
root). This command never installs anything and never prints the account number.

Usage::

    python3 -m scripts.cloud_preflight --repo-root . --evidence-remote <url> \\
        --evidence-root "$DCA_CRON_TRACKING_ROOT" --trading-day 2026-09-29 \\
        [--expect-prior-slots 0935,1030] [--init]

``--init`` is for a human creating the very first store; it allows a store with
no manifest. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import dca_paths
from dca_config import _reject_duplicate_keys
from scripts.verify_schedule import ET, SLOT_EVIDENCE_LAYOUT, verify_day

__all__ = ["main"]

EXIT_OK = 0
EXIT_REFUSED = 2
PINNED_SHA_ENV = "DCA_PINNED_SHA"
ACCOUNT_ENV = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"
PYTHON_MINIMUM = (3, 11)
# 1 GiB: a tripwire for "disk effectively full", not a capacity plan. On 2026-10-16 a host with ~76 MB
# free could not install Python 3.11 and the 10:30 slot aborted without the disk ever being named.
MIN_FREE_BYTES = 1024**3
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_VERSION = 1
EVIDENCE_BRANCH = "main"
# The window for a slot is minutes wide, so a hung network call is worse than a
# refusal: bound every remote operation instead of waiting on the platform.
NETWORK_TIMEOUT_SECONDS = 60
_SHA_RE = re.compile(r"[0-9a-f]{40}")
# The afternoon stream keeps its legacy directory and writes its runs one level down.
_EXTRA_ROOTS = (("ce16ba6b", "scheduled_runs"),)


class Refused(Exception):
    """A precondition failed; the message is the one-line Gate 2 abort reason."""


def _scrub(text: str, env: Mapping[str, str]) -> str:
    """Never let the pinned account number reach an alert (2026-09-08 leak)."""
    account = env.get(ACCOUNT_ENV) or ""
    return text.replace(account, "[REDACTED]") if len(account) >= 6 else text


def _git(
    cwd: Path | str,
    env: Mapping[str, str],
    *args: str,
    network: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess:
    git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), env=git_env, capture_output=True, text=True,
            timeout=NETWORK_TIMEOUT_SECONDS if network else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise Refused(f"git {args[0]} timed out after {NETWORK_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise Refused(f"git is not runnable: {exc.__class__.__name__}") from exc
    if check and result.returncode != 0:
        if network:
            raise Refused(f"git {args[0]} remote operation failed")
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise Refused(f"git {args[0]} failed: {lines[-1] if lines else 'no output'}"[:300])
    return result


def _check_python() -> None:
    if tuple(sys.version_info[:2]) < PYTHON_MINIMUM:
        raise Refused(f"python {PYTHON_MINIMUM[0]}.{PYTHON_MINIMUM[1]} or newer is required")


def _check_disk(root: Path) -> None:
    """Refuse when the volume that will hold the evidence root has less than ``MIN_FREE_BYTES`` free."""
    # The root does not exist on a fresh container and disk_usage raises on a missing path, so
    # measure the nearest existing ancestor (same volume the clone will land on).
    probe = root.absolute()
    while not probe.exists():
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        # Unknown free space is not proof of enough space: fail closed.
        raise Refused(f"cannot read free disk space: {exc.__class__.__name__}") from exc
    if free < MIN_FREE_BYTES:
        raise Refused(f"free disk space is {free // 10**6} MB, below the {MIN_FREE_BYTES // 10**6} MB required")


def _pinned_sha(env: Mapping[str, str]) -> str:
    pin = env.get(PINNED_SHA_ENV, "")
    if not _SHA_RE.fullmatch(pin):
        raise Refused(f"{PINNED_SHA_ENV} is missing or is not a full lower-case 40-character commit SHA")
    return pin


def _verify_checkout(repo_root: Path, env: Mapping[str, str], pin: str) -> None:
    head = _git(repo_root, env, "rev-parse", "HEAD").stdout.strip()
    if head != pin:
        raise Refused(f"checkout HEAD {head[:12]} is not the pinned release {pin[:12]}")
    # Untracked, non-ignored files count too: an untracked sitecustomize.py or .pth would run
    # code the pinned commit does not contain (adversarial review of PR #1571).
    changes = _git(repo_root, env, "status", "--porcelain", "--untracked-files=normal").stdout.splitlines()
    if any(not line.startswith("??") for line in changes):
        raise Refused("checkout has modified tracked files, so it is not the pinned release")
    if changes:
        raise Refused("checkout has untracked files, so it is not the pinned release")
    # Reachability from origin/main is what "merged" means: a pin on an
    # unreviewed side branch must not trade even though HEAD equals it.
    fetched = _git(repo_root, env, "fetch", "origin", f"+refs/heads/{EVIDENCE_BRANCH}:refs/remotes/origin/{EVIDENCE_BRANCH}",
                   network=True, check=False)
    if fetched.returncode != 0:
        raise Refused("could not fetch origin/main to prove the pinned commit is merged")
    ancestor = _git(repo_root, env, "merge-base", "--is-ancestor", pin, f"origin/{EVIDENCE_BRANCH}", check=False)
    if ancestor.returncode != 0:
        raise Refused("the pinned commit is not an ancestor of origin/main")


def _pull_evidence(remote: str, root: Path, env: Mapping[str, str]) -> bool:
    """Clone or fast-forward the evidence store into ``root``; True when this call created ``root``."""
    if (root / ".git").is_dir():
        # Same container, second slot: fast-forward only, so an unpushed local
        # commit from a failed publish is never discarded.
        _git(root, env, "fetch", remote, EVIDENCE_BRANCH, network=True)
        merged = _git(root, env, "merge", "--ff-only", "FETCH_HEAD", check=False)
        if merged.returncode != 0:
            raise Refused("evidence root has diverged from the store; refusing to overwrite local evidence")
        return False
    if root.exists() and any(root.iterdir()):
        raise Refused("evidence root exists but is not an evidence clone")
    created = not root.exists()
    cloned = _git(root.parent if root.parent.exists() else Path.cwd(), env, "clone", "--branch", EVIDENCE_BRANCH,
                  remote, str(root), network=True, check=False)
    if cloned.returncode != 0:
        if created and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        raise Refused("evidence store is unreachable or has no main branch")
    return True


def _read_manifest(root: Path, *, allow_missing: bool) -> Optional[Dict[str, Any]]:
    path = root / MANIFEST_NAME
    if not path.is_file():
        if allow_missing:
            return None
        raise Refused("evidence manifest is missing; a human must initialise the store with --init")
    try:
        # Duplicate keys are refused, not resolved by file order: conflicting completeness
        # evidence must fail closed (Copilot review, PR #1571).
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise Refused("evidence manifest is unusable: not valid JSON or has a duplicate key") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION or not isinstance(
        manifest.get("days"), dict
    ):
        raise Refused(f"evidence manifest is unusable: expected version {MANIFEST_VERSION} with a days object")
    return manifest


def _check_prior_slots(root: Path, manifest: Optional[Dict[str, Any]], day: str, expected: Sequence[str]) -> None:
    if not expected:
        return
    unknown = [slot for slot in expected if slot not in SLOT_EVIDENCE_LAYOUT]
    if unknown:
        raise Refused(f"--expect-prior-slots names unknown slot(s) {','.join(unknown)}")
    entry = (manifest or {}).get("days", {}).get(day, {})
    published = entry.get("slots", []) if isinstance(entry, dict) else []
    missing = [slot for slot in expected if slot not in published]
    if missing:
        raise Refused(f"evidence manifest is missing prior slot(s) {','.join(missing)} for {day}")
    # A manifest entry is a claim, not proof. ensure_roots() below creates empty stream
    # directories and prior_reference_bindings treats an existing empty root as a complete
    # scan, so a slot listed without its artifacts would let a later slot size against no
    # prior allocation evidence (Copilot review, PR #1571). The same audit verify_schedule
    # runs decides: run_state names this day and slot, is scheduled evidence, and the
    # artifact contract holds.
    rows = {row["slot_id"]: row for row in verify_day(date.fromisoformat(day), datetime.now(ET), root=root)["slots"]}
    unverified = [slot for slot in expected if rows[slot]["status"] != "verified"]
    if unverified:
        raise Refused(f"evidence store has no verified run artifacts for prior slot(s) {','.join(unverified)} on {day}")


def _ensure_roots(root: Path) -> None:
    for directory in dca_paths.STREAM_ARTIFACT_DIRS.values():
        (root / directory).mkdir(parents=True, exist_ok=True)
    for parts in _EXTRA_ROOTS:
        root.joinpath(*parts).mkdir(parents=True, exist_ok=True)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--repo-root", required=True, help="the checkout to verify against the pin")
    parser.add_argument("--evidence-remote", required=True, help="URL or path of the private evidence repository")
    parser.add_argument("--evidence-root", required=True, help="where the evidence clone lives (= $DCA_CRON_TRACKING_ROOT)")
    parser.add_argument("--trading-day", required=True, help="YYYY-MM-DD, America/New_York")
    parser.add_argument("--expect-prior-slots", default="", help="comma-separated slot ids already published today")
    parser.add_argument("--init", action="store_true", help="human only: allow a store that has no manifest yet")
    return parser.parse_args(argv)


def _run(args: argparse.Namespace, env: Mapping[str, str]) -> str:
    _check_python()
    _check_disk(Path(args.evidence_root))
    pin = _pinned_sha(env)
    _verify_checkout(Path(args.repo_root), env, pin)
    root = Path(args.evidence_root)
    created = False
    try:
        created = _pull_evidence(args.evidence_remote, root, env)
        manifest = _read_manifest(root, allow_missing=args.init)
        expected: List[str] = [slot.strip() for slot in args.expect_prior_slots.split(",") if slot.strip()]
        _check_prior_slots(root, manifest, args.trading_day, expected)
    except Refused:
        # A refusal must leave nothing behind that a later slot could mistake
        # for a pulled store.
        if created and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        raise
    _ensure_roots(root)
    return pin


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    """Run the preflight; ``env`` defaults to the process environment (tests inject their own)."""
    active_env: Mapping[str, str] = os.environ if env is None else env
    try:
        args = _parse_args(argv)
        pin = _run(args, active_env)
    except Refused as exc:
        print(f"GATE2_ABORT: {_scrub(str(exc), active_env)}")
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 -- fail closed: an unexpected error must still be a Gate 2 abort
        print(f"GATE2_ABORT: unexpected {exc.__class__.__name__} during preflight")
        return EXIT_REFUSED
    print(f"PREFLIGHT_OK {pin}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
