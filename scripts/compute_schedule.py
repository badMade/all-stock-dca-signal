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

_CRON_EXPR_RE = re.compile(
    r"\A[0-9*,/-]+ +[0-9*,/-]+ +[0-9*,/-]+ +[0-9A-Za-z*,/-]+ +[0-9A-Za-z*,/-]+\Z",
    re.ASCII,
)


def _validate_cron_schedule(schedule: str) -> str:
    """Validate that a schedule string is a single-line 5-field POSIX cron expression.

    Args:
        schedule (str): Candidate cron expression.

    Returns:
        str: The validated cron expression.

    Raises:
        ValueError: If ``schedule`` is not a single-line 5-field cron expression.
    """
    if not isinstance(schedule, str) or not _CRON_EXPR_RE.fullmatch(schedule):
        raise ValueError(f"Invalid 5-field cron schedule: {schedule!r}")
    return schedule


def run_cmd(cmd: Sequence[str]) -> Optional[str]:
    """Executes a command and returns its standard output."""
    if isinstance(cmd, str):
        raise TypeError("run_cmd requires an argument sequence, not a shell-style string command")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, shell=False)  # nosec B603 - fixed command, explicit shell=False
    except OSError:
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip()



def compute_cadence() -> tuple[str, str]:
    """
    Computes a cron schedule based on recent git activity.
    Implements the 5-tier self-scheduling logic, dynamically computing
    intervals and finding the quietest contiguous window for the execution hour.

    Returns:
        tuple[str, str]: A tuple containing the cron schedule string and the rationale.
    """
    import collections
    import datetime
    import json

    # 1. Telemetry sources (rolling lookback window)
    commits_output = run_cmd(["git", "log", "--since=30 days ago", "--format=%aI"])
    if commits_output is None:
        raise RuntimeError("`git log` failed; refusing to compute a cadence from missing telemetry.")

    thirty_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
    pr_output = run_cmd(["gh", "pr", "list", "--state", "merged", "--search", f"merged:>={thirty_days_ago}", "--json", "mergedAt", "--limit", "100"])

    ci_output = run_cmd(["gh", "run", "list", "--workflow=ci", "--json", "conclusion", "--limit", "50"]) or ""
    ci_failure_rate = 0.0  # noqa: F841
    try:
        ci_runs = json.loads(ci_output)
        failures = sum(1 for r in ci_runs if r.get("conclusion") in ["failure", "timed_out", "action_required"])
        if len(ci_runs) > 0:
            ci_failure_rate = failures / len(ci_runs)  # noqa: F841
    except Exception:
        pass

    pr_count = 0
    if pr_output:
        try:
            pr_data = json.loads(pr_output)
            pr_count = len(pr_data)
        except Exception:
            pr_count = len(commits_output.split("\n")) if commits_output else 0
    else:
        # Fallback to git log commits as a proxy for velocity if gh fails
        pr_count = len(commits_output.split("\n")) if commits_output else 0

    commits = [c for c in commits_output.split("\n") if c] if commits_output else []

    # 2. Active-period detection
    hour_counts = collections.defaultdict(int)
    for c in commits:
        if "T" in c:
            try:
                hour = int(c.split("T")[1][:2])
                hour_counts[hour] += 1
            except (IndexError, ValueError):
                pass

    min_commits = float('inf')
    best_start_hour = 3 # default fallback
    for i in range(24):
        window_sum = sum(hour_counts[(i + j) % 24] for j in range(4))
        if window_sum < min_commits:
            min_commits = window_sum
            best_start_hour = i

    run_hour = str(best_start_hour)

    if not commits:
        return f"0 {run_hour} 1 * *", "No recent commits, using fallback dormant cadence."

    # 3. Adjustment triggers
    tier_adjustment = 0

    # Check for 3+ consecutive empty selfheal runs -> reduce frequency by 1 tier
    try:
        sh_runs_output = run_cmd(["gh", "run", "list", "--workflow=self-heal.yml", "--json", "conclusion,createdAt", "--limit", "10"])
        sh_prs_output = run_cmd(["gh", "pr", "list", "--label", "self-heal", "--state", "all", "--json", "createdAt", "--limit", "10"])

        if sh_runs_output and sh_prs_output:
            sh_runs = json.loads(sh_runs_output)
            sh_prs = json.loads(sh_prs_output)

            # Count runs that occurred after the most recent PR
            latest_pr_time = sh_prs[0]["createdAt"] if sh_prs else ""
            empty_runs_since_last_pr = 0

            for run in sh_runs:
                if run.get("createdAt", "") > latest_pr_time:
                    empty_runs_since_last_pr += 1
                else:
                    break

            if empty_runs_since_last_pr >= 3:
                tier_adjustment -= 1
    except Exception:
        pass

    # Check for 3+ consecutive successful PR merges -> increase frequency by 1 tier
    try:
        recent_prs_output = run_cmd(["gh", "pr", "list", "--state", "all", "--json", "state", "--limit", "3"])
        if recent_prs_output:
            recent_prs = json.loads(recent_prs_output)
            if len(recent_prs) >= 3 and all(pr.get("state") == "MERGED" for pr in recent_prs):
                tier_adjustment += 1
    except Exception:
        pass

    # 4. Cadence tiers (computed from telemetry)
    tier = 2 # 0: dormant, 1: low, 2: standard, 3: active, 4: high

    if pr_count > 30:
        tier = 4
    elif pr_count > 15:
        tier = 3
    elif pr_count > 5:
        tier = 2
    elif pr_count > 0:
        tier = 1
    else:
        tier = 0

    tier += tier_adjustment
    tier = max(0, min(4, tier))

    if tier == 4:
        return f"0 {run_hour} * * *", f"High PR velocity; running daily at {run_hour}:00."
    elif tier == 3:
        return f"0 {run_hour} */2 * *", f"Active velocity; running every 2 days at {run_hour}:00."
    elif tier == 2:
        return f"0 {run_hour} */3 * *", f"Standard velocity; running every 3 days at {run_hour}:00."
    elif tier == 1:
        return f"0 {run_hour} */7 * *", f"Low-churn velocity; running weekly at {run_hour}:00."
    else:
        return f"0 {run_hour} 1 * *", f"Dormant velocity; running monthly at {run_hour}:00."

