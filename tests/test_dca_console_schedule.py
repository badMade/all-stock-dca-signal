"""Schedule-page reader regressions: configured vs deployable vs observed, label-only, lock-free."""

import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_runner
import scripts.file_lock
from dca_console_reader import RootSpec
from dca_console_routes import walk_redact
from dca_console_schedule import (
    LAUNCHD_LABEL,
    build_schedule_layers,
    deploy_layer,
    launchd_status,
    manifest_tasks,
    observed_from_pointer_strip,
    policy_block,
    roster_identity,
    workflows_layer,
)

DAY = date(2026, 9, 8)
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 12, tzinfo=ET)
REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
ACCOUNT = "999000111"
# Two or more directory components: the shape every builder must scrub.
ABSOLUTE = re.compile(r"(?<![\w:/])/(?:[^\s/'\"]+/){2,}")


def write_manifest(config_dir, tasks, **policy):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "scheduled_tasks.json").write_text(json.dumps({"tasks": tasks, **policy}), encoding="utf-8")
    return config_dir


def write_repo(root, crontab=None, service=None, workflow=None):
    """A tiny checkout: one crontab, one timer/service pair, one workflow."""
    (root / "deploy" / "systemd").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    if crontab is not None:
        (root / "deploy" / "crontab.example").write_text(crontab, encoding="utf-8")
    (root / "deploy" / "systemd" / "dca-drift-morning.timer").write_text(
        "[Unit]\nDescription=Timer\n[Timer]\nOnCalendar=Mon..Fri 10:30 America/New_York\nUnit=dca-drift-morning.service\n"
        "Persistent=false\n", encoding="utf-8")
    (root / "deploy" / "systemd" / "dca-drift-morning.service").write_text(
        service or "[Service]\nUser=dca-drift\nExecStart=/opt/x/deploy/run-drift-check.sh morning_dca 1030ET\n",
        encoding="utf-8")
    if workflow is not None:
        (root / ".github" / "workflows" / "one.yml").write_text(workflow, encoding="utf-8")
    return root


def pointer_row(stream, label="primary", **updates):
    """The exact dict shape dca_console_runs._pointer_row emits (evidence is a bool)."""
    row = {"stream": stream, "label": label, "at_risk": False, "pointer_file": f"{stream}_dca/latest_{stream}_run.json",
           "alias": False, "status": "valid", "evidence": True, "evidence_label": "scheduled evidence", "reasons": [],
           "trading_day": DAY.isoformat(), "run_state_present": True, "order_submit_status": None}
    row.update(updates)
    return row


def every_builder(config_dir, repo, roots, observed=None):
    return [manifest_tasks(config_dir, DAY, NOW), policy_block(config_dir, "policy"), deploy_layer(repo),
            workflows_layer(repo), launchd_status(roots, home=repo), roster_identity(config_dir),
            build_schedule_layers(config_dir, repo, roots, DAY, NOW, observed=observed)]


def test_launchd_label_matches_the_renderer():
    text = (REPO / "deploy" / "launchd" / "render_console_agent.py").read_text(encoding="utf-8")
    assert f'LABEL = "{LAUNCHD_LABEL}"' in text


def test_manifest_tasks_enriches_the_committed_schedule():
    result = manifest_tasks(CONFIG, DAY, NOW)
    assert result["task_count"] == 29 and "$schema" in result["policy_keys"] and "tasks" not in result["policy_keys"]
    by_id = {row["id"]: row for row in result["tasks"]}
    morning = by_id["42173154"]
    assert morning["cron_utc"] == "30 14 * * 1-5" and morning["timing"] == "cron_utc" and morning["trades"] is True
    assert morning["executor"] == "MorningDCAExecutor" and morning["time"] == "10:30"
    assert morning["notes"] and len(morning["notes"]) <= 303 and "/home/user" not in json.dumps(result)
    backups = [row for row in result["tasks"] if row["timing"] == "outside this repo"]
    assert backups and all(row["trades"] is True and row["cron_utc"] is None for row in backups)
    assert any(row["fires_at_et"] == "09:55" for row in backups)
    assert "disabled" in {row["status"] for row in result["tasks"]}
    assert {row["status"] for row in result["tasks"]} <= {
        "timing_unverified", "elapsed_unverified", "planned", "disabled", "not_registered", "market_closed",
        "not_scheduled_today", "elapsed_one_time"}


