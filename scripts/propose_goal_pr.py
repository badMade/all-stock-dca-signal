#!/usr/bin/env python3
"""Turn one saved goal proposal into a draft pull request against config/goals.json.

docs/adr-goals-and-loss-hedge-shadow.md, D5. The console's ``propose-goal`` job runs this; it
is the only step between a Goals-page proposal and ``config/goals.json``, and it never touches
the checkout the console runs from:

1. Re-validate the proposal (``dca_goals.validate_goal_proposal``). A proposal whose
   ``effective_from`` has passed since it was saved is refused: re-propose instead.
2. Fetch ``origin/main`` and add a throwaway, detached git worktree of it under ``--work-dir``.
3. Build the next document from the worktree's own ``config/goals.json``
   (``dca_goals.build_goals_document``). If main moved and the version no longer matches the
   proposal's, refuse: the operator re-proposes against the current goals.
4. Commit on ``goals/<goal_id>-v<N>``, push, and open a DRAFT PR with ``gh`` (AGENTS.md: every
   automated PR is a draft). The operator reviews and merges; nothing auto-merges (ADR Q4).
5. Remove the worktree, whatever happened.

``--dry-run`` stops after the commit and prints the diff. Stdlib only; never places orders.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import dca_goals  # noqa: E402

Runner = Callable[[Sequence[str], Path], str]


class ProposalError(Exception):
    """The proposal cannot be applied; nothing was pushed."""


def _run(argv: Sequence[str], cwd: Path) -> str:
    done = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if done.returncode != 0:
        raise ProposalError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {done.stderr.strip()[-500:]}")
    return done.stdout


def read_proposal(path: Path, *, now: datetime) -> Dict[str, Any]:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProposalError(f"cannot read proposal: {exc}") from exc
    if not isinstance(record, dict) or not isinstance(record.get("goal"), dict) \
            or isinstance(record.get("goal_version"), bool) or not isinstance(record.get("goal_version"), int):
        raise ProposalError("proposal file is malformed")
    try:
        dca_goals.validate_goal_proposal({"goal": record["goal"]}, now=now)
    except ValueError as exc:
        raise ProposalError(f"proposal is no longer valid ({exc}); propose it again") from exc
    return record


def next_document_text(current_text: str | None, record: Dict[str, Any]) -> str:
    """The new config/goals.json text, or ProposalError if main has moved past the proposal."""
    try:
        current = dca_goals.empty_document() if current_text is None else dca_goals.parse_document(current_text)
    except ValueError as exc:
        raise ProposalError(f"config/goals.json on main is unreadable ({exc}); fix it first") from exc
    document = dca_goals.build_goals_document(current, record["goal"])
    goal = next(g for g in document["goals"] if g.get("goal_id") == record["goal"]["goal_id"])
    if goal["goal_version"] != record["goal_version"]:
        raise ProposalError(f"main now holds a different version of {record['goal']['goal_id']} "
                            f"(would become v{goal['goal_version']}, proposal is v{record['goal_version']}); "
                            "propose again against the current goals")
    return json.dumps(document, indent=2) + "\n"


def pr_body(record: Dict[str, Any]) -> str:
    goal = record["goal"]
    lines = [f"Goal change proposed from the local console Goals page at {record.get('proposed_at', 'unknown time')}.",
             "", "```json", json.dumps(goal, indent=2), "```", "",
             "Review the diff to `config/goals.json`. Goals are evaluation inputs: no field changes a live dollar "
             "amount, and hedge mode can only be `off` or `shadow` (docs/adr-goals-and-loss-hedge-shadow.md)."]
    return "\n".join(lines)


def apply(record: Dict[str, Any], *, repo: Path, work_dir: Path, remote: str = "origin", base: str = "main",
          dry_run: bool = False, run: Runner = _run, log: Callable[[str], None] = print) -> str:
    goal_id = record["goal"]["goal_id"]
    version = record["goal_version"]
    branch = f"goals/{goal_id}-v{version}"
    work_dir.mkdir(parents=True, exist_ok=True)
    tree = work_dir / f"{goal_id}-v{version}-{int(time.time())}"
    run(["git", "fetch", remote, base], repo)
    run(["git", "worktree", "add", "--detach", str(tree), f"{remote}/{base}"], repo)
    try:
        goals_path = tree / "config" / dca_goals.GOALS_FILENAME
        current = goals_path.read_text(encoding="utf-8") if goals_path.is_file() else None
        goals_path.parent.mkdir(parents=True, exist_ok=True)
        goals_path.write_text(next_document_text(current, record), encoding="utf-8")
        run(["git", "checkout", "-B", branch], tree)
        run(["git", "add", "config/goals.json"], tree)
        run(["git", "commit", "-m", f"Goal {goal_id} v{version}", "-m", "Proposed from the console Goals page."], tree)
        if dry_run:
            log(run(["git", "show", "--stat", "--patch", "HEAD"], tree))
            return branch
        run(["git", "push", "-u", remote, branch], tree)
        url = run(["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
                   "--title", f"Goal {goal_id} v{version}", "--body", pr_body(record)], tree).strip()
        log(f"Opened draft PR: {url}")
        return url
    finally:
        try:
            run(["git", "worktree", "remove", "--force", str(tree)], repo)
        except ProposalError as exc:
            log(f"warning: could not remove worktree: {exc}")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        record = read_proposal(args.proposal, now=datetime.now(timezone.utc))
        apply(record, repo=args.repo, work_dir=args.work_dir, remote=args.remote, base=args.base,
              dry_run=args.dry_run)
    except ProposalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
