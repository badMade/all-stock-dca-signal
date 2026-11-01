#!/usr/bin/env python3
"""Run the local CI for every open PR and report each result as a ``local-ci`` commit status.

Why this exists (2026-10-31). GitHub-hosted Actions stopped starting on a billing block, and draft PRs skip CI
anyway (#2067), so a PR can sit with no signal at all. ``scripts/local_ci_status.sh`` already runs
``scripts/local_ci.sh`` for ONE commit and posts its result; nobody ran it for the other PRs. This is the loop
around it, meant to run on the operator's Mac from launchd every 30 minutes (``gh``, uv and the network are there,
the cloud sessions have none of them). See ``docs/local_ci.md``.

What it will and will not do:

* Only PRs whose head branch lives in THIS repo are tested. A fork's code never runs on the Mac. Drafts are
  included, because they are exactly what hosted CI skips.
* A commit that already has a ``local-ci`` ``success`` or ``failure`` is not re-run (same code, same answer); a new
  head commit is. ``error`` means the run was interrupted, so it is re-run, and so is a ``pending`` status older than
  ``DEFAULT_PENDING_MAX_AGE_MINUTES`` (a run that died without posting its result). A fresh ``pending`` is in flight.
* Every PR is tested in a throwaway ``git worktree`` under the work directory, never in the operator's checkout, and
  the worktree is always removed. One PR failing, or raising, never stops the others.
* A lock file stops two sweeps overlapping (one can outlast the timer); a lock older than
  ``DEFAULT_MAX_LOCK_AGE_MINUTES`` is taken over.
* A failing CI is reported through the commit status, not through this script's exit code. Exit 1 means a PR could
  not be tested at all (git or the runner broke); exit 3 means another sweep holds the lock.

CLI::

    python -m scripts.local_ci_sweep --dry-run          # list what would be tested, run nothing
    python -m scripts.local_ci_sweep                    # test up to 3 PRs with --quick
    python -m scripts.local_ci_sweep --mode changed --max-prs 5

Standard library only. The ``gh`` binary does the GitHub calls, like ``local_ci_status.sh``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

#: The status context ``scripts/local_ci_status.sh`` posts (it honours $LOCAL_CI_CONTEXT; the sweep reads the default).
STATUS_CONTEXT = "local-ci"
DEFAULT_MAX_PRS = 3
#: A ``pending`` status this old belongs to a run that died without posting success, failure or error.
DEFAULT_PENDING_MAX_AGE_MINUTES = 120
#: A sweep that has held the lock this long crashed; the next one takes over rather than waiting forever.
DEFAULT_MAX_LOCK_AGE_MINUTES = 240
#: One PR's local CI is a few minutes in --quick and far longer in --all; this only bounds a hung run.
RUN_TIMEOUT_SECONDS = 3 * 3600
MODES = {"quick": "--quick", "changed": "--changed", "all": "--all"}
EXIT_RAN_WITH_ERRORS = 1
EXIT_LOCKED = 3


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    head_ref: str
    head_repo: Optional[str]  # None when the head repository was deleted (a fork)
    base_repo: str
    draft: bool


@dataclass(frozen=True)
class PRResult:
    number: int
    sha: str
    outcome: str  # "ran" (local_ci_status.sh returned; its own status says pass or fail) or "error"
    returncode: Optional[int] = None
    detail: str = ""


@dataclass
class SweepResult:
    results: List[PRResult] = field(default_factory=list)
    skipped_reason: Optional[str] = None


# --------------------------------------------------------------------------- selection


def parse_pulls(raw: Sequence[Mapping[str, Any]]) -> List[PullRequest]:
    """Turn the GitHub ``GET /pulls`` payload into :class:`PullRequest` rows."""
    pulls = []
    for item in raw:
        head = item.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        base_repo = ((item.get("base") or {}).get("repo") or {}).get("full_name") or ""
        pulls.append(PullRequest(
            number=int(item["number"]), head_sha=str(head.get("sha") or ""), head_ref=str(head.get("ref") or ""),
            head_repo=head_repo, base_repo=base_repo, draft=bool(item.get("draft")),
        ))
    return pulls


def _parse_time(stamp: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _latest_local_ci(statuses: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The newest ``local-ci`` status of one commit; other contexts (hosted CI, bots) are not ours."""
    ours = [s for s in statuses if s.get("context") == STATUS_CONTEXT]
    stamped = [(s, _parse_time(s.get("created_at"))) for s in ours]
    stamped = [(s, t) for s, t in stamped if t is not None]
    return max(stamped, key=lambda pair: pair[1])[0] if stamped else (ours[0] if ours else None)


