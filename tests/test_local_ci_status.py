"""Tests for scripts/local_ci_status.sh: the local CI run's result reaches GitHub truthfully.

The wrapper runs in a throwaway git repo with a stub `gh` (records every call) and a
stub local_ci.sh (exits with a chosen code), so nothing touches the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "local_ci_status.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git")


def _setup(tmp_path: Path, ci_exit: int, commit_known: bool = True) -> tuple[Path, Path, dict]:
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(WRAPPER, work / "scripts" / "local_ci_status.sh")
    ci = work / "scripts" / "local_ci.sh"
    ci.write_text(f'#!/usr/bin/env bash\necho "local_ci args: $*"\nexit {ci_exit}\n')
    ci.chmod(0o755)
    calls = tmp_path / "gh_calls.txt"
    gh = tmp_path / "gh"
    commits_rc = 0 if commit_known else 1
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        f'case "$*" in *"/commits/"*) exit {commits_rc} ;; esac\n'
        "exit 0\n"
    )
    gh.chmod(0o755)
    env = {**os.environ, "GH": str(gh), "LOCAL_CI_REPO": "owner/repo",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=work, check=True, env=env)
    return work, calls, env


def _run(work: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/local_ci_status.sh", *args], cwd=work, env=env,
                          capture_output=True, text=True)


def _states(calls: Path) -> list[str]:
    return [part.split("=", 1)[1] for line in calls.read_text().splitlines() if "/statuses/" in line
            for part in line.split() if part.startswith("state=")]


def test_pass_posts_pending_then_success_on_the_tested_commit(tmp_path):
    work, calls, env = _setup(tmp_path, ci_exit=0)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()
    result = _run(work, env, "--all")
    assert result.returncode == 0, result.stderr
    assert "local_ci args: --all" in result.stdout
    assert _states(calls) == ["pending", "success"]
    assert all(f"repos/owner/repo/statuses/{sha}" in line
               for line in calls.read_text().splitlines() if "/statuses/" in line)
    assert "context=local-ci" in calls.read_text()


def test_failure_posts_failure_and_keeps_the_exit_code(tmp_path):
    work, calls, env = _setup(tmp_path, ci_exit=3)
    result = _run(work, env, "--quick")
    assert result.returncode == 3
    assert _states(calls) == ["pending", "failure"]


def test_dirty_tree_is_refused_without_posting(tmp_path):
    work, calls, env = _setup(tmp_path, ci_exit=0)
    (work / "stray.txt").write_text("uncommitted\n")
    result = _run(work, env)
    assert result.returncode == 2
    assert "dirty" in result.stderr
    assert not calls.exists() or _states(calls) == []


def test_unpushed_commit_is_refused_without_posting(tmp_path):
    work, calls, env = _setup(tmp_path, ci_exit=0, commit_known=False)
    result = _run(work, env)
    assert result.returncode == 2
    assert "push it first" in result.stderr
    assert _states(calls) == []