def test_manifest_tasks_absent_and_malformed_manifest(tmp_path):
    absent = manifest_tasks(tmp_path / "none", DAY, NOW)
    assert absent["tasks"] == [] and absent["task_count"] is None and absent["policy_keys"] == []
    assert any("missing or malformed" in warning for warning in absent["warnings"])
    tmp_path.joinpath("scheduled_tasks.json").write_text("[1, 2", encoding="utf-8")
    broken = manifest_tasks(tmp_path, DAY, NOW)
    assert broken["task_count"] is None and broken["tasks"] == []


def test_manifest_row_without_manifest_fields_still_lists(tmp_path):
    config = write_manifest(tmp_path, [{"id": "abc", "name": "Bare", "status": "disabled"}, "junk"], policy={"a": 1})
    result = manifest_tasks(config, DAY, NOW)
    assert [row["status"] for row in result["tasks"]] == ["disabled"]
    assert result["tasks"][0]["timing"] == "unspecified" and result["tasks"][0]["time"] == "unspecified"
    assert result["policy_keys"] == ["policy"]


def test_policy_block_found_tasks_and_unknown(tmp_path):
    found = policy_block(CONFIG, "standing_perplexity_backup_policy")
    assert found["found"] is True and found["value"] is not None and found["warnings"] == []
    assert policy_block(CONFIG, "tasks")["found"] is False
    assert policy_block(CONFIG, "no_such_key")["found"] is False
    assert policy_block(CONFIG, "")["found"] is False
    missing = policy_block(tmp_path, "version")
    assert missing["found"] is False and missing["warnings"]


def test_policy_block_bounds_lists_strings_floats_and_bytes(tmp_path):
    config = write_manifest(tmp_path, [], big={"items": list(range(300)), "text": "x" * 900, "nan": math.nan,
                                                 "blob": ["y" * 400] * 400})
    result = policy_block(config, "big")
    assert result["found"] is True
    value = result["value"]
    assert len(value["items"]) == 200 and len(value["text"]) == 503 and value["nan"] is None
    assert len(json.dumps(value)) <= 64 * 1024
    assert any("truncated to 200 entries" in note for note in result["notes"])
    assert any("truncated to 500 characters" in note for note in result["notes"])
    assert any("omitted to stay under 64 KB" in note for note in result["notes"])


def test_deploy_layer_parses_the_committed_examples():
    result = deploy_layer(REPO)
    assert result["cron_tz"] == "UTC" and len(result["cron"]) == 7
    first = result["cron"][0]
    assert first["schedule"] == "35 13 * * 1-5" and first["script"] == "run-drift-check.sh"
    assert first["slots"] == ["0935ET"] and first["streams"] == ["market-open"]
    assert {row["kind"] for row in result["systemd"]} == {"timer", "service"} and len(result["systemd"]) == 14
    afternoon = next(row for row in result["systemd"] if row["file"] == "dca-drift-afternoon.service")
    assert afternoon["user"] == "dca-drift" and afternoon["slots"] == ["1530ET"] and afternoon["streams"] == ["afternoon"]
    timer = next(row for row in result["systemd"] if row["file"] == "dca-drift-afternoon.timer")
    assert timer["persistent"] is False and timer["on_calendar"].startswith("Mon..Fri 15:30")
    assert result["env_files"] == ["drift-slack.env.example"]
    assert "/opt/" not in json.dumps(result)


def test_deploy_layer_absent_files_and_malformed_lines(tmp_path):
    empty = deploy_layer(tmp_path)
    assert empty["cron"] is None and empty["systemd"] == [] and empty["env_files"] == []
    assert any("crontab.example is absent" in warning for warning in empty["warnings"])
    repo = write_repo(tmp_path / "repo", crontab="CRON_TZ=UTC\nSLACK_TOKEN=abc123456\nnot a cron line\n"
                                                  "30 14 * * 1-5 /opt/x/deploy/run-drift-check.sh morning_dca 1030ET\n")
    result = deploy_layer(repo)
    assert len(result["cron"]) == 1 and result["cron"][0]["timezone"] == "UTC"
    assert any("did not parse" in warning for warning in result["warnings"])
    assert any("SLACK_TOKEN was noted by name only" in warning for warning in result["warnings"])
    assert "abc123456" not in json.dumps(result)


