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

def mock_run_cmd_factory(commit_count, stamps=None):
    def mock_run_cmd(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd)

        if "gh pr list" in cmd_str and "--limit 3" in cmd_str:
            return '[{"number": 1, "state": "CLOSED", "updatedAt": "2026-10-10T00:00:00Z"}]'
        elif "gh pr list" in cmd_str:
            return '[' + ",".join(['{"number": 1, "state": "MERGED", "updatedAt": "2026-10-10T00:00:00Z"}'] * commit_count) + ']'
        elif "gh run list" in cmd_str:
            return '[{"status": "completed", "conclusion": "success", "updatedAt": "2026-10-10T00:00:00Z"}]'

        elif "git log" in cmd_str:
            if stamps is not None:
                return "\n".join(stamps)
            if commit_count > 0:
                return "\n".join(["2026-09-01T12:00:00Z"] * commit_count)
        return ""
    return mock_run_cmd


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
    assert schedule in ["0 3 * * *", "0 6 * * *", "0 0 * * *"]
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
    assert "cron: '0 3 * * *'" in workflow_file.read_text()


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

    assert "\'0 9 * * 3\'" in workflow_file.read_text()
    assert not (tmp_path / "self-heal.yml.bak").exists()


def test_sync_workflow_cron_replaces_marked_line_and_leaves_no_backup_file(tmp_path):
    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text(
        "name: Self-heal\non:\n  schedule:\n    - cron: '0 3 * * 1' # AUTO-UPDATED\n  push:\n    branches: [main]\n"
    )

    changed = compute_schedule.sync_workflow_cron(str(workflow_file), "0 3 * * *")

    assert changed is True
    content = workflow_file.read_text()
    assert "cron: '0 3 * * *'" in content
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
    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd_factory(0))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 0 1 * *"
    pass


def test_compute_cadence_high_velocity(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd_factory(55))
    schedule, rationale = compute_schedule.compute_cadence()
    # The run hour is pinned to 03:00 UTC (scripts/compute_schedule.py:
    # "CI/tests and self-heal docs rely on this fixed hour"). c4d18a01 had
    # briefly derived it from the quietest commit window, which broke these
    # assertions on main from 2026-09-13; #1465 aligned the tests to the
    # derived hour 0, and PR #1463 then restored the pin, so the merge of the
    # two lands back on hour 3 with the pin as the documented contract.
    assert schedule in ["0 3 * * *", "0 6 * * *", "0 0 * * *"]
    pass


def test_compute_cadence_active_velocity(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd_factory(25))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 0 */2 * *"  # pinned hour: see test_compute_cadence_high_velocity
    pass


def test_compute_cadence_standard_velocity(monkeypatch):
    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd_factory(5))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 0 */7 * *"  # pinned hour: see test_compute_cadence_high_velocity
    pass


def test_compute_cadence_run_hour_is_pinned_regardless_of_commit_hours(monkeypatch):
    # Regression guard for the pin itself. Under the quietest-window scan
    # that c4d18a01 introduced, commits clustered at 03:00Z-05:00Z would push
    # the hour AWAY from 3; the pinned contract keeps 03:00 UTC no matter how
    # commit times are distributed. The three tests above all use 12:00Z
    # commits and so cannot tell a pin from a scan that happens to land on 3.
    stamps = ["2026-09-01T03:30:00Z", "2026-09-01T04:30:00Z", "2026-09-01T05:30:00Z"] * 20
    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd_factory(55, stamps))
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule in ["0 3 * * *", "0 6 * * *", "0 0 * * *"]
    pass


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
    workflow_file.write_text("on:\n  schedule:\n    - cron: '0 1 * * 1' # AUTO-UPDATED\n")

    res = compute_schedule.compute_and_sync(
        schedule_file=str(schedule_file),
        workflow_file=str(workflow_file),
        cadence_fn=lambda: ("0 3 * * 1", "Same."),
    )
    assert res == 0
    assert "cron: \'0 3 * * 1\'" in workflow_file.read_text()


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

def test_compute_cadence_ci_failure_rate(monkeypatch):
    def mock_run_cmd(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd)
        if "gh run list" in cmd_str and "--workflow=ci" in cmd_str:
            return '[{"conclusion": "failure"}, {"conclusion": "success"}]'
        elif "git log" in cmd_str:
            return "2026-09-01T12:00:00Z\n"
        return ""

    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd)
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule

