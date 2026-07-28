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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflow_policy import (  # noqa: E402
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


# --- Synthetic fixtures: the violation and exception mechanisms themselves ---

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
    import workflow_policy

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