def test_command_tokens_never_carry_embedded_paths(tmp_path):
    """Adversarial review round 2: a flag-style token with an embedded path was leaving unscrubbed."""
    repo = write_repo(
        tmp_path, crontab=f"30 14 * * 1-5 /opt/x/run.sh --state-root={tmp_path}/cron_tracking --acct {ACCOUNT} morning_dca 1030ET\n",
        service=f"[Service]\nUser=dca-drift\nEnvironmentFile=-{tmp_path}/etc/drift-slack.env\n"
                f"ExecStart={tmp_path}/deploy/run-drift-check.sh --root={tmp_path}/cron_tracking morning_dca 1030ET\n")
    result = deploy_layer(repo)
    dumped = json.dumps(result)
    assert str(tmp_path) not in dumped and ABSOLUTE.search(dumped) is None
    cron = result["cron"][0]
    assert cron["arguments"][0] == "--state-root=<path>/cron_tracking" and cron["slots"] == ["1030ET"]
    assert ACCOUNT not in json.dumps(walk_redact(result))
    service = next(row for row in result["systemd"] if row["kind"] == "service")
    assert service["environment_file"] == "drift-slack.env" and service["script"] == "run-drift-check.sh"
    assert service["arguments"][0] == "--root=<path>/cron_tracking"


def test_workflows_layer_committed_and_every_on_form(tmp_path):
    committed = workflows_layer(REPO)
    files = {row["file"]: row for row in committed["workflows"]}
    assert {"push", "pull_request"} <= set(files["ci.yml"]["triggers"]) and files["ci.yml"]["name"]
    assert any(re.fullmatch(r"[\d*/,-]+( [\d*/,-]+){4}", trigger) for row in files.values() for trigger in row["triggers"])
    assert workflows_layer(tmp_path)["workflows"] is None
    repo = write_repo(tmp_path / "block", workflow="name: Nightly\non:\n  schedule:\n    - cron: '0 13 * * 1-5'\n"
                                                    "  workflow_dispatch:\njobs:\n  x:\n    env: ${{ secrets.TOKEN }}\n")
    row = workflows_layer(repo)["workflows"][0]
    assert row["triggers"] == ["schedule", "0 13 * * 1-5", "workflow_dispatch"] and row["has_secrets_reference"] is True
    (repo / ".github" / "workflows" / "inline.yml").write_text("on: [push, pull_request]\njobs: {}\n", encoding="utf-8")
    (repo / ".github" / "workflows" / "scalar.yaml").write_text("on: push\n", encoding="utf-8")
    rows = {row["file"]: row for row in workflows_layer(repo)["workflows"]}
    assert rows["inline.yml"]["triggers"] == ["push", "pull_request"] and rows["scalar.yaml"]["triggers"] == ["push"]
    assert rows["inline.yml"]["has_secrets_reference"] is False and rows["scalar.yaml"]["name"] is None


def test_launchd_status_plist_and_log_tails(tmp_path):
    home = tmp_path / "home"
    plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    root = tmp_path / "root"
    (root / "console").mkdir(parents=True)
    lines = [f"line {index} at {root} acct {ACCOUNT}" for index in range(60)]
    (root / "console" / "launchd.out.log").write_text("\n".join(lines), encoding="utf-8")
    result = launchd_status([RootSpec.of(tmp_path / "other", "secondary"), RootSpec.of(root)], home=home)
    assert result["plist_present"] is True and result["plist_modified"].endswith(("-04:00", "-05:00"))
    assert result["log_root"] == RootSpec.of(root).label and [log["name"] for log in result["logs"]] == ["launchd.out.log"]
    tail = result["logs"][0]["tail"]
    assert len(tail) == 40 and tail[-1].startswith("line 59") and result["logs"][0]["size"] > 0
    assert str(tmp_path) not in json.dumps(result) and ACCOUNT not in json.dumps(result)
    bare = launchd_status([], home=tmp_path / "nohome")
    assert bare["plist_present"] is False and bare["plist_modified"] is None and bare["logs"] == []
    assert any("No artifact root" in warning for warning in bare["warnings"])