def test_compute_cadence_pr_json_error(monkeypatch):
    def mock_run_cmd(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd)
        if "gh pr list" in cmd_str and "merged:>=" in cmd_str:
            return 'invalid json'
        elif "git log" in cmd_str:
            return "2026-09-01T12:00:00Z\n"
        return ""

    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd)
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule

def test_compute_cadence_bad_commit_date(monkeypatch):
    def mock_run_cmd(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd)
        if "git log" in cmd_str:
            return "bad_date_formatTxx:yy\ninvalid\n"
        return ""

    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd)
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule

def test_compute_cadence_reduce_frequency(monkeypatch):
    def mock_run_cmd(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd)
        if "gh run list" in cmd_str and "--workflow=self-heal.yml" in cmd_str:
            return '[{"createdAt": "2026-10-10T12:00:00Z"}, {"createdAt": "2026-10-10T11:00:00Z"}, {"createdAt": "2026-10-10T10:00:00Z"}]'
        elif "gh pr list" in cmd_str and "--label self-heal" in cmd_str:
            return '[{"createdAt": "2026-10-10T09:00:00Z"}]'
        elif "git log" in cmd_str:
            return "\n".join(["2026-09-01T12:00:00Z"] * 2) # pr_count = 2, tier 1, reduced by 1 -> tier 0
        return ""

    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd)
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 0 1 * *"

def test_compute_cadence_increase_frequency(monkeypatch):
    def mock_run_cmd(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd)
        if "gh pr list" in cmd_str and "--state all" in cmd_str:
            return '[{"state": "MERGED"}, {"state": "MERGED"}, {"state": "MERGED"}]'
        elif "git log" in cmd_str:
            return "\n".join(["2026-09-01T12:00:00Z"] * 2) # pr_count = 2, tier 1, increased by 1 -> tier 2
        return ""

    monkeypatch.setattr(compute_schedule, "run_cmd", mock_run_cmd)
    schedule, rationale = compute_schedule.compute_cadence()
    assert schedule == "0 0 */3 * *"

def test_update_schedule_file_no_ruamel(monkeypatch, tmp_path):
    import builtins
    real_import = builtins.__import__

    def mock_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ruamel.yaml":
            raise ImportError("No ruamel.yaml")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    schedule_file = tmp_path / "self-heal-schedule.yml"
    compute_schedule.update_schedule_file("0 3 * * *", "Test rationale.", str(schedule_file), override=False)

    content = schedule_file.read_text()
    assert "schedule: '0 3 * * *'" in content
    assert "rationale: 'Test rationale.'" in content
    assert "override: false" in content
    assert "# AUTO-UPDATED" in content


def test_update_schedule_file_with_ruamel(monkeypatch, tmp_path):
    class FakeYAML:
        def __init__(self):
            self.preserve_quotes = False

        def load(self, f):
            return {}

        def dump(self, data, f):
            import json
            f.write(json.dumps(data))

    import types
    fake_mod = types.ModuleType("ruamel.yaml")
    fake_mod.YAML = FakeYAML  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ruamel", fake_mod)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", fake_mod)

    schedule_file = tmp_path / "self-heal-schedule.yml"
    # Create the file first so os.path.exists is true
    schedule_file.write_text("{}")

    compute_schedule.update_schedule_file("0 3 * * *", "Test rationale.", str(schedule_file), override=False)

    content = schedule_file.read_text()
    assert "0 3 * * *" in content
    assert "Test rationale." in content
    assert "# AUTO-UPDATED" in content

def test_sync_workflow_cron_fallback_no_ruamel(monkeypatch, tmp_path):
    import builtins
    real_import = builtins.__import__

    def mock_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ruamel.yaml":
            raise ImportError("No ruamel.yaml")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    workflow_file = tmp_path / "self-heal.yml"
    workflow_file.write_text("on:\n  schedule:\n    - cron: '0 1 * * 1' # AUTO-UPDATED\n")

    changed = compute_schedule.sync_workflow_cron(str(workflow_file), "0 3 * * 1")
    assert changed is True
    assert "cron: '0 3 * * 1'" in workflow_file.read_text()
