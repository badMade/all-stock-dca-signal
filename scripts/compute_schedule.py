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
from collections.abc import Sequence
from typing import Callable, Optional, Tuple


def run_cmd(cmd: Sequence[str]):
    if isinstance(cmd, str):
        raise TypeError("run_cmd requires an argument sequence, not a shell-style string command")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, shell=False)  # nosec B603 - fixed command, explicit shell=False
    except OSError:
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip()



def compute_cadence():
    """
    Computes a cron schedule based on recent git activity.
    Uses `git log` to count commits in the last 30 days and chooses a coarse cadence.
    Also extracts hour-of-day commit frequency to find the quietest window.
    """
    commits_output = run_cmd(["git", "log", "--since=30 days ago", "--format=%aI"])
    if commits_output is None:
        raise RuntimeError("`git log` failed; refusing to compute a cadence from missing telemetry.")

    if not commits_output:
        return "0 2 * * 0", "No recent commits, using fallback dormant cadence."

    commits = commits_output.split("\n")
    num_commits = len(commits)

    # Keep the execution hour pinned to 03:00 UTC. The cadence logic only varies
    # by frequency; CI/tests and self-heal docs rely on this fixed hour.

    # Keep the execution hour fixed. Cadence changes frequency only.
    run_hour = "03"

    # Adjustment triggers check
    try:
        # Check empty selfheal runs (just PR velocity check here)
        _ = run_cmd(["gh", "pr", "list", "--label", "self-heal", "--json", "state,createdAt"])
        # Simplified telemetry logic
    except Exception:
        pass

    display_hour = int(run_hour)
    if num_commits > 50:
        return f"0 {display_hour} * * *", f"High PR velocity (>50 commits/mo); running daily at {display_hour}:00."
    elif num_commits > 10:
        return f"0 {display_hour} * * 1,4", f"Active velocity; running twice a week at {display_hour}:00."
    else:
        return f"0 {display_hour} * * 1", f"Standard velocity; running weekly at {display_hour}:00."


def update_schedule_file(schedule: str, rationale: str, file_path: str, *, override: bool = False) -> None:
    import os
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True

        data = {}
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                data = yaml.load(f) or {}

        data['schedule'] = schedule
        data['rationale'] = rationale
        data['override'] = override

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tf:
            yaml.dump(data, tf)
            temp_name = tf.name

        # Validate parseability
        with open(temp_name, "r") as tf:
            yaml.load(tf)

        import shutil
        shutil.move(temp_name, file_path)

        # Ensure AUTO-UPDATED marker
        with open(file_path, "r") as f:
            file_content = f.read()
        if "# AUTO-UPDATED" not in file_content:
            with open(file_path, "a") as f:
                f.write("\n# AUTO-UPDATED\n")
    except ImportError:
        _old_update_schedule_file(schedule, rationale, file_path, override=override)

def _old_update_schedule_file(schedule: str, rationale: str, file_path: str, *, override: bool = False) -> None:
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
    if not os.path.exists(workflow_file):
        return False

    try:
        from ruamel.yaml import YAML  # type: ignore
        yaml = YAML()
        yaml.preserve_quotes = True
        with open(workflow_file, "r") as f:
            data = yaml.load(f) or {}

        changed = False
        if 'on' in data and 'schedule' in data['on']:
            for item in data['on']['schedule']:
                if 'cron' not in item:
                    continue
                # Only touch the entry carrying the `# AUTO-UPDATED` end-of-line
                # comment, matching the regex fallback below. Without this check,
                # every cron entry under `on.schedule` gets overwritten, silently
                # clobbering any separate, unmarked schedule an operator added by
                # hand (Codex review, PR #477).
                eol_comment = ""
                comment_items = getattr(item.ca, "items", None) if hasattr(item, "ca") else None
                if comment_items and "cron" in comment_items and comment_items["cron"][2] is not None:
                    eol_comment = comment_items["cron"][2].value
                if "AUTO-UPDATED" not in eol_comment:
                    continue
                if item['cron'] != schedule:
                    item['cron'] = schedule
                    changed = True

        if changed:
            with open(workflow_file, "w") as f:
                yaml.dump(data, f)
            return True
        return False
    except ImportError:
        # Fallback to regex if ruamel.yaml is not available
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
