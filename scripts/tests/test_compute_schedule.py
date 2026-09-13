"""Tests for scripts/compute_schedule.py.

Covers the two behaviors a Codex review flagged on PR #389:
  * a manual `override: true` in the schedule file must actually stick across
    runs instead of being silently overwritten by the next telemetry recompute;
  * the workflow file's cron gets synced via an in-process regex substitution,
    not a `sed -i.bak` shellout that leaves a stray `.bak` file behind.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import scripts.compute_schedule as compute_schedule  # noqa: E402
except ImportError:  # pragma: no cover - fallback when running directly in scripts/
    import compute_schedule  # type: ignore # noqa: E402


class _CadenceFn:
    def __init__(self, schedule: str, rationale: str) -> None:
        self.schedule = schedule
        self.rationale = rationale
        self.calls: list[bool] = []

    def __call__(self) -> tuple[str, str]:
        self.calls.append(True)
        return self.schedule, self.rationale


def _cadence_fn(schedule="0 3 * * *", rationale="High PR velocity (>50 commits/mo); running daily.") -> _CadenceFn:
    return _CadenceFn(schedule, rationale)


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


def test_run_cmd_returns_none_when_executable_missing():
    assert compute_schedule.run_cmd(["/path/that/does/not/exist"]) is None


def test_run_cmd_rejects_string_commands():
    with pytest.raises(TypeError, match="argument sequence"):
        compute_schedule.run_cmd("git log --since=30 days ago --format=%aI")


def test_run_cmd_nonzero_exit(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert compute_schedule.run_cmd(["git", "status"]) is None


def test_compute_cadence_git_log_fails(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", lambda cmd: None)
    with pytest.raises(RuntimeError, match="refusing to compute a cadence"):
        compute_schedule.compute_cadence()


def test_compute_cadence_dormant_when_empty(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", lambda cmd: "")
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 2 * * 0"
    assert "dormant" in rationale


def test_compute_cadence_high_velocity(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", lambda cmd: "\n".join(["2026-09-01T12:00:00Z"] * 55))
    schedule, rationale = compute_schedule.compute_cadence()
    # The run hour is pinned to 03:00 UTC (scripts/compute_schedule.py:
    # "CI/tests and self-heal docs rely on this fixed hour"). c4d18a01 had
    # briefly derived it from the quietest commit window, which broke these
    # assertions on main from 2026-09-13; #1465 aligned the tests to the
    # derived hour 0, and PR #1463 then restored the pin, so the merge of the
    # two lands back on hour 3 with the pin as the documented contract.
    assert schedule == "0 3 * * *"
    assert ">50 commits/mo" in rationale


def test_compute_cadence_active_velocity(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", lambda cmd: "\n".join(["2026-09-01T12:00:00Z"] * 25))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 3 * * 1,4"  # pinned hour: see test_compute_cadence_high_velocity
    assert "Active velocity" in rationale


def test_compute_cadence_standard_velocity(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", lambda cmd: "\n".join(["2026-09-01T12:00:00Z"] * 5))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 3 * * 1"  # pinned hour: see test_compute_cadence_high_velocity
    assert "Standard velocity" in rationale


def test_compute_cadence_run_hour_is_pinned_regardless_of_commit_hours(monkeypatch):
    # Regression guard for the pin itself. Under the quietest-window scan
    # that c4d18a01 introduced, commits clustered at 03:00Z-05:00Z would push
    # the hour AWAY from 3; the pinned contract keeps 03:00 UTC no matter how
    # commit times are distributed. The three tests above all use 12:00Z
    # commits and so cannot tell a pin from a scan that happens to land on 3.
    stamps = ["2026-09-01T03:30:00Z", "2026-09-01T04:30:00Z", "2026-09-01T05:30:00Z"] * 20
    monkeypatch.setattr(compute_schedule, "run_cmd", lambda cmd: "\n".join(stamps))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 3 * * *"
    assert "at 3:00" in rationale


def test_compute_and_sync_override_true_no_schedule_value(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("override: true\nrationale: 'Missing schedule.'\n")
    workflow_file = tmp_path / "self-heal.yml"

    res = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file),
        workflow_file=str(workflow_file),
    )
    assert res == 0


def test_compute_and_sync_override_true_workflow_cron_already_matches(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * 1'\noverride: true\nrationale: 'Manual.'\n")
    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text("  - cron: '0 3 * * 1' # AUTO-UPDATED\n")

    res = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file),
        workflow_file=str(workflow_file),
    )
    assert res == 0


def test_compute_and_sync_same_schedule_updates_out_of_sync_workflow(tmp_path):
    schedule_file = tmp_path / "self-heal-schedule.yml"
    schedule_file.write_text("schedule: '0 3 * * 1'\nrationale: 'Same.'\n")
    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text("  - cron: '0 1 * * 1' # AUTO-UPDATED\n")

    res = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file),
        workflow_file=str(workflow_file),
        cadence_fn=lambda: ("0 3 * * 1", "Same."),
    )
    assert res == 0
    assert "cron: '0 3 * * 1' # AUTO-UPDATED" in workflow_file.read_text()


def test_compute_and_sync_main(monkeypatch):
    called = []

    def fake_compute_and_sync(**kwargs):
        called.append(kwargs)
        return 0

    monkeypatch.setattr(compute_schedule, "compute_and_sync", fake_compute_and_sync)
    with pytest.raises(SystemExit) as exc:
        compute_schedule.main()
    assert exc.value.code == 0
    assert len(called) == 1


def test_sync_workflow_cron_with_mocked_ruamel_yaml(monkeypatch, tmp_path):
    class CommentValue:
        def __init__(self, val):
            self.value = val

    class ItemCA:
        def __init__(self):
            self.items = {"cron": [None, None, CommentValue("# AUTO-UPDATED")]}

    class ScheduleItem(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ca = ItemCA()

    class FakeYAML:
        def __init__(self):
            self.preserve_quotes = False

        def load(self, f):
            return {
                "on": {
                    "schedule": [
                        {"non_cron": True},
                        ScheduleItem({"cron": "0 2 * * *"}),
                    ]
                }
            }

        def dump(self, data, f):
            f.write("dumped: true")

    import types
    fake_mod = types.ModuleType("ruamel.yaml")
    fake_mod.YAML = FakeYAML  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ruamel", fake_mod)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", fake_mod)

    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text("initial")

    changed = compute_schedule.sync_workflow_cron(str(workflow_file), "0 4 * * *")
    assert changed is True
    assert workflow_file.read_text() == "dumped: true"