def update_schedule_file(schedule: str, rationale: str, file_path: str, *, override: bool = False) -> None:
    """Updates the schedule file with the new schedule and rationale.

    Args:
        schedule (str): The cron schedule string.
        rationale (str): The rationale for the schedule.
        file_path (str): The path to the schedule file.
        override (bool, optional): Whether this is a manual override. Defaults to False.
    """
    _validate_cron_schedule(schedule)
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
        target_dir = os.path.dirname(os.path.abspath(file_path)) or "."
        temp_name: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=target_dir, delete=False) as tf:
                yaml.dump(data, tf)
                temp_name = tf.name

            with open(temp_name, "r") as f:
                file_content = f.read()
            if "# AUTO-UPDATED" not in file_content:
                with open(temp_name, "a") as f:
                    f.write("\n# AUTO-UPDATED\n")

            # Validate parseability before replacing the target file
            with open(temp_name, "r") as tf:
                yaml.load(tf)

            os.replace(temp_name, file_path)
            temp_name = None
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
    except ImportError:
        _old_update_schedule_file(schedule, rationale, file_path, override=override)

def _old_update_schedule_file(schedule: str, rationale: str, file_path: str, *, override: bool = False) -> None:
    """(Over)write the schedule file. Plain writes -- no YAML library needed for this
    fixed, four-key shape (avoids the ruamel dependency: Codex review, PR #389).

    Args:
        schedule (str): The cron schedule string.
        rationale (str): The rationale for the schedule.
        file_path (str): The path to the schedule file.
        override (bool, optional): Whether this is a manual override. Defaults to False.
    """
    _validate_cron_schedule(schedule)
    # Collapse newlines and escape single quotes so rationale cannot break out of the YAML scalar.
    safe_rationale = " ".join(rationale.splitlines()).replace("'", "''")
    new_lines = [
        f"schedule: '{schedule}'\n",
        f"rationale: '{safe_rationale}'\n",
        f"override: {'true' if override else 'false'}\n",
        "# AUTO-UPDATED\n",
    ]
    with open(file_path, "w") as f:
        f.writelines(new_lines)


def parse_schedule_file(file_path: str) -> Tuple[Optional[str], Optional[str], bool]:
    """Returns (schedule, rationale, override) parsed from the schedule file, or
    (None, None, False) if the file does not exist yet. A missing `override:` line
    (e.g. a file written before that key existed) defaults to `override=False`, which
    preserves the original always-recompute behavior for anyone who hasn't opted in.

    Args:
        file_path (str): The path to the schedule file.

    Returns:
        Tuple[Optional[str], Optional[str], bool]: The parsed schedule, rationale, and override flag.
    """
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
    """Synchronizes the workflow file's cron schedule.

    Args:
        workflow_file (str): The path to the workflow file.
        schedule (str): The new cron schedule string.

    Returns:
        bool: True if the file was modified, False otherwise.
    """
    _validate_cron_schedule(schedule)
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
    """Computes the schedule and synchronizes it with the workflow file.

    Args:
        schedule_file (str): The path to the schedule file.
        workflow_file (str): The path to the workflow file.
        cadence_fn (Callable[[], Tuple[str, str]], optional): Function to compute the cadence. Defaults to compute_cadence.

    Returns:
        int: Exit code (0 for success).
    """
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
    """Main entrypoint for the compute_schedule script."""
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
