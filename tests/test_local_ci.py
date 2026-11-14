"""scripts/local_ci.sh: the local stand-in for .github/workflows/ci.yml.

Added 2026-10-31, the day GitHub refused to start any Actions job ("recent account
payments have failed or your spending limit needs to be increased"): the script became
the only way to check a PR, so it has to keep covering every job CI would run.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "local_ci.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _script_siblings() -> list[str]:
    match = re.search(r'^SIBLINGS="([^"]+)"', SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "local_ci.sh lost its SIBLINGS list"
    return match.group(1).split()


def _ci_siblings() -> list[str]:
    match = re.search(r'projects="([^"]+)"', CI.read_text(encoding="utf-8"))
    assert match, "ci.yml's changes job lost its projects list"
    return match.group(1).split()


def _dry_run(*args: str) -> str:
    done = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_sibling_list_matches_ci_changes_job():
    # A sibling job added to ci.yml without a matching entry here would never run locally.
    assert _script_siblings() == _ci_siblings()


def test_every_ci_sibling_job_exists_for_each_listed_project():
    text = CI.read_text(encoding="utf-8")
    for project in _script_siblings():
        assert f"name: Lint & test ({project})" in text, project


def test_default_dry_run_is_the_core_job():
    out = _dry_run()
    assert "ruff check ." in out and "--cov-fail-under=90" in out
    assert "dry-run demo smoke test" in out and "scripts/tests" in out
    assert "pip-audit" not in out and "ruff + pytest:" not in out
    assert "Dry run: nothing was executed." in out


def test_all_dry_run_covers_both_audits_and_every_sibling_in_ci_order():
    out = _dry_run("--all")
    assert "pip-audit (pinned dev deps)" in out and "pip-audit (research dependency trees)" in out
    # Step headers only ("==> ..."): every name appears again in the closing summary.
    order = [line.split("ruff + pytest: ", 1)[1].strip() for line in out.splitlines() if line.startswith("==> ruff + pytest: ")]
    assert order == _ci_siblings()


def test_named_siblings_are_deduplicated_and_unknown_names_refused():
    out = _dry_run("ticker-advisor", "ticker-advisor", "--quick")
    assert out.count("==> ruff + pytest: ticker-advisor") == 1
    assert "coverage gate" not in out
    refused = subprocess.run(["bash", str(SCRIPT), "--dry-run", "not-a-project"], cwd=ROOT, capture_output=True,
                             text=True, check=False)
    assert refused.returncode == 2 and "not a CI sibling project" in refused.stderr


def test_pr_number_must_be_numeric():
    done = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--pr", "abc"], cwd=ROOT, capture_output=True,
                          text=True, check=False)
    assert done.returncode == 2


def test_registration_dry_run_runs_only_the_fast_registration_set():
    # The same set as ci.yml's "Registration checks" job (scripts/tests/test_workflow_policy.py
    # pins the two together): collection first, then the three registration test files.
    out = _dry_run("--registration")
    assert "--collect-only -q" in out
    for path in ("tests/test_dca_console_docs.py", "tests/test_dca_console_diagnostics.py",
                 "tests/test_config_invariants.py"):
        assert path in out, path
    assert "ruff check ." not in out and "--cov-fail-under" not in out
    assert "dry-run demo smoke test" not in out and "==> scripts/tests" not in out
    assert "Dry run: nothing was executed." in out


def test_registration_mode_refuses_options_that_would_widen_it():
    done = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--registration", "--all"], cwd=ROOT,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 2 and "--registration" in done.stderr
