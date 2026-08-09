"""Tests for scripts/self_heal.py.

Same injected-runner approach as test_healthcheck.py: no real ruff/git/pip
invocation, just verification of what gets run, in what order, and how the
pass/fail-plus-diff gate composes.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import self_heal  # noqa: E402
from healthcheck import ROOT  # noqa: E402


def _runner(returncode=0):
    def _fn(cmd, cwd):
        del cmd, cwd
        return SimpleNamespace(returncode=returncode)

    return _fn


def _capture_runner(stdout):
    """Same fake ``git status --porcelain`` output on every call."""

    def _fn(cmd, cwd):
        del cmd, cwd
        return SimpleNamespace(stdout=stdout)

    return _fn


def _capture_runner_sequence(*stdouts):
    """A different fake output each call -- main() checks git status both
    before the repair (should be clean) and after (should show the fix)."""
    remaining = list(stdouts)

    def _fn(cmd, cwd):
        del cmd, cwd
        return SimpleNamespace(stdout=remaining.pop(0) if remaining else stdouts[-1])

    return _fn


def _healthcheck_main(returncode):
    def _fn(matrix_pythons, psal_python, audit_python):
        del matrix_pythons, psal_python, audit_python
        return returncode

    return _fn


def test_lint_autofix_runs_both_lint_scopes_under_the_given_interpreter():
    calls = []

    def runner(cmd, cwd):
        calls.append((tuple(cmd), cwd))
        return SimpleNamespace(returncode=0)

    self_heal.lint_autofix(python="/some/python", runner=runner)

    assert len(calls) == 2
    assert calls[0][0][:4] == ("/some/python", "-m", "ruff", "check")
    assert calls[1][0][0] == "/some/python"
    assert "production-safe-agent-loop" in calls[1][0][-1]
    assert all(cwd == ROOT for _, cwd in calls)


def test_has_uncommitted_changes_true_when_git_status_is_non_empty():
    assert self_heal.has_uncommitted_changes(runner=_capture_runner(" M scripts/x.py\n")) is True


def test_has_uncommitted_changes_false_when_git_status_is_empty():
    assert self_heal.has_uncommitted_changes(runner=_capture_runner("")) is False


def test_main_returns_zero_when_healthcheck_passes_and_diff_present():
    code = self_heal.main(
        runner=_runner(0),
        capture_runner=_capture_runner_sequence("", " M scripts/healthcheck.py\n"),
        healthcheck_main=_healthcheck_main(0),
    )
    assert code == 0


def test_main_returns_one_when_healthcheck_still_fails_after_autofix():
    code = self_heal.main(
        runner=_runner(0),
        capture_runner=_capture_runner_sequence("", " M scripts/healthcheck.py\n"),
        healthcheck_main=_healthcheck_main(1),
    )
    assert code == 1


def test_main_returns_one_when_healthcheck_passes_but_nothing_changed():
    code = self_heal.main(
        runner=_runner(0),
        capture_runner=_capture_runner(""),
        healthcheck_main=_healthcheck_main(0),
    )
    assert code == 1


def test_main_refuses_to_run_when_tree_is_already_dirty_before_any_repair():
    """A pre-existing, unrelated dirty tree must not be misattributed to the
    repair -- has_uncommitted_changes() alone can't tell "already dirty" from
    "dirtied by ruff --fix" (Codex review, PR #379)."""
    autofix_calls = []
    original_lint_autofix = self_heal.lint_autofix
    self_heal.lint_autofix = lambda **kwargs: autofix_calls.append(kwargs)
    try:
        code = self_heal.main(
            runner=_runner(0),
            capture_runner=_capture_runner(" M some/preexisting/change.py\n"),
            healthcheck_main=_healthcheck_main(0),
        )
    finally:
        self_heal.lint_autofix = original_lint_autofix

    assert code == 1
    assert autofix_calls == []  # never even attempted a repair


def test_main_forwards_matrix_psal_and_audit_pythons_to_healthcheck():
    seen = []

    def healthcheck_main(matrix_pythons, psal_python, audit_python):
        seen.append((matrix_pythons, psal_python, audit_python))
        return 0

    self_heal.main(
        matrix_pythons=["python3.11", "python3.12"],
        psal_python="/venv-psal/bin/python",
        audit_python="/venv-audit/bin/python",
        runner=_runner(0),
        capture_runner=_capture_runner_sequence("", " M scripts/healthcheck.py\n"),
        healthcheck_main=healthcheck_main,
    )
    assert seen == [(["python3.11", "python3.12"], "/venv-psal/bin/python", "/venv-audit/bin/python")]


def test_main_uses_ruff_python_for_the_repair_itself():
    calls = []

    def runner(cmd, cwd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    self_heal.main(
        ruff_python="/venv-core/bin/python",
        runner=runner,
        capture_runner=_capture_runner_sequence("", " M scripts/healthcheck.py\n"),
        healthcheck_main=_healthcheck_main(0),
    )
    assert calls[0][0] == "/venv-core/bin/python"
