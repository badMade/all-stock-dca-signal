"""Every workflow step that runs the GitHub CLI must give it a token.

Why (2026-11-15): compute-schedule.yml's "Check oscillation guard" step ran `gh pr list` with no
GH_TOKEN, so `gh` printed "To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN
environment variable" and exited 4. That step runs before anything else useful, so every scheduled
run (6 of 6, 2026-06 to 2026-11-01) failed and self-heal's cron was never re-tuned. Later steps in
the same file set GH_TOKEN, which is why the gap was easy to miss.

`gh` reads GH_TOKEN or GITHUB_TOKEN from the environment. It can come from the step, the job, or the
workflow `env:`; this test accepts any of the three. A step that authenticates some other way (none
does today) would need an explicit exception here, with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# A `gh` invocation at a command position: line start, or after a shell separator / substitution.
GH_CALL = re.compile(r"(?:^|[\s;&|(`$])gh\s+[a-z]", re.MULTILINE)
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


def _has_token(*envs: object) -> bool:
    return any(isinstance(env, dict) and any(var in env for var in TOKEN_VARS) for env in envs)


def _gh_steps_without_token(workflow_path: Path) -> list[str]:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
    missing = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        for index, step in enumerate(job.get("steps") or []):
            run = step.get("run")
            if not isinstance(run, str) or not GH_CALL.search(run):
                continue
            if not _has_token(step.get("env"), job.get("env"), workflow.get("env")):
                missing.append(f"{job_id} / {step.get('name') or f'step {index}'}")
    return missing


def test_workflows_are_discovered():
    assert WORKFLOWS, "no workflow files found; the path in this test is wrong"


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_every_gh_step_has_a_token(workflow_path):
    assert _gh_steps_without_token(workflow_path) == [], (
        f"{workflow_path.name}: these steps run `gh` with no GH_TOKEN/GITHUB_TOKEN in step, job or workflow env"
    )


def test_detector_flags_a_tokenless_gh_step(tmp_path):
    # Guards the guard: the exact shape of the 2026-11-01 failure must be caught.
    sample = tmp_path / "sample.yml"
    sample.write_text(
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        "      - name: Check oscillation guard\n"
        "        run: |\n"
        "          LAST=$(gh pr list --label x --json mergedAt)\n"
        "      - name: Tokened\n"
        "        env:\n"
        "          GH_TOKEN: ${{ github.token }}\n"
        "        run: gh pr list\n",
        encoding="utf-8",
    )
    assert _gh_steps_without_token(sample) == ["j / Check oscillation guard"]
