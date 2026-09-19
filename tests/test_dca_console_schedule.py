"""Schedule page reader: manifest, deploy/, workflows, launchd, roster, and the join matrix.

Every fixture tree lives under ``tmp_path``; the real ``config/`` and checkout
are used only where the assertion is about the committed schedule itself.
"""

import json
import math
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_console_schedule as schedule
from dca_console_reader import RootSpec
from dca_console_routes import walk_redact
from dca_console_schedule import (
    LAUNCHD_LABEL,
    build_schedule_layers,
    deploy_layer,
    launchd_status,
    manifest_tasks,
    policy_block,
    roster_identity,
    workflows_layer,
)

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
DAY = date(2026, 9, 17)
NOW = datetime(2026, 9, 17, 12, tzinfo=ZoneInfo("America/New_York"))
ACCOUNT = "999000111"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(tmp_path: Path, secret_note: str = "") -> Path:
    """A miniature checkout: crontab, systemd pair, env files and three workflows."""
    repo = tmp_path / "repo"
    write(repo / "deploy" / "crontab.example",
          "# comment line\nCRON_TZ=UTC\nFOO=supersecretvalue\n"
          f"# {secret_note}\n"
          "30 14 * * 1-5 /opt/all-stock-dca/deploy/run-drift-check.sh morning_dca 1030ET\n"
          f"30 19 * * 1-5 {tmp_path}/deploy/run-drift-check.sh ce16ba6b/scheduled_runs 1530ET {secret_note}\n"
          "this line is garbage\n")
    write(repo / "deploy" / "drift-slack.env", "DCA_DRIFT_SLACK_WEBHOOK=https://hooks.example/SECRETWEBHOOK\n")
    write(repo / "deploy" / "drift-slack.env.example", "DCA_DRIFT_SLACK_WEBHOOK=\n")
    write(repo / "deploy" / "systemd" / "dca-drift-morning.timer",
          "# header\n[Unit]\nDescription=Schedule drift (morning)\n[Timer]\n"
          "OnCalendar=Mon..Fri 10:30 America/New_York\nUnit=dca-drift-morning.service\nPersistent=true\n")
    write(repo / "deploy" / "systemd" / "dca-drift-morning.service",
          "[Service]\nType=oneshot\nUser=dca-drift\nEnvironmentFile=-/etc/dca/drift-slack.env\n"
          f"ExecStart=/opt/all-stock-dca/deploy/run-drift-check.sh morning_dca 1030ET {secret_note}\n")
    write(repo / "deploy" / "systemd" / "README", "not a unit\n")
    write(repo / ".github" / "workflows" / "self-heal.yml",
          "name: Self-Heal\n\non:\n  schedule:\n    # weekly\n    - cron: '0 3 * * 1' # AUTO\n"
          "  workflow_dispatch:\n  workflow_run:\n    workflows: [\"CI\"]\n\njobs:\n  x:\n"
          f"    env:\n      GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}\n      NOTE: {secret_note}\n")
    write(repo / ".github" / "workflows" / "ci.yml", "name: CI\non: [push, pull_request]\njobs: {}\n")
    write(repo / ".github" / "workflows" / "lint.yml", "on: push\njobs: {}\n")
    write(repo / ".github" / "workflows" / "notes.txt", "on: schedule\n")
    return repo


