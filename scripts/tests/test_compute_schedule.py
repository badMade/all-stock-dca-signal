"""Tests for scripts/compute_schedule.py.

Covers the two behaviors a Codex review flagged on PR #389:
  * a manual `override: true` in the schedule file must actually stick across
    runs instead of being silently overwritten by the next telemetry recompute;
  * the workflow file's cron gets synced via an in-process regex substitution,
    not a `sed -i.bak` shellout that leaves a stray `.bak` file behind.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import compute_schedule  # noqa: E402


def _cadence_fn(schedule="0 3 * * *", rationale="High PR velocity (>50 commits/mo); running daily."):
    calls = []

    def _fn():
        calls.append(True)
        return schedule, rationale

    _fn.calls = calls
    return _fn


def test_parse_schedule_file_missing_file_returns_defaults(tmp_path):
    missing = tmp_path / "self-heal-schedule.yml"
    assert compute_schedule.parse_schedule_file(str(missing)) == (None, None, False)


def test_parse_schedule_file_without_override_key_defaults_to_false(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * 1'\nrationale: 'Standard velocity.'\n# AUTO-UPDATED\n")
    assert compute_schedule.parse_schedule_file(str(schedule_file)) == ("0 3 * * 1", "Standard velocity.", False)


def test_parse_schedule_file_reads_override_true(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * 1'\nrationale: 'Manual pin.'\noverride: true\n")
    assert compute_schedule.parse_schedule_file(str(schedule_file)) == ("0 3 * * 1", "Manual pin.", True)


def test_compute_and_sync_updates_schedule_when_no_override_and_cadence_differs(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * 1'\nrationale: 'Standard velocity.'\n# AUTO-UPDATED\n")
    workflow_file = tmp_path / "self-heal.yml"

    cadence_fn = _cadence_fn(schedule="0 3 * * *")
    code = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file), workflow_file=str(workflow_file), cadence_fn=cadence_fn
    )

    assert code == 0
    assert len(cadence_fn.calls) == 1
    schedule, rationale, override = compute_schedule.parse_schedule_file(str(schedule_file))
    assert schedule == "0 3 * * *"
    assert override is False


def test_compute_and_sync_leaves_schedule_untouched_when_cadence_unchanged(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * *'\nrationale: 'High velocity.'\n# AUTO-UPDATED\n")
    workflow_file = tmp_path / "self-heal.yml"

    cadence_fn = _cadence_fn(schedule="0 3 * * *")
    compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file), workflow_file=str(workflow_file), cadence_fn=cadence_fn
    )

    assert schedule_file.read_text() == "schedule: '0 3 * * *'\nrationale: 'High velocity.'\n# AUTO-UPDATED\n"


def test_compute_and_sync_syncs_workflow_cron_even_when_metadata_unchanged(tmp_path):
    """A prior version returned before syncing whenever the metadata schedule
    already matched telemetry, so a workflow cron edited independently of the
    metadata file (or never yet synced) stayed stale forever (Codex review,
    PR #389)."""
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * *'\nrationale: 'High velocity.'\n# AUTO-UPDATED\n")
    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text("on:\n  schedule:\n    - cron: '0 2 * * 0' # AUTO-UPDATED\n")

    cadence_fn = _cadence_fn(schedule="0 3 * * *")  # matches metadata already
    code = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file), workflow_file=str(workflow_file), cadence_fn=cadence_fn
    )

    assert code == 0
    assert "cron: '0 3 * * *' # AUTO-UPDATED" in workflow_file.read_text()


def test_compute_and_sync_with_override_never_calls_telemetry_and_keeps_value(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 9 * * 3'\nrationale: 'Operator pin.'\noverride: true\n")
    workflow_file = tmp_path / "self-heal.yml"

    cadence_fn = _cadence_fn(schedule="0 3 * * *")  # would differ if it were ever consulted
    code = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file), workflow_file=str(workflow_file), cadence_fn=cadence_fn
    )

    assert code == 0
    assert cadence_fn.calls == []  # telemetry never recomputed
    schedule, _rationale, override = compute_schedule.parse_schedule_file(str(schedule_file))
    assert schedule == "0 9 * * 3"  # operator's value survives the run
    assert override is True


def test_compute_and_sync_override_syncs_workflow_cron_without_recompute(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 9 * * 3'\nrationale: 'Operator pin.'\noverride: true\n")
    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text("on:\n  schedule:\n    - cron: '0 3 * * 1' # AUTO-UPDATED\n")

    compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file), workflow_file=str(workflow_file), cadence_fn=_cadence_fn()
    )

    assert "'0 9 * * 3' # AUTO-UPDATED" in workflow_file.read_text()
    assert not (tmp_path / "self-heal.yml.bak").exists()


def test_sync_workflow_cron_replaces_marked_line_and_leaves_no_backup_file(tmp_path):
    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text(
        "name: Self-heal\non:\n  schedule:\n    - cron: '0 3 * * 1' # AUTO-UPDATED\n  push:\n    branches: [main]\n"
    )

    changed = compute_schedule.sync_workflow_cron(str(workflow_file), "0 3 * * *")

    assert changed is True
    content = workflow_file.read_text()
    assert "cron: '0 3 * * *' # AUTO-UPDATED" in content
    assert "push:" in content  # unrelated content untouched
    assert not (tmp_path / "self-heal.yml.bak").exists()


def test_sync_workflow_cron_is_noop_when_no_auto_updated_marker(tmp_path):
    workflow_file = tmp_path / "self-heal.yml"
    original = "name: Self-heal\non:\n  workflow_run:\n    workflows: ['CI']\n"
    workflow_file.write_text(original)

    changed = compute_schedule.sync_workflow_cron(str(workflow_file), "0 3 * * *")

    assert changed is False
    assert workflow_file.read_text() == original


def test_sync_workflow_cron_is_noop_when_workflow_file_missing(tmp_path):
    missing = tmp_path / "self-heal.yml"
    assert compute_schedule.sync_workflow_cron(str(missing), "0 3 * * *") is False


def test_update_schedule_file_writes_override_flag(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    compute_schedule.update_schedule_file("0 3 * * *", "High velocity.", str(schedule_file), override=True)

    schedule, rationale, override = compute_schedule.parse_schedule_file(str(schedule_file))
    assert (schedule, rationale, override) == ("0 3 * * *", "High velocity.", True)
