"""Tests for scripts/workflow_policy.py.

Two kinds of test here, deliberately kept separate:

  * against the REAL workflow files -- every check must return zero
    violations today. These are the actual CI guardrails: a change to
    .github/workflows/*.yml that drops a timeout, a concurrency block, or
    persist-credentials: false fails the suite the same way a config
    invariant failure does.
  * against SYNTHETIC fixture documents -- exercising the violation and
    exception-mechanism code paths themselves. The real workflow files should
    have zero violations and zero exceptions, so the mechanism is unit-tested
    against small in-memory YAML rather than by inventing a fictional
    exemption in a real file just to prove the escape hatch works.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from scripts.workflow_policy import (  # noqa: E402
        PolicyException,
        _exceptions_for,
        _is_exempt,
        _normalize_on_key,
        check_concurrency,
        check_persist_credentials,
        check_timeout_minutes,
        load_workflows,
    )
except ModuleNotFoundError as _import_exc:  # pragma: no cover - fallback when running directly in scripts/
    # Only a failure to resolve the "scripts" package itself -- e.g. running
    # this file directly as `python scripts/tests/test_workflow_policy.py`
    # without the repo root on sys.path -- means package-style import
    # doesn't apply here and the flat, run-from-scripts/ import style is
    # needed instead. A ModuleNotFoundError for anything else (a real
    # missing dependency inside workflow_policy.py's OWN imports) must not
    # be silently reinterpreted as "package layout doesn't resolve" and
    # retried through a duplicate module instance under a different import
    # path, which would obscure the real failure and could test against a
    # second, separately-initialized copy of the module (Codex review, PR
    # #1166: "Narrow the remaining direct-import fallbacks"; AGENTS.md: "Do
    # not wrap imports in broad try/except blocks").
    if _import_exc.name not in ("scripts", "scripts.workflow_policy"):
        raise
    from workflow_policy import (  # type: ignore # noqa: E402
        PolicyException,
        _exceptions_for,
        _is_exempt,
        _normalize_on_key,
        check_concurrency,
        check_persist_credentials,
        check_timeout_minutes,
        load_workflows,
    )

REAL_DOCS = load_workflows()


def test_at_least_the_expected_workflows_are_present():
    """A guard on the guard: if the glob pattern or directory ever stopped
    matching real files, every check below would vacuously pass."""
    assert len(REAL_DOCS) >= 2, REAL_DOCS.keys()


# --- Real files: the actual CI guardrails -------------------------------------

def test_every_job_in_every_real_workflow_sets_a_timeout():
    violations = check_timeout_minutes(REAL_DOCS)
    assert not violations, "\n" + "\n".join(f"  - {v}" for v in violations)


def test_every_job_in_every_real_workflow_declares_concurrency():
    violations = check_concurrency(REAL_DOCS)
    assert not violations, "\n" + "\n".join(f"  - {v}" for v in violations)


def test_every_checkout_step_in_every_real_workflow_disables_persisted_credentials():
    violations = check_persist_credentials(REAL_DOCS)
    assert not violations, "\n" + "\n".join(f"  - {v}" for v in violations)


def test_no_real_workflow_declares_a_policy_exception():
    """Not a policy requirement -- a canary. If this starts failing, it means
    an exception was added for real, which is fine; it just means this test
    should be updated to name and justify it explicitly rather than silently
    letting exceptions accumulate unnoticed."""
    for file, doc in REAL_DOCS.items():
        assert "x-policy-exceptions" not in doc, (
            f"{file} declares x-policy-exceptions; update this test to assert "
            "on the specific exception(s) once one is added deliberately"
        )


def test_ci_audits_locked_and_independent_dependency_trees():
    """CI must audit sibling dependencies without expanding self-heal's scope."""
    root = Path(__file__).resolve().parents[2]
    audit = REAL_DOCS[".github/workflows/ci.yml"]["jobs"]["research-audit"]
    scope_check = next(
        step for step in REAL_DOCS[".github/workflows/self-heal.yml"]["jobs"]["repair"]["steps"]
        if step.get("id") == "scope_check"
    )
    match = re.search(r"grep -qE '([^']+)'", scope_check["run"])
    assert match is not None
    scope_pattern = match.group(1)
    assert re.search(scope_pattern, "Dependency & secret audit")
    assert re.search(scope_pattern, "Lint & test (core DCA) (3.12)")
    assert not re.search(scope_pattern, f"{audit['name']} (3.11)")
    assert not re.search(scope_pattern, f"{audit['name']} (3.12)")
    assert set(audit["strategy"]["matrix"]["python-version"]) == {"3.11", "3.12"}
    commands = "\n".join(step.get("run", "") for step in audit["steps"])
    assert "uv export --locked --all-extras --no-emit-project" in commands
    assert "pip-audit --locked" in commands
    assert "uv pip compile" in commands and "--no-build" in commands
    for manifest in (
        "production-safe-agent-loop/requirements.txt",
        "ticker-advisor/requirements.txt",
        "ml-forecaster/requirements.txt",
        "wondrous-octopus/requirements.txt",
        "ticker-market-breadth/requirements.txt",
        "trading-strategies/requirements.txt",
        "analysis/market-graph-3d/requirements.txt",
    ):
        assert (root / manifest).is_file(), f"Missing documented install manifest: {manifest}"
        assert manifest in commands, f"CI does not audit {manifest}"