def make_config(tmp_path: Path, secret_note: str = "") -> Path:
    """A tiny manifest with one cron task, one Perplexity-style backup and a policy block."""
    config = tmp_path / "config"
    manifest = {
        "version": 1,
        "standing_policy": {"items": list(range(300)), "text": "x" * 600, "nan": math.nan, "note": secret_note},
        "tasks": [
            {"id": "42173154", "name": "Morning buy", "type": "trading", "status": "active", "trades": True,
             "cron_utc": "30 14 * * 1-5", "et_equivalent": "10:30 AM ET weekdays", "executor": "MorningDCAExecutor",
             "notes": "n" * 400 + secret_note},
            {"id": "4412c3c4", "name": "Perplexity backup 09:55", "type": "trading", "status": "active",
             "trades": True, "fires_at_et": "09:55", "notes": "backup"},
            {"id": "d1f7a2c0", "name": "Old monitor", "type": "monitoring", "status": "disabled", "trades": False,
             "scheduler_registered": False, "cron_utc": "0 13 * * 1-5", "et_equivalent": "9:00 AM ET"},
            "not a task",
        ],
    }
    write(config / "scheduled_tasks.json", json.dumps(manifest))
    return config


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    write(home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist", "<plist/>")
    return home


def make_root(tmp_path: Path, secret_note: str = "") -> RootSpec:
    root = tmp_path / "cron_tracking"
    lines = [f"line {index} {tmp_path}/cron_tracking/console/summaries account_number={ACCOUNT} {secret_note}"
             for index in range(60)]
    write(root / "console" / "launchd.out.log", "\n".join(lines) + "\n")
    return RootSpec.of(root)


def forbid_locks(monkeypatch):
    import dca_runner
    import scripts.file_lock

    def refuse(*args, **kwargs):
        raise AssertionError("a console reader took a cross-process lock")

    monkeypatch.setattr(scripts.file_lock, "cross_process_lock", refuse)
    monkeypatch.setattr(dca_runner, "state_transaction", refuse)


# -- manifest -----------------------------------------------------------------


def test_manifest_tasks_enriches_the_committed_schedule():
    result = manifest_tasks(CONFIG, DAY, NOW)
    assert result["task_count"] == 29 and len(result["tasks"]) >= 29
    assert "tasks" not in result["policy_keys"] and "standing_perplexity_backup_policy" in result["policy_keys"]
    assert result["policy_keys"] == sorted(result["policy_keys"]) and len(result["policy_keys"]) >= 57
    backups = [row for row in result["tasks"] if row["timing"] == "outside this repo"]
    assert len(backups) >= 7 and all(row["trades"] is True and row["cron_utc"] is None for row in backups)
    # Disabled and unregistered tasks must stay visible, not be filtered out.
    assert any(row["status"] == "disabled" for row in result["tasks"])
    assert all(len(row["notes"] or "") <= 303 for row in result["tasks"])
    assert not any("timing verified" in warning for warning in result["warnings"])


def test_manifest_multi_slot_task_yields_one_enriched_row_per_slot():
    rows = [row for row in manifest_tasks(CONFIG, DAY, NOW)["tasks"] if row["id"] == "7be82b97"]
    assert sorted(row["time"] for row in rows) == ["11:30", "13:30", "14:30"]
    assert all(row["cron_utc"] and row["timing"] == "cron_utc" and row["type"] == "trading" for row in rows)


def test_manifest_tasks_fixture_semantics_and_timezone_naive_now(tmp_path):
    result = manifest_tasks(make_config(tmp_path), DAY, NOW.replace(tzinfo=None))
    by_id = {row["id"]: row for row in result["tasks"]}
    assert by_id["42173154"]["executor"] == "MorningDCAExecutor" and by_id["42173154"]["timing"] == "cron_utc"
    assert by_id["42173154"]["notes"].endswith("...") and len(by_id["42173154"]["notes"]) == 303
    assert by_id["4412c3c4"]["timing"] == "outside this repo" and by_id["4412c3c4"]["fires_at_et"] == "09:55"
    assert by_id["d1f7a2c0"]["status"] == "disabled" and by_id["d1f7a2c0"]["scheduler_registered"] is False
    assert result["task_count"] == 4 and result["policy_keys"] == ["standing_policy", "version"]
    # No market calendar in the fixture: the daily snapshot's own wording survives.
    assert any("timing is unverified" in warning for warning in result["warnings"])
    assert result["warnings"] == list(dict.fromkeys(result["warnings"]))


def test_manifest_tasks_absent_or_malformed_manifest(tmp_path):
    for content in (None, "{", "[]", '{"tasks": "nope"}'):
        config = tmp_path / (content or "absent").replace("{", "brace").replace("[", "bracket").replace('"', "")
        config.mkdir()
        if content is not None:
            write(config / "scheduled_tasks.json", content)
        result = manifest_tasks(config, DAY, NOW)
        assert result["tasks"] == [] and result["task_count"] is None and result["policy_keys"] == []
        assert any("manifest" in warning for warning in result["warnings"])


def test_policy_block_bounds_values_and_rejects_unknown_keys(tmp_path):
    config = make_config(tmp_path)
    block = policy_block(config, "standing_policy")
    assert block["found"] is True
    assert len(block["value"]["items"]) == 200 and len(block["value"]["text"]) == 503
    assert block["value"]["nan"] is None and block["value"]["items"][0] == 0
    assert any("200" in note for note in block["notes"]) and any("500" in note for note in block["notes"])
    json.dumps(block, allow_nan=False)
    for key in ("tasks", "missing", "../x", ""):
        assert policy_block(config, key) == {"key": key, "found": False, "value": None, "notes": [], "warnings": []}
    absent = policy_block(tmp_path / "nowhere", "version")
    assert absent["found"] is False and absent["warnings"]


def test_policy_block_fits_sixty_four_kilobytes(tmp_path):
    config = tmp_path / "config"
    write(config / "scheduled_tasks.json",
          json.dumps({"big": {f"k{index}": "v" * 400 for index in range(200)}, "tasks": []}))
    block = policy_block(config, "big")
    assert block["found"] is True and len(json.dumps(block["value"])) <= 64 * 1024
    assert len(block["value"]) < 200 and any("omitted" in note for note in block["notes"])


# -- deploy/ and workflows ------------------------------------------------------


def test_deploy_layer_parses_cron_and_systemd_without_reading_secrets(tmp_path):
    result = deploy_layer(make_repo(tmp_path))
    text = json.dumps(result)
    assert "SECRETWEBHOOK" not in text and "supersecretvalue" not in text and "/opt/" not in text
    assert str(tmp_path) not in text
    assert result["cron_tz"] == "UTC" and result["env_files"] == ["drift-slack.env", "drift-slack.env.example"]
    morning, afternoon = result["cron"]
    assert morning == {"schedule": "30 14 * * 1-5", "timezone": "UTC", "script": "run-drift-check.sh",
                       "arguments": ["morning_dca", "1030ET"], "slots": ["1030ET"], "streams": ["morning"]}
    assert afternoon["streams"] == ["afternoon"] and afternoon["slots"] == ["1530ET"]
    assert any("garbage" not in warning and "did not parse" in warning for warning in result["warnings"])
    assert any("FOO" in warning for warning in result["warnings"])
    timer = next(row for row in result["systemd"] if row["kind"] == "timer")
    service = next(row for row in result["systemd"] if row["kind"] == "service")
    assert timer["on_calendar"] == "Mon..Fri 10:30 America/New_York" and timer["persistent"] is True
    assert timer["unit"] == "dca-drift-morning.service" and len(result["systemd"]) == 2
    assert service["user"] == "dca-drift" and service["environment_file"] == "drift-slack.env"
    assert service["slots"] == ["1030ET"] and service["script"] == "run-drift-check.sh"


def test_deploy_layer_parses_the_committed_examples():
    result = deploy_layer(REPO)
    assert len(result["cron"]) == 7 and sorted(row["slots"][0] for row in result["cron"]) == sorted(
        f"{slot}ET" for slot in ("0935", "1030", "1130", "1230", "1330", "1430", "1530"))
    assert len(result["systemd"]) == 14 and all(row["persistent"] is False for row in result["systemd"]
                                                 if row["kind"] == "timer")
    assert "drift-slack.env.example" in result["env_files"]


def test_deploy_and_workflow_layers_report_absent_directories(tmp_path):
    (tmp_path / "empty").mkdir()
    deploy = deploy_layer(tmp_path / "empty")
    assert deploy["cron"] is None and deploy["systemd"] == [] and deploy["env_files"] == []
    assert any("absent" in warning for warning in deploy["warnings"])
    workflows = workflows_layer(tmp_path / "empty")
    assert workflows["workflows"] is None and workflows["warnings"]


def test_workflows_layer_scans_triggers_without_yaml(tmp_path):
    result = workflows_layer(make_repo(tmp_path))
    rows = {row["file"]: row for row in result["workflows"]}
    assert set(rows) == {"self-heal.yml", "ci.yml", "lint.yml"}
    assert rows["self-heal.yml"]["triggers"] == ["schedule", "0 3 * * 1", "workflow_dispatch", "workflow_run"]
    assert rows["self-heal.yml"]["has_secrets_reference"] is True and rows["self-heal.yml"]["name"] == "Self-Heal"
    assert rows["ci.yml"]["triggers"] == ["push", "pull_request"] and rows["ci.yml"]["has_secrets_reference"] is False
    assert rows["lint.yml"] == {"file": "lint.yml", "name": None, "triggers": ["push"], "has_secrets_reference": False}
    assert "GITHUB_TOKEN" not in json.dumps(result)
    committed = {row["file"]: row for row in workflows_layer(REPO)["workflows"]}
    assert "0 13 * * 1-5" in committed["drift-check.yml"]["triggers"]
    assert committed["ci.yml"]["triggers"] == ["push", "pull_request"]


# -- launchd ------------------------------------------------------------------


def test_launchd_status_reports_plist_presence_and_bounded_redacted_tails(tmp_path):
    root = make_root(tmp_path)
    result = launchd_status([RootSpec.of(tmp_path / "other", "secondary"), root], home=make_home(tmp_path))
    assert result["plist_present"] is True and result["plist_modified"].startswith("20")
    assert result["log_root"] == root.label and [log["name"] for log in result["logs"]] == ["launchd.out.log"]
    tail = result["logs"][0]["tail"]
    assert len(tail) == 40 and tail[-1].startswith("line 59") and result["logs"][0]["size"] > 0
    text = json.dumps(result)
    assert str(tmp_path) not in text and ACCOUNT not in text and "<path>/summaries" in text
    assert launchd_status([root], home=tmp_path / "nohome")["plist_present"] is False
    empty = launchd_status([], home=tmp_path / "nohome")
    assert empty["logs"] == [] and empty["plist_modified"] is None and empty["warnings"]


def test_launchd_label_matches_the_renderer_and_path_scrub_keeps_urls():
    renderer = (REPO / "deploy" / "launchd" / "render_console_agent.py").read_text(encoding="utf-8")
    assert f'LABEL = "{LAUNCHD_LABEL}"' in renderer
    scrubbed = schedule._scrub_paths("root=/Users/who/Documents/Git/repo/cron_tracking/x via http://127.0.0.1:8765/api/x "
                                     "route=/api/bootstrap")
    assert scrubbed == "root=<path>/x via http://127.0.0.1:8765/api/x route=/api/bootstrap"


# -- roster and matrix ----------------------------------------------------------


def test_roster_identity_reads_constants_without_constructing_executors(monkeypatch):
    import morning_dca

    def boom(self, *args, **kwargs):
        raise AssertionError("an executor was constructed")

    monkeypatch.setattr(morning_dca.MorningDCAExecutor, "__init__", boom)
    result = roster_identity(CONFIG)
    rows = {row["slot_id"]: row for row in result["slots"]}
    assert list(rows) == ["0935", "1030", "1130", "1230", "1330", "1430", "1530"]
    assert rows["1030"]["executor_class"] == "MorningDCAExecutor" and rows["1030"]["run_dca_entry"] == "run_slot"
    assert rows["1030"]["pointer_name"] == "latest_morning_run.json" and rows["1030"]["artifact_dir"] == "morning_dca"
    assert rows["1030"]["window"] == {"start": "10:30", "end": "11:00"}
    assert rows["1030"]["executor_window"] == {"start": "10:25", "end": "10:35"}
    assert rows["0935"]["run_dca_entry"] == "disabled" and rows["0935"]["cron_id"] == "2ba19526"
    assert rows["1330"]["executor_class"] == "IntradayDCAExecutor" and rows["1330"]["cron_id"] == "7be82b97"
    assert rows["1530"]["executor_class"] is None and rows["1530"]["artifact_dir"] == "ce16ba6b/scheduled_runs"
    assert rows["1530"]["pointer_name"] == "latest_scheduled_run.json" and rows["1530"]["run_dca_entry"] == "run_slot"
    assert rows["1230"]["live_execution_enabled"] is True and rows["0935"]["live_execution_enabled"] is False
    # morning_dca.json carries no flag and near_close has no stream config: None, never False.
    assert rows["1030"]["live_execution_enabled"] is None and rows["1530"]["live_execution_enabled"] is None
    assert str(REPO) not in json.dumps(result) and result["warnings"] == []


def test_roster_identity_without_stream_configs_reports_unknown_flags(tmp_path):
    result = roster_identity(tmp_path)
    assert all(row["live_execution_enabled"] is None for row in result["slots"])
    assert len(result["warnings"]) == 5 and all("live flag is unknown" in warning for warning in result["warnings"])


def test_build_schedule_layers_joins_slots_and_observed_evidence(tmp_path):
    observed = {"morning": {"status": "valid", "evidence": "pointer", "run_state_present": True},
                "afternoon": {"status": "recovery", "evidence": "context", "run_state_present": False},
                "market_open": {"status": "missing"}}
    result = build_schedule_layers(CONFIG, REPO, [make_root(tmp_path)], DAY, NOW, observed=observed)
    matrix = {row["slot_id"]: row for row in result["matrix"]}
    assert len(matrix) == 7 and all(row["configured"] is True and row["deployable"] is True for row in matrix.values())
    assert matrix["1030"]["observed"] is True and matrix["1030"]["observed_entry"]["status"] == "valid"
    assert matrix["1530"]["observed"] is False and matrix["0935"]["observed"] is None
    assert matrix["1130"]["observed"] is None and matrix["1130"]["observed_entry"] is None
    assert matrix["1030"]["manifest_task_ids"] == ["42173154"] and matrix["1030"]["cron_entries"] == 1
    assert matrix["1330"]["systemd_units"] == ["dca-drift-intraday-1330.service"]
    assert set(result) == {"manifest", "deploy", "workflows", "launchd", "roster", "matrix", "warnings"}
    assert result["warnings"][-1].endswith("scheduler execution is unverified.")
    assert result["warnings"] == list(dict.fromkeys(result["warnings"]))


def test_matrix_uses_none_for_unavailable_layers_not_false(tmp_path):
    (tmp_path / "bare").mkdir()
    result = build_schedule_layers(tmp_path / "bare", tmp_path / "bare", [], DAY, NOW)
    assert result["matrix"] and all(row["configured"] is None and row["deployable"] is None and row["observed"] is None
                                    for row in result["matrix"])


def test_no_secret_or_absolute_path_survives_any_builder(tmp_path):
    note = f"acct {ACCOUNT} at {tmp_path}/cron_tracking/console/x"
    repo, config, root = make_repo(tmp_path, note), make_config(tmp_path, note), make_root(tmp_path, note)
    raw = build_schedule_layers(config, repo, [root], DAY, NOW, observed={"morning": {"status": note}})
    raw["policy"] = policy_block(config, "standing_policy")
    before = json.dumps(raw)
    assert str(tmp_path) not in before
    after = json.dumps(walk_redact(raw))
    assert ACCOUNT not in after and str(tmp_path) not in after


def test_every_builder_is_lock_free(tmp_path, monkeypatch):
    forbid_locks(monkeypatch)
    repo, config, root = make_repo(tmp_path), make_config(tmp_path), make_root(tmp_path)
    manifest_tasks(config, DAY, NOW)
    manifest_tasks(CONFIG, DAY, NOW)
    policy_block(config, "standing_policy")
    deploy_layer(repo)
    workflows_layer(repo)
    launchd_status([root], home=make_home(tmp_path))
    roster_identity(CONFIG)
    build_schedule_layers(CONFIG, REPO, [root], DAY, NOW, observed={})


@pytest.mark.parametrize("value, expected", [("30 14 * * 1-5", "30 14 * * 1-5"), ("30 14 * *", None), (5, None)])
def test_cron_utc_requires_five_fields(value, expected):
    assert schedule._cron_utc(value) == expected
