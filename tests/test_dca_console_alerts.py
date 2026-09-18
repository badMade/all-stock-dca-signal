"""Alerts & health reader regressions: bounds, at-risk flags, leaks, and lock-freedom."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import dca_runner
from dca_console_alerts import (
    MAX_DRIFT_RUNS,
    alert_digest_view,
    alert_inbox,
    attempt_log_view,
    build_alerts_snapshot,
    delivery_channels,
    drift_summary,
    model_usage_view,
    reliability_engines,
    self_heal_view,
)
from dca_console_reader import RootSpec
from dca_console_routes import walk_redact
from scripts import file_lock

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
DAY = date(2026, 9, 8)  # a Tuesday inside the approved trading window
NEXT = date(2026, 9, 9)
NOW = datetime(2026, 9, 9, 16, tzinfo=timezone.utc)
ACCOUNT = "999000111"
ALERT_FILE = f"{DAY.isoformat()}-1030-gate5.txt"
BODY = "Slot: 1030 ET\nGate: 5\nVerdict: GATE5_DISABLED\nDate: 2026-09-08\nNo signal-generation entry point exists.\n"


def write_alert(root, stream_dir="morning_dca", name=ALERT_FILE, body=BODY, day_folder=None):
    folder = root / stream_dir / "alerts"
    if day_folder:
        folder = folder / day_folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(body, encoding="utf-8")
    return folder / name


def write_marker(root, day="20260908", stream="morning", **extra):
    folder = root / "attempt_log" / day
    folder.mkdir(parents=True, exist_ok=True)
    marker = {"stream": stream, "error_class": "RuntimeError", "error_message": "boom", "traceback": "…",
              "triggered_at_utc": "2026-09-08T14:30:00Z", "failure_phase": "before_run_artifact", **extra}
    (folder / f"{stream}.json").write_text(json.dumps(marker), encoding="utf-8")
    with (folder / f"{stream}.attempts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(marker) + "\n")
    return folder


def write_usage(root, month="202609", *entries):
    folder = root / "model_usage" / month
    folder.mkdir(parents=True, exist_ok=True)
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    (folder / "model_usage.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def usage_entry(root, **updates):
    entry = {"run_id": "morning_live_20260908_1030ET", "task_name": "Morning DCA", "task_id": "10ce362c",
             "scheduled_for_et": "10:30", "run_origin": "scheduled", "stream": "morning",
             "model_requested": "cost_conscious_default", "model_actual": "not_exposed",
             "model_routing_mode": "cost_conscious", "reason_for_premium_model": None,
             "triggered_at_utc": "2026-09-08T14:30:00Z", "artifact_path": str(root / "morning_dca" / "20260908_1030ET")}
    entry.update(updates)
    return entry


def roots_for(tmp_path):
    primary, secondary = tmp_path / "cron_tracking", tmp_path / "artifacts"
    primary.mkdir(exist_ok=True)
    secondary.mkdir(exist_ok=True)
    return (RootSpec.of(primary, "primary"), RootSpec.of(secondary, "secondary"))


def test_alert_inbox_reads_both_trees_and_flags_the_secondary_as_at_risk(tmp_path):
    primary, secondary = roots_for(tmp_path)
    write_alert(primary.path)
    write_alert(secondary.path, "intraday_dca", f"{DAY.isoformat()}-1130-gate4.txt", day_folder=DAY.isoformat())
    result = alert_inbox((primary, secondary), DAY, DAY, config_dir=CONFIG)
    assert result["count"] == 2 and result["at_risk_count"] == 1
    by_stream = {row["stream"]: row for row in result["alerts"]}
    assert by_stream["morning"]["at_risk"] is False and by_stream["morning"]["root_label"] == primary.label
    assert by_stream["intraday"]["at_risk"] is True and by_stream["intraday"]["slot"] == "1130"
    assert by_stream["morning"]["slot"] == "1030" and by_stream["morning"]["contract_filename"] is True
    assert by_stream["morning"]["id"].startswith("run-") and by_stream["morning"]["file"].startswith("morning_dca/alerts/")
    assert by_stream["morning"]["delivered"] is None  # a file proves only the repo-file channel
    assert len(by_stream["morning"]["excerpt"]) <= 300 and "[path]" not in by_stream["morning"]["excerpt"]
    assert result["by_slot"] == {"1030": 1, "1130": 1}


def test_alert_inbox_filters_the_window_and_rejects_long_or_inverted_windows(tmp_path):
    primary, secondary = roots_for(tmp_path)
    write_alert(primary.path)
    assert alert_inbox((primary,), NEXT, NEXT, config_dir=CONFIG)["count"] == 0
    with pytest.raises(ValueError):
        alert_inbox((primary,), date(2026, 1, 1), date(2026, 9, 8), config_dir=CONFIG)
    with pytest.raises(ValueError):
        alert_inbox((primary,), NEXT, DAY, config_dir=CONFIG)


def test_alert_inbox_handles_absent_roots_unreadable_files_and_stray_text(tmp_path):
    absent = RootSpec.of(tmp_path / "missing", "primary")
    assert alert_inbox((absent,), DAY, DAY, config_dir=CONFIG) == {
        "alerts": [], "count": 0, "by_gate": {}, "by_slot": {}, "at_risk_count": 0, "warnings": []}
    primary, _ = roots_for(tmp_path)
    write_alert(primary.path).write_bytes(b"\xff\xfe\x00garbage")
    (primary.path / "morning_dca" / "alerts" / "notes.md").write_text("not an alert", encoding="utf-8")
    (primary.path / "morning_dca" / "run_notes.txt").write_text("Gate 5 mentioned outside alerts/", encoding="utf-8")
    result = alert_inbox((primary,), DAY, DAY, config_dir=CONFIG)
    assert result["count"] == 0
    assert any("could not be read" in warning for warning in result["warnings"])


def test_alert_digest_view_keeps_the_digest_vocabulary_and_never_calls_silence_healthy(tmp_path):
    primary, _ = roots_for(tmp_path)
    write_alert(primary.path)
    result = alert_digest_view((primary,), DAY, NEXT, config_dir=CONFIG)
    digest = result["digest"]
    assert digest["schema_version"] == "alert-digest-v1" and digest["totals"]["alerts"] == 1
    assert digest["day_classification"][DAY.isoformat()] == "expected"
    assert digest["day_classification"][NEXT.isoformat()] == "roster_silent"
    assert result["observed_days_supplied"] is False
    assert digest["alerts"][0]["alert_id"] == f"morning_dca/alerts/{DAY.isoformat()}-1030-gate5.txt"
    # A valid pointer for the quiet day is the caller's evidence that the
    # roster ran; only then does the digest stop calling it roster-silent.
    observed = alert_digest_view((primary,), DAY, NEXT, observed_days=[DAY, NEXT], config_dir=CONFIG)
    assert observed["digest"]["day_classification"][NEXT.isoformat()] == "expected"
    assert observed["observed_days_supplied"] is True
    other = alert_digest_view((primary,), DAY, NEXT, config_dir=tmp_path)
    assert any("DCA_CONFIG_DIR" in warning for warning in other["warnings"])


def test_attempt_log_view_reads_markers_from_the_primary_root_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    primary, secondary = roots_for(tmp_path)
    folder = write_marker(primary.path, correlation_id="dca-20260908-morning-1030")
    (folder / "morning.attempts.jsonl").write_text("{not json}\n" + json.dumps({"triggered_at_utc": "x"}) + "\n")
    write_marker(primary.path, day="20260701")  # older than the 30-day window
    result = attempt_log_view((primary, secondary), days=30, now=NOW)
    assert result["root_present"] is True
    kinds = sorted((row["kind"], row["day"]) for row in result["markers"])
    assert kinds == [("attempt", "2026-09-08"), ("failed_start", "2026-09-08")]
    marker = next(row for row in result["markers"] if row["kind"] == "failed_start")
    # The shared account-shaped sanitizer (the same one walk_redact applies at
    # the route boundary) treats the correlation id's bare 8-digit date stamp
    # as account-shaped, so the page sees "dca-[redacted]-morning-1030".
    assert marker["summary"] == {"triggered_at_utc": "2026-09-08T14:30:00Z", "error_class": "RuntimeError",
                                 "correlation_id": "dca-[redacted]-morning-1030", "phase": "before_run_artifact"}
    assert marker["stream"] == "morning" and marker["root_label"] == primary.label
    assert "error_message" not in json.dumps(result) and "traceback" not in json.dumps(result)
    assert any("malformed line" in warning for warning in result["warnings"])


def test_attempt_log_view_honours_the_env_override_at_call_time_and_reports_absence(tmp_path, monkeypatch):
    primary, _ = roots_for(tmp_path)
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    assert attempt_log_view((primary,), now=NOW) == {"markers": [], "root_present": False, "warnings": []}
    assert attempt_log_view((), now=NOW) == {"markers": [], "root_present": False,
                                             "warnings": ["No artifact root is configured."]}
    elsewhere = tmp_path / "elsewhere"
    write_marker(elsewhere, stream="midday")
    monkeypatch.setenv("DCA_ATTEMPT_LOG_ROOT", str(elsewhere / "attempt_log"))
    result = attempt_log_view((primary,), now=NOW)
    assert result["root_present"] is True and {row["stream"] for row in result["markers"]} == {"midday"}


def test_model_usage_view_hashes_run_ids_and_reduces_artifact_paths_to_basenames(tmp_path):
    primary, _ = roots_for(tmp_path)
    write_usage(primary.path, "202609", usage_entry(primary.path), "{broken", "[1, 2]", usage_entry(primary.path, run_id=7))
    result = model_usage_view((primary,), "2026-09")
    assert len(result["entries"]) == 2 and result["month"] == "2026-09"
    first = result["entries"][0]
    assert first["run_id"].startswith("run-") and "morning_live" not in first["run_id"]
    assert first["artifact"] == "20260908_1030ET" and first["stream"] == "morning" and first["root_label"] == primary.label
    assert result["entries"][1]["run_id"] is None
    assert any("malformed line" in warning for warning in result["warnings"])
    assert any("unsupported shape" in warning for warning in result["warnings"])
    assert model_usage_view((primary,), "2026-08") == {"entries": [], "month": "2026-08", "warnings": []}
    with pytest.raises(ValueError):
        model_usage_view((primary,), "202609")


def test_delivery_channels_reports_names_and_flags_but_never_values(tmp_path, monkeypatch):
    import dca_alerts

    # heartbeat_url() resolves the hostname to vet the destination, which is a
    # network call; the console must decide from the variable NAME alone.
    monkeypatch.setattr(dca_alerts, "heartbeat_url", lambda stream: pytest.fail("heartbeat_url resolves DNS"))
    for name in ("DCA_ALERT_SLACK_WEBHOOK", "DCA_DRIFT_SLACK_WEBHOOK", "DCA_HEARTBEAT_URL", "DCA_HEARTBEAT_URL_MORNING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DCA_ALERT_SLACK_WEBHOOK", "https://hooks.example.com/services/T000/B000/secretvalue")
    monkeypatch.setenv("DCA_HEARTBEAT_URL_MORNING", "https://hc.example.com/ping/morning-secret")
    result = delivery_channels(CONFIG)
    rows = {row["name"]: row for row in result["channels"]}
    assert rows["DCA_ALERT_SLACK_WEBHOOK"] == {"name": "DCA_ALERT_SLACK_WEBHOOK", "configured": True, "source": "env",
                                              "status": "configured"}
    assert rows["DCA_DRIFT_SLACK_WEBHOOK"]["status"] == "silent_no_op"
    assert rows["heartbeat:morning"]["configured"] is True and rows["heartbeat:midday"]["configured"] is False
    assert rows["slack"]["source"] == "config" and rows["slack"]["configured"] is False
    assert rows["in_app_message_center"]["configured"] is True
    assert result["routing"]["gate5_disabled_alerts"] == ["in_app_message_center", "email"]
    dumped = json.dumps(result)
    assert "secretvalue" not in dumped and "morning-secret" not in dumped and "hooks.example.com" not in dumped
    missing = delivery_channels(tmp_path)
    assert {row["name"] for row in missing["channels"] if row["source"] == "config"} == set()
    assert any("notifications.json" in warning for warning in missing["warnings"])


def test_drift_summary_runs_the_detector_without_posting_and_caps_the_batch(tmp_path, monkeypatch):
    import scripts.drift_detection as drift

    monkeypatch.setattr(drift, "send_slack_alert", lambda *args, **kwargs: pytest.fail("posted"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "signals.csv").write_text("ticker,auto_buy_signal,incremental_order_amount\nABC,true,25\n", encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text("ticker,order_id,amount_usd\n", encoding="utf-8")
    pairs = [("run-aaaaaaaaaaaa", run_dir), ("run-bbbbbbbbbbbb", tmp_path / "absent")]
    result = drift_summary(pairs)
    drifted, absent = result["runs"]
    assert drifted["label"] == "run-aaaaaaaaaaaa" and drifted["drifted"] is True and drifted["symbols_affected"] == 1
    assert drifted["causes"] and all(isinstance(cause, str) for cause in drifted["causes"])
    assert absent == {"label": "run-bbbbbbbbbbbb", "drifted": None, "causes": [], "symbols_affected": None,
                      "recommended_action": None}
    capped = drift_summary(pairs * (MAX_DRIFT_RUNS // 2 + 1))
    assert len(capped["runs"]) == MAX_DRIFT_RUNS and any("Only the first" in warning for warning in capped["warnings"])


def test_self_heal_view_reads_the_checkout_and_reports_a_rejected_config(tmp_path):
    result = self_heal_view(REPO)
    assert result["config"]["enabled"] is False and result["config"]["max_tick_seconds"] == 600
    assert "ADMITTED" in result["reasons"] and "DISABLED" in result["refusal_reasons"]
    assert result["reasons"] == sorted(result["reasons"])
    workflows = {row["file"]: row for row in result["workflows"]}
    assert workflows["self-heal.yml"]["present"] is True and workflows["self-heal.yml"]["cron"] == ["0 3 * * 1"]
    assert workflows["compute-schedule.yml"]["cron"] == ["0 0 1 * *"]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "self_heal.json").write_text('{"enabled": true}', encoding="utf-8")
    rejected = self_heal_view(tmp_path)
    assert rejected["config"] is None and rejected["reasons"] == result["reasons"]
    assert any(warning.startswith("Self-heal config rejected") for warning in rejected["warnings"])
    assert str(tmp_path) not in json.dumps(rejected)
    assert all(row["present"] is False and row["cron"] == [] for row in rejected["workflows"])


def test_reliability_engines_distinguish_library_only_from_observed_data(tmp_path):
    primary, secondary = roots_for(tmp_path)
    empty = reliability_engines((primary, secondary))
    engines = {row["name"]: row for row in empty["engines"]}
    assert engines["run_ledger"] == {"name": "run_ledger", "has_data": False, "count": 0,
                                     "legend": "library only, no data on this machine"}
    assert engines["scheduler_lease"]["has_data"] is False and engines["correlation"]["has_data"] is None
    assert engines["run_state_machine"]["has_data"] is None and "pending > preflight" in engines["run_state_machine"]["legend"]
    (primary.path / "run_ledger").mkdir()
    (primary.path / "run_ledger" / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    (secondary.path / "2026-09-08__morning__1030.lease").write_text("{}", encoding="utf-8")
    run = primary.path / "morning_dca" / "20260908_1030ET"
    run.mkdir(parents=True)
    (run / "run_state.json").write_text(json.dumps({"correlation_id": "dca-x"}), encoding="utf-8")
    (run / "run_state.corrupt.json").write_text("{", encoding="utf-8")
    write_usage(primary.path, "202609", usage_entry(primary.path))  # a model_usage.jsonl is not a run ledger
    engines = {row["name"]: row for row in reliability_engines((primary, secondary))["engines"]}
    assert engines["run_ledger"]["count"] == 1 and engines["scheduler_lease"]["count"] == 1
    assert engines["correlation"] == {"name": "correlation", "has_data": True, "count": 1,
                                      "legend": "run_state.json files carrying a correlation_id"}


def test_build_alerts_snapshot_composes_every_section_and_merges_warnings(tmp_path, monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    primary, secondary = roots_for(tmp_path)
    write_alert(secondary.path)
    result = build_alerts_snapshot((primary, secondary), DAY, DAY, config_dir=CONFIG, repo=REPO, now=NOW)
    assert result["window"] == {"from": DAY.isoformat(), "to": DAY.isoformat()}
    assert set(result) == {"window", "inbox", "digest", "attempt_log", "channels", "self_heal", "engines", "warnings"}
    assert result["inbox"]["count"] == 1 and result["inbox"]["alerts"][0]["at_risk"] is True
    assert result["digest"]["digest"]["totals"]["alerts"] == 1
    assert result["attempt_log"]["root_present"] is False
    assert "drift" not in result
    assert result["warnings"] == list(dict.fromkeys(result["warnings"]))
    with pytest.raises(ValueError):
        build_alerts_snapshot((primary,), DAY, date(2027, 9, 8), config_dir=CONFIG, repo=REPO)


def test_no_absolute_path_or_account_number_reaches_the_response(tmp_path, monkeypatch):
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    primary, secondary = roots_for(tmp_path)
    poison = f"account {ACCOUNT} written under {tmp_path} by order abc-123"
    write_alert(primary.path, body=BODY + poison + "\n")
    write_alert(secondary.path, "ce16ba6b", f"{DAY.isoformat()}-1530-gate9.txt", body=BODY + poison + "\n")
    write_marker(primary.path, error_class=poison, triggered_at_utc=poison, correlation_id=poison)
    write_usage(primary.path, "202609", usage_entry(primary.path, notes=poison, task_name=poison))
    run = primary.path / "run"
    run.mkdir()
    snapshot = build_alerts_snapshot((primary, secondary), DAY, DAY, config_dir=CONFIG, repo=REPO, now=NOW)
    results = [snapshot, model_usage_view((primary, secondary), "2026-09"), drift_summary([(poison, run)])]
    for result in results:
        raw = json.dumps(result)
        assert str(tmp_path) not in raw, "an absolute path leaked before redaction"
        assert primary.path.name in raw or "cron_tracking" not in raw  # labels may carry the basename only
        redacted = json.dumps(walk_redact(result))
        assert ACCOUNT not in redacted and str(tmp_path) not in redacted
    assert ACCOUNT not in json.dumps(snapshot["inbox"])  # redact_value already ran inside the builder
    assert "[path]" in snapshot["inbox"]["alerts"][0]["excerpt"] and "abc-123" in snapshot["inbox"]["alerts"][0]["excerpt"]


def test_builders_never_take_a_cross_process_lock_or_a_state_transaction(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a read-only console builder took a lock")

    monkeypatch.setattr(file_lock, "cross_process_lock", forbidden)
    monkeypatch.setattr(dca_runner, "state_transaction", forbidden)
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    primary, secondary = roots_for(tmp_path)
    write_alert(primary.path)
    write_marker(primary.path)
    write_usage(primary.path, "202609", usage_entry(primary.path))
    run = primary.path / "run"
    run.mkdir()
    alert_inbox((primary, secondary), DAY, DAY, config_dir=CONFIG)
    alert_digest_view((primary, secondary), DAY, NEXT, observed_days=[DAY], config_dir=CONFIG)
    attempt_log_view((primary, secondary), now=NOW)
    model_usage_view((primary, secondary), "2026-09")
    delivery_channels(CONFIG)
    drift_summary([("run-aaaaaaaaaaaa", run)])
    self_heal_view(REPO)
    reliability_engines((primary, secondary))
    build_alerts_snapshot((primary, secondary), DAY, DAY, config_dir=CONFIG, repo=REPO, now=NOW)
