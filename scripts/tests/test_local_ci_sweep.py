"""Failing-first tests: a sweeper that runs the local CI for every open PR and reports it as a commit status.

GitHub-hosted Actions are unavailable or deliberately skipped (draft PRs since #2067; the 2026-10-31 billing block),
and ``scripts/local_ci_status.sh`` already posts one ``local-ci`` commit status for one commit. This pins the loop
around it, ``scripts/local_ci_sweep.py``, which runs on the operator's Mac (that is where ``gh``, uv and the network
are) from a launchd timer. Contract:

* Selection is a pure function. Only branches of THIS repo are tested: a fork's code must never run on the Mac.
  Drafts are included. A commit that already has a ``local-ci`` result (success, failure) is not re-run, because the
  same code gives the same answer; a new head commit is. An ``error`` status (interrupted run) and a ``pending``
  status older than the stale limit (a run that died without posting) are re-run; a fresh ``pending`` is in flight.
* Each PR is tested in a throwaway git worktree under the work directory, never in the operator's checkout, and the
  worktree is always removed, even when the run fails or raises. One PR failing never stops the others.
* A lock stops two sweeps overlapping (a sweep can outlast the 30-minute timer); a stale lock is taken over.
* The default mode is the full core gate (no flag: lint, coverage-gated tests, ``scripts/tests``, demo), passed straight
  through to ``local_ci_status.sh``; 2026-11-01: a ``--quick`` pass skips the root suite, so it showed ``local-ci: success``
  on #2088 while 14 root tests failed. ``--quick`` stays opt-in. A failing CI is reported through
  the status, not the sweep's exit code. ``--dry-run`` lists what it would test and runs nothing.
* The launchd template runs it every 30 minutes from the checkout's venv Python, like the other agents.
"""

from __future__ import annotations

import json
import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import local_ci_sweep as sweep

REPO = "owner/repo"
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
TEMPLATE = Path(__file__).resolve().parents[2] / "deploy/launchd/com.all-stock-dca.local-ci-sweep.plist.template"


def pr(number=1, sha=None, *, head_repo=REPO, base_repo=REPO, draft=False):
    return sweep.PullRequest(
        number=number, head_sha=sha or f"{number:040x}", head_ref=f"branch-{number}",
        head_repo=head_repo, base_repo=base_repo, draft=draft,
    )


