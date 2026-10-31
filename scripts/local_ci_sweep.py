"""Run the local CI for every open PR of this repo and report each as a ``local-ci`` commit status.

Runs on the operator's Mac (``gh``, uv and the network live there) from a launchd timer. For each selected PR it
checks the exact head commit out in a throwaway git worktree under the work directory and runs
``scripts/local_ci_status.sh`` there, which posts the result on that commit. A failing CI is reported through the
status, not through this script's exit code. See ``docs/local_ci.md``.

Usage:
    python -m scripts.local_ci_sweep [--repo OWNER/REPO] [--dry-run] [--max-prs N] [--quick | --changed | --all]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTEXT = "local-ci"
STALE_PENDING = timedelta(hours=2)  # a pending status older than this belongs to a run that died without posting
STALE_LOCK = timedelta(hours=2)
DEFAULT_MAX_PRS = 10
EXIT_LOCKED = 3
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    head_ref: str
    head_repo: str | None
    base_repo: str | None
    draft: bool = False


@dataclass(frozen=True)
class PRResult:
    number: int
    outcome: str  # "ran" or "error"
    returncode: int | None = None
    error: str | None = None


@dataclass
class SweepResult:
    results: list[PRResult] = field(default_factory=list)
    skipped_reason: str | None = None


def parse_pulls(raw: Iterable[Mapping]) -> list[PullRequest]:
    """Turn the GitHub pulls API payload into PullRequest rows; a deleted fork has ``head.repo`` null."""
    parsed = []
    for item in raw:
        head = item.get("head") or {}
        base = item.get("base") or {}
        parsed.append(PullRequest(
            number=int(item["number"]),
            head_sha=str(head.get("sha") or ""),
            head_ref=str(head.get("ref") or ""),
            head_repo=(head.get("repo") or {}).get("full_name"),
            base_repo=(base.get("repo") or {}).get("full_name"),
            draft=bool(item.get("draft", False)),
        ))
    return parsed


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _needs_run(statuses: Sequence[Mapping], now: datetime, stale_after: timedelta) -> bool:
    ours = [s for s in statuses if s.get("context") == CONTEXT and s.get("created_at")]
    if not ours:
        return True
    newest = max(ours, key=lambda s: _parse_time(s["created_at"]))
    state = newest.get("state")
    if state in ("success", "failure"):
        return False
    if state == "pending":
        return now - _parse_time(newest["created_at"]) > stale_after
    return True  # error (interrupted) or anything unrecognised: run again


def select_prs(
    prs: Iterable[PullRequest],
    statuses: Mapping[str, Sequence[Mapping]],
    *,
    now: datetime,
    max_prs: int = DEFAULT_MAX_PRS,
    stale_after: timedelta = STALE_PENDING,
) -> list[PullRequest]:
    """Pick the PRs to test: same-repo branches only (a fork's code never runs here), oldest first, capped."""
    chosen = []
    for p in sorted(prs, key=lambda p: p.number):
        if not p.head_sha or p.head_repo is None or p.head_repo != p.base_repo:
            continue
        if _needs_run(statuses.get(p.head_sha, ()), now, stale_after):
            chosen.append(p)
    return chosen[:max_prs]


# -- lock --------------------------------------------------------------------------------------------------------


def _acquire_lock(lock_path: Path, now: datetime) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "started_at": now.isoformat()})
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                started = _parse_time(json.loads(lock_path.read_text(encoding="utf-8"))["started_at"])
                if now - started <= STALE_LOCK:
                    return False
            except (OSError, ValueError, KeyError, TypeError):
                pass  # unreadable lock: treat as stale and take it over
            lock_path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return True
    return False


# -- running -----------------------------------------------------------------------------------------------------


