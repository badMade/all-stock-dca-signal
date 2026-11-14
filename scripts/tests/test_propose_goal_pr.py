"""scripts/propose_goal_pr.py: a console goal proposal becomes a draft PR, never a local edit.

Support tests for the approved goals ADR (docs/adr-goals-and-loss-hedge-shadow.md, D5). The git
steps run against a real temporary repository and bare remote; only ``gh`` is stubbed, because
opening a real pull request is exactly what a test must not do.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import propose_goal_pr as pr

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)
FUTURE = "2030-01-01T00:00:00-05:00"

CURRENT = {"$schema": "../schemas/goals.schema.json", "version": 1, "status": "active",
           "goals": [{"goal_id": "growth-twr-5y", "goal_version": 1, "kind": "aspirational", "metric": "twr_cagr",
                      "horizon_years": 5, "target": 0.20, "benchmark": "SP500_TR",
                      "effective_from": "2026-11-01T00:00:00-05:00"}]}


def record(version: int = 2, **overrides) -> dict:
    goal = {"goal_id": "growth-twr-5y", "kind": "aspirational", "metric": "twr_cagr", "horizon_years": 5,
            "target": 0.15, "benchmark": "SP500_TR", "effective_from": FUTURE}
    goal.update(overrides)
    return {"goal": goal, "goal_version": version, "proposed_at": NOW.isoformat()}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    checkout = tmp_path / "checkout"
    git("init", "-b", "main", str(checkout), cwd=tmp_path)
    (checkout / "config").mkdir()
    (checkout / "config" / "goals.json").write_text(json.dumps(CURRENT, indent=2) + "\n")
    git("add", ".", cwd=checkout)
    git("commit", "-m", "seed", cwd=checkout)
    git("remote", "add", "origin", str(origin), cwd=checkout)
    git("push", "-u", "origin", "main", cwd=checkout)
    return checkout, origin


def test_next_document_bumps_only_the_edited_goal():
    text = pr.next_document_text(json.dumps(CURRENT), record())
    document = json.loads(text)
    assert document["version"] == 2
    assert document["goals"][0]["goal_version"] == 2
    assert document["goals"][0]["target"] == pytest.approx(0.15)


def test_next_document_refuses_when_main_moved_past_the_proposal():
    with pytest.raises(pr.ProposalError, match="propose again"):
        pr.next_document_text(json.dumps(CURRENT), record(version=5))


def test_next_document_refuses_an_unreadable_main_file():
    with pytest.raises(pr.ProposalError, match="unreadable"):
        pr.next_document_text("{", record())


def test_a_new_goal_on_an_empty_main_starts_at_version_1():
    new = record(version=1, goal_id="growth-mwr")
    assert json.loads(pr.next_document_text(None, new))["goals"][0]["goal_version"] == 1


def test_read_proposal_revalidates_and_refuses_a_stale_effective_date(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(record(effective_from="2026-11-01T00:00:00-05:00")))
    with pytest.raises(pr.ProposalError, match="no longer valid"):
        pr.read_proposal(path, now=NOW)
    path.write_text(json.dumps({"goal": {}, "goal_version": True}))
    with pytest.raises(pr.ProposalError, match="malformed"):
        pr.read_proposal(path, now=NOW)
    path.write_text(json.dumps(record()))
    assert pr.read_proposal(path, now=NOW)["goal_version"] == 2


def test_dry_run_commits_in_a_throwaway_worktree_and_leaves_the_checkout_alone(repo, tmp_path):
    checkout, _ = repo
    before = (checkout / "config" / "goals.json").read_bytes()
    logs: list[str] = []
    branch = pr.apply(record(), repo=checkout, work_dir=tmp_path / "work", dry_run=True, log=logs.append)
    assert branch == "goals/growth-twr-5y-v2"
    assert (checkout / "config" / "goals.json").read_bytes() == before
    assert "goals/growth-twr-5y-v2" in git("branch", "--list", "goals/*", cwd=checkout)
    assert git("worktree", "list", cwd=checkout).count("\n") == 1, "the worktree must be removed"
    assert any('"target": 0.15' in line for line in logs)


def test_a_real_run_pushes_the_branch_and_opens_a_draft_pr(repo, tmp_path):
    checkout, origin = repo
    calls: list[list[str]] = []

    def run(argv, cwd):
        if argv[0] == "gh":
            calls.append(list(argv))
            return "https://example.invalid/pull/1\n"
        return pr._run(argv, cwd)

    url = pr.apply(record(), repo=checkout, work_dir=tmp_path / "work", run=run, log=lambda _: None)
    assert url == "https://example.invalid/pull/1"
    assert "goals/growth-twr-5y-v2" in git("branch", "--list", cwd=origin)
    assert len(calls) == 1 and "--draft" in calls[0], "every automated PR is a draft (AGENTS.md)"
    pushed = json.loads(git("show", "goals/growth-twr-5y-v2:config/goals.json", cwd=origin))
    assert pushed["goals"][0]["target"] == pytest.approx(0.15)
    assert json.loads(git("show", "main:config/goals.json", cwd=origin)) == CURRENT, "main is untouched"


def test_a_failure_still_removes_the_worktree(repo, tmp_path):
    checkout, _ = repo
    with pytest.raises(pr.ProposalError):
        pr.apply(record(version=9), repo=checkout, work_dir=tmp_path / "work", log=lambda _: None)
    assert git("worktree", "list", cwd=checkout).count("\n") == 1


def test_main_returns_2_on_a_bad_proposal(tmp_path, capsys):
    path = tmp_path / "p.json"
    path.write_text("{")
    assert pr.main(["--proposal", str(path), "--work-dir", str(tmp_path / "w")]) == 2
    assert "cannot read proposal" in capsys.readouterr().err
