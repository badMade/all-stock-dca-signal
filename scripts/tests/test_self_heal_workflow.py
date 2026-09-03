"""Regression tests for the self-heal workflow's repair guards."""

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "self-heal.yml"
SCHEDULE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "compute-schedule.yml"
HEALTHCHECK = REPO_ROOT / "scripts" / "healthcheck.py"
SELF_HEAL = REPO_ROOT / "scripts" / "self_heal.py"


def _trigger_keys(workflow_path: Path) -> set[str]:
    # YAML 1.1 treats a bare `on` mapping key as the boolean True, not the
    # string "on" -- yaml.safe_load's parsed doc key is `True`, not "on"
    # (Codex review: "Check the YAML trigger instead of every comment" --
    # scanning the raw file text for "workflow_dispatch" also matches this
    # module's own trust-boundary comments explaining why the trigger is
    # deliberately absent, so every assertion here always failed).
    doc = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = doc.get(True, doc.get("on", {}))
    if isinstance(triggers, str):
        return {triggers}
    return set(triggers)


def test_write_enabled_workflows_cannot_be_dispatched_from_selectable_refs():
    assert "workflow_dispatch" not in _trigger_keys(WORKFLOW)
    assert "workflow_dispatch" not in _trigger_keys(SCHEDULE_WORKFLOW)


def test_trigger_parser_ignores_comments_but_detects_declared_dispatch(tmp_path: Path):
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "# workflow_dispatch is intentionally discussed in this comment\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "  schedule:\n"
        "    - cron: '0 3 * * 1'\n",
        encoding="utf-8",
    )

    assert _trigger_keys(workflow) == {"schedule", "workflow_dispatch"}


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
