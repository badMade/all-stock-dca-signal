"""Tests for scripts/healthcheck.py.

The real checks shell out to ruff/pytest/run_dca.py -- exercising those for
real here would mean pytest running pytest inside itself. Every test instead
injects a fake runner (the same dependency-injection pattern
dca_live_adapters uses for broker calls) so the check *sequencing* and
*pass/fail aggregation* are verified without any real subprocess.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from healthcheck import ROOT, CheckSpec, build_checks, main, run_check  # noqa: E402


def _runner(returncode):
    def _fn(cmd, cwd):
        del cmd, cwd
        return SimpleNamespace(returncode=returncode)

    return _fn


def test_build_checks_default_single_interpreter_matches_ci_yml_checks_in_order():
    checks = build_checks()
    python = sys.executable
    assert [c.name for c in checks] == [
        f"Lint (ruff, core modules) [{python}]",
        f"Tests + coverage (core suite) [{python}]",
        f"Dry-run smoke test [{python}]",
        "Lint (ruff, production-safe-agent-loop)",
        "Tests (production-safe-agent-loop)",
        "Dependency audit (pip-audit)",
    ]
    for check in checks:
        assert isinstance(check, CheckSpec)
        assert check.cwd.exists()


def test_build_checks_repeats_matrix_checks_once_per_interpreter():
    checks = build_checks(matrix_pythons=("python3.11", "python3.12"))
    matrix_names = [c.name for c in checks if c.name.startswith("Lint (ruff, core modules)")]
    assert matrix_names == [
        "Lint (ruff, core modules) [python3.11]",
        "Lint (ruff, core modules) [python3.12]",
    ]


def test_build_checks_single_interpreter_checks_default_to_first_and_last_matrix_python():
    checks = build_checks(matrix_pythons=("python3.11", "python3.12"))
    psal_check = next(c for c in checks if c.name == "Lint (ruff, production-safe-agent-loop)")
    assert psal_check.cmd[0] == "python3.11"
    audit_check = next(c for c in checks if c.name == "Dependency audit (pip-audit)")
    assert audit_check.cmd[0] == "python3.12"


def test_build_checks_single_interpreter_checks_can_be_overridden_explicitly():
    checks = build_checks(matrix_pythons=("python3.11", "python3.12"), psal_python="pypy3", audit_python="pypy3")
    psal_check = next(c for c in checks if c.name == "Lint (ruff, production-safe-agent-loop)")
    assert psal_check.cmd[0] == "pypy3"


def test_build_checks_uses_a_fresh_temp_dir_per_call_when_demo_dir_omitted():
    first_demo_dir = build_checks()[2].cmd[-1]
    second_demo_dir = build_checks()[2].cmd[-1]
    assert first_demo_dir != second_demo_dir


def test_run_check_true_on_zero_exit(capsys):
    spec = CheckSpec("demo check", ["true"], ROOT)
    assert run_check(spec, runner=_runner(0)) is True
    assert "[OK] demo check" in capsys.readouterr().out


def test_run_check_false_on_nonzero_exit_and_reports_the_code(capsys):
    spec = CheckSpec("demo check", ["false"], ROOT)
    assert run_check(spec, runner=_runner(1)) is False
    assert "[FAIL] demo check (exit 1)" in capsys.readouterr().out


def test_main_returns_zero_when_every_check_passes():
    assert main(runner=_runner(0)) == 0


def test_main_returns_nonzero_when_any_single_check_fails():
    codes = iter([0, 0, 1, 0, 0, 0])

    def runner(cmd, cwd):
        del cmd, cwd
        return SimpleNamespace(returncode=next(codes))

    assert main(runner=runner) == 1


def test_main_forwards_matrix_pythons_to_build_checks():
    seen_cmds = []

    def runner(cmd, cwd):
        del cwd
        seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0)

    main(matrix_pythons=["python3.11", "python3.12"], runner=runner)
    assert seen_cmds[0][0] == "python3.11"
    # 3 matrix checks x 2 interpreters + 3 single-interpreter checks
    assert len(seen_cmds) == 9


def test_main_forwards_psal_and_audit_python_overrides():
    seen_cmds = []

    def runner(cmd, cwd):
        del cwd
        seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0)

    main(
        matrix_pythons=["python3.11", "python3.12"],
        psal_python="/venv-psal/bin/python",
        audit_python="/venv-audit/bin/python",
        runner=runner,
    )
    psal_cmd = next(c for c in seen_cmds if "production-safe-agent-loop" in " ".join(c) and "check" in c)
    assert psal_cmd[0] == "/venv-psal/bin/python"
    audit_cmd = next(c for c in seen_cmds if "pip_audit" in c)
    assert audit_cmd[0] == "/venv-audit/bin/python"