def select_prs(
    prs: Sequence[PullRequest],
    statuses: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    now: datetime,
    max_prs: int = DEFAULT_MAX_PRS,
    pending_max_age_minutes: int = DEFAULT_PENDING_MAX_AGE_MINUTES,
) -> List[PullRequest]:
    """The PRs that need a local CI run now, oldest first, at most ``max_prs``.

    ``statuses`` maps a head commit to its commit statuses. A fork is never selected: this sweep executes the
    PR's code on the operator's Mac, so only branches of this repository qualify.
    """
    chosen = []
    for pr in sorted(prs, key=lambda p: p.number):
        if pr.head_repo is None or pr.head_repo != pr.base_repo or not pr.head_sha:
            continue
        latest = _latest_local_ci(statuses.get(pr.head_sha, ()))
        state = latest.get("state") if latest else None
        if state in ("success", "failure"):
            continue  # same commit, same code: the answer would not change
        if state == "pending":
            started = _parse_time(latest.get("created_at"))
            if started is not None and now - started <= timedelta(minutes=pending_max_age_minutes):
                continue  # a run is in flight somewhere
        chosen.append(pr)
    return chosen[:max_prs]


# --------------------------------------------------------------------------- running


def _read_lock_age(lock_path: Path, now: datetime) -> Optional[timedelta]:
    """Age of the lock, or None when there is none or it cannot be read (an unreadable lock is stale)."""
    try:
        started = _parse_time(json.loads(lock_path.read_text(encoding="utf-8")).get("started_at"))
    except (OSError, ValueError, AttributeError):
        return None
    return now - started if started is not None else None


def _acquire_lock(lock_path: Path, now: datetime, max_age_minutes: int) -> bool:
    if lock_path.exists():
        age = _read_lock_age(lock_path, now)
        if age is not None and age <= timedelta(minutes=max_age_minutes):
            return False
        lock_path.unlink(missing_ok=True)  # stale or unreadable: the sweep that wrote it is gone
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": now.isoformat()}), encoding="utf-8")
    return True


def _test_one(
    pr: PullRequest, *, git: Callable[..., str], run: Callable[[Sequence[str], str], int], workdir: Path,
    mode_args: Sequence[str],
) -> PRResult:
    path = workdir / f"pr-{pr.number}-{pr.head_sha[:8]}"
    added = False
    try:
        # pull/N/head exists for same-repo branches too, and fetching it makes the exact commit available.
        git(["fetch", "origin", f"pull/{pr.number}/head"])
        git(["worktree", "add", "--detach", str(path), pr.head_sha])
        added = True
        returncode = run([str(path / "scripts" / "local_ci_status.sh"), *mode_args], str(path))
        return PRResult(pr.number, pr.head_sha, "ran", returncode=returncode)
    except Exception as exc:  # noqa: BLE001 - one PR's failure must never stop the sweep of the others
        return PRResult(pr.number, pr.head_sha, "error", detail=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if added:
            try:
                git(["worktree", "remove", "--force", str(path)])
            except Exception:  # noqa: BLE001 - cleanup is best effort; the directory is removed below regardless
                pass
        shutil.rmtree(path, ignore_errors=True)


def run_sweep(
    *,
    prs: Sequence[PullRequest],
    statuses: Mapping[str, Sequence[Mapping[str, Any]]],
    git: Callable[..., str],
    run: Callable[[Sequence[str], str], int],
    workdir: Path,
    lock_path: Path,
    now: datetime,
    mode_args: Sequence[str] = ("--quick",),
    max_prs: int = DEFAULT_MAX_PRS,
    pending_max_age_minutes: int = DEFAULT_PENDING_MAX_AGE_MINUTES,
    max_lock_age_minutes: int = DEFAULT_MAX_LOCK_AGE_MINUTES,
) -> SweepResult:
    """Test every selected PR in its own worktree; ``git`` and ``run`` are injected so tests never shell out."""
    selected = select_prs(prs, statuses, now=now, max_prs=max_prs, pending_max_age_minutes=pending_max_age_minutes)
    if not selected:
        return SweepResult()
    if not _acquire_lock(lock_path, now, max_lock_age_minutes):
        return SweepResult(skipped_reason="locked")
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        results = [_test_one(pr, git=git, run=run, workdir=workdir, mode_args=mode_args) for pr in selected]
        return SweepResult(results=results)
    finally:
        lock_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- GitHub and subprocess plumbing


def _gh_json(args: Sequence[str]) -> Any:
    """``gh api <args>`` parsed as JSON; raises on a gh failure so a broken login never reads as 'no PRs'."""
    done = subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True, timeout=120)
    return json.loads(done.stdout or "null")


