#!/usr/bin/env python3
"""
Computes the optimal schedule for the self-heal workflow based on repository telemetry.
Updates .github/self-heal-schedule.yml safely, and keeps any `# AUTO-UPDATED` cron line
in .github/workflows/self-heal.yml in sync with it.

Manual override: set `override: true` in .github/self-heal-schedule.yml (alongside a
`schedule:` value) to freeze the cadence. With the flag set, this script skips the
telemetry recompute entirely on every future run and only re-syncs the workflow file's
cron to the override value, so an operator's manually chosen schedule is never silently
replaced by the next telemetry-driven run (see SELF_HEAL_SETUP.md, "Overriding the
Schedule"; previously this script recomputed from telemetry unconditionally and
overwrote any manual edit whenever the computed value differed -- Codex review, PR #389).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Callable, Optional, Tuple


def run_cmd(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def compute_cadence():
    """
    Computes a cron schedule based on recent git activity.
    Uses `git log` to count commits in the last 30 days and chooses a coarse cadence.

    Raises RuntimeError if `git log` itself fails. That is deliberately NOT treated the
    same as `git log` succeeding with zero matching commits: `run_cmd` returns None on a
    nonzero exit, and folding that into "no recent commits" would silently propose (and
    the caller would then apply) the dormant cadence based on missing telemetry rather
    than a real activity signal -- e.g. history being unreadable in a shallow checkout
    would masquerade as "this repo is dormant" (Codex review, PR #389). Letting this
    propagate uncaught means the caller's process exits nonzero and the schedule file is
    never rewritten, which is the fail-closed behavior wanted here.
    """
    commits_output = run_cmd("git log --since='30 days ago' --format='%aI'")
    if commits_output is None:
        raise RuntimeError("`git log` failed; refusing to compute a cadence from missing telemetry.")

    if not commits_output:
        # `git log` succeeded with zero matching commits -- a real dormant signal,
        # unlike the command-failure case above.
        return "0 2 * * 0", "No recent commits, using fallback dormant cadence."

    commits = commits_output.split("\n")
    num_commits = len(commits)

    if num_commits > 50:
        # High velocity: daily at 03:00
        return "0 3 * * *", "High PR velocity (>50 commits/mo); running daily."
    elif num_commits > 10:
        # Active: twice a week (Mon, Thu)
        return "0 3 * * 1,4", "Active velocity; running twice a week."
    else:
        # Standard/Low: once a week
        return "0 3 * * 1", "Standard velocity; running weekly."


def update_schedule_file(schedule: str, rationale: str, file_path: str, *, override: bool = False) -> None:
    """(Over)write the schedule file. Plain writes -- no YAML library needed for this
    fixed, four-key shape (avoids the ruamel dependency: Codex review, PR #389)."""
    new_lines = [
        f"schedule: '{schedule}'\n",
        f"rationale: '{rationale}'\n",
        f"override: {'true' if override else 'false'}\n",
        "# AUTO-UPDATED\n",
    ]
    with open(file_path, "w") as f:
        f.writelines(new_lines)


def parse_schedule_file(file_path: str) -> Tuple[Optional[str], Optional[str], bool]:
    """Returns (schedule, rationale, override) parsed from the schedule file, or
    (None, None, False) if the file does not exist yet. A missing `override:` line
    (e.g. a file written before that key existed) defaults to `override=False`, which
    preserves the original always-recompute behavior for anyone who hasn't opted in."""
    if not os.path.exists(file_path):
        return None, None, False

    schedule = None
    rationale = None
    override = False
    with open(file_path, "r") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("schedule:"):
                schedule = stripped.split(":", 1)[1].strip().strip("'\"")
            elif stripped.startswith("rationale:"):
                rationale = stripped.split(":", 1)[1].strip().strip("'\"")
            elif stripped.startswith("override:"):
                override = stripped.split(":", 1)[1].strip().lower() == "true"
    return schedule, rationale, override


def sync_workflow_cron(workflow_file: str, schedule: str) -> bool:
    """Update the `# AUTO-UPDATED` cron line in the self-heal workflow to match
    `schedule`, via an in-process regex substitution rather than shelling out to
    `sed -i.bak`. The old `sed -i.bak` approach left a stray `self-heal.yml.bak` file
    behind that the caller's `git add .` would sweep into the generated PR (Codex
    review, PR #389). Returns True if the file was changed. A no-op (returns False) when
    the workflow has no `# AUTO-UPDATED` cron line to sync -- e.g. self-heal.yml
    currently has no `schedule:` trigger at all by design (see that file's own header),
    so this is a deliberate no-op until/unless one is reintroduced."""
    if not os.path.exists(workflow_file):
        return False

    with open(workflow_file, "r") as f:
        content = f.read()

    new_content, count = re.subn(
        r"^(\s*-\s*cron:\s*)'[^']*'(\s*# AUTO-UPDATED\s*)$",
        lambda m: f"{m.group(1)}'{schedule}'{m.group(2)}",
        content,
        flags=re.MULTILINE,
    )
    if count == 0 or new_content == content:
        return False

    with open(workflow_file, "w") as f:
        f.write(new_content)
    return True


def compute_and_sync(
    *,
    schedule_file: str,
    workflow_file: str,
    cadence_fn: Callable[[], Tuple[str, str]] = compute_cadence,
) -> int:
    current_schedule, _current_rationale, override = parse_schedule_file(schedule_file)

    if override:
        if not current_schedule:
            print("override: true but no schedule value set; nothing to sync.")
            return 0
        print(f"Manual override active ({current_schedule!r}); skipping telemetry recompute.")
        if sync_workflow_cron(workflow_file, current_schedule):
            print("Synced workflow file's cron to the manual override.")
        else:
            print("Workflow file's cron already matches the override (or has nothing to sync).")
        return 0

    new_schedule, rationale = cadence_fn()
    print(f"Computed schedule: {new_schedule} - {rationale}")

    if current_schedule == new_schedule:
        # Metadata already matches, but the workflow's own cron line could
        # still be stale (e.g. self-heal.yml was hand-edited independently),
        # so this must not return before syncing it (Codex review, PR #389).
        print("Schedule metadata unchanged.")
        if sync_workflow_cron(workflow_file, new_schedule):
            print("Workflow file's cron was out of sync; updated it to match.")
        return 0

    print(f"Updating schedule from {current_schedule} to {new_schedule}")
    os.makedirs(os.path.dirname(schedule_file) or ".", exist_ok=True)
    update_schedule_file(new_schedule, rationale, schedule_file)

    if sync_workflow_cron(workflow_file, new_schedule):
        print("Updated workflow file's cron to match.")

    print("Schedule update complete.")
    return 0


def main() -> None:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo_root)

    sys.exit(
        compute_and_sync(
            schedule_file=".github/self-heal-schedule.yml",
            workflow_file=".github/workflows/self-heal.yml",
        )
    )


if __name__ == "__main__":
    main()
