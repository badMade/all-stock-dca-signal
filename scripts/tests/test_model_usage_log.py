"""Tests for scripts/model_usage_log.py.

Runnable with ``pytest scripts/tests`` or ``python scripts/tests/test_model_usage_log.py``.

Covers the model-usage audit-log writer that implements the
``model_usage_log`` contract in ``config/scheduled_tasks.json`` /
``schemas/model_usage_log.schema.json``:
  * entry construction, manifest lookup, and schema-shape validation
  * the append-only JSONL write and its YYYYMM path layout
  * the best-effort ``log_model_usage`` wrapper (opt-in root, never raises)
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_usage_log import (  # noqa: E402
    UnknownTaskError,
    append_model_usage_entry,
    build_entry,
    log_model_usage,
    resolve_log_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIGGERED = datetime(2026, 7, 21, 14, 30, 0, tzinfo=timezone.utc)

COVERED_TASK_IDS = (
    "2ba19526",
    "bb4f6ef1",
    "42173154",
    "7be82b97",
    "a6239dae",
    "10ce362c",
    "67aa802b",
    "a46bf007",
)


def test_build_entry_looks_up_manifest_fields():
    entry = build_entry(
        run_id="morning_live_20260721_1030ET",
        task_id="42173154",
        artifact_path="/home/user/workspace/cron_tracking/morning_dca/20260721_1030ET/run_state.json",
        triggered_at_utc=TRIGGERED,
    )
    assert entry["scheduled_for_et"] == "10:30"
    assert entry["stream"] == "morning"
    assert entry["task_name"] == "morning live execution"
    assert entry["model_actual"] == "not_exposed"
    assert entry["model_requested"] == "cost_conscious_default"
    assert entry["model_routing_mode"] == "cost_conscious"
    assert "reason_for_premium_model" not in entry


def test_build_entry_covers_every_manifest_task():
    for task_id in COVERED_TASK_IDS:
        entry = build_entry(
            run_id=f"run_{task_id}", task_id=task_id, artifact_path="/tmp/a", triggered_at_utc=TRIGGERED
        )
        assert entry["task_id"] == task_id
        assert entry["scheduled_for_et"]
        assert entry["stream"]
        assert entry["task_name"]


def test_build_entry_accepts_cron_id_alone():
    entry = build_entry(
        run_id="r1", cron_id="a46bf007", artifact_path="/tmp/a", triggered_at_utc=TRIGGERED
    )
    assert entry["cron_id"] == "a46bf007"
    assert "task_id" not in entry
    assert entry["stream"] == "watchdog"


def test_build_entry_requires_an_identifier():
    try:
        build_entry(run_id="r1", artifact_path="/tmp/a")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_build_entry_unknown_task_raises():
    try:
        build_entry(run_id="r1", task_id="does-not-exist", artifact_path="/tmp/a")
        raise AssertionError("expected UnknownTaskError")
    except UnknownTaskError:
        pass


def test_build_entry_premium_requires_valid_reason():
    for bad_kwargs in (
        {},  # missing reason entirely
        {"reason_for_premium_model": "because"},  # not an approved enum value
    ):
        try:
            build_entry(
                run_id="r1",
                task_id="10ce362c",
                artifact_path="/tmp/a",
                model_routing_mode="premium",
                triggered_at_utc=TRIGGERED,
                **bad_kwargs,
            )
            raise AssertionError(f"expected ValueError for {bad_kwargs}")
        except ValueError:
            pass

    ok = build_entry(
        run_id="r1",
        task_id="10ce362c",
        artifact_path="/tmp/a",
        model_routing_mode="premium",
        reason_for_premium_model="failure_recovery",
        triggered_at_utc=TRIGGERED,
    )
    assert ok["reason_for_premium_model"] == "failure_recovery"


def test_build_entry_rejects_premium_reason_on_non_premium():
    try:
        build_entry(
            run_id="r1",
            task_id="10ce362c",
            artifact_path="/tmp/a",
            model_routing_mode="cost_conscious",
            reason_for_premium_model="failure_recovery",
            triggered_at_utc=TRIGGERED,
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_resolve_log_path_uses_yyyymm_layout():
    path = resolve_log_path(TRIGGERED, root="/some/root")
    assert path == Path("/some/root/202607/model_usage.jsonl")


def test_append_model_usage_entry_writes_one_jsonl_line():
    entry = build_entry(
        run_id="r1", task_id="a6239dae", artifact_path="/tmp/a", triggered_at_utc=TRIGGERED
    )
    with tempfile.TemporaryDirectory() as td:
        path = append_model_usage_entry(entry, root=td)
        assert path == Path(td) / "202607" / "model_usage.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == entry

        # A second append goes to the same file as a new line.
        append_model_usage_entry(entry, root=td)
        lines_after = path.read_text(encoding="utf-8").splitlines()
        assert len(lines_after) == 2


def test_log_model_usage_root_none_is_a_noop():
    assert log_model_usage(root=None, run_id="r1", task_id="42173154", artifact_path="/tmp/a") is None


def test_log_model_usage_unknown_task_is_swallowed():
    with tempfile.TemporaryDirectory() as td:
        result = log_model_usage(root=td, run_id="r1", task_id="does-not-exist", artifact_path="/tmp/a")
        assert result is None
        assert list(Path(td).rglob("*.jsonl")) == []


def test_log_model_usage_writes_when_root_given():
    with tempfile.TemporaryDirectory() as td:
        path = log_model_usage(
            root=td, run_id="r1", task_id="67aa802b", artifact_path="/tmp/a", triggered_at_utc=TRIGGERED
        )
        assert path is not None
        assert path.exists()


def test_entry_matches_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO_ROOT / "schemas" / "model_usage_log.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    for task_id in COVERED_TASK_IDS:
        entry = build_entry(
            run_id=f"run_{task_id}", task_id=task_id, artifact_path="/tmp/a", triggered_at_utc=TRIGGERED
        )
        assert list(validator.iter_errors(entry)) == []
    premium_entry = build_entry(
        run_id="r1",
        task_id="10ce362c",
        artifact_path="/tmp/a",
        model_routing_mode="premium",
        reason_for_premium_model="safety_validation",
        triggered_at_utc=TRIGGERED,
    )
    assert list(validator.iter_errors(premium_entry)) == []


def test_cli_appends_entry_for_tasks_with_no_python_entry_point():
    # bb4f6ef1/e3e6f09e/a46bf007 have no in-repo executor to wire a
    # log_model_usage() call into -- they're run directly by the external
    # scheduler/agent, which shells out to this CLI instead.
    from model_usage_log import _main

    with tempfile.TemporaryDirectory() as td:
        rc = _main(
            [
                "--run-id", "watchdog_20260721_1540ET",
                "--task-id", "a46bf007",
                "--artifact-path", "/tmp/watchdog_report.json",
                "--triggered-at-utc", TRIGGERED.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--root", td,
            ]
        )
        assert rc == 0
        log_path = resolve_log_path(TRIGGERED, root=td)
        assert log_path.exists()
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["task_id"] == "a46bf007"
        assert entry["stream"] == "watchdog"


def test_cli_scheduled_for_et_override_beats_manifest_combined_default():
    # 7be82b97 fires at three ET slots from one cron entry, so its manifest
    # row carries the combined default "11:30,13:30,14:30" (Codex review:
    # "Expose the intraday slot override through the logging CLI"). Without
    # --scheduled-for-et every CLI-driven append for this task would log that
    # ambiguous combined value instead of the slot that actually fired.
    from model_usage_log import _main

    with tempfile.TemporaryDirectory() as td:
        rc = _main(
            [
                "--run-id", "intraday_live_20260727_1330ET",
                "--task-id", "7be82b97",
                "--artifact-path", "/tmp/intraday_report.json",
                "--scheduled-for-et", "13:30",
                "--triggered-at-utc", TRIGGERED.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--root", td,
            ]
        )
        assert rc == 0
        log_path = resolve_log_path(TRIGGERED, root=td)
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["task_id"] == "7be82b97"
        assert entry["scheduled_for_et"] == "13:30"


def test_cli_exits_nonzero_and_writes_nothing_for_unknown_task():
    from model_usage_log import _main

    with tempfile.TemporaryDirectory() as td:
        rc = _main(
            ["--run-id", "r1", "--task-id", "does-not-exist", "--artifact-path", "/tmp/a", "--root", td]
        )
        assert rc != 0
        assert list(Path(td).rglob("*.jsonl")) == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


def test_module_is_importable_with_only_scripts_on_sys_path():
    """The documented direct invocation must keep working.

    ``python scripts/model_usage_log.py --run-id ...`` (and this test file, which
    the module docstring says is runnable the same way) puts only ``scripts/`` on
    sys.path. When this module gained a top-level ``from dca_paths import ...``
    it started dying with ModuleNotFoundError before argparse ran; the fix is the
    repo-root bootstrap at the top of the module (Codex review, PR #219).
    """
    import subprocess
    import sys

    scripts_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import model_usage_log; print(model_usage_log.DEFAULT_LOG_ROOT)"],
        cwd=scripts_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("model_usage")


def test_cli_help_works_from_a_direct_invocation():
    import subprocess
    import sys

    scripts_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "model_usage_log.py", "--help"],
        cwd=scripts_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--run-id" in result.stdout