def test_roster_identity_names_every_runner_slot():
    result = roster_identity(CONFIG)
    rows = {row["slot_id"]: row for row in result["slots"]}
    assert list(rows) == ["0935", "1030", "1130", "1230", "1330", "1430", "1530"]
    morning = rows["1030"]
    assert morning["executor_class"] == "MorningDCAExecutor" and morning["cron_id"] == "42173154"
    assert morning["run_dca_entry"] == "run_slot" and morning["window"] == {"start": "10:30", "end": "11:00"}
    assert morning["executor_window"] == {"start": "10:25", "end": "10:35"} and morning["trades"] is True
    assert morning["artifact_dir"] == "morning_dca" and morning["pointer_name"] == "latest_morning_run.json"
    assert morning["config_file"] == "morning_dca.json" and morning["live_execution_enabled"] is None
    assert rows["0935"]["run_dca_entry"] == "disabled" and rows["0935"]["executor_class"] == "MarketOpenDCAExecutor"
    assert rows["1330"]["stream"] == "intraday" and rows["1330"]["executor_class"] == "IntradayDCAExecutor"
    final = rows["1530"]
    assert final["executor_class"] is None and final["cron_id"] == "10ce362c" and final["run_dca_entry"] == "run_slot"
    assert final["artifact_dir"] == "ce16ba6b/scheduled_runs" and final["pointer_name"] == "latest_scheduled_run.json"
    assert ABSOLUTE.search(json.dumps(result)) is None


def test_roster_identity_missing_config_reports_unknown_flag(tmp_path):
    result = roster_identity(tmp_path)
    assert all(row["live_execution_enabled"] is None for row in result["slots"])
    assert any("midday_dca.json is missing" in warning for warning in result["warnings"])
    (tmp_path / "midday_dca.json").write_text(json.dumps({"live_execution_enabled": False}), encoding="utf-8")
    rows = {row["slot_id"]: row for row in roster_identity(tmp_path)["slots"]}
    assert rows["1230"]["live_execution_enabled"] is False


def test_build_schedule_layers_matrix_without_observed(tmp_path):
    result = build_schedule_layers(CONFIG, REPO, [RootSpec.of(tmp_path)], DAY, NOW)
    assert set(result) == {"manifest", "deploy", "workflows", "launchd", "roster", "matrix", "evidence", "warnings"}
    rows = {row["slot_id"]: row for row in result["matrix"]}
    assert len(rows) == 7 and all(row["observed"] is None and row["observed_entry"] is None for row in rows.values())
    assert all(row["deployable"] is True and row["cron_entries"] == 1 for row in rows.values())
    assert "42173154" in rows["1030"]["manifest_task_ids"] and rows["1030"]["configured"] is True
    assert rows["1030"]["systemd_units"] == ["dca-drift-morning.service"]
    assert result["warnings"][-1].endswith("not that any order filled.")


def test_matrix_layers_unavailable_are_none_not_false(tmp_path):
    result = build_schedule_layers(tmp_path / "config", tmp_path / "repo", [], DAY, NOW)
    assert all(row["configured"] is None and row["deployable"] is None for row in result["matrix"])
    assert result["matrix"] and result["manifest"]["task_count"] is None


def test_observed_from_pointer_strip_prefers_primary_and_skips_alias():
    strip = {"streams": [
        pointer_row("morning", "backup", at_risk=True, status="valid"),
        pointer_row("morning", alias=True, status="pending", evidence=False, evidence_label="unverified"),
        pointer_row("morning", status="missing", evidence=False, run_state_present=False, evidence_label="rejected"),
        pointer_row("afternoon", status="missing", evidence=False, run_state_present=False),
        pointer_row("afternoon", status="valid"),
        pointer_row("midday", "backup", at_risk=True),
        "junk", {"no": "stream"},
    ]}
    observed = observed_from_pointer_strip(strip)
    assert set(observed) == {"morning", "afternoon", "midday"}
    # A secondary root's valid pointer never outranks the primary root's canonical row.
    assert observed["morning"]["status"] == "missing" and observed["morning"]["at_risk"] is False
    assert observed["afternoon"]["status"] == "valid" and observed["midday"]["at_risk"] is True
    assert observed_from_pointer_strip(None) == {} and observed_from_pointer_strip({"streams": "x"}) == {}


