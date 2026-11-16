"""Regression tests for the self-heal workflow's repair guards."""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "self-heal.yml"
HEALTHCHECK = REPO_ROOT / "scripts" / "healthcheck.py"
SELF_HEAL = REPO_ROOT / "scripts" / "self_heal.py"


def test_healthcheck_writes_demo_artifacts_outside_the_worktree():
    # The smoke test used to write into an unignored repo path
    # (dca-demo-artifacts/), which the guard step's `git add --all` staged
    # and then rejected as out-of-scope, aborting every successful repair.
    # Writing to a tempdir avoids the problem entirely instead of relying on
    # the workflow to clean the path up before staging (Codex review, PR #507).
    healthcheck = HEALTHCHECK.read_text(encoding="utf-8")

    assert "tempfile.TemporaryDirectory" in healthcheck
    assert "dca-demo-artifacts" not in WORKFLOW.read_text(encoding="utf-8")


def test_repair_formatter_excludes_disallowed_claude_tree():
    # claude/ must stay out of self-heal's own repair commands (so a repair
    # can't touch a tree the guard's allowed-scope check would then reject),
    # but must NOT be excluded from [tool.ruff] globally -- that would also
    # exempt it from the ordinary `ruff check .` CI lint gate that covers the
    # tracked morning/midday signal harnesses under it (Codex review, PR #507).
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    self_heal = SELF_HEAL.read_text(encoding="utf-8")

    assert "claude" not in config["tool"]["ruff"]["extend-exclude"]
    assert '"ruff", "check", "--fix", "--extend-exclude", "claude"' in self_heal
    # `ruff format` has no --extend-exclude flag (unlike `ruff check`) -- passing
    # it is a hard CLI argument error, so the formatter must use plain --exclude
    # instead (Codex review, PR #569).
    assert '"ruff", "format", "--exclude", "claude"' in self_heal
    assert '"ruff", "format", "--extend-exclude", "claude"' not in self_heal


def _repair_job() -> dict:
    import yaml

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["repair"]


def test_repair_job_follows_the_ci_runner_variable():
    # 2026-11-15: CI moved to the self-hosted Mac runners via vars.CI_RUNS_ON. Self-heal's healthcheck
    # reruns the full suite, so it belongs on the same (unbilled) runners, with the hosted fallback kept.
    assert _repair_job()["runs-on"] == "${{ vars.CI_RUNS_ON || 'ubuntu-latest' }}"


def test_repair_job_timeout_fits_its_healthcheck_passes():
    # Pre-healthcheck + one per changed repair step (scripts/self_heal.py) + post-healthcheck is up to ~5
    # full parallel suite runs at ~6-12 min each on the Mac. The old 15-minute ceiling timed out every
    # in-scope run (17 of 436 triggers in 2026-11-08..15 ran ~15 min and were cancelled).
    assert _repair_job()["timeout-minutes"] == 90