def run_sweep(
    *,
    prs: Iterable[PullRequest],
    statuses: Mapping[str, Sequence[Mapping]],
    git: Callable[..., str],
    run: Callable[[Sequence[str], Path], int],
    workdir: Path,
    lock_path: Path,
    now: datetime,
    mode_args: Sequence[str] = ("--quick",),
    max_prs: int = DEFAULT_MAX_PRS,
) -> SweepResult:
    """Test each selected PR in its own worktree; one PR failing or raising never stops the others."""
    result = SweepResult()
    if not _acquire_lock(lock_path, now):
        result.skipped_reason = "locked"
        return result
    try:
        selected = select_prs(prs, statuses, now=now, max_prs=max_prs)
        if selected:
            workdir.mkdir(parents=True, exist_ok=True)
        for p in selected:
            tree = workdir / f"pr-{p.number}-{p.head_sha[:8]}"
            try:
                git(["fetch", "origin", f"pull/{p.number}/head"])
                git(["worktree", "add", "--detach", str(tree), p.head_sha])
                cmd = [str(tree / "scripts/local_ci_status.sh"), *mode_args]
                result.results.append(PRResult(p.number, "ran", returncode=run(cmd, tree)))
            except Exception as exc:  # noqa: BLE001 - one PR must never stop the sweep
                result.results.append(PRResult(p.number, "error", error=f"{type(exc).__name__}: {exc}"))
            finally:
                try:
                    git(["worktree", "remove", "--force", str(tree)])
                except Exception:  # noqa: BLE001
                    shutil.rmtree(tree, ignore_errors=True)
    finally:
        lock_path.unlink(missing_ok=True)
    return result


# -- GitHub and git ----------------------------------------------------------------------------------------------


def _gh_json(args: Sequence[str]):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)  # noqa: S603, S607
    return json.loads(proc.stdout)


def list_open_prs(repo: str) -> list[PullRequest]:
    raw: list = []
    page = 1
    while True:
        batch = _gh_json(["api", f"repos/{repo}/pulls?state=open&per_page=100&page={page}"])
        raw.extend(batch)
        if len(batch) < 100:
            return parse_pulls(raw)
        page += 1


def fetch_statuses(repo: str, prs: Iterable[PullRequest]) -> dict[str, list[dict]]:
    return {p.head_sha: _gh_json(["api", f"repos/{repo}/commits/{p.head_sha}/statuses?per_page=100"]) for p in prs
            if p.head_repo is not None and p.head_repo == p.base_repo}


def _git(args: Sequence[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd or REPO_ROOT, capture_output=True, text=True, check=True)
    return proc.stdout


def _run(cmd: Sequence[str], cwd: Path) -> int:
    return subprocess.run(list(cmd), cwd=cwd, check=False).returncode  # noqa: S603


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="OWNER/REPO (default: the checkout's GitHub repo)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be tested and run nothing")
    parser.add_argument("--max-prs", type=int, default=DEFAULT_MAX_PRS)
    parser.add_argument("--workdir", type=Path, default=Path(tempfile.gettempdir()) / "dca-local-ci-sweep")
    parser.add_argument("--lock", type=Path, default=Path(tempfile.gettempdir()) / "dca-local-ci-sweep.lock")
    mode = parser.add_mutually_exclusive_group()
    for flag in ("--quick", "--changed", "--all"):
        mode.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)

    mode_args = ("--changed",) if args.changed else ("--all",) if args.all else ("--quick",)
    repo = args.repo or _gh_json(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
    prs = list_open_prs(repo)
    statuses = fetch_statuses(repo, prs)
    now = datetime.now(timezone.utc)

    if args.dry_run:
        selected = select_prs(prs, statuses, now=now, max_prs=args.max_prs)
        print(f"dry run: {len(selected)} PR(s) would be tested with {' '.join(mode_args)}")
        for p in selected:
            print(f"  #{p.number} {p.head_ref} {p.head_sha[:8]}{' (draft)' if p.draft else ''}")
        return 0

    result = run_sweep(
        prs=prs, statuses=statuses, git=_git, run=_run, workdir=args.workdir, lock_path=args.lock, now=now,
        mode_args=mode_args, max_prs=args.max_prs)
    if result.skipped_reason == "locked":
        print("local_ci_sweep: another sweep is running; skipping.", file=sys.stderr)
        return EXIT_LOCKED
    for r in result.results:
        print(f"#{r.number}: {r.outcome}" + (f" (exit {r.returncode})" if r.returncode is not None else "")
              + (f" {r.error}" if r.error else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