def test_ci_runs_the_documented_market_breadth_suite():
    """The breadth tool's independent test/lint gates must run in CI too."""
    jobs = REAL_DOCS[".github/workflows/ci.yml"]["jobs"]
    assert "ticker-market-breadth" in jobs
    steps = jobs["ticker-market-breadth"]["steps"]
    assert any(
        step.get("working-directory") == "ticker-market-breadth"
        and "python -m pytest tests/" in step.get("run", "")
        for step in steps
    )
    assert any(
        "ruff check --isolated --select=E,F,B --line-length=120 ticker-market-breadth"
        in step.get("run", "")
        for step in steps
    )


def test_ci_minute_budget_triggers():
    """2026-10-31 Actions-spend cut: keep the cheap triggers cheap and the full run reachable.

    Pushes to main used to run 3.11 + 3.12 and every sibling project (~59 runner-minutes a push,
    ~60% of the repo's Actions minutes). Now only the weekday schedule / workflow_dispatch run the
    3.11 leg, the research audit runs only when a dependency manifest moved (or on those full runs),
    and every job's runner is switchable to a self-hosted one through the CI_RUNS_ON variable.
    """
    ci = REAL_DOCS[".github/workflows/ci.yml"]
    on = _normalize_on_key(ci)["on"]
    assert {"push", "pull_request", "schedule", "workflow_dispatch"} <= set(on)
    assert on["schedule"], "the catch-all scheduled run is what still exercises 3.11 and every sibling"
    jobs = ci["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]["python-version"]
    assert "'schedule'" in matrix and "'workflow_dispatch'" in matrix
    assert "'[\"3.11\", \"3.12\"]'" in matrix and "'[\"3.12\"]'" in matrix
    assert jobs["research-audit"]["needs"] == "changes"
    assert jobs["research-audit"]["if"] == "needs.changes.outputs.deps == 'true'"
    assert "deps" in jobs["changes"]["outputs"]
    diff = jobs["changes"]["steps"][0]["run"]
    assert "compare/$BEFORE...$AFTER" in diff, "pushes to main must diff their range, not run everything"
    for name, job in jobs.items():
        assert job["runs-on"] == "${{ inputs.runner || vars.CI_RUNS_ON || 'ubuntu-26.04' }}", name


def test_ci_python_setup_works_on_self_hosted_runners():
    """Every ci.yml job sets up Python through the local composite action (2026-11-01).

    actions/setup-python's macOS builds run `sudo installer`, which fails on the unprivileged
    self-hosted `ghrunner` user, so the action must use it only on GitHub-hosted runners and
    build a uv venv on self-hosted ones, without sudo.
    """
    root = Path(__file__).resolve().parents[2]
    jobs = REAL_DOCS[".github/workflows/ci.yml"]["jobs"]
    for name, job in jobs.items():
        for step in job.get("steps", []):
            assert step.get("uses") != "actions/setup-python@v7", f"{name} bypasses the composite action"
    action = yaml.safe_load((root / ".github/actions/setup-python/action.yml").read_text())
    steps = action["runs"]["steps"]
    hosted = [s for s in steps if s.get("uses", "").startswith("actions/setup-python@")]
    assert len(hosted) == 1 and hosted[0]["if"] == "runner.environment != 'self-hosted'"
    self_hosted = [s for s in steps if s.get("if") == "runner.environment == 'self-hosted'"]
    assert len(self_hosted) == 1
    assert "sudo" not in self_hosted[0]["run"]
    assert '/bin/uv" venv' in self_hosted[0]["run"] and "GITHUB_PATH" in self_hosted[0]["run"]
    dispatch = _normalize_on_key(REAL_DOCS[".github/workflows/ci.yml"])["on"]["workflow_dispatch"]
    assert dispatch["inputs"]["runner"]["default"] == ""



def test_registration_job_runs_on_every_pr_including_drafts():
    """2026-11-14: the fast registration gate must run where the core job does not.

    Draft PRs skip every other ci.yml job (the 2026-10-31 Actions-spend cut), and PRs were merged
    before the 25-35 minute core job finished, so registration breaks (an unindexed docs page, an
    account-shaped test literal, an unimportable test file, an unnoted module) reached main four
    times on 2026-11-13/14. This job is the cheap check that closes that gap, so it must stay
    unconditional (no draft `if`, no `needs` on the path-filtered `changes` job) and stay small.
    """
    root = Path(__file__).resolve().parents[2]
    ci = REAL_DOCS[".github/workflows/ci.yml"]
    on = _normalize_on_key(ci)["on"]
    assert "paths" not in on["pull_request"] and "paths-ignore" not in on["pull_request"]
    job = ci["jobs"]["registration"]
    assert job["name"] == "Registration checks"
    assert "if" not in job, "the registration job must run on draft PRs too"
    assert "needs" not in job, "the registration job must not wait on (or be filtered by) another job"
    assert job["timeout-minutes"] <= 10
    runs = [step.get("run", "") for step in job["steps"]]
    collect = [r for r in runs if "--collect-only" in r]
    assert len(collect) == 1 and "pytest" in collect[0]
    tests = [r for r in runs if "pytest" in r and "--collect-only" not in r]
    assert len(tests) == 1
    # The same set scripts/local_ci.sh --registration runs (tests/test_local_ci.py pins that side).
    script = (root / "scripts" / "local_ci.sh").read_text(encoding="utf-8")
    match = re.search(r'^REGISTRATION_TESTS="([^"]+)"', script, re.MULTILINE)
    assert match, "local_ci.sh lost its REGISTRATION_TESTS list"
    assert match.group(1).split() == [
        "tests/test_dca_console_docs.py",
        "tests/test_dca_console_diagnostics.py",
        "tests/test_config_invariants.py",
    ]
    for path in match.group(1).split():
        assert path in tests[0], f"ci.yml's registration job does not run {path}"
        assert (root / path).is_file(), path


# --- Synthetic fixtures: the violation and exception mechanisms themselves ---

def test_ci_gives_every_main_push_its_own_concurrency_group():
    """A push to main must never be superseded by a later push (#1901's run was cancelled unstarted).

    ``cancel-in-progress: false`` still lets GitHub drop an older run that is merely *pending* in a
    group when a newer one queues, so main's runs, which are the record of what landed (and, with the
    weekday scheduled run, where the 3.11 leg runs), must not share a group. Other refs keep one group per ref so a PR's
    older runs are still cancelled by its newer pushes.
    """
    concurrency = REAL_DOCS[".github/workflows/ci.yml"]["concurrency"]
    group = concurrency["group"]
    assert "github.sha" in group, group
    assert "refs/heads/main" in group, group
    assert "github.ref" in group, group
    assert concurrency["cancel-in-progress"] == "${{ github.ref != 'refs/heads/main' }}"


def test_missing_timeout_is_reported_with_file_and_job():
    docs = {"x.yml": {"jobs": {"build": {"steps": []}}}}
    violations = check_timeout_minutes(docs)
    assert len(violations) == 1
    assert violations[0].file == "x.yml"
    assert violations[0].job == "build"
    assert "timeout-minutes" in violations[0].message


def test_a_present_timeout_is_not_a_violation():
    docs = {"x.yml": {"jobs": {"build": {"timeout-minutes": 5, "steps": []}}}}
    assert check_timeout_minutes(docs) == []


def test_a_job_named_in_x_policy_exceptions_is_exempt_from_timeout():
    docs = {
        "x.yml": {
            "x-policy-exceptions": {
                "timeout-minutes": [{"job": "build", "reason": "synthetic test case"}]
            },
            "jobs": {"build": {"steps": []}},
        }
    }
    assert check_timeout_minutes(docs) == []


def test_an_exception_for_a_different_job_does_not_exempt_this_one():
    docs = {
        "x.yml": {
            "x-policy-exceptions": {
                "timeout-minutes": [{"job": "other-job", "reason": "synthetic test case"}]
            },
            "jobs": {"build": {"steps": []}},
        }
    }
    violations = check_timeout_minutes(docs)
    assert len(violations) == 1 and violations[0].job == "build"


def test_an_exception_missing_a_reason_is_rejected_rather_than_silently_ignored():
    """An exception with no stated reason defeats the point of requiring one --
    fail loudly at parse time rather than accept a bare job name."""
    docs = {
        "x.yml": {
            "x-policy-exceptions": {"timeout-minutes": [{"job": "build"}]},
            "jobs": {"build": {"steps": []}},
        }
    }
    with pytest.raises(ValueError, match="reason"):
        check_timeout_minutes(docs)


@pytest.mark.parametrize(
    "concurrency",
    [
        None,
        {},
        {"group": "g"},  # missing cancel-in-progress
        {"cancel-in-progress": True},  # missing group
        "not-even-a-mapping",
    ],
)
def test_concurrency_shapes_that_must_be_rejected(concurrency):
    doc = {"jobs": {"build": {"timeout-minutes": 5, "steps": []}}}
    if concurrency is not None:
        doc["concurrency"] = concurrency
    violations = check_concurrency({"x.yml": doc})
    assert len(violations) == 1, violations


def test_workflow_level_concurrency_covers_every_job_without_its_own():
    doc = {
        "concurrency": {"group": "g-${{ github.ref }}", "cancel-in-progress": True},
        "jobs": {"a": {"steps": []}, "b": {"steps": []}},
    }
    assert check_concurrency({"x.yml": doc}) == []


def test_job_level_concurrency_overrides_and_still_satisfies_the_policy():
    doc = {
        "jobs": {
            "a": {"concurrency": {"group": "g-a", "cancel-in-progress": False}},
        }
    }
    assert check_concurrency({"x.yml": doc}) == []


@pytest.mark.parametrize("value", [True, "true", None, "yes", 1])
def test_persist_credentials_values_that_must_be_rejected(value):
    doc = {
        "jobs": {
            "build": {
                "steps": [
                    {"name": "checkout", "uses": "actions/checkout@v4", "with": {"persist-credentials": value}}
                ]
            }
        }
    }
    violations = check_persist_credentials({"x.yml": doc})
    assert len(violations) == 1
    assert violations[0].step == "checkout"


def test_persist_credentials_false_is_accepted_in_either_yaml_spelling():
    for spelling in (False, "false"):
        doc = {
            "jobs": {
                "build": {
                    "steps": [
                        {
                            "name": "checkout",
                            "uses": "actions/checkout@v4",
                            "with": {"persist-credentials": spelling},
                        }
                    ]
                }
            }
        }
        assert check_persist_credentials({"x.yml": doc}) == [], spelling


def test_steps_that_are_not_actions_checkout_are_ignored():
    doc = {"jobs": {"build": {"steps": [{"name": "lint", "run": "ruff check ."}]}}}
    assert check_persist_credentials({"x.yml": doc}) == []


def test_a_step_scoped_exception_exempts_only_that_step():
    doc = {
        "x-policy-exceptions": {
            "persist-credentials": [
                {"job": "build", "step": "special checkout", "reason": "synthetic test case"}
            ]
        },
        "jobs": {
            "build": {
                "steps": [
                    {"name": "special checkout", "uses": "actions/checkout@v4", "with": {}},
                    {"name": "ordinary checkout", "uses": "actions/checkout@v4", "with": {}},
                ]
            }
        },
    }
    violations = check_persist_credentials({"x.yml": doc})
    assert len(violations) == 1
    assert violations[0].step == "ordinary checkout"


def test_a_job_scoped_exception_with_no_step_exempts_every_checkout_in_it():
    doc = {
        "x-policy-exceptions": {
            "persist-credentials": [{"job": "build", "reason": "synthetic test case"}]
        },
        "jobs": {
            "build": {
                "steps": [
                    {"name": "a", "uses": "actions/checkout@v4", "with": {}},
                    {"name": "b", "uses": "actions/checkout@v4", "with": {}},
                ]
            }
        },
    }
    assert check_persist_credentials({"x.yml": doc}) == []


# --- The exception primitives directly ----------------------------------------

def test_is_exempt_matches_job_and_optional_step():
    exceptions = [PolicyException(job="a", step=None, reason="r")]
    assert _is_exempt(exceptions, "a") is True
    assert _is_exempt(exceptions, "a", "any step") is True
    assert _is_exempt(exceptions, "b") is False


def test_exceptions_for_reads_the_named_policy_only():
    doc = {
        "x-policy-exceptions": {
            "timeout-minutes": [{"job": "a", "reason": "r"}],
            "concurrency": [{"job": "b", "reason": "r"}],
        }
    }
    assert [e.job for e in _exceptions_for(doc, "timeout-minutes")] == ["a"]
    assert [e.job for e in _exceptions_for(doc, "concurrency")] == ["b"]
    assert _exceptions_for(doc, "persist-credentials") == []


def test_exceptions_for_a_workflow_with_no_exceptions_block():
    assert _exceptions_for({}, "timeout-minutes") == []


# --- The on: / True YAML-1.1 gotcha --------------------------------------------

def test_on_key_is_normalized_from_the_yaml_boolean_it_parses_as():
    """PyYAML resolves the bare scalar `on` as the boolean True, so
    doc["on"] raises KeyError on completely ordinary workflow YAML unless
    this is normalized. Every real workflow file trips it."""
    doc = _normalize_on_key({True: {"push": {}}, "name": "x"})
    assert doc["on"] == {"push": {}}
    assert True not in doc


def test_on_key_normalization_does_not_clobber_a_literal_string_on_key():
    doc = _normalize_on_key({"on": {"push": {}}})
    assert doc["on"] == {"push": {}}


def test_every_real_workflow_has_a_normalized_on_key():
    for file, doc in REAL_DOCS.items():
        assert "on" in doc, f"{file}: missing 'on' after normalization"
        assert True not in doc, f"{file}: raw boolean key survived normalization"


# --- Both standard workflow-file extensions are discovered --------------------

def test_yaml_extension_files_are_also_loaded(tmp_path, monkeypatch):
    """GitHub Actions accepts .yml AND .yaml; a checker that only globbed one
    would stay green forever on a workflow using the other, because none of
    the policy checks would ever see it (Codex review, PR #219).

    load_workflows() keys results by path relative to REPO_ROOT, so REPO_ROOT
    is monkeypatched to tmp_path's parent for this one call -- otherwise a
    directory outside the real repo would make that relativization raise.
    """
    try:
        from scripts import workflow_policy
    except ModuleNotFoundError as _import_exc:  # pragma: no cover
        if _import_exc.name not in ("scripts", "scripts.workflow_policy"):
            raise
        import workflow_policy  # type: ignore

    monkeypatch.setattr(workflow_policy, "REPO_ROOT", tmp_path)
    (tmp_path / "a.yml").write_text("name: a\non: push\njobs: {}\n")
    (tmp_path / "b.yaml").write_text("name: b\non: push\njobs: {}\n")
    docs = workflow_policy.load_workflows(tmp_path)
    assert set(docs) == {"a.yml", "b.yaml"}
    assert docs["a.yml"]["name"] == "a"
    assert docs["b.yaml"]["name"] == "b"


def test_yaml_extension_is_included_without_duplicating_yml():
    """Guards the set-union glob against ever double-counting a real .yml
    file if a future glob change made the two patterns overlap."""
    docs = load_workflows()
    names = [Path(f).name for f in docs]
    assert len(names) == len(set(names)), names