def status(state, *, minutes_ago=5, context="local-ci"):
    return {"state": state, "context": context, "created_at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def select(prs, statuses=None, **kw):
    return [p.number for p in sweep.select_prs(prs, statuses or {}, now=NOW, **kw)]


# -- selection ---------------------------------------------------------------------------------------------------


def test_a_fork_is_never_selected_and_a_draft_is():
    prs = [pr(1), pr(2, head_repo="stranger/repo"), pr(3, draft=True), pr(4, head_repo=None)]
    assert select(prs) == [1, 3]  # head repo None is a deleted fork: skipped, not crashed on


@pytest.mark.parametrize("state, selected", [
    ("success", False), ("failure", False), ("error", True),
])
def test_a_commit_with_a_finished_result_is_not_rerun_but_an_interrupted_one_is(state, selected):
    prs = [pr(1)]
    assert (select(prs, {prs[0].head_sha: [status(state)]}) == [1]) is selected


def test_a_fresh_pending_run_is_in_flight_and_a_stale_one_is_rerun():
    prs = [pr(1), pr(2)]
    statuses = {prs[0].head_sha: [status("pending", minutes_ago=10)],
                prs[1].head_sha: [status("pending", minutes_ago=300)]}
    assert select(prs, statuses) == [2]


def test_only_the_newest_local_ci_status_of_a_commit_counts_and_other_contexts_are_ignored():
    p = pr(1)
    old_failure_then_success = [status("failure", minutes_ago=60), status("success", minutes_ago=5)]
    assert select([p], {p.head_sha: old_failure_then_success}) == []
    success_then_error = [status("success", minutes_ago=60), status("error", minutes_ago=5)]
    assert select([p], {p.head_sha: success_then_error}) == [1]
    assert select([p], {p.head_sha: [status("success", context="ci/other")]}) == [1]


def test_a_new_head_commit_is_tested_even_though_the_old_one_has_a_result():
    new = pr(1, sha="b" * 40)
    assert select([new], {"a" * 40: [status("success")]}) == [1]


def test_selection_is_oldest_pr_first_and_capped():
    assert select([pr(5), pr(2), pr(9)], max_prs=2) == [2, 5]


def test_parse_pulls_reads_the_github_api_shape():
    raw = [
        {"number": 7, "draft": True, "head": {"sha": "c" * 40, "ref": "feat", "repo": {"full_name": REPO}},
         "base": {"repo": {"full_name": REPO}}},
        {"number": 8, "draft": False, "head": {"sha": "d" * 40, "ref": "x", "repo": None},
         "base": {"repo": {"full_name": REPO}}},
    ]
    parsed = sweep.parse_pulls(raw)
    assert [(p.number, p.head_sha, p.head_repo, p.draft) for p in parsed] == [
        (7, "c" * 40, REPO, True), (8, "d" * 40, None, False)]


# -- running -----------------------------------------------------------------------------------------------------


class Recorder:
    """Stands in for git and for the local CI command; records every call."""

    def __init__(self, *, rc=None, boom=()):
        self.git_calls, self.runs = [], []
        self.rc = rc or {}
        self.boom = set(boom)

    def git(self, args, cwd=None):
        self.git_calls.append((tuple(args), cwd))
        return ""

    def run(self, cmd, cwd):
        self.runs.append((tuple(cmd), cwd))
        number = int(Path(cwd).name.removeprefix("pr-").split("-")[0])
        if number in self.boom:
            raise RuntimeError("runner exploded")
        return self.rc.get(number, 0)


def do_sweep(tmp_path, prs, statuses=None, rec=None, **kw):
    rec = rec or Recorder()
    result = sweep.run_sweep(
        prs=prs, statuses=statuses or {}, git=rec.git, run=rec.run, workdir=tmp_path / "work",
        lock_path=tmp_path / "sweep.lock", now=NOW, **kw)
    return result, rec


def test_each_pr_is_tested_in_its_own_worktree_with_the_full_core_gate_by_default(tmp_path):
    result, rec = do_sweep(tmp_path, [pr(1), pr(2)])
    assert [Path(cwd).parent for _, cwd in rec.runs] == [tmp_path / "work"] * 2  # under the work dir only
    assert all(cmd[0].endswith("scripts/local_ci_status.sh") and cmd[1:] == () for cmd, _ in rec.runs)
    adds = [c for c, _ in rec.git_calls if c[:2] == ("worktree", "add")]
    assert len(adds) == 2 and all("--detach" in c for c in adds)
    assert adds[0][-1] == pr(1).head_sha  # the exact commit that gets the status
    assert [(r.number, r.outcome) for r in result.results] == [(1, "ran"), (2, "ran")]


def test_a_requested_mode_is_passed_straight_through(tmp_path):
    _, rec = do_sweep(tmp_path, [pr(1)], mode_args=("--changed",))
    assert rec.runs[0][0][1:] == ("--changed",)


def test_the_worktree_is_removed_after_a_failing_run_and_after_an_exception(tmp_path):
    result, rec = do_sweep(tmp_path, [pr(1), pr(2), pr(3)], rec=Recorder(rc={1: 1}, boom={2}))
    removes = [c for c, _ in rec.git_calls if c[:2] == ("worktree", "remove")]
    assert len(removes) == 3 and all("--force" in c for c in removes)
    # One PR failing, or raising, never stops the others, and the failing CI is not a sweep error.
    assert [(r.number, r.outcome) for r in result.results] == [(1, "ran"), (2, "error"), (3, "ran")]
    assert result.results[0].returncode == 1


def test_the_operators_checkout_is_never_the_working_directory(tmp_path):
    _, rec = do_sweep(tmp_path, [pr(1)])
    repo_root = Path(__file__).resolve().parents[2]
    assert all(Path(cwd) != repo_root and repo_root not in Path(cwd).parents for _, cwd in rec.runs)


def test_a_fresh_lock_stops_the_sweep_and_a_stale_one_is_taken_over(tmp_path):
    lock = tmp_path / "sweep.lock"
    lock.write_text(json.dumps({"pid": 1, "started_at": (NOW - timedelta(minutes=10)).isoformat()}))
    result, rec = do_sweep(tmp_path, [pr(1)])
    assert result.skipped_reason == "locked" and rec.runs == [] and rec.git_calls == []
    lock.write_text(json.dumps({"pid": 1, "started_at": (NOW - timedelta(hours=6)).isoformat()}))
    result, rec = do_sweep(tmp_path, [pr(1)])
    assert result.skipped_reason is None and len(rec.runs) == 1


def test_the_lock_is_released_even_when_a_run_raises(tmp_path):
    do_sweep(tmp_path, [pr(1)], rec=Recorder(boom={1}))
    assert not (tmp_path / "sweep.lock").exists()


def test_nothing_to_test_runs_nothing_and_takes_no_worktree(tmp_path):
    p = pr(1)
    result, rec = do_sweep(tmp_path, [p], statuses={p.head_sha: [status("success")]})
    assert result.results == [] and rec.runs == [] and rec.git_calls == []


# -- command line ------------------------------------------------------------------------------------------------


def test_dry_run_lists_the_prs_it_would_test_and_runs_nothing(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(sweep, "_gh_json", lambda args: calls.append(tuple(args)) or [])
    monkeypatch.setattr(sweep, "list_open_prs", lambda repo: [pr(1), pr(2, head_repo="stranger/repo")])
    monkeypatch.setattr(sweep, "fetch_statuses", lambda repo, prs: {})
    rc = sweep.main(["--repo", REPO, "--dry-run", "--workdir", str(tmp_path / "w"), "--lock", str(tmp_path / "l")])
    out = capsys.readouterr().out
    assert rc == 0 and "#1" in out and "#2" not in out and "dry run" in out.lower()
    assert not (tmp_path / "w").exists()  # no worktree, no lock, nothing run


def test_a_locked_sweep_exits_3(tmp_path, monkeypatch):
    lock = tmp_path / "l"
    lock.write_text(json.dumps({"pid": 1, "started_at": datetime.now(timezone.utc).isoformat()}))
    monkeypatch.setattr(sweep, "list_open_prs", lambda repo: [pr(1)])
    monkeypatch.setattr(sweep, "fetch_statuses", lambda repo, prs: {})
    assert sweep.main(["--repo", REPO, "--workdir", str(tmp_path / "w"), "--lock", str(lock)]) == 3


# -- launchd -----------------------------------------------------------------------------------------------------


def _agent():
    text = TEMPLATE.read_text(encoding="utf-8").replace("REPLACE_WITH_REPO_PATH", "/repo")
    assert "REPLACE_WITH_" not in text, "unknown placeholder left in the template"
    return plistlib.loads(text.encode("utf-8"))


def test_the_launchd_agent_sweeps_every_30_minutes_from_the_checkout():
    agent = _agent()
    assert agent["Label"] == "com.all-stock-dca.local-ci-sweep"
    assert agent["ProgramArguments"][:3] == ["/repo/.venv/bin/python", "-m", "scripts.local_ci_sweep"]
    assert agent["WorkingDirectory"] == "/repo"
    assert agent["StartInterval"] == 1800
    assert "/opt/homebrew/bin" in agent["EnvironmentVariables"]["PATH"]  # gh and uv live there


# -- full core gate by default (2026-11-01) ----------------------------------------------------------------------
# The first real sweep tested #2088 with --quick and posted `local-ci: success`, although that PR carries 14 failing
# root tests (hosted CI failed on the same commit): --quick skips the coverage-gated root suite, where tests/ lives.


def test_the_default_mode_is_the_full_core_gate_and_quick_is_opt_in():
    assert sweep.DEFAULT_MODE == "core"
    assert sweep.MODES == {"core": (), "quick": ("--quick",), "changed": ("--changed",), "all": ("--all",)}


def test_run_sweep_with_no_mode_runs_local_ci_status_with_no_flag(tmp_path):
    _, rec = do_sweep(tmp_path, [pr(1)])
    assert [cmd[1:] for cmd, _ in rec.runs] == [()]


def _record_run_sweep(monkeypatch):
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return sweep.SweepResult()

    monkeypatch.setattr(sweep, "run_sweep", fake)
    monkeypatch.setattr(sweep, "list_open_prs", lambda repo: [pr(1)])
    monkeypatch.setattr(sweep, "fetch_statuses", lambda repo, prs: {})
    return seen


@pytest.mark.parametrize("argv, expected", [
    ([], ()),
    (["--mode", "core"], ()),
    (["--mode", "quick"], ("--quick",)),
    (["--mode", "changed"], ("--changed",)),
    (["--mode", "all"], ("--all",)),
])
def test_the_cli_passes_the_chosen_mode_through(tmp_path, monkeypatch, argv, expected):
    seen = _record_run_sweep(monkeypatch)
    rc = sweep.main(["--repo", REPO, "--workdir", str(tmp_path / "w"), "--lock", str(tmp_path / "l"), *argv])
    assert rc == 0 and tuple(seen["mode_args"]) == expected


def test_dry_run_names_the_mode_and_warns_that_quick_skips_the_root_suite(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sweep, "list_open_prs", lambda repo: [pr(1)])
    monkeypatch.setattr(sweep, "fetch_statuses", lambda repo, prs: {})
    base = ["--repo", REPO, "--dry-run", "--workdir", str(tmp_path / "w"), "--lock", str(tmp_path / "l")]
    assert sweep.main(base) == 0
    default_out = capsys.readouterr().out
    assert "full core gate" in default_out and "skips" not in default_out
    assert sweep.main([*base, "--mode", "quick"]) == 0
    quick_out = capsys.readouterr().out
    assert "--quick" in quick_out and "skips the coverage-gated root suite" in quick_out
