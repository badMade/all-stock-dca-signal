"""Tests for registry.py: fingerprints, append-only lines, rollback lookup."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import registry as registry_module


def _report(status="published", model_version="v2", **overrides):
    report = {
        "model_version": model_version,
        "run_at": "2026-10-22T00:00:00+00:00",
        "status": status,
        "dataset": {"n_rows_all": 100, "n_rows_dip_days": 40},
        "evaluation": {"overall": {"model_mae": 0.012}},
    }
    report.update(overrides)
    return report


def _run_dir(tmp_path, name, *, with_model=True):
    run_dir = tmp_path / "models" / name
    run_dir.mkdir(parents=True)
    if with_model:
        (run_dir / "model.json").write_text('{"m": 1}', encoding="utf-8")
    return run_dir


def _context(**overrides):
    context = {
        "dataset_sha256": "d" * 64,
        "code_git_sha": "c" * 40,
        "feature_set_version": "pooled-ridge-v1",
        "prev_model_version": "v1",
        "prev_model_artifact_path": "models/v1/model.json",
        "deployed_at": "2026-10-22T00:01:00+00:00",
    }
    context.update(overrides)
    return context


def test_dataset_fingerprint_is_order_independent_and_tracks_content(tmp_path):
    (tmp_path / "AAA.csv").write_text("a", encoding="utf-8")
    (tmp_path / "BBB.csv").write_text("b", encoding="utf-8")
    first = registry_module.dataset_fingerprint(tmp_path, ["AAA", "BBB"], as_of_iso="2026-10-22")
    assert first == registry_module.dataset_fingerprint(tmp_path, ["bbb", "AAA"], as_of_iso="2026-10-22")
    assert first != registry_module.dataset_fingerprint(tmp_path, ["AAA", "BBB"], as_of_iso="2026-10-23")
    (tmp_path / "AAA.csv").write_text("changed", encoding="utf-8")
    assert first != registry_module.dataset_fingerprint(tmp_path, ["AAA", "BBB"], as_of_iso="2026-10-22")


def test_dataset_fingerprint_marks_a_missing_file_instead_of_raising(tmp_path):
    (tmp_path / "AAA.csv").write_text("a", encoding="utf-8")
    with_file = registry_module.dataset_fingerprint(tmp_path, ["AAA", "ZZZ"], as_of_iso="x")
    (tmp_path / "ZZZ.csv").write_text("z", encoding="utf-8")
    assert with_file != registry_module.dataset_fingerprint(tmp_path, ["AAA", "ZZZ"], as_of_iso="x")


def test_current_git_sha_reads_head_and_degrades_outside_a_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert registry_module.current_git_sha(repo) is None
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, check=True, env=env)
    sha = registry_module.current_git_sha(repo)
    assert sha is not None and len(sha) == 40


def test_incumbent_reference_handles_missing_and_malformed_config():
    assert registry_module.incumbent_reference(None) == {
        "prev_model_version": None,
        "prev_model_artifact_path": None,
    }
    assert registry_module.incumbent_reference({"model_version": 7, "model_artifact_path": ["x"]}) == {
        "prev_model_version": None,
        "prev_model_artifact_path": None,
    }
    assert registry_module.incumbent_reference({"model_version": "v1", "model_artifact_path": "p"}) == {
        "prev_model_version": "v1",
        "prev_model_artifact_path": "p",
    }


def test_build_entry_records_deployment_only_for_a_published_run(tmp_path):
    run_dir = _run_dir(tmp_path, "2026-10-22-aaaa1111")
    published = registry_module.build_entry(_report(), run_dir=run_dir, context=_context())
    assert published["deployed_at"] == "2026-10-22T00:01:00+00:00"
    assert published["model_json_sha256"] is not None
    assert published["model_mae"] == 0.012
    assert published["n_rows_dip_days"] == 40
    assert published["prev_model_version"] == "v1"

    for status in ("blocked_regression", "dry_run_not_published", "blocked_no_fresh_snapshot"):
        entry = registry_module.build_entry(_report(status=status), run_dir=run_dir, context=_context())
        assert entry["deployed_at"] is None


def test_build_entry_tolerates_a_missing_model_file_and_sparse_report(tmp_path):
    run_dir = _run_dir(tmp_path, "r", with_model=False)
    entry = registry_module.build_entry({"status": "blocked_no_fresh_snapshot"}, run_dir=run_dir, context={})
    assert entry["model_json_sha256"] is None
    assert entry["model_mae"] is None
    assert entry["feature_set_version"] == registry_module.FEATURE_SET_VERSION


def test_append_entry_is_append_only_one_line_per_call(tmp_path):
    models_dir = tmp_path / "models"
    registry_module.append_entry(models_dir, {"n": 1})
    registry_module.append_entry(models_dir, {"n": 2})
    lines = (models_dir / registry_module.REGISTRY_FILENAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_read_entries_skips_torn_and_non_object_lines(tmp_path):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / registry_module.REGISTRY_FILENAME).write_text('{"n": 1}\n{"n": \n[1,2]\n{"n": 3}\n', encoding="utf-8")
    assert [e["n"] for e in registry_module.read_entries(models_dir)] == [1, 3]
    assert registry_module.read_entries(tmp_path / "absent") == []


def test_rollback_target_resolves_the_replaced_models_record(tmp_path):
    models_dir = tmp_path / "models"
    v1 = {"model_version": "v1", "status": "published", "model_json_path": "m/v1/model.json",
          "prev_model_version": None}
    blocked = {"model_version": "v2x", "status": "blocked_regression", "model_json_path": "m/v2x/model.json",
               "prev_model_version": "v1"}
    v2 = {"model_version": "v2", "status": "published", "model_json_path": "m/v2/model.json",
          "prev_model_version": "v1"}
    for entry in (v1, blocked, v2):
        registry_module.append_entry(models_dir, entry)
    assert registry_module.latest_published(models_dir)["model_version"] == "v2"
    assert registry_module.rollback_target(models_dir)["model_version"] == "v1"


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"model_version": "v1", "status": "published", "prev_model_version": None}],
        [{"model_version": "v2", "status": "published", "prev_model_version": "gone",
          "model_json_path": "x"}],
    ],
)
def test_rollback_target_is_none_without_a_recorded_predecessor(tmp_path, entries):
    models_dir = tmp_path / "models"
    for entry in entries:
        registry_module.append_entry(models_dir, entry)
    assert registry_module.rollback_target(models_dir) is None