def resolve_repo() -> str:
    done = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return done.stdout.strip()


def list_open_prs(repo: str) -> List[PullRequest]:
    pulls: List[PullRequest] = []
    for page in range(1, 6):  # 500 open PRs is far past anything this repo carries
        batch = _gh_json([f"repos/{repo}/pulls?state=open&per_page=100&page={page}"]) or []
        pulls.extend(parse_pulls(batch))
        if len(batch) < 100:
            break
    return pulls


def fetch_statuses(repo: str, prs: Sequence[PullRequest]) -> Dict[str, List[Mapping[str, Any]]]:
    """Commit statuses per head commit, for the PRs that could qualify (forks are never looked up)."""
    found: Dict[str, List[Mapping[str, Any]]] = {}
    for pr in prs:
        if pr.head_repo is None or pr.head_repo != pr.base_repo or not pr.head_sha:
            continue
        found[pr.head_sha] = _gh_json([f"repos/{repo}/commits/{pr.head_sha}/statuses?per_page=100"]) or []
    return found


def _real_git(args: Sequence[str], cwd: Optional[str] = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=600)
    return done.stdout


def _real_run(cmd: Sequence[str], cwd: str) -> int:
    # Output streams to this process's stdout/stderr, which launchd writes to the agent's log file.
    return subprocess.run(list(cmd), cwd=cwd, timeout=RUN_TIMEOUT_SECONDS).returncode


# --------------------------------------------------------------------------- cli


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", help="OWNER/NAME (default: the repository `gh` resolves from the checkout)")
    parser.add_argument("--mode", choices=sorted(MODES), default="quick", help="local_ci.sh mode (default quick)")
    parser.add_argument("--max-prs", type=int, default=DEFAULT_MAX_PRS, help="PRs to test per sweep")
    parser.add_argument("--dry-run", action="store_true", help="list the PRs that would be tested and run nothing")
    parser.add_argument("--workdir", default=str(Path(tempfile.gettempdir()) / "dca-local-ci-sweep"),
                        help="where the throwaway worktrees go")
    parser.add_argument("--lock", default=str(Path(tempfile.gettempdir()) / "dca-local-ci-sweep.lock"))
    args = parser.parse_args(argv)

    repo = args.repo or resolve_repo()
    now = datetime.now(timezone.utc)
    prs = list_open_prs(repo)
    statuses = fetch_statuses(repo, prs)

    if args.dry_run:
        picked = select_prs(prs, statuses, now=now, max_prs=args.max_prs)
        for pr in picked:
            print(f"#{pr.number} {pr.head_sha[:8]} {pr.head_ref}{' (draft)' if pr.draft else ''}")
        print(f"dry run: would test {len(picked)} PR(s) with {MODES[args.mode]}; nothing was run")
        return 0

    result = run_sweep(
        prs=prs, statuses=statuses, git=_real_git, run=_real_run, workdir=Path(args.workdir),
        lock_path=Path(args.lock), now=now, mode_args=(MODES[args.mode],), max_prs=args.max_prs,
    )
    if result.skipped_reason == "locked":
        print("local_ci_sweep: another sweep holds the lock; nothing run", file=sys.stderr)
        return EXIT_LOCKED
    for item in result.results:
        tail = f"exit {item.returncode}" if item.outcome == "ran" else item.detail
        print(f"local_ci_sweep: #{item.number} {item.sha[:8]} {item.outcome} ({tail})")
    if not result.results:
        print("local_ci_sweep: nothing to test")
    return EXIT_RAN_WITH_ERRORS if any(r.outcome == "error" for r in result.results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