def test_matrix_observed_from_real_pointer_row_shape(tmp_path):
    """Evidence stays a bool, at_risk and label ride along, and a shared stream never lights three slots."""
    observed = observed_from_pointer_strip({"streams": [
        pointer_row("morning"),
        pointer_row("midday", "backup", at_risk=True, status="valid_recovery_context", evidence=False,
                    evidence_label="recovery context, not scheduled evidence"),
        pointer_row("afternoon", status="pending", evidence=False, evidence_label="unverified", run_state_present=False),
        pointer_row("intraday"),
    ]})
    rows = {row["slot_id"]: row for row in
            build_schedule_layers(CONFIG, REPO, [RootSpec.of(tmp_path)], DAY, NOW, observed=observed)["matrix"]}
    morning = rows["1030"]
    assert morning["observed"] is True and morning["scheduled_evidence"] is True and morning["observed_at_risk"] is False
    assert morning["observed_entry"]["evidence"] is True and morning["observed_entry"]["label"] == "primary"
    assert morning["observed_entry"]["evidence_label"] == "scheduled evidence"
    midday = rows["1230"]
    assert midday["observed"] is True and midday["scheduled_evidence"] is False and midday["observed_at_risk"] is True
    assert midday["observed_entry"]["status"] == "valid_recovery_context"
    final = rows["1530"]  # runner label 'final' joins the runs module's 'afternoon'
    assert final["observed"] is False and final["scheduled_evidence"] is False and final["observed_entry"]["per_slot"]
    for slot_id in ("1130", "1330", "1430"):
        row = rows[slot_id]
        assert row["observed"] is None and row["scheduled_evidence"] is None
        assert row["observed_entry"]["per_slot"] is False and row["observed_entry"]["run_state_present"] is True
        assert "per-stream, not per-slot" in row["observed_note"]
    assert rows["0935"]["observed"] is None and rows["0935"]["observed_note"] is None
    assert rows["1030"]["observed_note"] is None


def test_matrix_slot_keyed_observed_lights_only_that_slot(tmp_path):
    observed = {"1330": pointer_row("intraday"), "intraday@1430": pointer_row("intraday", status="missing", evidence=False,
                                                                                 run_state_present=False)}
    rows = {row["slot_id"]: row for row in
            build_schedule_layers(CONFIG, REPO, [RootSpec.of(tmp_path)], DAY, NOW, observed=observed)["matrix"]}
    assert rows["1330"]["observed"] is True and rows["1330"]["observed_note"] is None
    assert rows["1430"]["observed"] is False and rows["1130"]["observed"] is None
    # A string in the evidence field (the old fixture shape) is unknown, never a label.
    loose = {"1030": {**pointer_row("morning"), "evidence": "pointer", "at_risk": "no"}}
    row = next(row for row in build_schedule_layers(CONFIG, REPO, [], DAY, NOW, observed=loose)["matrix"]
               if row["slot_id"] == "1030")
    assert row["scheduled_evidence"] is None and row["observed_at_risk"] is None and row["observed"] is True


def test_no_secret_or_absolute_path_survives_any_builder(tmp_path):
    leak = f"ledger under {tmp_path}/cron_tracking/morning_dca for account {ACCOUNT}"
    config = write_manifest(tmp_path / "config", [
        {"id": "t1", "name": leak, "type": "trading", "status": "active", "trades": True, "cron_utc": "30 14 * * 1-5",
         "et_slots": ["10:30"], "executor": leak, "notes": leak * 3},
        {"id": "t2", "name": "Backup", "trades": True, "fires_at_et": "10:50", "notes": leak},
    ], policy={"where": leak, "list": [leak] * 3})
    repo = write_repo(tmp_path / "repo", crontab=f"30 14 * * 1-5 {tmp_path}/run.sh --root={tmp_path}/x {ACCOUNT} morning_dca 1030ET\n",
                      workflow=f"name: {leak}\non:\n  push:\n")
    root = tmp_path / "root"
    (root / "console").mkdir(parents=True)
    (root / "console" / "launchd.err.log").write_text(leak + "\n", encoding="utf-8")
    observed = {"morning": pointer_row("morning", label=leak, evidence_label=leak, status=leak)}
    for result in every_builder(config, repo, [RootSpec.of(root)], observed):
        dumped = json.dumps(result)
        assert str(tmp_path) not in dumped and ABSOLUTE.search(dumped) is None
        assert ACCOUNT not in json.dumps(walk_redact(result))
    tasks = manifest_tasks(config, DAY, NOW)["tasks"]
    assert all(len(row["notes"]) <= 303 for row in tasks) and all("<path>/morning_dca" in row["notes"] for row in tasks)
    assert next(row for row in tasks if row["id"] == "t2")["timing"] == "outside this repo"


def test_builders_never_take_a_lock(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a console reader took a lock")

    monkeypatch.setattr(scripts.file_lock, "cross_process_lock", refuse)
    monkeypatch.setattr(dca_runner, "state_transaction", refuse)
    roots = [RootSpec.of(tmp_path)]
    for result in every_builder(CONFIG, REPO, roots, observed_from_pointer_strip({"streams": [pointer_row("morning")]})):
        assert isinstance(result["warnings"], list)
    assert policy_block(CONFIG, "version")["found"] is True


@pytest.mark.parametrize("key", ["tasks", "../x", "", "no_such_policy"])
def test_policy_block_rejects_non_policy_keys(key):
    result = policy_block(CONFIG, key)
    assert result["found"] is False and result["value"] is None


def test_elapsed_live_rows_take_their_own_slots_evidence_verdict(tmp_path, monkeypatch):
    """2026-09-22 schedule audit: an elapsed live row must say verified or missing, never stay 'elapsed_unverified'."""
    from scripts.finalize_run_evidence import finalize

    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", ACCOUNT)
    after_close = datetime(2026, 9, 8, 17, 0, tzinfo=ET)
    root = tmp_path / "cron"
    assert finalize("1030", datetime(2026, 9, 8, 10, 31, tzinfo=ET), outcome="NO_ORDERS",
                    order_submit_status="no_orders_submitted", root=root, now=after_close)["verified"]
    (root / "intraday_dca" / "20260908_1130ET").mkdir(parents=True)  # fired, never finalized

    result = build_schedule_layers(CONFIG, REPO, [RootSpec.of(root)], DAY, after_close)
    manifest = {(row["time"], row["id"]): row for row in result["manifest"]["tasks"]}
    assert manifest[("10:30", "42173154")]["status"] == "verified"
    assert manifest[("11:30", "7be82b97")]["status"] == "evidence_incomplete"
    # The 13:30 row of the same multi-slot task gets ITS slot's verdict, not 11:30's.
    assert manifest[("13:30", "7be82b97")]["status"] == "no_evidence"
    assert manifest[("09:35", "2ba19526")]["evidence"]["run_dir"] == "market_open_dca/20260908_0935ET"
    # Non-live rows keep their calendar/manifest status.
    assert "elapsed_unverified" not in {row["status"] for (time, _), row in manifest.items()
                                         if time in {"09:35", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"}
                                         and row["id"] in {"2ba19526", "42173154", "7be82b97", "a6239dae", "10ce362c"}}
    matrix = {row["slot_id"]: row for row in result["matrix"]}
    assert matrix["1030"]["slot_evidence"] == "verified" and matrix["1130"]["slot_evidence"] == "evidence_incomplete"
    assert any("have no finalized scheduled run_state" in warning for warning in result["warnings"])
    assert not ABSOLUTE.search(json.dumps(result["evidence"]))


def test_slot_evidence_before_the_slot_is_not_due(tmp_path):
    from dca_console_schedule import slot_evidence

    evidence = slot_evidence([RootSpec.of(tmp_path)], DAY, datetime(2026, 9, 8, 9, 0, tzinfo=ET))
    assert {row["status"] for row in evidence.values()} == {"not_due"}
