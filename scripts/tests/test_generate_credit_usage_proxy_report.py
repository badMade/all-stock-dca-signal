"""Tests for scripts/generate_credit_usage_proxy_report.py.

Runnable with ``pytest scripts/tests`` or
``python scripts/tests/test_generate_credit_usage_proxy_report.py``.

Covers the one-time 2026-07-27 4:30 PM ET credit-usage proxy report generator
(config/scheduled_tasks.json -> credit_usage_proxy_report_2026_07_27):
  * a fully empty cron_tracking root classifies every chain slot did_not_run
  * a valid scheduled run classifies as valid_scheduled_evidence
  * a recovery_rerun=true run appears in both scheduled_runs and the recovery
    section
  * a run_dir with no run_state.json is failed_start_no_evidence: the
    scheduled_runs row is a cross-reference only (not counted), and the one
    substantive record lives in the recovery section, forcing error_load to
    high ahead of the plain count thresholds (precedence_rule)
  * model_load derives from premium model_usage.jsonl rows
  * account numbers are masked in the report's own output
  * artifact_load goes high once retry_count reaches 2 (precedence_rule)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

from generate_credit_usage_proxy_report import (  # noqa: E402
    _CONSUMER_REQUIRED_OUTPUTS,
    _RENDERED_PER_RUN_FIELDS,
    ALL_STREAMS,
    RECOVERY_ARTIFACT_TYPES,
    _execution_date_refusal,
    _main,
    generate_report,
    render_markdown,
)

TRADING_DAY = "2026-07-27"


def _run_state(**overrides):
    # cron_id/scheduled_for_et/run_id/triggered_at_utc/state_schema_version
    # are required by the repository classification contract (scripts/
    # run_classification.SCHEDULED_REQUIRED_FIELDS) on every real scheduled
    # run_classification; _identity_matches rejects a classification missing
    # any of them (codex review discussion_r3659124641, discussion_r3660464531),
    # so this default fixture must carry all five to still read as
    # valid_scheduled_evidence.
    base = {
        "run_id": "market_open_live_20260727_0935ET",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "cron_id": "2ba19526",
            "scheduled_for_et": "09:35",
            "run_id": "market_open_live_20260727_0935ET",
            "triggered_at_utc": "2026-07-27T13:35:00Z",
            "state_schema_version": "test-v1",
        },
    }
    base.update(overrides)
    return base


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _set_mtime_on_trading_day(path: Path) -> None:
    """Pin ``path``'s mtime to 3:40 PM ET on the report's trading day.

    _is_current_day_artifact accepts a file either because its path carries
    the day stamp or because its mtime falls on the trading day; a fixture
    file with no day stamp in its name needs the mtime pinned so the test is
    deterministic rather than passing only while the wall clock happens to
    sit on 2026-07-27.
    """
    when = datetime(2026, 7, 27, 15, 40, tzinfo=ZoneInfo("America/New_York")).timestamp()
    os.utime(path, (when, when))


def test_empty_root_all_did_not_run(tmp_path):
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    # 8 chain ids, bb4f6ef1 split into 2, 7be82b97 split into 3, plus the
    # paused e3e6f09e monitor entry reported for completeness only.
    assert len(report["scheduled_runs"]) == 12
    assert all(row["classification"] == "did_not_run" for row in report["scheduled_runs"])
    assert all(row["run_id"] == "not_applicable_did_not_run" for row in report["scheduled_runs"])
    assert report["usage_counts"]["run_count_scheduled"] == 0
    assert report["recovery_and_escalation_runs"] == []
    assert report["proxy_bands"]["run_volume"]["band"] == "low"
    paused_row = next(r for r in report["scheduled_runs"] if r["task_id"] == "e3e6f09e")
    assert paused_row["paused"] is True
    # The monthly model_usage.jsonl is absent from this empty root, so the
    # aggregate is not_available -- NOT a sum of the did_not_run placeholders'
    # definitional zeroes. Emitting 0 here directly contradicted the report's
    # own caveat ("reported not_available, not 0"), and for a stream with no
    # attempt marker a missing artifact cannot prove the invocation consumed
    # no model usage before crashing -- codex review discussion_r3659298011.
    # The zeroes ARE honest when the log exists and a slot simply never ran;
    # test_model_usage_jsonl_row_count_stays_measurable_alongside_did_not_run_slots
    # covers that case.
    assert report["usage_counts"]["model_usage_jsonl_row_count"] == "not_available"


def test_valid_scheduled_run_classified_correctly(tmp_path):
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["run_id"] == "market_open_live_20260727_0935ET"
    assert report["usage_counts"]["run_count_scheduled"] == 1


def test_run_id_nested_only_in_run_classification_is_preserved(tmp_path):
    # _identity_matches() accepts run_id from EITHER rc or the top-level
    # run_state (rc.get("run_id") or run_state.get("run_id")), so a producer
    # that stores it only inside run_classification still passes identity as
    # valid_scheduled_evidence -- but the row's own run_id assignment checked
    # only the top-level field, discarding the real ID and reporting
    # no_run_id_written for a run identity had just accepted. That broke the
    # exact-run-ID contract and hid the run's telemetry from
    # _model_usage_rows_for_run() -- codex review discussion_r3661312420.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        # No top-level run_id at all -- only nested in run_classification.
        {
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "scheduled_for_et": "09:35",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps({"run_id": "market_open_live_20260727_0935ET", "model_routing_mode": "premium"}) + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["run_id"] == "market_open_live_20260727_0935ET"
    # The model_usage row logged under that real ID is actually found.
    assert row["model_usage_rows"] == 1


def test_did_not_run_model_usage_depends_on_attempt_marker_coverage(tmp_path):
    # A did_not_run slot contributes a measured 0 ONLY when its stream writes
    # an attempt_log marker, so "no run directory and no marker" genuinely
    # rules out an attempted-and-crashed invocation. For the five ids with no
    # in-repo marker writer the same evidence cannot distinguish a slot that
    # never fired from one that crashed after consuming model usage, so they
    # contribute not_available -- the report's own caveat already says
    # exactly this, and hardcoding 0 contradicted it -- codex review
    # discussion_r3659713621.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = {(r["task_id"], r["et"]): r for r in report["scheduled_runs"]}
    # Marker-covered: morning, midday, afternoon.
    for task_id, et in (("42173154", "10:30"), ("a6239dae", "12:30"), ("10ce362c", "15:30")):
        assert rows[(task_id, et)]["model_usage_rows"] == 0, task_id
    # Not marker-covered: market_open, both 09:45 sub-invocations, all three
    # intraday slots, and the two consumer-only roles.
    for task_id, et in (
        ("2ba19526", "09:35"), ("bb4f6ef1", "09:45"), ("7be82b97", "11:30"),
        ("67aa802b", "15:35"), ("a46bf007", "15:40"),
    ):
        assert rows[(task_id, et)]["model_usage_rows"] == "not_available", task_id


def test_model_usage_jsonl_row_count_measurable_on_a_fully_covered_day(tmp_path):
    # The aggregate stays a real int when every slot is accounted for -- the
    # not_available propagation above is scoped to genuinely ambiguous
    # absences, not to any day containing a did_not_run row.
    _write_fully_covered_day(tmp_path, extra_entries=[{"run_id": "r1", "model_routing_mode": "cost_conscious"}])
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    # 9 chain runs + 2 consumer rows + the one appended row for r1.
    assert report["usage_counts"]["model_usage_jsonl_row_count"] == 12


def test_recovery_rerun_appears_in_both_sections(tmp_path):
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(recovery_rerun=True, recovery={"trigger_reason": "internal_persistence_failure"}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    scheduled_row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert scheduled_row["classification"] == "valid_scheduled_evidence"

    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "2ba19526"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["source"] == "recovery_rerun"
    assert recovery_rows[0]["triggering_issue"] == "internal_persistence_failure"
    assert report["usage_counts"]["retry_count"] == 1
    # scheduled_recovery_rule: an in-place scheduled recovery is already
    # counted once as the slot's own run_count_scheduled above; it must not
    # ALSO inflate run_count_recovery (that's reserved for a recovery that is
    # its own separate run/artifact, e.g. a manual override).
    assert report["usage_counts"]["run_count_scheduled"] == 1
    assert report["usage_counts"]["run_count_recovery"] == 0


def test_failed_start_is_cross_reference_only_in_scheduled_runs(tmp_path):
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "partial.log").write_text("started but crashed", encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    scheduled_row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert scheduled_row["classification"] == "failed_start_no_evidence"
    assert scheduled_row["run_id"] == "no_run_id_written"
    # failed_start_scheduled_slot_rule: the scheduled_runs row is a
    # cross-reference only, never counted as a scheduled run.
    assert scheduled_row["artifacts_written"] == "cross_reference_only"
    assert report["usage_counts"]["run_count_scheduled"] == 0

    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "2ba19526"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["source"] == "failed_start_escalation"
    assert recovery_rows[0]["classification"] == "failed_start_no_evidence"

    # error_count=1, retry_count=0 would fall in error_load's "moderate" (1 to
    # 3) range on count alone, but any failed scheduled start is always high
    # (precedence_rule) -- codex review discussion_r3657964320.
    assert report["usage_counts"]["error_count"] == 1
    assert report["usage_counts"]["failed_start_escalation_count"] == 1
    assert report["proxy_bands"]["error_load"]["band"] == "high"
    assert report["proxy_bands"]["error_load"]["any_failed_start"] is True
    # The cross-reference row is not double-counted into recovery_and_escalation_count.
    assert report["usage_counts"]["recovery_and_escalation_count"] == 1


def test_attempt_log_marker_evidences_failed_start_over_did_not_run(tmp_path):
    # No run directory at all for 42173154 -- ambiguous on its own -- but a
    # same-day attempt_log marker proves an attempt actually crashed, so this
    # must be failed_start_no_evidence, not did_not_run.
    _write_json(
        tmp_path / "attempt_log" / "20260727" / "morning.json",
        {
            "stream": "morning",
            "error_class": "ConnectionError",
            "error_message": "Robinhood API timed out",
            "triggered_at_utc": "2026-07-27T14:30:00Z",
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    scheduled_row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert scheduled_row["classification"] == "failed_start_no_evidence"

    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["triggering_issue"] == "ConnectionError: Robinhood API timed out"


def test_attempt_log_marker_honors_the_root_env_override(monkeypatch, tmp_path):
    # dca_alerts.write_failed_start_marker resolves its write root as
    # ``root or $DCA_ATTEMPT_LOG_ROOT or DEFAULT_ATTEMPT_LOG_ROOT``, and no
    # production call site passes an explicit root -- so a deployment that
    # sets DCA_ATTEMPT_LOG_ROOT to anything other than
    # <cron_tracking_root>/attempt_log writes markers there instead. The
    # report unconditionally read <cron_root>/attempt_log regardless, so a
    # real failed start persisted successfully but was reported as
    # did_not_run -- codex review discussion_r3660464523.
    from generate_credit_usage_proxy_report import ATTEMPT_LOG_ROOT_ENV

    override_root = tmp_path / "elsewhere" / "attempt_log"
    monkeypatch.setenv(ATTEMPT_LOG_ROOT_ENV, str(override_root))
    _write_json(
        override_root / "20260727" / "morning.json",
        {
            "stream": "morning",
            "error_class": "ConnectionError",
            "error_message": "Robinhood API timed out",
            "triggered_at_utc": "2026-07-27T14:30:00Z",
        },
    )
    # cron_tracking_root's OWN attempt_log subdir is deliberately left empty,
    # to prove the override -- not this default -- is what got read.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    scheduled_row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert scheduled_row["classification"] == "failed_start_no_evidence"
    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["triggering_issue"] == "ConnectionError: Robinhood API timed out"


def test_did_not_run_caveats_flag_streams_with_no_marker_coverage(tmp_path):
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    caveats_text = " ".join(report["caveats"])
    # 2ba19526 (market_open) has no in-repo entrypoint -- did_not_run cannot
    # rule out an unrecorded crash, and the report says so explicitly.
    assert "2ba19526" in caveats_text and "no in-repo entrypoint writes an attempt_log marker" in caveats_text
    # 42173154 (morning) IS covered by guarded_run; its did_not_run caveat
    # must not carry the same "uncovered" disclaimer.
    morning_caveats = [c for c in report["caveats"] if c.startswith("42173154")]
    assert not any("no in-repo entrypoint writes an attempt_log marker" in c for c in morning_caveats)


def test_model_usage_rows_not_available_when_row_missing_even_if_file_exists(tmp_path):
    # The monthly log exists (another run logged fine), but has no row for
    # market_open_live_20260727_0935ET specifically -- this must never read
    # as 0, since log_model_usage() silently swallows its own append
    # failures -- codex review discussion_r3658096711.
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps({"run_id": "some_other_run", "model_routing_mode": "cost_conscious"}) + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["model_usage_rows"] == "not_available"


def test_artifact_load_goes_high_at_two_retries(tmp_path):
    for slot_dir in ("market_open_dca/20260727_0935ET", "morning_dca/20260727_1030ET"):
        _write_json(
            tmp_path / slot_dir / "run_state.json",
            _run_state(recovery_rerun=True, recovery={"trigger_reason": "transient_connector_throttling"}),
        )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["retry_count"] == 2
    assert report["proxy_bands"]["artifact_load"]["band"] == "high"
    assert report["proxy_bands"]["artifact_load"]["retry_count"] == 2


def test_model_load_band_from_premium_rows(tmp_path):
    # Built on a fully covered day: a did_not_run slot with no attempt-marker
    # writer now contributes not_available (discussion_r3659713621), which
    # would make model_usage_jsonl_row_count -- and therefore model_load
    # itself -- unavailable, leaving this band assertion vacuous.
    _write_fully_covered_day(
        tmp_path,
        extra_entries=[
            {"run_id": "r1", "model_routing_mode": "cost_conscious"},
            {"run_id": "r1", "model_routing_mode": "premium"},
            # A different run entirely -- must not count toward today's total.
            {"run_id": "some_other_days_run", "model_routing_mode": "premium"},
        ],
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["proxy_bands"]["model_load"]["observed"] == 1
    assert report["proxy_bands"]["model_load"]["band"] == "moderate"


def test_model_load_low_when_only_default_routing(tmp_path):
    _write_fully_covered_day(
        tmp_path,
        extra_entries=[{"run_id": "r1", "model_routing_mode": "default"}],
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["proxy_bands"]["model_load"]["observed"] == 0
    assert report["proxy_bands"]["model_load"]["band"] == "low"


def test_account_number_is_masked_everywhere_in_output(tmp_path):
    # Synthetic placeholder, not the repo's real account number -- this
    # contract never restates a real account number even for illustration.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "scheduled_for_et": "09:35",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
                "account_number": "123456789",
            }
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["account_number"] == "*****6789"
    assert "123456789" not in json.dumps(report)


def test_top_level_account_number_field_is_not_treated_as_a_fingerprint(tmp_path):
    """near_close_dca.build_run_artifacts's top-level "account_number" field
    is no longer a raw account number -- it is a non-reversible HMAC
    fingerprint (or the "account-fingerprint-unavailable" sentinel), and
    run_classification carries no account_number at all for a
    run_daily_compact-produced artifact, scheduled or manual. Before this
    fix, _account_number_from's fallback read that fingerprint as though it
    were the real account number, masked it, and published a meaningless
    fingerprint suffix under "account_number" -- corrupting account
    attribution for every newly written run (Codex review, PR #911 round
    29: "Stop reporting the fingerprint as an account number")."""
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "scheduled_for_et": "09:35",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
                # No account_number here -- this is the real shape a
                # run_daily_compact-produced artifact's classification has.
            },
            # A 32-character lowercase hex HMAC fingerprint, matching
            # near_close_dca._broker_account_fingerprint's own format --
            # NOT the raw account number.
            account_number="0123456789abcdef0123456789abcdef",
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["account_number"] is None
    assert "0123456789abcdef0123456789abcdef" not in json.dumps(report)


def test_numeric_hmac_like_top_level_account_number_is_not_reported(tmp_path):
    """A digit-only fingerprint must still fail the legacy account-number check."""
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(account_number="1" * 32),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["account_number"] is None


def test_top_level_account_number_field_still_reports_a_legacy_raw_number(tmp_path):
    """The mirror case: an artifact written BEFORE the fingerprint change
    genuinely stored the raw account number in the top-level field. A
    purely numeric top-level value must still be read and masked as an
    account number -- this fix narrows the fallback, it does not remove
    it."""
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "scheduled_for_et": "09:35",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
            },
            account_number="123456789",
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["account_number"] == "*****6789"
    assert "123456789" not in json.dumps(report)


def test_manual_recovery_classification_reads_source_artifact(tmp_path):
    # missed_morning_run_recovery_policy / midday_dca_manual_recovery_2026_07_20
    # both stamp same_day_top_up_context_only=true on manual recovery
    # artifacts -- those are top_up_context_only, not manual_context_only.
    _write_json(
        tmp_path / "morning_dca" / "manual_recovery" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "morning_manual_recovery_20260727_1030ET",
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "same_day_top_up_context_only": True,
                "valid_scheduled_evidence": False,
                "trading_day": TRADING_DAY,
            },
        },
    )
    # A manual dry run/diagnostic with no same-day-guard participation stays
    # manual_context_only.
    _write_json(
        tmp_path / "midday_dca" / "manual_runs" / "20260727_1300ET" / "run_state.json",
        {
            "run_id": "midday_manual_diagnostic_20260727_1300ET",
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "trading_day": TRADING_DAY,
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_rows = {r["run_id"]: r for r in report["recovery_and_escalation_runs"]}
    assert recovery_rows["morning_manual_recovery_20260727_1030ET"]["classification"] == "top_up_context_only"
    assert recovery_rows["midday_manual_diagnostic_20260727_1300ET"]["classification"] == "manual_context_only"
    # Neither manual_recovery_classification_rule outcome inflates
    # run_count_recovery differently -- both ARE separate manual artifacts.
    assert report["usage_counts"]["run_count_recovery"] == 2


def test_never_prints_an_exact_credit_or_dollar_figure(tmp_path):
    # The caveats text itself is allowed to say "credit"/"dollar" (it's the
    # rule stating these are never exposed); what must never appear is an
    # actual numeric dollar amount like "$1.23" or a credit-count field.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    dumped = json.dumps(report)
    assert not re.search(r"\$\s*\d", dumped)
    assert "credit_count" not in dumped
    assert "estimated_credits" not in dumped


def test_consumer_task_status_derived_from_model_usage_log(tmp_path):
    # 67aa802b has no dedicated artifact directory, but its own model_usage
    # row proves it fired -- it must not be unconditionally did_not_run. The
    # logged artifact_path must also exist, since the append happens in a
    # finally block (see the failed-output test below).
    # The complete documented output set: a lone file is a failed invocation
    # now (discussion_r3660192605).
    slippage_out = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "slippage_20260727_1535ET",
                "cron_id": "67aa802b",
                "run_origin": "scheduled",
                "stream": "slippage",
                "triggered_at_utc": "2026-07-27T19:35:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": str(slippage_out),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["run_id"] == "slippage_20260727_1535ET"
    assert row["model_usage_rows"] == 1
    assert row["order_attempts"] == 0


def test_consumer_with_missing_output_is_not_scheduled_evidence(tmp_path):
    # dca_reporting.generate_scheduled_reports() appends its model_usage row
    # from a finally block, so a run whose generate_reports() raised still
    # logs one. A logged row whose artifact_path does not exist is therefore a
    # fired-but-failed run, not successful scheduled evidence -- codex review
    # discussion_r3658801008.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "slippage_20260727_1535ET",
                "cron_id": "67aa802b",
                "run_origin": "scheduled",
                "stream": "slippage",
                "triggered_at_utc": "2026-07-27T19:35:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": str(tmp_path / "never-written.json"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    # The scheduled_runs row is a cross-reference; the substantive record is
    # the recovery-section failed-start escalation.
    assert row["classification"] == "failed_start_no_evidence"
    assert row["artifacts_written"] == "cross_reference_only"
    assert report["usage_counts"]["run_count_scheduled"] == 0

    escalations = [
        r for r in report["recovery_and_escalation_runs"]
        if r["task_id"] == "67aa802b" and r["source"] == "failed_start_escalation"
    ]
    assert len(escalations) == 1
    assert report["usage_counts"]["failed_start_escalation_count"] == 1
    assert report["proxy_bands"]["error_load"]["band"] == "high"


def test_non_string_artifact_path_does_not_crash_the_report(tmp_path):
    # scripts/model_usage_log.py documents artifact_path as a string, but a
    # partially corrupted row (e.g. a stray 42) is still a syntactically
    # valid JSON object -- _load_model_usage_entries() accepts it, and a bare
    # Path(logged) on the non-string value raised TypeError, escaping
    # generate_report() entirely and preventing BOTH report outputs from
    # being written -- codex review discussion_r3660464514. It must instead
    # degrade to "no artifact recorded", exactly like a missing artifact_path.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "slippage_20260727_1535ET",
                "cron_id": "67aa802b",
                "run_origin": "scheduled",
                "stream": "slippage",
                "triggered_at_utc": "2026-07-27T19:35:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": 42,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "failed_start_no_evidence"
    assert row["artifacts_written"] == "cross_reference_only"


def test_manual_consumer_rerun_excluded_from_scheduled_evidence(tmp_path):
    # A manual rerun of the read-only slippage/watchdog consumer role still
    # appends a model_usage row for the same task_id/date, but with
    # run_origin='manual' -- it must not be counted as this slot's scheduled
    # evidence (that would both mislabel a manual rerun as scheduled and
    # inflate run_count_scheduled) -- codex review discussion_r3658511867.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "slippage_manual_20260727",
                "cron_id": "67aa802b",
                "run_origin": "manual",
                "stream": "slippage",
                "triggered_at_utc": "2026-07-27T20:10:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": "/tmp/does-not-exist-slippage-manual.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "did_not_run"


def test_consumer_row_artifact_path_is_measured(tmp_path):
    # The matched model_usage entry's artifact_path is a real, measurable
    # file; it must be included in artifacts_written/artifact_bytes rather
    # than reported not_available -- codex review discussion_r3658511877.
    artifact = tmp_path / "watchdog_artifact.json"
    artifact.write_text('{"ok": true}', encoding="utf-8")
    # This filename carries no day stamp, so current-day identity is proven
    # by mtime (see _is_current_day_artifact). Pinned explicitly rather than
    # left at the wall clock, so the test asserts the intended behavior on
    # every future run date instead of only while today happens to be the
    # trading day -- codex review discussion_r3659140605.
    _set_mtime_on_trading_day(artifact)
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "watchdog_20260727_1540ET",
                "cron_id": "a46bf007",
                "run_origin": "scheduled",
                "stream": "watchdog",
                "triggered_at_utc": "2026-07-27T19:40:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": str(artifact),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "a46bf007")
    assert row["artifacts_written"] == [str(artifact)]
    assert row["artifact_bytes"] == artifact.stat().st_size
    assert row["order_attempts"] == 0


def test_canonical_afternoon_manual_runs_root_is_scanned(tmp_path):
    _write_json(
        tmp_path / "ce16ba6b" / "manual_runs" / "final_manual_override_20260727_1536ET" / "run_state.json",
        {
            "run_id": "final_manual_override_20260727_1536ET",
            "trading_day": TRADING_DAY,
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "same_day_top_up_context_only": True,
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_run_ids = {r["run_id"] for r in report["recovery_and_escalation_runs"]}
    assert "final_manual_override_20260727_1536ET" in recovery_run_ids


def test_manual_artifact_top_level_trading_day_is_the_authority(tmp_path):
    # trading_day lives at the TOP LEVEL of run_state for this repo's manual
    # producers; a stale artifact from a different day must be excluded even
    # though its nested run_classification carries no trading_day at all
    # (the old bug: nothing nested -> None -> accepted unconditionally).
    _write_json(
        tmp_path / "morning_dca" / "manual_recovery" / "20260713_1000ET" / "run_state.json",
        {
            "run_id": "stale_manual_recovery_20260713",
            "trading_day": "2026-07-13",
            "run_classification": {"run_origin": "manual", "manual_run": True, "same_day_top_up_context_only": True},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_run_ids = {r["run_id"] for r in report["recovery_and_escalation_runs"]}
    assert "stale_manual_recovery_20260713" not in recovery_run_ids


def test_manual_scan_expands_wildcard_perplexity_override_subroot(tmp_path):
    # MANUAL_SCAN_SUBROOTS carries the wildcard entry
    # "perplexity_primary_*/manual_override_*" for dated Perplexity manual
    # overrides. Joining that literal string onto cron_root produces a path
    # containing "*", which is never a real directory, so the run silently
    # disappeared from the recovery/usage report entirely until the scan
    # expanded it with Path.glob() the same way manual exclusion/top-up
    # context roots already do (Codex review: "Expand wildcard manual roots
    # in the usage report").
    _write_json(
        tmp_path / "perplexity_primary_20260727" / "manual_override_1" / "run_state.json",
        {
            "run_id": "perplexity_override_20260727",
            "trading_day": TRADING_DAY,
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "same_day_top_up_context_only": True,
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_run_ids = {r["run_id"] for r in report["recovery_and_escalation_runs"]}
    assert "perplexity_override_20260727" in recovery_run_ids


def test_recovery_rows_carry_context_only_label(tmp_path):
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(recovery_rerun=True, recovery={"trigger_reason": "transient_connector_throttling"}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["recovery_and_escalation_runs"]
    assert all(r["recovery_context_label"] == "recovery context only" for r in report["recovery_and_escalation_runs"])


def test_recovery_rerun_does_not_double_count_artifacts_or_model_usage(tmp_path):
    # Built on a fully covered day so model_usage_jsonl_row_count is a real
    # int to compare against: an ambiguous did_not_run slot would make it
    # not_available and hide any double-count -- see discussion_r3659713621.
    _write_fully_covered_day(
        tmp_path,
        extra_entries=[{"run_id": "r1", "model_routing_mode": "cost_conscious"}],
        extra_state={
            "market_open_dca/20260727_0935ET": {
                "recovery_rerun": True,
                "recovery": {"trigger_reason": "internal_persistence_failure"},
            }
        },
    )
    baseline = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    # The recovery_rerun row cross-references the SAME artifact and the same
    # model-usage row the scheduled row already counted, so neither aggregate
    # moves relative to a day without the rerun flag.
    assert baseline["usage_counts"]["retry_count"] == 1
    assert any(r["source"] == "recovery_rerun" for r in baseline["recovery_and_escalation_runs"])

    clean = tmp_path / "no_rerun"
    _write_fully_covered_day(clean, extra_entries=[{"run_id": "r1", "model_routing_mode": "cost_conscious"}])
    reference = generate_report(trading_day=TRADING_DAY, cron_tracking_root=clean)
    assert baseline["usage_counts"]["artifact_count"] == reference["usage_counts"]["artifact_count"]
    assert (
        baseline["usage_counts"]["model_usage_jsonl_row_count"]
        == reference["usage_counts"]["model_usage_jsonl_row_count"]
        == 12
    )


def test_identity_mismatch_downgrades_to_failed_start(tmp_path):
    # A run_state at 42173154's expected path but carrying a DIFFERENT
    # task's cron_id is not valid evidence for this slot.
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "wrong_stream_run",
            "run_classification": {"run_origin": "scheduled", "manual_run": False, "cron_id": "a6239dae"},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "failed_start_no_evidence"


def test_prior_day_artifact_with_no_trading_day_field_is_rejected(tmp_path):
    # The optional top-level trading_day field is a manual-recovery-producer
    # convention, not something every scheduled classification stamps. When
    # it's absent, nothing previously caught a stale prior-day artifact (a
    # real, otherwise-complete July 24 classification) copied or left under
    # today's (July 27) run directory -- run_id is deliberately not
    # date-validated for most slots either. triggered_at_utc IS required and
    # every real producer stamps it, so its own ET trading date must now be
    # compared against the day this report covers -- codex review
    # discussion_r3660858026.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        {
            "run_id": "market_open_live_20260724_0935ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "scheduled_for_et": "09:35",
                "run_id": "market_open_live_20260724_0935ET",
                # A real July 24 timestamp -- no top-level/nested trading_day
                # field at all, exactly the shape this fix must catch.
                "triggered_at_utc": "2026-07-24T13:35:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_malformed_triggered_at_utc_is_rejected(tmp_path):
    # A required field that doesn't parse as the documented timestamp format
    # is as untrustworthy as a missing one -- it must fail identity, not pass
    # for lack of a comparable date.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(run_classification={
            "run_origin": "scheduled",
            "manual_run": False,
            "cron_id": "2ba19526",
            "scheduled_for_et": "09:35",
            "run_id": "market_open_live_20260727_0935ET",
            "triggered_at_utc": "not-a-real-timestamp",
            "state_schema_version": "test-v1",
        }),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"


def test_incomplete_scheduled_classification_rejected_as_failed_start(tmp_path):
    # A crash that leaves a partial-but-syntactically-valid state (only
    # run_origin='scheduled'/manual_run=false, missing cron_id, run_id,
    # triggered_at_utc, and state_schema_version) must not be accepted as
    # valid_scheduled_evidence just because there's nothing to contradict --
    # codex review discussion_r3659124641.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        {"run_id": "market_open_live_20260727_0935ET", "run_classification": {"run_origin": "scheduled", "manual_run": False}},
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_recorded_error_on_completed_run_counts_toward_error_count(tmp_path):
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(order_submit_status="not_submitted_error"),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["recorded_error"] == "order_submit_status=not_submitted_error"
    assert report["usage_counts"]["error_count"] == 1
    # A single completed-run recorded_error is not itself a failed start, so
    # the any_failed_start high override does NOT fire -- codex review
    # discussion_r3658511848. That override (see the failed-start test above)
    # is reserved for an actual failed_start_no_evidence record.
    assert report["usage_counts"]["failed_start_escalation_count"] == 0
    assert report["proxy_bands"]["error_load"]["any_failed_start"] is False
    # Without a definite failed start, error_load leans on connector
    # error/retry telemetry this generator cannot read, so the band is
    # not_available rather than a numeric band derived only from the narrower
    # run-state error count -- codex review discussion_r3658801041. The
    # observed run-state floor is still surfaced.
    assert report["proxy_bands"]["error_load"]["band"] == "not_available"
    assert report["proxy_bands"]["error_load"]["observed"] == 1


def test_sum_int_propagates_unavailable_instead_of_partial_total(tmp_path):
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "morning_live_20260727_1030ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "42173154",
                "run_id": "morning_live_20260727_1030ET",
                "triggered_at_utc": "2026-07-27T14:30:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    # Neither run wrote submitted_orders.csv, so both order_attempts values
    # are "not_logged" -- placed_order_count must not silently read as 0.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["placed_order_count"] == "not_available"
    assert any("placed_order_count" in c for c in report["caveats"])


def test_generate_report_refuses_unsupported_trading_day(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="content-gated"):
        generate_report(trading_day="2027-07-27", cron_tracking_root=tmp_path)


def test_malformed_model_usage_line_makes_whole_file_unavailable(tmp_path):
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps({"run_id": "some_run", "model_routing_mode": "premium"}) + "\n" + "not valid json\n",
        encoding="utf-8",
    )
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["model_usage_rows"] == "not_available"
    assert any("malformed" in c for c in report["caveats"])


def test_absent_month_file_is_not_reported_as_malformed(tmp_path):
    # model_usage/ exists (a DIFFERENT month was logged) but this trading
    # day's own model_usage/202607/model_usage.jsonl is simply absent --
    # that must read as "not found", not "malformed" -- codex review
    # discussion_r3658511882.
    other_month_dir = tmp_path / "model_usage" / "202606"
    other_month_dir.mkdir(parents=True)
    (other_month_dir / "model_usage.jsonl").write_text(
        json.dumps({"run_id": "old_run", "model_routing_mode": "cost_conscious"}) + "\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert not any("malformed" in c for c in report["caveats"])
    assert any("not found" in c and "model_usage" in c for c in report["caveats"])


def test_non_utf8_model_usage_log_is_treated_as_unavailable_not_fatal(tmp_path):
    # A truncated/corrupt model_usage.jsonl raises UnicodeDecodeError from
    # read_text() -- a ValueError subclass, not an OSError -- so it must be
    # caught explicitly rather than escaping generate_report() and preventing
    # both report outputs from being written -- codex review
    # discussion_r3659124627. _load_json already handles this for
    # run_state.json/attempt markers; _load_model_usage_entries did not.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_bytes(b'{"run_id": "r1"}\n\xff\xfe not valid utf-8\n')
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["model_usage_rows"] == "not_available"


def test_model_usage_row_missing_run_id_makes_the_log_unavailable(tmp_path):
    # A syntactically valid JSON object missing its required run_id passed
    # every downstream check unnoticed -- nothing here compares run_id
    # against anything that would raise on absence, it just silently never
    # matches. A real producer's own build_entry() would never let this onto
    # disk, so it is malformed in the same sense a non-object line already
    # is -- codex review discussion_r3661312388.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps({"model_routing_mode": "cost_conscious"}) + "\n", encoding="utf-8"
    )
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["model_usage_rows"] == "not_available"
    assert report["proxy_bands"]["model_load"]["band"] == "not_available"


def test_model_usage_row_invalid_routing_mode_makes_the_log_unavailable(tmp_path):
    # A model_routing_mode outside MODEL_ROUTING_MODE_VALUES ('cost_conscious',
    # 'default', 'premium') -- e.g. a corrupted or partially-written value --
    # previously read as silently non-premium: _premium_run_ids() only ever
    # checks `== "premium"`, so a garbled value understated model_load
    # instead of making it unavailable -- codex review discussion_r3661312388.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps({"run_id": "market_open_live_20260727_0935ET", "model_routing_mode": "prem"}) + "\n",
        encoding="utf-8",
    )
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["model_usage_rows"] == "not_available"
    assert report["proxy_bands"]["model_load"]["band"] == "not_available"


def test_recursive_raw_directory_captures_are_counted(tmp_path):
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "finance_response.json").write_text("{}", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert "raw/finance_response.json" in row["artifacts_written"]
    assert row["artifact_bytes"] == (run_dir / "run_state.json").stat().st_size + len("{}")


def test_run_volume_low_band_covers_all_eleven_baseline_executions(tmp_path):
    # bb4f6ef1 (preflight + watchlist), 7be82b97 x3, plus the other 6
    # single-slot active ids = 11 scheduled executions on a clean day; this
    # must still be "low", not "moderate".
    for slot_dir, cron_id, run_id, extra_rc in [
        ("market_open_dca/20260727_0935ET", "2ba19526", "r1", {"scheduled_for_et": "09:35"}),
        # bb4f6ef1's two 09:45 sub-invocations share a task_id AND an ET, so
        # each must carry its own documented run_type and scheduled run_id for
        # _identity_matches to tell them apart -- codex review
        # discussion_r3658790008, discussion_r3659293057. run_type is required
        # by schemas/morning_run_classification.schema.json and always stamped
        # by the real producers, so a fixture omitting it is not a realistic
        # artifact.
        (
            "morning_dca_preflight/20260727_0945ET",
            "bb4f6ef1",
            "morning_preflight_20260727_0945ET",
            {"run_type": "morning_dca_preflight", "scheduled_for_et": "09:45"},
        ),
        (
            "morning_dca_watchlist/20260727_0945ET",
            "bb4f6ef1",
            "morning_20260727_0945ET",
            {"run_type": "morning_dca_watchlist_scan", "scheduled_for_et": "09:45"},
        ),
        ("morning_dca/20260727_1030ET", "42173154", "r4", {"scheduled_for_et": "10:30"}),
        ("intraday_dca/20260727_1130ET", "7be82b97", "r5", {"scheduled_for_et": "11:30"}),
        ("midday_dca/20260727_1230ET", "a6239dae", "r6", {"scheduled_for_et": "12:30"}),
        ("intraday_dca/20260727_1330ET", "7be82b97", "r7", {"scheduled_for_et": "13:30"}),
        ("intraday_dca/20260727_1430ET", "7be82b97", "r8", {"scheduled_for_et": "14:30"}),
        ("ce16ba6b/scheduled_runs/20260727_1530ET", "10ce362c", "r9", {"scheduled_for_et": "15:30"}),
    ]:
        _write_json(
            tmp_path / slot_dir / "run_state.json",
            {
                "run_id": run_id,
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "cron_id": cron_id,
                    "run_id": run_id,
                    "triggered_at_utc": "2026-07-27T14:30:00Z",
                    "state_schema_version": "test-v1",
                    **extra_rc,
                },
            },
        )
    # Both consumer roles need their logged artifact_path to actually exist to
    # count as successful scheduled evidence -- codex review
    # discussion_r3658801008.
    # 67aa802b needs its COMPLETE documented output set to count as a
    # successful invocation (discussion_r3660192605); a46bf007 has no such
    # documented set, so any current-day file suffices.
    # Neither path carries the day stamp, so both are judged by mtime -- back
    # them onto TRADING_DAY rather than leaving them at "now", which is what
    # made this test fail every day after the one it was written on.
    slip_out = _stamp_to_trading_day(_write_slippage_outputs(tmp_path / "reports" / "slippage"))
    watch_out = tmp_path / "reports" / "watchdog.json"
    watch_out.write_text("{}", encoding="utf-8")
    _stamp_to_trading_day(watch_out)
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    entries = [
        {
            "run_id": "slip",
            "cron_id": "67aa802b",
            "run_origin": "scheduled",
            "stream": "slippage",
            "triggered_at_utc": "2026-07-27T19:35:00Z",
            "model_routing_mode": "cost_conscious",
            "artifact_path": str(slip_out),
        },
        {
            "run_id": "watch",
            "cron_id": "a46bf007",
            "run_origin": "scheduled",
            "stream": "watchdog",
            "triggered_at_utc": "2026-07-27T19:40:00Z",
            "model_routing_mode": "cost_conscious",
            "artifact_path": str(watch_out),
        },
    ]
    model_usage_dir.joinpath("model_usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["run_count_scheduled"] == 11
    assert report["proxy_bands"]["run_volume"]["band"] == "low"


def test_recovery_run_count_by_stream_zero_initialized(tmp_path):
    # An all-clean day with no recovery activity must still report every
    # stream at 0, not an empty mapping consumers can't tell apart from an
    # unsupported/forgotten stream -- codex review discussion_r3658511865.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    by_stream = report["usage_counts"]["recovery_run_count_by_stream"]
    assert set(by_stream) == set(ALL_STREAMS)
    assert all(count == 0 for count in by_stream.values())
    assert sum(by_stream.values()) == report["usage_counts"]["recovery_and_escalation_count"] == 0


def test_manual_recovery_preserves_originating_stream(tmp_path):
    # A manual recovery scanned under a stream-specific root (morning_dca/
    # manual_recovery) must be attributed to that stream, not a synthetic
    # 'recovery' bucket that defeats the per-stream breakdown -- codex
    # review discussion_r3658511853.
    _write_json(
        tmp_path / "morning_dca" / "manual_recovery" / "20260727_1100ET" / "run_state.json",
        {
            "run_id": "morning_manual_recovery_20260727_1100ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["run_id"] == "morning_manual_recovery_20260727_1100ET")
    assert row["stream"] == "morning"
    assert report["usage_counts"]["recovery_run_count_by_stream"]["morning"] == 1
    assert report["usage_counts"]["recovery_run_count_by_stream"]["recovery"] == 0


def test_manual_recovery_normalizes_a_producer_specific_stream_alias(tmp_path):
    # The Jul. 21 midday manual shape recorded stream='midday_manual' -- a
    # producer-specific alias, not one of this report's own canonical stream
    # names. Republishing it verbatim both put a non-canonical value on the
    # row and added an unexpected 'midday_manual' key to
    # recovery_run_count_by_stream alongside the zero-initialized canonical
    # 'midday' -- codex review discussion_r3660464546.
    _write_json(
        tmp_path / "midday_dca" / "manual_recovery" / "20260727_1300ET" / "run_state.json",
        {
            "run_id": "midday_manual_recovery_20260727_1300ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True, "stream": "midday_manual"},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(
        r for r in report["recovery_and_escalation_runs"]
        if r["run_id"] == "midday_manual_recovery_20260727_1300ET"
    )
    assert row["stream"] == "midday"
    by_stream = report["usage_counts"]["recovery_run_count_by_stream"]
    assert "midday_manual" not in by_stream
    assert by_stream["midday"] == 1
    assert set(by_stream) == set(ALL_STREAMS)


def test_manual_recovery_reads_concrete_triggering_issue(tmp_path):
    # A manual recovery artifact that records a concrete cause must surface
    # it, rather than discarding it for the generic 'manual recovery' label
    # -- codex review discussion_r3658511885.
    _write_json(
        tmp_path / "midday_dca" / "manual_recovery" / "20260727_1300ET" / "run_state.json",
        {
            "run_id": "midday_manual_recovery_20260727_1300ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "recovery": {"trigger_reason": "missed the 12:30 PM scheduled slot"},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["run_id"] == "midday_manual_recovery_20260727_1300ET")
    assert row["triggering_issue"] == "missed the 12:30 PM scheduled slot"


def test_manual_recovery_without_recorded_cause_falls_back_to_generic_label(tmp_path):
    _write_json(
        tmp_path / "midday_dca" / "manual_recovery" / "20260727_1305ET" / "run_state.json",
        {
            "run_id": "midday_manual_recovery_20260727_1305ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["run_id"] == "midday_manual_recovery_20260727_1305ET")
    assert row["triggering_issue"] == "manual recovery (reason not recorded)"


def test_manual_recovery_order_attempts_are_counted(tmp_path):
    # A manual recovery directory with a persisted submitted_orders.csv must
    # have its order attempts parsed and included in placed_order_count,
    # not hard-coded not_logged/omitted -- codex review discussion_r3658511891.
    run_dir = tmp_path / "midday_dca" / "manual_recovery" / "20260727_1310ET"
    _write_json(
        run_dir / "run_state.json",
        {
            "run_id": "midday_manual_recovery_20260727_1310ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
        },
    )
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nAAPL,o1,100,filled\nMSFT,o2,50,filled\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["run_id"] == "midday_manual_recovery_20260727_1310ET")
    assert row["order_attempts"] == 2
    assert report["usage_counts"]["placed_order_count"] == 2


def test_manual_recovery_dedupes_mirrored_run_id_across_roots(tmp_path):
    # The documented final-manual-override flow mirrors one execution under
    # both ce16ba6b/manual_runs and midday_dca/manual_reruns; the same run_id
    # must only be counted once, not double-counted as two rows -- codex
    # review discussion_r3658511860.
    shared_run_id = "final_manual_override_20260727_1536ET"
    for subroot in ("ce16ba6b/manual_runs", "midday_dca/manual_reruns"):
        _write_json(
            tmp_path / subroot / shared_run_id / "run_state.json",
            {
                "run_id": shared_run_id,
                "trading_day": TRADING_DAY,
                "run_classification": {
                    "run_origin": "manual",
                    "manual_run": True,
                    "same_day_top_up_context_only": True,
                },
            },
        )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    matching = [r for r in report["recovery_and_escalation_runs"] if r["run_id"] == shared_run_id]
    assert len(matching) == 1
    assert report["usage_counts"]["run_count_recovery"] == 1


def test_failed_start_triggering_issue_redacts_secrets(tmp_path):
    # A live adapter's raised exception can embed a raw broker response,
    # account identifier, or credential; that text must be redacted before
    # it is copied into triggering_issue -- codex review
    # discussion_r3658373978.
    marker = {
        "stream": "morning",
        "error_class": "BrokerError",
        "error_message": "order rejected for account 123456789: Bearer abcXYZ123token invalid",
        "traceback": "Traceback (most recent call last):\n...",
        "triggered_at_utc": "2026-07-27T14:35:00Z",
    }
    _write_json(tmp_path / "attempt_log" / "20260727" / "morning.json", marker)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "failed_start_no_evidence"
    assert "123456789" not in row["triggering_issue"]
    assert "abcXYZ123token" not in row["triggering_issue"]
    assert "[REDACTED]" in row["triggering_issue"]
    assert "BrokerError" in row["triggering_issue"]


def test_main_creates_output_parent_directories(tmp_path):
    # On a fresh artifact tree, credit_usage_proxy_report/20260727/ does not
    # pre-exist; _main must create it rather than raising FileNotFoundError
    # -- codex review discussion_r3658511841 (P1).
    json_out = tmp_path / "reports" / "credit_usage_proxy_report" / "20260727" / "report.json"
    md_out = tmp_path / "reports" / "credit_usage_proxy_report" / "20260727" / "report.md"
    assert not json_out.parent.exists()
    exit_code = _main(
        [
            "--trading-day",
            TRADING_DAY,
            "--cron-tracking-root",
            str(tmp_path / "cron_tracking"),
            "--json-out",
            str(json_out),
            "--md-out",
            str(md_out),
        ]
    )
    assert exit_code == 0
    assert json_out.is_file()
    assert md_out.is_file()
    assert json.loads(json_out.read_bytes())["trading_day"] == TRADING_DAY


def test_main_write_defaults_derives_root_and_paths_without_any_shell_hardcoding(
    monkeypatch, tmp_path
):
    """Codex review: "Keep the report command on the shared artifact root".
    The registered scheduler command used to hardcode a shell
    ${DCA_CRON_TRACKING_ROOT:-/home/user/workspace/cron_tracking} fallback
    for --cron-tracking-root/--json-out/--md-out, which diverges from
    dca_paths.cron_tracking_root()'s own checkout-relative unset-default --
    the same root every other in-repo producer/consumer uses. --write-defaults
    removes the shell-side computation entirely: with no --cron-tracking-root/
    --json-out/--md-out given at all, the script must derive the root via
    cron_tracking_root() and write to default_report_paths()'s exact
    locations, so a scheduler invocation using --write-defaults can never
    drift from the shared root."""
    from scripts.generate_credit_usage_proxy_report import (
        CRON_TRACKING_ROOT_ENV,
        default_report_paths,
    )

    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, str(tmp_path))
    exit_code = _main(
        ["--trading-day", TRADING_DAY, "--write-defaults"]
    )
    assert exit_code == 0

    expected = default_report_paths(TRADING_DAY, tmp_path)
    assert expected["json"].is_file()
    assert expected["md"].is_file()
    assert json.loads(expected["json"].read_bytes())["trading_day"] == TRADING_DAY


def test_main_write_defaults_is_ignored_when_explicit_paths_are_given(tmp_path):
    """--write-defaults must not override an explicitly passed --json-out/
    --md-out -- explicit flags stay authoritative."""
    json_out = tmp_path / "explicit.json"
    md_out = tmp_path / "explicit.md"
    exit_code = _main(
        [
            "--trading-day",
            TRADING_DAY,
            "--cron-tracking-root",
            str(tmp_path / "cron_tracking"),
            "--json-out",
            str(json_out),
            "--md-out",
            str(md_out),
            "--write-defaults",
        ]
    )
    assert exit_code == 0
    assert json_out.is_file()
    assert md_out.is_file()
    assert not (tmp_path / "credit_usage_proxy_report").exists()


def test_non_utf8_run_state_does_not_abort_the_report(tmp_path):
    # UnicodeDecodeError is a ValueError, not an OSError or JSONDecodeError,
    # so an invalid-UTF-8 run_state.json used to escape _load_json and prevent
    # BOTH report artifacts from being generated -- codex review
    # discussion_r3658801044.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_bytes(b'{"run_id": "\xff\xfe bad bytes"}')
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    # Degrades to unavailable evidence for that one run, not a crash.
    assert row["classification"] == "failed_start_no_evidence"


def test_non_object_attempt_marker_is_rejected(tmp_path):
    # A syntactically valid but non-object marker ([1]) reached
    # attempt_marker.get(...) and aborted the whole report with AttributeError
    # -- codex review discussion_r3658688909.
    marker = tmp_path / "attempt_log" / "20260727" / "morning.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("[1]", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    # Unusable marker -> treated as absent, so the slot is an honest did_not_run.
    assert row["classification"] == "did_not_run"


def test_unreadable_artifact_size_makes_bytes_unavailable(tmp_path, monkeypatch):
    # A file that cannot be stat'ed must not be silently dropped from the
    # byte total, which would present a partial sum as complete and could
    # select a lower artifact_load band -- codex review discussion_r3658688914.
    import generate_credit_usage_proxy_report as gen

    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "vanishes.bin").write_text("x" * 32, encoding="utf-8")
    real_stat = Path.stat
    # is_file() also calls stat(), so only the SIZE lookup is made to fail:
    # the first call for this path (the is_file check) succeeds, the second
    # (the st_size read) raises, mimicking a file that disappears mid-scan.
    seen: set = set()

    def flaky_stat(self, *args, **kwargs):
        if self.name == "vanishes.bin":
            key = str(self)
            if key in seen:
                raise OSError("simulated I/O error")
            seen.add(key)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(gen.Path, "stat", flaky_stat)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["artifact_bytes_approx"] == "not_available"
    assert report["proxy_bands"]["artifact_load"]["band"] == "not_available"


def test_watchlist_artifact_cannot_satisfy_the_preflight_slot(tmp_path):
    # Both bb4f6ef1 sub-invocations share a cron ID and scheduled ET, so
    # run_type is the only discriminator -- a watchlist artifact sitting in the
    # preflight directory must not count as the machine-complete preflight the
    # 10:30 gate depends on -- codex review discussion_r3658801012.
    _write_json(
        tmp_path / "morning_dca_preflight" / "20260727_0945ET" / "run_state.json",
        {
            "run_id": "morning_20260727_0945ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "bb4f6ef1",
                "run_type": "morning_dca_watchlist_scan",
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    preflight = next(r for r in report["scheduled_runs"] if r["stream"] == "preflight")
    assert preflight["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_copied_09_45_artifact_is_rejected_on_run_id_when_run_type_is_absent(tmp_path):
    # Same copied-artifact scenario, but the copy records no run_type at all,
    # so the run_type discriminator abstains. The expected run-ID shape is the
    # second discriminator the reviewer asked for, and it still rejects the
    # watchlist id sitting in the preflight directory -- codex review
    # discussion_r3658790008.
    _write_json(
        tmp_path / "morning_dca_preflight" / "20260727_0945ET" / "run_state.json",
        {
            "run_id": "morning_20260727_0945ET",  # the WATCHLIST id
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "bb4f6ef1",
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    preflight = next(r for r in report["scheduled_runs"] if r["stream"] == "preflight")
    assert preflight["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_each_09_45_slot_accepts_its_own_documented_identity(tmp_path):
    # The mirror of the rejection tests: each sub-invocation's own documented
    # run_type and scheduled run_id are accepted, so the discriminators cannot
    # produce a false failed_start_no_evidence on a clean day.
    _write_json(
        tmp_path / "morning_dca_preflight" / "20260727_0945ET" / "run_state.json",
        {
            "run_id": "morning_preflight_20260727_0945ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "bb4f6ef1",
                "scheduled_for_et": "09:45",
                "run_id": "morning_preflight_20260727_0945ET",
                "triggered_at_utc": "2026-07-27T13:45:00Z",
                "state_schema_version": "test-v1",
                "run_type": "morning_dca_preflight",
            },
        },
    )
    _write_json(
        tmp_path / "morning_dca_watchlist" / "20260727_0945ET" / "run_state.json",
        {
            "run_id": "morning_20260727_0945ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "bb4f6ef1",
                "scheduled_for_et": "09:45",
                "run_id": "morning_20260727_0945ET",
                "triggered_at_utc": "2026-07-27T13:45:00Z",
                "state_schema_version": "test-v1",
                "run_type": "morning_dca_watchlist_scan",
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    for stream in ("preflight", "watchlist"):
        row = next(r for r in report["scheduled_runs"] if r["stream"] == stream)
        assert row["classification"] == "valid_scheduled_evidence", stream
    assert report["usage_counts"]["run_count_scheduled"] == 2


def test_non_utf8_submitted_orders_csv_reads_as_not_logged(tmp_path):
    # UnicodeDecodeError is a ValueError, not an OSError, so a partially
    # written or corrupted CSV escaped the OSError-only handler and aborted
    # the whole report. The helper's contract is not_logged for an unreadable
    # CSV -- codex review discussion_r3659293079.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "submitted_orders.csv").write_bytes(b"ticker,order_id\n\xff\xfe binary junk\n")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["order_attempts"] == "not_logged"
    # And the unmeasurable value propagates rather than silently reading as 0.
    assert report["usage_counts"]["placed_order_count"] == "not_available"


def test_zero_byte_submitted_orders_csv_is_not_a_measured_zero(tmp_path):
    # scripts/scheduled_artifacts.py's validate_run_artifacts() treats a
    # zero-byte or truncated-before-the-header submitted_orders.csv as
    # CORRUPT, not as a valid no-order file -- the header row is what proves
    # write_submitted_orders_csv() actually ran. Reading len(rows) - 1
    # unconditionally returned a measured 0 for this shape (an empty file has
    # zero lines), undercounting a possibly-submitted run as a no-op and
    # keeping placed_order_count numeric after the artifact was damaged --
    # codex review discussion_r3660464556.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "submitted_orders.csv").write_text("", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["order_attempts"] == "not_logged"
    assert report["usage_counts"]["placed_order_count"] == "not_available"


def test_malformed_header_submitted_orders_csv_is_not_a_measured_zero(tmp_path):
    # A header truncated mid-write (or simply wrong) is the same corrupt
    # shape as zero bytes: it is not the documented SUBMITTED_ORDERS_FIELDS
    # header, so it cannot be trusted to mean "zero orders."
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "submitted_orders.csv").write_text("tick\n", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["order_attempts"] == "not_logged"


def test_valid_header_zero_data_rows_is_still_a_measured_zero(tmp_path):
    # The mirror: the header ALONE (no data rows) is exactly what a genuine
    # zero-order run writes, and must still read as a real, measured 0.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["order_attempts"] == 0
    assert report["usage_counts"]["placed_order_count"] == 0


def test_non_utf8_model_usage_log_is_treated_as_unavailable(tmp_path):
    # Same ValueError-not-OSError gap on the monthly model log; it must
    # degrade to not_available like any other malformed log rather than
    # aborting generation -- codex review discussion_r3659293067.
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_bytes(b'{"run_id":"x"}\n\xff\xfe not utf8\n')
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["model_usage_rows"] == "not_available"
    assert report["proxy_bands"]["model_load"]["band"] == "not_available"
    assert any("malformed" in c for c in report["caveats"])


def test_09_45_state_missing_run_type_is_not_scheduled_evidence(tmp_path):
    # run_type is the ONLY field that tells the two 09:45 rows apart -- every
    # other identity field they carry is identical by construction -- so
    # treating its absence as "unknown, pass" let a malformed state satisfy
    # EITHER slot and corrupt the scheduled/artifact counts. It is required by
    # schemas/morning_run_classification.schema.json and always stamped by the
    # real producers, so absence is malformed, not merely unknown -- codex
    # review discussion_r3659293057.
    # Deliberately complete in EVERY other respect -- including the four
    # fields SCHEDULED_REQUIRED_FIELDS makes mandatory -- so run_type is the
    # only thing missing and this asserts the run_type rule specifically
    # rather than passing via the generic required-field check.
    _write_json(
        tmp_path / "morning_dca_preflight" / "20260727_0945ET" / "run_state.json",
        {
            "run_id": "morning_preflight_20260727_0945ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "bb4f6ef1",
                "run_id": "morning_preflight_20260727_0945ET",
                "triggered_at_utc": "2026-07-27T13:45:00Z",
                "state_schema_version": "test-v1",
                "scheduled_for_et": "09:45",
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    preflight = next(r for r in report["scheduled_runs"] if r["stream"] == "preflight")
    assert preflight["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_consumer_directory_artifact_path_is_expanded(tmp_path):
    # generate_scheduled_reports() logs artifact_path=str(output_dir) -- a
    # DIRECTORY -- so a bare stat() counted one entry and omitted every report
    # inside it -- codex review discussion_r3658801019.
    out_dir = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "slippage_20260727_1535ET",
                "cron_id": "67aa802b",
                "run_origin": "scheduled",
                "stream": "slippage",
                "triggered_at_utc": "2026-07-27T19:35:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": str(out_dir),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert len(row["artifacts_written"]) == len(SLIPPAGE_OUTPUTS)
    assert row["artifact_bytes"] > 0


def test_failed_start_marker_survives_a_successful_retry(tmp_path):
    # An initial crash (durable marker) followed by a successful external
    # retry (run directory now exists) must still report the earlier failed
    # start, not silently show only the success -- codex review
    # discussion_r3658801030.
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "morning_live_20260727_1030ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "42173154",
                "scheduled_for_et": "10:30",
                "run_id": "morning_live_20260727_1030ET",
                "triggered_at_utc": "2026-07-27T14:30:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    marker = tmp_path / "attempt_log" / "20260727" / "morning.json"
    marker.parent.mkdir(parents=True)
    _write_json(marker, {"error_class": "ConnectionError", "error_message": "broker unreachable"})

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    # The successful retry keeps its own scheduled row.
    assert row["classification"] == "valid_scheduled_evidence"
    escalations = [
        r for r in report["recovery_and_escalation_runs"]
        if r["task_id"] == "42173154" and r["source"] == "failed_start_escalation"
    ]
    assert len(escalations) == 1
    assert "ConnectionError" in escalations[0]["triggering_issue"]
    assert report["usage_counts"]["failed_start_escalation_count"] == 1


def test_orphan_repair_artifact_is_reported(tmp_path):
    # A repair/reconciliation pass can persist submitted_orders.csv with no
    # run_state.json; the run_state-keyed sweep never visited it, so the
    # execution was omitted from the recovery section entirely -- codex review
    # discussion_r3658688900.
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260727_repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nNVDA,o1,2.00,submitted\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if "repair" in str(r["triggering_issue"])]
    assert len(rows) == 1
    assert rows[0]["order_attempts"] == 1
    assert rows[0]["recovery_context_label"] == "recovery context only"
    assert report["usage_counts"]["recovery_and_escalation_count"] == 1


def test_orphan_repair_artifact_from_another_day_is_ignored(tmp_path):
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260101_repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "submitted_orders.csv").write_text("symbol,amount\nNVDA,2.00\n", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["recovery_and_escalation_runs"] == []


def test_premium_routed_run_outranks_equal_row_count_runs(tmp_path):
    # A lone premium escalation tied with routine runs on model row count
    # could be dropped from the stable top-five entirely -- codex review
    # discussion_r3658688929.
    slots = [
        ("market_open_dca/20260727_0935ET", "2ba19526", "r1", "cost_conscious", "09:35"),
        ("morning_dca_preflight/20260727_0945ET", "bb4f6ef1", "morning_preflight_20260727_0945ET", "cost_conscious", "09:45"),
        ("morning_dca_watchlist/20260727_0945ET", "bb4f6ef1", "morning_20260727_0945ET", "cost_conscious", "09:45"),
        ("morning_dca/20260727_1030ET", "42173154", "r4", "cost_conscious", "10:30"),
        ("intraday_dca/20260727_1130ET", "7be82b97", "r5", "cost_conscious", "11:30"),
        # Logged last, so a count-only tie-break would leave it outside the top 5.
        ("midday_dca/20260727_1230ET", "a6239dae", "premium_run", "premium", "12:30"),
    ]
    entries = []
    for slot_dir, cron_id, run_id, mode, scheduled_for_et in slots:
        _write_json(
            tmp_path / slot_dir / "run_state.json",
            {
                "run_id": run_id,
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "cron_id": cron_id,
                    "scheduled_for_et": scheduled_for_et,
                    "run_id": run_id,
                    "triggered_at_utc": "2026-07-27T14:30:00Z",
                    "state_schema_version": "test-v1",
                },
            },
        )
        entries.append(
            {
                "run_id": run_id,
                "cron_id": "x",
                "run_origin": "scheduled",
                "triggered_at_utc": "2026-07-27T14:00:00Z",
                "model_routing_mode": mode,
            }
        )
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    model_usage_dir.joinpath("model_usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    driver_ids = [r["run_id"] for r in report["biggest_usage_drivers"]]
    assert "premium_run" in driver_ids
    assert driver_ids[0] == "premium_run"


def test_connector_heavy_run_with_no_orders_or_premium_still_ranks(tmp_path):
    # A run that pulled 100 connector calls but placed no orders and used no
    # premium model scored 0 for that pull entirely -- _driver_score() had no
    # weight or evidence for connector_calls at all -- so it could be omitted
    # from biggest_usage_drivers even though connector_load itself was high
    # for that run -- codex review discussion_r3660464565.
    _write_fully_covered_day(
        tmp_path,
        extra_state={
            "market_open_dca/20260727_0935ET": {
                "usage_proxy": {"finance_calls_attempted": 60, "robinhood_calls_attempted": 40},
            }
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    driver = next(r for r in report["biggest_usage_drivers"] if r["run_id"] == "r1")
    assert driver["connector_calls"] == 100
    assert "connector_calls=100" in driver["usage_driver_evidence"]


def _write_fully_covered_day(tmp_path, extra_entries=None, extra_state=None):
    """Every active slot ran and wrote its stream's required artifacts.

    This is what a genuinely clean day looks like. An EMPTY artifact tree is
    emphatically not one -- it is the worst possible day, every slot missing
    -- so a clean-day assertion must be built on a covered tree or it asserts
    the opposite of what it claims (see discussion_r3659561357).

    ``extra_entries`` appends additional model_usage.jsonl rows;
    ``extra_state`` maps a slot directory to extra top-level run_state keys.
    Both let a test exercise one variable against an otherwise-covered day --
    which matters for anything reading model_usage_jsonl_row_count, since a
    did_not_run slot with no attempt-marker writer now contributes
    not_available rather than a fabricated 0 (discussion_r3659713621), and
    would otherwise make the aggregate unavailable and the assertion vacuous.
    """
    extra_state = extra_state or {}
    covered_run_ids: list = []
    rc_base = {
        "run_origin": "scheduled",
        "manual_run": False,
        "triggered_at_utc": "2026-07-27T14:30:00Z",
        "state_schema_version": "test-v1",
    }
    orders_header = "ticker,order_id,submitted_amount_usd,status\n"
    for slot_dir, cron_id, run_id, extra_rc, extra_files in [
        ("market_open_dca/20260727_0935ET", "2ba19526", "r1", {"scheduled_for_et": "09:35"},
         ["run_summary.json", "market_open_signals.csv", "submitted_orders.csv"]),
        # validation.csv is one of the four files
        # also_runs_required_preflight_writer.preflight_artifacts.files
        # declares (discussion_r3660192627).
        ("morning_dca_preflight/20260727_0945ET", "bb4f6ef1", "morning_preflight_20260727_0945ET",
         {"run_type": "morning_dca_preflight", "scheduled_for_et": "09:45"},
         ["signals.csv", "validation.csv", "run_summary.json"]),
        ("morning_dca_watchlist/20260727_0945ET", "bb4f6ef1", "morning_20260727_0945ET",
         {"run_type": "morning_dca_watchlist_scan", "scheduled_for_et": "09:45"}, ["watchlist_signals.csv"]),
        ("morning_dca/20260727_1030ET", "42173154", "r4", {"scheduled_for_et": "10:30"}, ["morning_signals.csv"]),
        ("intraday_dca/20260727_1130ET", "7be82b97", "r5", {"scheduled_for_et": "11:30"},
         ["run_summary.json", "intraday_signals.csv", "submitted_orders.csv"]),
        ("midday_dca/20260727_1230ET", "a6239dae", "r6", {"scheduled_for_et": "12:30"}, ["run_summary.json"]),
        ("intraday_dca/20260727_1330ET", "7be82b97", "r7", {"scheduled_for_et": "13:30"},
         ["run_summary.json", "intraday_signals.csv", "submitted_orders.csv"]),
        ("intraday_dca/20260727_1430ET", "7be82b97", "r8", {"scheduled_for_et": "14:30"},
         ["run_summary.json", "intraday_signals.csv", "submitted_orders.csv"]),
        ("ce16ba6b/scheduled_runs/20260727_1530ET", "10ce362c", "r9", {"scheduled_for_et": "15:30"},
         ["run_summary.json", "submitted_orders.csv"]),
    ]:
        run_dir = tmp_path / slot_dir
        _write_json(
            run_dir / "run_state.json",
            {
                "run_id": run_id,
                "run_classification": {**rc_base, "cron_id": cron_id, "run_id": run_id, **extra_rc},
                **extra_state.get(slot_dir, {}),
            },
        )
        for name in extra_files:
            (run_dir / name).write_text(orders_header if "orders" in name else "{}", encoding="utf-8")
        covered_run_ids.append(run_id)
    # The two consumer-only roles prove they fired via model_usage.jsonl plus
    # a real current-day artifact (see _is_current_day_artifact).
    out_dir = tmp_path / "reports" / "20260727"
    out_dir.mkdir(parents=True)
    entries = []
    for run_id, cron_id, stream, hhmm in [
        ("slip", "67aa802b", "slippage", "19:35"),
        ("watch", "a46bf007", "watchdog", "19:40"),
    ]:
        if cron_id in _CONSUMER_REQUIRED_OUTPUTS:
            # 67aa802b must have written its COMPLETE documented output set to
            # count as a successful invocation (discussion_r3660192605), so a
            # covered day writes all four into its own directory.
            artifact = _write_slippage_outputs(out_dir / stream)
        else:
            artifact = out_dir / f"{stream}.json"
            artifact.write_text("{}", encoding="utf-8")
        entries.append({
            "run_id": run_id, "cron_id": cron_id, "run_origin": "scheduled", "stream": stream,
            "triggered_at_utc": f"2026-07-27T{hhmm}:00Z", "model_routing_mode": "cost_conscious",
            "artifact_path": str(artifact),
        })
    # Every covered run logs its own model usage, as a real clean day does.
    # Without this, a run with no matching row reports not_available (a
    # missing row never proves zero usage -- discussion_r3658096711) and the
    # aggregate is unavailable even on a fully covered tree.
    entries.extend(
        {"run_id": run_id, "model_routing_mode": "cost_conscious"} for run_id in covered_run_ids
    )
    entries.extend(extra_entries or [])
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def test_clean_day_emits_the_no_actionable_issues_statement(tmp_path):
    # A deliberately clean report and a generator that simply omitted
    # actionable-issue analysis were indistinguishable -- codex review
    # discussion_r3658688924.
    #
    # Built on a FULLY COVERED tree. This test previously used an empty
    # tmp_path, which is not a clean day at all -- it is 11 missing slots --
    # so it was asserting the exact false-clean output discussion_r3659561357
    # flagged.
    from generate_credit_usage_proxy_report import NO_ACTIONABLE_ISSUES_STATEMENT, render_markdown

    _write_fully_covered_day(tmp_path)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["run_count_scheduled"] == 11
    assert report["actionable_issues"] == []
    markdown = render_markdown(report)
    assert "## Actionable issues" in markdown
    assert NO_ACTIONABLE_ISSUES_STATEMENT in markdown


def test_empty_run_dir_with_marker_agrees_across_both_sections(tmp_path):
    # A pre-created empty run directory plus a real attempt marker used to
    # classify did_not_run in scheduled_runs (markers were suppressed once the
    # directory existed) while _scan_recovery_runs -- which read the marker
    # unconditionally -- emitted a substantive failed-start row for the same
    # slot. The two sections contradicted each other and the scheduled row was
    # not the cross-reference failed_start_scheduled_slot_rule requires --
    # codex review discussion_r3659561339.
    (tmp_path / "morning_dca" / "20260727_1030ET").mkdir(parents=True)
    _write_json(
        tmp_path / "attempt_log" / "20260727" / "morning.json",
        {"stream": "morning", "error_class": "ConnectionError", "error_message": "crashed before writing"},
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    scheduled = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    recovery = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert scheduled["classification"] == "failed_start_no_evidence"
    # The scheduled row is the cross-reference; the substantive record is the
    # single recovery row.
    assert scheduled["triggering_issue"] == "see recovery_and_escalation_runs"
    assert len(recovery) == 1
    assert "ConnectionError" in recovery[0]["triggering_issue"]
    assert report["usage_counts"]["failed_start_escalation_count"] == 1


def test_empty_run_dir_without_marker_says_the_directory_is_empty(tmp_path):
    # The did_not_run text must not claim "no run artifact found" when the
    # directory demonstrably exists -- it is empty, which is a different
    # (and reportable) situation -- codex review discussion_r3659561339.
    (tmp_path / "morning_dca" / "20260727_1030ET").mkdir(parents=True)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "did_not_run"
    assert "exists but is empty" in row["triggering_issue"]


def test_blocking_reason_is_redacted_in_recorded_error(tmp_path):
    # blocking_reason is arbitrary free text and reaches the JSON report, the
    # Markdown actionable-issues list, and biggest_usage_drivers evidence --
    # codex review discussion_r3659561350.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(blocking_reason="halted for account 123456789 after Bearer abcXYZ123token expired"),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["recorded_error"] is not None
    assert "[REDACTED]" in row["recorded_error"]
    # Gone from every rendered surface, not just the row.
    dumped = json.dumps(report)
    assert "123456789" not in dumped
    assert "abcXYZ123token" not in dumped
    assert "abcXYZ123token" not in render_markdown(report)


def test_missing_scheduled_slots_are_actionable(tmp_path):
    # An empty artifact tree means every active slot is missing. The report
    # must say so, not state that no actionable issues were found -- codex
    # review discussion_r3659561357 (P1).
    from generate_credit_usage_proxy_report import NO_ACTIONABLE_ISSUES_STATEMENT, render_markdown

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    missing = [
        r for r in report["scheduled_runs"]
        if r["classification"] == "did_not_run" and not r.get("paused")
    ]
    assert len(missing) == 11
    assert len(report["actionable_issues"]) == 1
    issue = report["actionable_issues"][0]
    assert "11 expected scheduled slot(s) produced no run and no artifacts" in issue
    # Every affected slot is named, so the line is actionable rather than a
    # bare count.
    for task_id in ("2ba19526", "bb4f6ef1", "42173154", "7be82b97", "a6239dae", "10ce362c", "67aa802b", "a46bf007"):
        assert task_id in issue
    markdown = render_markdown(report)
    assert NO_ACTIONABLE_ISSUES_STATEMENT not in markdown


def test_one_missing_slot_on_an_otherwise_clean_day_is_actionable(tmp_path):
    # The boundary case: a single missed slot must still surface, rather than
    # being lost among ten healthy ones.
    _write_fully_covered_day(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "midday_dca" / "20260727_1230ET")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    issues = [i for i in report["actionable_issues"] if "produced no run" in i]
    assert len(issues) == 1
    assert "a6239dae (midday, 12:30)" in issues[0]


def test_actionable_issues_lists_failed_starts(tmp_path):
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "partial.log").write_text("crashed", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    from generate_credit_usage_proxy_report import NO_ACTIONABLE_ISSUES_STATEMENT, render_markdown

    assert report["actionable_issues"]
    assert any("failed scheduled start" in issue for issue in report["actionable_issues"])
    markdown = render_markdown(report)
    assert NO_ACTIONABLE_ISSUES_STATEMENT not in markdown


def test_markdown_bands_render_deciding_values_and_thresholds(tmp_path):
    # artifact_load can go high on bytes or retry_count while `observed` (the
    # file count) stays small, so rendering only `observed` produced a
    # misleading line with the deciding value invisible -- codex review
    # discussion_r3658801025.
    from generate_credit_usage_proxy_report import render_markdown

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    markdown = render_markdown(report)
    artifact_line = next(line for line in markdown.splitlines() if line.startswith("- `artifact_load`"))
    assert "bytes:" in artifact_line
    assert "retry_count:" in artifact_line
    assert "thresholds:" in artifact_line


def test_markdown_cells_escape_pipes_and_newlines(tmp_path):
    # A trigger reason carrying a pipe or a newline would otherwise split the
    # row into extra columns or start a bogus row, silently misaligning the
    # per-run evidence against its headers -- codex review
    # discussion_r3659140614.
    _write_json(
        tmp_path / "midday_dca" / "manual_recovery" / "20260727_1300ET" / "run_state.json",
        {
            "run_id": "midday_manual_recovery_20260727_1300ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "recovery": {"trigger_reason": "provider failed | retry blocked\nsecond line"},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    markdown = render_markdown(report)
    row_line = next(line for line in markdown.splitlines() if "provider failed" in line)
    # The raw pipe is escaped and the newline is folded, so the row still has
    # exactly the columns its header declares.
    assert r"provider failed \| retry blocked" in row_line
    assert "<br>second line" in row_line
    # The escaped pipe is no longer a column delimiter, so the recovery row
    # still renders exactly its declared columns (the per-run fields plus
    # recovery_context_label). Counting UNESCAPED bars is what the Markdown
    # renderer itself keys off.
    unescaped_bars = len(re.findall(r"(?<!\\)\|", row_line))
    # Counted against the RENDERED field list (the task_id/et identity columns
    # plus the per-run fields), not PER_RUN_REQUIRED_FIELDS alone -- codex
    # review discussion_r3659298029.
    assert unescaped_bars == len(_RENDERED_PER_RUN_FIELDS) + 2  # n+1 delimiters for n+1 cells
    # And the multiline reason did not spawn a second table row.
    assert sum(1 for line in markdown.splitlines() if "provider failed" in line) == 1


def test_repeated_same_day_failures_are_each_reported(tmp_path):
    # write_failed_start_marker appends every attempt to <stream>.attempts.jsonl
    # because <stream>.json is a single overwritten path; the report must
    # consume all of them or repeated failures collapse into one -- codex
    # review discussion_r3659140585.
    attempts = [
        {"stream": "morning", "error_class": "ConnectionError", "error_message": "first failure"},
        {"stream": "morning", "error_class": "TimeoutError", "error_message": "second failure"},
    ]
    history = tmp_path / "attempt_log" / "20260727" / "morning.attempts.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("\n".join(json.dumps(a) for a in attempts) + "\n", encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(rows) == 2
    assert "first failure" in rows[0]["triggering_issue"]
    assert "second failure" in rows[1]["triggering_issue"]
    assert report["usage_counts"]["failed_start_escalation_count"] == 2
    assert report["usage_counts"]["error_count"] == 2
    assert report["usage_counts"]["recovery_count_by_artifact_type"]["failed_scheduled_start"] == 2


def test_single_marker_without_history_still_reported(tmp_path):
    # A marker written before the append-only history existed (or whose append
    # failed) must still be read, so the fallback path stays exercised.
    _write_json(
        tmp_path / "attempt_log" / "20260727" / "morning.json",
        {"stream": "morning", "error_class": "ConnectionError", "error_message": "only failure"},
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(rows) == 1
    assert "only failure" in rows[0]["triggering_issue"]


def test_evidenced_external_retry_counts_toward_retry_count(tmp_path):
    # A crash that left a marker followed by a later invocation that DID write
    # the run directory is an observed retry, even though the succeeding run
    # never stamped recovery_rerun -- codex review discussion_r3659140576.
    _write_json(
        tmp_path / "attempt_log" / "20260727" / "morning.json",
        {"stream": "morning", "error_class": "ConnectionError", "error_message": "crashed before the retry"},
    )
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "morning_live_20260727_1030ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "42173154",
                "scheduled_for_et": "10:30",
                "run_id": "morning_live_20260727_1030ET",
                "triggered_at_utc": "2026-07-27T14:30:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "valid_scheduled_evidence"
    # Previously 0: the retry was invisible because no recovery_rerun flag exists.
    assert report["usage_counts"]["retry_count"] == 1


def test_manual_recovery_reason_is_redacted(tmp_path):
    # trigger_reason/blocking_reason are arbitrary persisted free text and can
    # carry a credential or account identifier, exactly like an attempt
    # marker's exception message -- codex review discussion_r3659140593.
    _write_json(
        tmp_path / "midday_dca" / "manual_recovery" / "20260727_1300ET" / "run_state.json",
        {
            "run_id": "midday_manual_recovery_20260727_1300ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "recovery": {"trigger_reason": "reconciling account 123456789 after Bearer abcXYZ123token expired"},
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(
        r for r in report["recovery_and_escalation_runs"] if r["run_id"] == "midday_manual_recovery_20260727_1300ET"
    )
    assert "123456789" not in row["triggering_issue"]
    assert "abcXYZ123token" not in row["triggering_issue"]
    assert "[REDACTED]" in row["triggering_issue"]
    # And the secret never reaches either rendered artifact.
    assert "123456789" not in json.dumps(report)
    assert "abcXYZ123token" not in render_markdown(report)


def test_execution_year_guard_refuses_an_annual_cron_refire(tmp_path):
    # The registered command hardcodes --trading-day 2026-07-27, so
    # generate_report()'s own SUPPORTED_TRADING_DAY gate can never notice a
    # 2027 re-fire of '30 20 27 7 *'. _main compares the ACTUAL execution date
    # -- the one input the hardcoded command cannot pin -- and refuses --
    # codex review discussion_r3659140568.
    assert _execution_date_refusal(TRADING_DAY, "2026-07-27") is None
    # A same-year manual re-run the next day is still allowed.
    assert _execution_date_refusal(TRADING_DAY, "2026-07-28") is None
    refusal = _execution_date_refusal(TRADING_DAY, "2027-07-27")
    assert refusal is not None and "2027-07-27" in refusal


def test_main_refuses_and_writes_nothing_on_a_later_year(tmp_path, monkeypatch):
    import generate_credit_usage_proxy_report as gen

    json_out = tmp_path / "out" / "report.json"
    monkeypatch.setattr(gen, "_execution_date_refusal", lambda *_: "simulated later-year re-fire")
    exit_code = _main(
        [
            "--trading-day",
            TRADING_DAY,
            "--cron-tracking-root",
            str(tmp_path / "cron_tracking"),
            "--json-out",
            str(json_out),
        ]
    )
    assert exit_code == 2
    # Nothing written: the whole point is not overwriting the real report.
    assert not json_out.exists()


def test_main_allows_a_deliberate_replay_with_the_explicit_flag(tmp_path, monkeypatch):
    import generate_credit_usage_proxy_report as gen

    json_out = tmp_path / "out" / "report.json"
    monkeypatch.setattr(gen, "_execution_date_refusal", lambda *_: "simulated later-year re-fire")
    exit_code = _main(
        [
            "--trading-day",
            TRADING_DAY,
            "--cron-tracking-root",
            str(tmp_path / "cron_tracking"),
            "--json-out",
            str(json_out),
            "--allow-execution-date-mismatch",
        ]
    )
    assert exit_code == 0
    assert json_out.is_file()


def test_every_recovery_artifact_type_reports_a_count(tmp_path):
    # recovery_artifact_types_rule: a type with nothing found reports zero
    # rather than being omitted, so a silent miss is distinguishable from a
    # real zero -- codex review discussion_r3659140599.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    by_type = report["usage_counts"]["recovery_count_by_artifact_type"]
    assert set(by_type) == set(RECOVERY_ARTIFACT_TYPES)
    assert all(count == 0 for count in by_type.values())


def test_recovery_rows_classify_into_the_declared_taxonomy(tmp_path):
    # One row per distinct artifact, each filed under exactly one type, and
    # the per-type counts sum to recovery_and_escalation_count.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(recovery_rerun=True, recovery={"trigger_reason": "transient_connector_throttling"}),
    )
    _write_json(
        tmp_path / "morning_dca" / "manual_recovery" / "20260727_1048ET" / "run_state.json",
        {
            "run_id": "morning_manual_recovery_20260727_1048ET",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
            "recovery": {"trigger_reason": "the scheduled 10:30 morning task did not fire"},
        },
    )
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260727_repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "submitted_orders.csv").write_text("ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    by_type = report["usage_counts"]["recovery_count_by_artifact_type"]
    assert by_type["scheduled_recovery_attempt"] == 1
    # missed_morning_run_recovery_policy's own documented shape: a manual
    # recovery for a slot that never fired.
    assert by_type["missed_run_recovery_artifact"] == 1
    # An orphan submitted_orders.csv with no run_state.json is exactly
    # recovery_artifact_types_rule's order_placement_recovery_artifact.
    assert by_type["order_placement_recovery_artifact"] == 1
    assert sum(by_type.values()) == report["usage_counts"]["recovery_and_escalation_count"] == 3


def test_missed_order_wording_is_not_a_missed_run(tmp_path):
    # "missed fill for AAPL" / "reconcile missed order" describe a missed
    # ORDER on a slot that DID fire, not a missed RUN -- a bare `missed`
    # alternative mis-filed both as missed_run_recovery_artifact, corrupting
    # the per-type inventory whose missed-run category specifically means
    # the slot never fired -- codex review discussion_r3660464552.
    for index, reason in enumerate(("missed fill for AAPL", "reconcile missed order")):
        _write_json(
            tmp_path / "morning_dca" / "manual_recovery" / f"20260727_104{index}ET" / "run_state.json",
            {
                "run_id": f"morning_manual_recovery_missed_order_{index}",
                "trading_day": TRADING_DAY,
                "run_classification": {"run_origin": "manual", "manual_run": True},
                "recovery": {"trigger_reason": reason},
            },
        )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    by_type = report["usage_counts"]["recovery_count_by_artifact_type"]
    assert by_type["missed_run_recovery_artifact"] == 0
    assert by_type["manual_recovery_artifact"] == 2


def test_missed_run_wording_still_classifies_as_a_missed_run(tmp_path):
    # The mirror: wording that actually identifies a missed RUN/SLOT (not
    # just any use of "missed") must still classify correctly.
    for index, reason in enumerate(("missed run for the 10:30 slot", "the 12:30 slot missed entirely")):
        _write_json(
            tmp_path / "morning_dca" / "manual_recovery" / f"20260727_105{index}ET" / "run_state.json",
            {
                "run_id": f"morning_manual_recovery_missed_run_{index}",
                "trading_day": TRADING_DAY,
                "run_classification": {"run_origin": "manual", "manual_run": True},
                "recovery": {"trigger_reason": reason},
            },
        )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    by_type = report["usage_counts"]["recovery_count_by_artifact_type"]
    assert by_type["missed_run_recovery_artifact"] == 2


def test_orphan_repair_artifact_does_not_inflate_run_count_recovery(tmp_path):
    # run_count_recovery is the subset of recovery rows representing an actual
    # additional execution WITH ITS OWN RUN ID; an orphan repair artifact has
    # no run_id, so it must not be counted there (nor inflate run_volume) --
    # codex review discussion_r3659140611.
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260727_repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "submitted_orders.csv").write_text("ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    # It is still reported as a recovery row -- it is real evidence.
    assert report["usage_counts"]["recovery_and_escalation_count"] == 1
    # But it is not an identified execution.
    assert report["usage_counts"]["run_count_recovery"] == 0
    assert report["proxy_bands"]["run_volume"]["observed"] == 0


def _consumer_model_usage(tmp_path, artifact_path):
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True, exist_ok=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps(
            {
                "run_id": "slippage_20260727_1535ET",
                "cron_id": "67aa802b",
                "run_origin": "scheduled",
                "stream": "slippage",
                "triggered_at_utc": "2026-07-27T19:35:00Z",
                "model_routing_mode": "cost_conscious",
                "artifact_path": str(artifact_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_consumer_empty_output_dir_is_not_scheduled_evidence(tmp_path):
    # generate_scheduled_reports() logs artifact_path from a finally block and
    # the logged path is the output DIRECTORY, which it creates before writing
    # anything. An empty directory left behind by a run that then raised must
    # not read as a successful consumer run just because the path exists --
    # codex review discussion_r3659140605.
    out_dir = tmp_path / "consumer_out"
    out_dir.mkdir()
    _consumer_model_usage(tmp_path, out_dir)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_consumer_stale_prior_day_file_is_not_scheduled_evidence(tmp_path):
    # A leftover file from an earlier day inside the logged output directory
    # is not evidence that TODAY's consumer run produced anything -- codex
    # review discussion_r3659140605.
    out_dir = tmp_path / "consumer_out"
    out_dir.mkdir()
    stale = out_dir / "slippage.csv"
    stale.write_text("a,b\n1,2\n", encoding="utf-8")
    old = datetime(2026, 7, 20, 15, 35, tzinfo=ZoneInfo("America/New_York")).timestamp()
    os.utime(stale, (old, old))
    _consumer_model_usage(tmp_path, out_dir)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "failed_start_no_evidence"


def test_consumer_current_day_file_is_scheduled_evidence(tmp_path):
    # The same shape as the two tests above, but with a file this trading day
    # actually produced -- that IS successful consumer evidence.
    out_dir = _write_slippage_outputs(tmp_path / "consumer_out")
    for name in SLIPPAGE_OUTPUTS:
        _set_mtime_on_trading_day(out_dir / name)
    _consumer_model_usage(tmp_path, out_dir)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "valid_scheduled_evidence"


def test_empty_pre_created_run_dir_without_evidence_is_did_not_run(tmp_path):
    # A run_dir that merely exists (e.g. pre-created by deployment or a
    # cleanup tool) with no run_state.json, no other file, and no attempt
    # marker is not itself proof an invocation was attempted -- codex review
    # discussion_r3658509331.
    (tmp_path / "market_open_dca" / "20260727_0935ET").mkdir(parents=True)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "did_not_run"
    assert report["usage_counts"]["error_count"] == 0
    assert report["proxy_bands"]["error_load"]["any_failed_start"] is False


def test_non_empty_run_dir_without_run_state_stays_failed_start(tmp_path):
    # A run_dir with a genuine partial artifact (not merely pre-created
    # empty) still counts as an attempted-and-failed start, matching prior
    # behavior -- codex review discussion_r3658509331.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    run_dir.mkdir(parents=True)
    (run_dir / "partial.log").write_text("started but crashed", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"


def test_recovery_from_escalation_surfaces_without_recovery_rerun_flag(tmp_path):
    # recovery_from_escalation=true is a real, independent recovery signal
    # (stamped by scripts.run_classification.build_raw_persistence_state)
    # that never sets recovery_rerun -- keying recovery-section membership
    # on recovery_rerun alone silently dropped it -- codex review
    # discussion_r3658509308.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(recovery_from_escalation=True),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "2ba19526"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["source"] == "subagent_escalation"
    # A cross-reference of the scheduled row's own artifact -- not double
    # counted into run_count_recovery or artifact_count.
    assert report["usage_counts"]["run_count_recovery"] == 0


def test_parent_recovery_submission_of_a_manual_override_counts_as_a_recovery_run(tmp_path):
    # The documented parent-takes-over-after-subagent-failure shape: a
    # subagent's scheduled attempt failed/escalated, and the PARENT then
    # manually resubmits the SAME slot -- run_origin='manual' (so its own
    # classification is manual_context_only, never counted by
    # run_count_scheduled) with parent_recovery_submission=true. This is a
    # genuinely separate execution with its own run_id, not a cross-
    # reference to an already-counted valid_scheduled_evidence row (that
    # carve-out only applies when the SAME source's own classification IS
    # valid_scheduled_evidence) -- codex review discussion_r3661312409.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        {
            "run_id": "market_open_parent_recovery_20260727_0935ET",
            "parent_recovery_submission": True,
            "recovery": {"trigger_reason": "subagent recovery attempt failed; parent resubmitted"},
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "trading_day": TRADING_DAY,
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "2ba19526"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["source"] == "parent_recovery_submission"
    assert recovery_rows[0]["classification"] == "manual_context_only"
    assert recovery_rows[0]["run_id"] == "market_open_parent_recovery_20260727_0935ET"
    # Not counted via run_count_scheduled (classification isn't
    # valid_scheduled_evidence)...
    assert report["usage_counts"]["run_count_scheduled"] == 0
    # ...so it MUST be counted here, or the execution is invisible to both counters.
    assert report["usage_counts"]["run_count_recovery"] == 1


def test_repair_artifact_filename_surfaces_without_any_recovery_flag(tmp_path):
    # A re-submission/repair/reconciliation artifact can be present under a
    # scheduled run's own directory without the run_state ever setting
    # recovery_rerun/parent_recovery_submission/recovery_from_escalation --
    # codex review discussion_r3658509308.
    run_dir = tmp_path / "intraday_dca" / "20260727_1130ET"
    _write_json(
        run_dir / "run_state.json",
        _run_state(
            run_id="intraday_live_20260727_1130ET",
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "7be82b97",
                "scheduled_for_et": "11:30",
                "run_id": "intraday_live_20260727_1130ET",
                "triggered_at_utc": "2026-07-27T15:30:00Z",
                "state_schema_version": "test-v1",
            },
        ),
    )
    (run_dir / "parent_recovery_submission_summary.json").write_text("{}", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "7be82b97" and r["et"] == "11:30"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["source"] == "repair_artifact"


def test_missing_required_artifact_surfaces_as_actionable_issue(tmp_path):
    # market_open's own documented contract (config/scheduled_tasks.json
    # tasks[2ba19526].artifacts.always_write) requires run_summary.json,
    # market_open_signals.csv, and submitted_orders.csv alongside
    # run_state.json; a directory with only run_state.json must not be
    # silently accepted as complete -- codex review discussion_r3658790021.
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert set(row["missing_required_artifacts"]) == {"run_summary.json", "market_open_signals.csv", "submitted_orders.csv"}
    assert any("missing required artifact" in issue for issue in report["actionable_issues"])


def test_complete_required_artifacts_do_not_trigger_missing_artifact_issue(tmp_path):
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
    (run_dir / "market_open_signals.csv").write_text("ticker\n", encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text("ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["missing_required_artifacts"] == []
    assert not any("missing required artifact" in issue for issue in report["actionable_issues"])


def test_afternoon_latest_run_summary_does_not_substitute_for_the_canonical_one(tmp_path):
    # near_close_dca.py's own artifact check (validate_run_artifacts(...,
    # require_summary=True)) deliberately rejects latest_run_summary.json --
    # the separate post-trade/consumer report dca_reporting writes -- as a
    # stand-in for the canonical run_summary.json, since the canonical file
    # must mirror the run state's own classification. Accepting the any-of
    # SUMMARY_FILENAMES group here let an afternoon run missing its canonical
    # summary report no missing artifact as long as latest_run_summary.json
    # happened to exist -- codex review discussion_r3660464558.
    run_dir = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260727_1530ET"
    _write_json(
        run_dir / "run_state.json",
        _run_state(
            run_id="afternoon_live_20260727_1530ET",
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "10ce362c",
                "scheduled_for_et": "15:30",
                "run_id": "afternoon_live_20260727_1530ET",
                "triggered_at_utc": "2026-07-27T19:30:00Z",
                "state_schema_version": "test-v1",
            },
        ),
    )
    (run_dir / "latest_run_summary.json").write_text("{}", encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "10ce362c")
    assert row["missing_required_artifacts"] == ["run_summary.json"]
    assert any("run_summary.json" in issue for issue in report["actionable_issues"])


def test_afternoon_canonical_run_summary_satisfies_the_requirement(tmp_path):
    # The mirror: the canonical file alone (no latest_run_summary.json) must
    # still be accepted -- this is the shape a genuinely complete run writes.
    run_dir = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260727_1530ET"
    _write_json(
        run_dir / "run_state.json",
        _run_state(
            run_id="afternoon_live_20260727_1530ET",
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "10ce362c",
                "scheduled_for_et": "15:30",
                "run_id": "afternoon_live_20260727_1530ET",
                "triggered_at_utc": "2026-07-27T19:30:00Z",
                "state_schema_version": "test-v1",
            },
        ),
    )
    (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "10ce362c")
    assert row["missing_required_artifacts"] == []


def test_non_utf8_submitted_orders_csv_is_not_logged_not_fatal(tmp_path):
    # UnicodeDecodeError is a ValueError subclass, not an OSError, so a
    # truncated submitted_orders.csv escaped _count_submitted_orders and
    # aborted the whole report instead of degrading that run's order count --
    # codex review discussion_r3659297982.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "submitted_orders.csv").write_bytes(b"ticker,order_id\n\xff\xfe truncated\n")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["order_attempts"] == "not_logged"


def test_repair_csv_beside_malformed_run_state_is_still_reported(tmp_path):
    # The run_state-keyed sweep skips a directory whose run_state.json will
    # not parse, and the orphan sweep used to skip it too on a bare is_file()
    # check -- so the persisted reconciliation attempt vanished from the
    # recovery rows, order totals, and proxy bands entirely -- codex review
    # discussion_r3659297993.
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260727_repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "run_state.json").write_text("{not valid json", encoding="utf-8")
    (repair_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nNVDA,o1,2.00,submitted\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if "repair" in str(r["triggering_issue"])]
    assert len(rows) == 1
    assert rows[0]["order_attempts"] == 1
    assert report["usage_counts"]["recovery_and_escalation_count"] == 1


def test_valid_run_state_beside_repair_csv_is_not_double_reported(tmp_path):
    # The mirror of the test above: a PARSEABLE run_state.json means the first
    # sweep already made a deliberate decision about the directory, so the
    # orphan sweep must not emit a second row for the same execution.
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260727_repair"
    _write_json(
        repair_dir / "run_state.json",
        {
            "run_id": "manual_repair_20260727",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
        },
    )
    (repair_dir / "submitted_orders.csv").write_text("ticker,order_id\nNVDA,o1\n", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert len(report["recovery_and_escalation_runs"]) == 1
    assert report["recovery_and_escalation_runs"][0]["run_id"] == "manual_repair_20260727"


def test_usage_proxy_errors_count_is_a_recorded_error(tmp_path):
    # _derive_order_submit_status makes the whole run 'submitted' as soon as
    # one order lands, so a run that placed one order and errored on another
    # candidate has no *_error status and no blocking_reason -- yet
    # usage_proxy.errors_count records the failure -- codex review
    # discussion_r3659298024.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(order_submit_status="submitted", usage_proxy={"errors_count": 2}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "valid_scheduled_evidence"
    assert row["recorded_error"] == "usage_proxy.errors_count=2"
    assert report["usage_counts"]["error_count"] == 1
    assert any("recorded run error" in issue for issue in report["actionable_issues"])


def test_zero_usage_proxy_errors_count_is_not_an_error(tmp_path):
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(order_submit_status="submitted", usage_proxy={"errors_count": 0}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["recorded_error"] is None
    assert report["usage_counts"]["error_count"] == 0


def test_persisted_connector_calls_are_summed_and_banded(tmp_path):
    # near_close_dca.build_usage_proxy() persists these counts on every run it
    # writes; the report discarded them and hardcoded both the counter and the
    # connector_load band to unavailable -- codex review discussion_r3659298016.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(usage_proxy={"finance_calls_attempted": 20, "robinhood_calls_attempted": 12}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["connector_calls"] == 32
    assert report["usage_counts"]["connector_pull_count_where_logged"] == 32
    # The contract bands connector_load SOLELY from this count: 32 <= 50 -> low.
    assert report["proxy_bands"]["connector_load"]["band"] == "low"
    assert not any("connector_load: not_available" in c for c in report["caveats"])


def test_connector_load_bands_climb_with_the_pull_count(tmp_path):
    # 60 + 40 = 100 total -> high (>= 91), per the contract's thresholds.
    for slot_dir, cron_id, run_id, finance, scheduled_for_et in [
        ("market_open_dca/20260727_0935ET", "2ba19526", "r1", 60, "09:35"),
        ("morning_dca/20260727_1030ET", "42173154", "r4", 40, "10:30"),
    ]:
        _write_json(
            tmp_path / slot_dir / "run_state.json",
            {
                "run_id": run_id,
                "run_classification": {
                    "run_origin": "scheduled",
                    "manual_run": False,
                    "cron_id": cron_id,
                    "scheduled_for_et": scheduled_for_et,
                    "run_id": run_id,
                    "triggered_at_utc": "2026-07-27T14:30:00Z",
                    "state_schema_version": "test-v1",
                },
                "usage_proxy": {"finance_calls_attempted": finance},
            },
        )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["connector_pull_count_where_logged"] == 100
    assert report["proxy_bands"]["connector_load"]["band"] == "high"


def test_connector_load_stays_unavailable_without_persisted_counts(tmp_path):
    # No run logged a usage_proxy connector count -> honest not_logged/
    # not_available rather than a fabricated 0/low.
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["connector_pull_count_where_logged"] == "not_logged"
    assert report["proxy_bands"]["connector_load"]["band"] == "not_available"


def test_absent_model_usage_log_makes_the_aggregate_unavailable(tmp_path):
    # With no readable monthly log, the did_not_run placeholders' definitional
    # zeroes must not be summed into a confident 0 that contradicts the
    # report's own caveat -- codex review discussion_r3659298011.
    _write_json(tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json", _run_state())
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["model_usage_jsonl_row_count"] == "not_available"
    # And the caveat and the value now agree with each other.
    assert any("not found" in c and "model_usage" in c for c in report["caveats"])


def test_markdown_tables_carry_task_and_slot_identity(tmp_path):
    # The rendered tables are built from PER_RUN_REQUIRED_FIELDS, which omits
    # task_id and et -- so the human-readable report never showed the eight
    # required task IDs and could not tell 7be82b97's three slots apart --
    # codex review discussion_r3659298029.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    markdown = render_markdown(report)
    header = next(line for line in markdown.splitlines() if line.startswith("| task_id"))
    assert header.startswith("| task_id | et | run_id |")
    for task_id in ("2ba19526", "bb4f6ef1", "42173154", "7be82b97", "a6239dae", "10ce362c", "67aa802b", "a46bf007"):
        assert f"| {task_id} |" in markdown, task_id
    # 7be82b97's three slots are individually distinguishable by their ET.
    for et in ("11:30", "13:30", "14:30"):
        assert f"| 7be82b97 | {et} |" in markdown, et


def test_late_phase_marker_is_not_reported_as_a_failed_start(tmp_path):
    # A run that persisted a valid run_state.json and only THEN raised (the
    # generate_full_reports=True path fails after run_daily_compact() has
    # written the run) leaves a marker whose phase says so. Treating it as a
    # failed start made the report invent an earlier failed start plus a
    # successful retry, inflating recovery/error counts and forcing the high
    # error band -- codex review discussion_r3659298031.
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "morning_live_20260727_1030ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "42173154",
                "scheduled_for_et": "10:30",
                "run_id": "morning_live_20260727_1030ET",
                "triggered_at_utc": "2026-07-27T14:30:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    _write_json(
        tmp_path / "attempt_log" / "20260727" / "morning.json",
        {
            "stream": "morning",
            "error_class": "RuntimeError",
            "error_message": "report generation failed after the run was written",
            "failure_phase": "after_run_artifact",
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "valid_scheduled_evidence"
    # No fabricated failed start, and therefore no forced high error band...
    assert report["usage_counts"]["failed_start_escalation_count"] == 0
    assert report["proxy_bands"]["error_load"]["any_failed_start"] is False
    assert not any(r["source"] == "failed_start_escalation" for r in report["recovery_and_escalation_runs"])
    # ...but the late failure is still a real, counted error on that run.
    assert "after_run_artifact RuntimeError" in row["recorded_error"]
    assert report["usage_counts"]["error_count"] == 1


def test_repeated_late_phase_failures_are_each_counted(tmp_path):
    # write_failed_start_marker() persists one after_run_artifact history
    # entry per attempt: report generation failed, the scheduler retried,
    # and it failed again before eventually succeeding. The scheduled row's
    # own recorded_error reflects only the LATEST attempt -- the earlier one
    # must not simply vanish from error/retry accounting -- codex review
    # discussion_r3661312397.
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        {
            "run_id": "morning_live_20260727_1030ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "42173154",
                "scheduled_for_et": "10:30",
                "run_id": "morning_live_20260727_1030ET",
                "triggered_at_utc": "2026-07-27T14:30:00Z",
                "state_schema_version": "test-v1",
            },
        },
    )
    day_dir = tmp_path / "attempt_log" / "20260727"
    day_dir.mkdir(parents=True)
    first = {
        "stream": "morning", "error_class": "RuntimeError",
        "error_message": "report generation failed the first time",
        "failure_phase": "after_run_artifact", "triggered_at_utc": "2026-07-27T14:32:00Z",
    }
    second = {
        "stream": "morning", "error_class": "RuntimeError",
        "error_message": "report generation failed again on retry",
        "failure_phase": "after_run_artifact", "triggered_at_utc": "2026-07-27T14:35:00Z",
    }
    (day_dir / "morning.attempts.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
    )
    (day_dir / "morning.json").write_text(json.dumps(second), encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "valid_scheduled_evidence"
    # The latest attempt is still this run's own recorded_error.
    assert "report generation failed again on retry" in row["recorded_error"]
    # No fabricated failed start: an after_run_artifact marker is never one.
    assert report["usage_counts"]["failed_start_escalation_count"] == 0
    assert report["proxy_bands"]["error_load"]["any_failed_start"] is False

    late_rows = [r for r in report["recovery_and_escalation_runs"] if r.get("source") == "late_error_escalation"]
    assert len(late_rows) == 1
    assert "report generation failed the first time" in late_rows[0]["triggering_issue"]
    assert late_rows[0]["run_id"] == "morning_live_20260727_1030ET"
    assert late_rows[0]["classification"] == "valid_scheduled_evidence"

    # Both attempts now count: the row's own recorded_error PLUS the earlier
    # late_error_escalation row.
    assert report["usage_counts"]["error_count"] == 2
    # The repeated late failure is an observed retry.
    assert report["usage_counts"]["retry_count"] == 1
    # Not double-counted as a second execution of the same run.
    assert report["usage_counts"]["run_count_recovery"] == 0
    assert report["usage_counts"]["run_count_scheduled"] == 1


def test_pre_artifact_marker_is_still_a_failed_start(tmp_path, monkeypatch):
    # The mirror: a marker whose phase says the crash preceded any artifact
    # (and a legacy marker with no phase field at all) still classifies as a
    # failed start, so the r3659298031 fix cannot mask a genuine one.
    #
    # This test deliberately uses a DIFFERENT cron_tracking_root per marker
    # (so the two iterations don't collide under the same attempt_log path),
    # which conflicts with conftest.py's repo-wide autouse fixture that
    # anchors DCA_ATTEMPT_LOG_ROOT to `tmp_path/attempt_log` for every test --
    # now that the report honors that env var (discussion_r3660464523), it
    # must be cleared here so each iteration's own root is what gets read.
    monkeypatch.delenv("DCA_ATTEMPT_LOG_ROOT", raising=False)
    for marker in (
        {"stream": "morning", "error_class": "ConnectionError", "error_message": "died early",
         "failure_phase": "before_run_artifact"},
        {"stream": "morning", "error_class": "ConnectionError", "error_message": "died early"},
    ):
        root = tmp_path / marker.get("failure_phase", "legacy")
        _write_json(root / "attempt_log" / "20260727" / "morning.json", marker)
        report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=root)
        row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
        assert row["classification"] == "failed_start_no_evidence", marker
        assert report["usage_counts"]["failed_start_escalation_count"] == 1, marker
        assert report["proxy_bands"]["error_load"]["band"] == "high", marker


def test_blocking_reason_is_redacted_before_it_reaches_either_format(tmp_path):
    # recorded_error is republished in the JSON rows AND the Markdown
    # actionable-issues/usage-driver sections, so a blocking_reason copied
    # from a connector exception bypassed the _redact_secrets() protection
    # already applied to recovery reasons and failed-start markers -- codex
    # review discussion_r3659559649 (P1). Synthetic account number.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(blocking_reason="broker rejected for account 123456789 token=abcSECRET123"),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert "[REDACTED]" in row["recorded_error"]
    for leak in ("123456789", "abcSECRET123"):
        assert leak not in json.dumps(report), leak
        assert leak not in render_markdown(report), leak


def test_multiline_recorded_error_does_not_break_markdown_list_rendering(tmp_path):
    # A persisted blocking_reason can carry a raw newline; interpolating it
    # directly into a bullet line wrote the continuation as a new,
    # unindented Markdown line in BOTH the actionable-issues and
    # biggest_usage_drivers sections -- able to render as a bogus heading or
    # extra list item and break the compact human-readable report -- codex
    # review discussion_r3660464570.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(blocking_reason="broker rejected\n## Injected heading\n- injected bullet"),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    # The JSON value itself keeps the real newline -- only rendering escapes it.
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert "\n" in row["recorded_error"]

    markdown = render_markdown(report)
    lines = markdown.splitlines()
    # The injected content must never land on its own raw Markdown line --
    # only embedded (via <br>) inside the line it was interpolated into.
    assert "## Injected heading" not in lines
    assert "- injected bullet" not in lines
    assert "Injected heading" in markdown
    assert "<br>" in markdown


def test_artifact_contract_failure_is_not_scheduled_evidence(tmp_path):
    # near_close_dca persists this marker when artifact validation fails, and
    # run_classification.is_scheduled_state() fails closed on it; keying only
    # off the identity fields admitted a contract-rejected partial run into
    # run_count_scheduled -- codex review discussion_r3659559695.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(artifact_contract_failure={"errors": ["missing market_open_signals.csv"]}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_valid_scheduled_evidence_false_is_not_scheduled_evidence(tmp_path):
    # The same run also sets run_classification.valid_scheduled_evidence=false;
    # honour the producer's own verdict rather than re-deriving it.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
                "valid_scheduled_evidence": False,
            }
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"


def test_morning_partial_submission_denial_is_not_scheduled_evidence(tmp_path):
    # Codex review, PR #538 discussion_r3837630717 ("Clear nested evidence
    # validity on partial failures"): morning_dca.py's partial-persistence-
    # after-submission and preserved-blocked-retry paths used to set only the
    # TOP-LEVEL scheduled_evidence_denied flag, leaving run_classification's
    # own nested valid_scheduled_evidence at its earlier `true` value.
    # _classify_chain_run reads that nested field directly (not the top-level
    # flag), so a run shaped like that -- scheduled_evidence_denied=True at
    # the top level, but valid_scheduled_evidence still true underneath --
    # would be wrongly counted as valid_scheduled_evidence here even though
    # the run genuinely failed after a partial submission. Both fields must
    # now be denied together (scripts.run_classification.deny_scheduled_evidence).
    _write_json(
        tmp_path / "morning_dca" / "20260727_1030ET" / "run_state.json",
        _run_state(
            run_id="morning_live_20260727_1030ET",
            status="blocked",
            block_reason="raw input persistence failed: disk full",
            is_trading_failure=True,
            scheduled_evidence_denied=True,
            run_classification_outcome="failed_after_partial_submission",
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "42173154",
                "scheduled_for_et": "10:30",
                "run_id": "morning_live_20260727_1030ET",
                "triggered_at_utc": "2026-07-27T14:30:00Z",
                "state_schema_version": "test-v1",
                "valid_scheduled_evidence": False,
            },
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "42173154")
    assert row["classification"] == "failed_start_no_evidence"


def test_failed_recovery_rerun_counts_as_a_failed_start(tmp_path):
    # recovery_rerun=true alongside valid_scheduled_evidence=false is the
    # documented shape when a bounded recovery itself fails to produce valid
    # evidence -- classification is failed_start_no_evidence, but the
    # recovery_source branch used to file the row under
    # source='recovery_rerun' regardless. _usage_counts() recognizes a
    # failed start only by source=='failed_start_escalation', so
    # error_count/failed_start_escalation_count stayed 0 and error_load's
    # mandatory any_failed_start override never fired for a genuinely failed
    # scheduled run -- codex review discussion_r3660464539.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(
            recovery_rerun=True,
            recovery={"trigger_reason": "internal_persistence_failure"},
            run_classification={
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "scheduled_for_et": "09:35",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
                "valid_scheduled_evidence": False,
            },
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    scheduled_row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert scheduled_row["classification"] == "failed_start_no_evidence"
    assert scheduled_row["artifacts_written"] == "cross_reference_only"

    recovery_rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "2ba19526"]
    assert len(recovery_rows) == 1
    assert recovery_rows[0]["source"] == "failed_start_escalation"
    assert recovery_rows[0]["classification"] == "failed_start_no_evidence"
    assert recovery_rows[0]["triggering_issue"] == "internal_persistence_failure"
    # The retry is still credited even though the recovery attempt failed.
    assert recovery_rows[0]["recovery_rerun_flagged"] is True

    assert report["usage_counts"]["failed_start_escalation_count"] == 1
    assert report["usage_counts"]["error_count"] == 1
    assert report["proxy_bands"]["error_load"]["band"] == "high"
    assert report["proxy_bands"]["error_load"]["any_failed_start"] is True
    assert report["usage_counts"]["retry_count"] == 1


def _intraday_state(run_id, scheduled_for_et):
    return {
        "run_id": run_id,
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "cron_id": "7be82b97",
            "run_id": run_id,
            "scheduled_for_et": scheduled_for_et,
            "triggered_at_utc": "2026-07-27T15:30:00Z",
            "state_schema_version": "test-v1",
        },
    }


def test_combined_scheduled_for_et_does_not_admit_another_slots_artifact(tmp_path):
    # The legacy combined value lists every intraday slot, so a substring test
    # accepted an 11:30 artifact copied into the 13:30 directory as valid
    # 13:30 evidence -- codex review discussion_r3659559657. The slot's own
    # deterministic run_id is the discriminator when the ET field cannot be.
    _write_json(
        tmp_path / "intraday_dca" / "20260727_1330ET" / "run_state.json",
        _intraday_state("intraday_live_20260727_1130ET", "11:30,13:30,14:30"),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "7be82b97" and r["et"] == "13:30")
    assert row["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_combined_scheduled_for_et_still_accepts_the_right_slot(tmp_path):
    # The mirror: the combined form is a known-legacy logging shape, so a run
    # sitting in its OWN slot directory must still classify as valid evidence.
    for et_dir, run_id in (
        ("20260727_1130ET", "intraday_live_20260727_1130ET"),
        ("20260727_1330ET", "intraday_live_20260727_1330ET"),
        ("20260727_1430ET", "intraday_live_20260727_1430ET"),
    ):
        _write_json(
            tmp_path / "intraday_dca" / et_dir / "run_state.json",
            _intraday_state(run_id, "11:30,13:30,14:30"),
        )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    intraday = [r for r in report["scheduled_runs"] if r["task_id"] == "7be82b97"]
    assert [r["classification"] for r in intraday] == ["valid_scheduled_evidence"] * 3
    assert report["usage_counts"]["run_count_scheduled"] == 3


def test_missing_scheduled_for_et_is_not_accepted_as_slot_evidence(tmp_path):
    # scheduled_for_et is one of scripts/run_classification.
    # SCHEDULED_REQUIRED_FIELDS on every real scheduled run_classification --
    # omitting it entirely (not merely leaving it ambiguous/combined) let a
    # partial state pass identity purely for lack of anything to contradict.
    # For 7be82b97 specifically, an absent field also skipped the per-slot
    # run-id discriminator, so an 11:30 artifact copied into the 13:30
    # directory with scheduled_for_et stripped was accepted as 13:30's own
    # evidence -- codex review discussion_r3660464531.
    _write_json(
        tmp_path / "intraday_dca" / "20260727_1330ET" / "run_state.json",
        {
            "run_id": "intraday_live_20260727_1130ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "7be82b97",
                "run_id": "intraday_live_20260727_1130ET",
                "triggered_at_utc": "2026-07-27T15:30:00Z",
                "state_schema_version": "test-v1",
                # scheduled_for_et deliberately omitted.
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "7be82b97" and r["et"] == "13:30")
    assert row["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["run_count_scheduled"] == 0


def test_single_slot_task_also_requires_scheduled_for_et(tmp_path):
    # Even a task with no sibling-ET ambiguity (2ba19526 has only one slot)
    # must not accept a classification silently missing this required field.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        {
            "run_id": "market_open_live_20260727_0935ET",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "cron_id": "2ba19526",
                "run_id": "market_open_live_20260727_0935ET",
                "triggered_at_utc": "2026-07-27T13:35:00Z",
                "state_schema_version": "test-v1",
                # scheduled_for_et deliberately omitted.
            },
        },
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["classification"] == "failed_start_no_evidence"


def test_corrupt_middle_history_line_is_surfaced_as_a_caveat(tmp_path):
    # A truncated/malformed line strictly BETWEEN two valid ones (unlike a
    # truncated LAST line, which the single <stream>.json marker can still
    # recover) has no fallback at all: if that lost line recorded an earlier
    # failure, the report's recovery/error/retry counts derived from the
    # surviving markers alone are lower than the persisted history actually
    # contains, with nothing else in the report saying so -- codex review
    # discussion_r3661312403.
    day_dir = tmp_path / "attempt_log" / "20260727"
    day_dir.mkdir(parents=True)
    first = {
        "stream": "morning", "error_class": "ConnectionError",
        "error_message": "first attempt", "triggered_at_utc": "2026-07-27T13:31:00Z",
    }
    third = {
        "stream": "morning", "error_class": "ValueError",
        "error_message": "third attempt", "triggered_at_utc": "2026-07-27T14:05:00Z",
    }
    (day_dir / "morning.attempts.jsonl").write_text(
        json.dumps(first) + "\n" + '{"truncated middle line": ' + "\n" + json.dumps(third) + "\n",
        encoding="utf-8",
    )
    (day_dir / "morning.json").write_text(json.dumps(third), encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    # The two surviving markers are still recovered and counted -- the
    # middle line's own loss doesn't take them down with it.
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(rows) == 2
    issues = " ".join(r["triggering_issue"] for r in rows)
    assert "first attempt" in issues and "third attempt" in issues
    assert report["usage_counts"]["failed_start_escalation_count"] == 2
    # ...but the loss itself is disclosed, not silently absorbed.
    caveats_text = " ".join(report["caveats"])
    assert "morning" in caveats_text and "unparseable" in caveats_text


def test_partially_corrupt_history_still_recovers_the_latest_marker(tmp_path):
    # write_failed_start_marker appends to the history FIRST and writes the
    # single <stream>.json second, so a truncated newest append leaves the
    # latest failure recoverable only in that single marker. Returning the
    # older valid lines alone undercounted failed starts -- codex review
    # discussion_r3659559667.
    day_dir = tmp_path / "attempt_log" / "20260727"
    day_dir.mkdir(parents=True)
    older = {
        "stream": "morning",
        "error_class": "ConnectionError",
        "error_message": "first attempt",
        "triggered_at_utc": "2026-07-27T13:31:00Z",
    }
    latest = {
        "stream": "morning",
        "error_class": "ValueError",
        "error_message": "second attempt",
        "triggered_at_utc": "2026-07-27T14:05:00Z",
    }
    (day_dir / "morning.attempts.jsonl").write_text(
        json.dumps(older) + "\n" + '{"truncated": ', encoding="utf-8"
    )
    (day_dir / "morning.json").write_text(json.dumps(latest), encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(rows) == 2
    issues = " ".join(r["triggering_issue"] for r in rows)
    assert "first attempt" in issues and "second attempt" in issues
    assert report["usage_counts"]["failed_start_escalation_count"] == 2


def test_intact_history_does_not_duplicate_the_single_marker(tmp_path):
    # The mirror: when the history parses cleanly it is authoritative, and the
    # <stream>.json copy of its newest entry must not be added a second time.
    day_dir = tmp_path / "attempt_log" / "20260727"
    day_dir.mkdir(parents=True)
    latest = {
        "stream": "morning",
        "error_class": "ValueError",
        "error_message": "only attempt",
        "triggered_at_utc": "2026-07-27T14:05:00Z",
    }
    (day_dir / "morning.attempts.jsonl").write_text(json.dumps(latest) + "\n", encoding="utf-8")
    (day_dir / "morning.json").write_text(json.dumps(latest), encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(rows) == 1
    assert report["usage_counts"]["failed_start_escalation_count"] == 1


def test_dual_flag_recovery_files_as_the_parent_recovery_type(tmp_path):
    # The documented parent-recovery shape
    # (intraday_dca_scheduled_recovery_2026_07_24) sets BOTH flags; checking
    # the generic rerun flag first filed it as scheduled_recovery_attempt and
    # left parent_recovery_run at zero -- codex review discussion_r3659559675.
    state = _intraday_state("intraday_live_20260727_1130ET", "11:30")
    state["recovery_rerun"] = True
    state["parent_recovery_submission"] = True
    _write_json(tmp_path / "intraday_dca" / "20260727_1130ET" / "run_state.json", state)
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "7be82b97")
    assert row["recovery_artifact_type"] == "parent_recovery_run"
    by_type = report["usage_counts"]["recovery_count_by_artifact_type"]
    assert by_type["parent_recovery_run"] == 1
    assert by_type["scheduled_recovery_attempt"] == 0
    # Filing it under the more specific type must not drop it from
    # retry_count -- it really is a rerun as well.
    assert report["usage_counts"]["retry_count"] == 1


def test_persisted_notifications_sent_is_aggregated(tmp_path):
    # near_close_dca.build_usage_proxy() records this; the report hardcoded
    # not_logged and so claimed the source was unavailable even when the exact
    # count was in the artifact -- codex review discussion_r3659559689.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(usage_proxy={"notifications_sent": 3}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["notifications_sent"] == 3
    assert report["usage_counts"]["notification_count"] == 3
    assert not any("notification_count" in c for c in report["caveats"])


def test_notification_count_stays_not_logged_without_the_field(tmp_path):
    # build_usage_proxy leaves notifications_sent Optional (defaulting to
    # None), so a run that did not record it is out of scope -- never a 0.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(usage_proxy={"notifications_sent": None}),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert report["usage_counts"]["notification_count"] == "not_logged"
    assert any("notification_count" in c for c in report["caveats"])


def test_no_candidates_status_counts_as_zero_orders(tmp_path):
    # morning/midday write no submitted_orders.csv when there was nothing to
    # submit, but order_submit_status='not_submitted_no_candidates' is the
    # run's own persisted proof of exactly zero attempts -- reporting
    # not_logged kept it out of no_op_count and forced placed_order_count to
    # not_available -- codex review discussion_r3659713603.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(order_submit_status="not_submitted_no_candidates"),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["order_attempts"] == 0
    assert report["usage_counts"]["no_op_count"] == 1
    assert report["usage_counts"]["placed_order_count"] == 0


def test_ambiguous_no_order_statuses_stay_not_logged(tmp_path):
    # not_submitted_error and not_submitted_confirmation_required are NOT
    # proof of zero attempts -- an attempt may have been made and then failed
    # or been blocked mid-flight -- so they must not be coerced to 0.
    for status in ("not_submitted_error", "not_submitted_confirmation_required"):
        root = tmp_path / status
        _write_json(
            root / "market_open_dca" / "20260727_0935ET" / "run_state.json",
            _run_state(order_submit_status=status),
        )
        report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=root)
        row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
        assert row["order_attempts"] == "not_logged", status


def test_persisted_csv_still_wins_over_the_status(tmp_path):
    # A real order artifact is stronger evidence than the status field: a run
    # that submitted then re-derived a no-candidates status must report the
    # CSV's count, not 0.
    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state(order_submit_status="not_submitted_no_candidates"))
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\nAAPL,o1,100,filled\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "2ba19526")
    assert row["order_attempts"] == 1


def test_manual_recovery_filled_orders_csv_is_counted(tmp_path):
    # A manual override under midday_dca/manual_reruns persists its attempts
    # in filled_orders.csv; looking only for submitted_orders.csv reported
    # not_logged, and because manual rows feed placed_order_count one such
    # recovery made the whole aggregate unavailable -- codex review
    # discussion_r3659713605.
    run_dir = tmp_path / "midday_dca" / "manual_reruns" / "final_manual_override_20260727"
    _write_json(
        run_dir / "run_state.json",
        {
            "run_id": "final_manual_override_20260727",
            "trading_day": TRADING_DAY,
            "run_classification": {"run_origin": "manual", "manual_run": True},
        },
    )
    (run_dir / "filled_orders.csv").write_text(
        "order_id,ticker,amount_usd\no1,AAPL,3.00\no2,MSFT,2.00\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["recovery_and_escalation_runs"] if r["run_id"] == "final_manual_override_20260727")
    assert row["order_attempts"] == 2
    assert report["usage_counts"]["placed_order_count"] == 2


def test_orphan_repair_dir_is_discovered_by_filled_orders_csv(tmp_path):
    # The orphan sweep discovered directories by globbing submitted_orders.csv
    # alone, so a repair artifact recording only filled_orders.csv was never
    # even found -- the same single-filename assumption.
    repair_dir = tmp_path / "ce16ba6b" / "manual_runs" / "20260727_repair"
    repair_dir.mkdir(parents=True)
    (repair_dir / "filled_orders.csv").write_text("order_id,ticker,amount_usd\no1,NVDA,2.00\n", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if "repair" in str(r["triggering_issue"])]
    assert len(rows) == 1
    assert rows[0]["order_attempts"] == 1


def test_duplicate_consumer_artifact_paths_are_counted_once(tmp_path):
    # A consumer can log the same output path on more than one row (a retry,
    # or a duplicate audit append). Expanding and measuring per row repeated
    # those files in artifacts_written and added their bytes once per row,
    # inflating artifact_count/artifact_bytes_approx -- codex review
    # discussion_r3659713615.
    # The complete documented output set, so each invocation genuinely
    # SUCCEEDS -- dedup is what this test is about, not completeness.
    out_dir = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    entries = [
        {
            "run_id": "slip", "cron_id": "67aa802b", "run_origin": "scheduled", "stream": "slippage",
            "triggered_at_utc": "2026-07-27T19:35:00Z", "model_routing_mode": "cost_conscious",
            "artifact_path": str(out_dir),
        },
        # Same directory logged a second time, plus a file already inside it.
        {
            "run_id": "slip", "cron_id": "67aa802b", "run_origin": "scheduled", "stream": "slippage",
            "triggered_at_utc": "2026-07-27T19:36:00Z", "model_routing_mode": "cost_conscious",
            "artifact_path": str(out_dir),
        },
        {
            "run_id": "slip", "cron_id": "67aa802b", "run_origin": "scheduled", "stream": "slippage",
            "triggered_at_utc": "2026-07-27T19:37:00Z", "model_routing_mode": "cost_conscious",
            "artifact_path": str(out_dir / "slippage_ranked.csv"),
        },
    ]
    (model_usage_dir / "model_usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    # Each concrete file listed and measured exactly once -- despite three
    # logged rows and an overlapping directory/file pair.
    assert sorted(row["artifacts_written"]) == sorted(str(out_dir / n) for n in SLIPPAGE_OUTPUTS)
    assert row["artifact_bytes"] == len(SLIPPAGE_OUTPUTS) * len("{}")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))


def _scheduled_run_state(cron_id, run_id, **extra_rc):
    return {
        "run_id": run_id,
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "cron_id": cron_id,
            "run_id": run_id,
            "triggered_at_utc": "2026-07-27T13:45:00Z",
            "state_schema_version": "test-v1",
            **extra_rc,
        },
    }


def test_read_only_0945_slots_record_zero_orders(tmp_path):
    # Codex review discussion_r3660021380: the 9:45 pre-flight and watchlist
    # are read-only, so their documented artifact sets carry no order CSV and
    # no order_submit_status. That made _count_submitted_orders return
    # not_logged, which propagates through placed_order_count (turning the
    # whole day's total not_available) and kept the row out of no_op_count --
    # even though a read-only role's order count is necessarily zero.
    _write_json(
        tmp_path / "morning_dca_preflight/20260727_0945ET/run_state.json",
        _scheduled_run_state(
            "bb4f6ef1", "morning_preflight_20260727_0945ET",
            run_type="morning_dca_preflight", scheduled_for_et="09:45",
        ),
    )
    _write_json(
        tmp_path / "morning_dca_watchlist/20260727_0945ET/run_state.json",
        _scheduled_run_state(
            "bb4f6ef1", "morning_20260727_0945ET",
            run_type="morning_dca_watchlist_scan", scheduled_for_et="09:45",
        ),
    )

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    preflight = next(r for r in report["scheduled_runs"] if r["stream"] == "preflight")
    watchlist = next(r for r in report["scheduled_runs"] if r["stream"] == "watchlist")
    assert preflight["classification"] == "valid_scheduled_evidence"
    assert watchlist["classification"] == "valid_scheduled_evidence"
    # The known zero, not an unmeasured gap.
    assert preflight["order_attempts"] == 0
    assert watchlist["order_attempts"] == 0
    # And therefore the day's aggregate stays measurable...
    assert report["usage_counts"]["placed_order_count"] == 0
    # ...and both rows count as no-ops rather than being excluded.
    assert report["usage_counts"]["no_op_count"] >= 2


def test_read_only_slot_still_prefers_a_real_order_artifact(tmp_path):
    # The zero is a fallback for the unmeasured case only: a real order CSV, if
    # one somehow exists, must still win over the assumed zero.
    run_dir = tmp_path / "morning_dca_preflight/20260727_0945ET"
    _write_json(
        run_dir / "run_state.json",
        _scheduled_run_state(
            "bb4f6ef1", "morning_preflight_20260727_0945ET",
            run_type="morning_dca_preflight", scheduled_for_et="09:45",
        ),
    )
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,status\n"
        "AAPL,o1,1.00,submitted\nMSFT,o2,2.00,submitted\n",
        encoding="utf-8",
    )

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    preflight = next(r for r in report["scheduled_runs"] if r["stream"] == "preflight")
    assert preflight["order_attempts"] == 2


def _consumer_entry(run_id, artifact_path, triggered_at):
    return {
        "run_id": run_id,
        "cron_id": "67aa802b",
        "task_name": "post_trade_slippage_report",
        "scheduled_for_et": "15:35",
        "run_origin": "scheduled",
        "stream": "slippage",
        "triggered_at_utc": triggered_at,
        "model_requested": "cost_conscious_default",
        "model_actual": "not_exposed",
        "model_routing_mode": "cost_conscious",
        "artifact_path": str(artifact_path),
    }


def _write_model_usage(tmp_path, entries):
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True, exist_ok=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


# The complete set dca_reporting.generate_reports() writes. A 67aa802b
# invocation only counts as successful once ALL FOUR landed -- one file plus a
# raise is a failed attempt the finally block still logged
# (discussion_r3660192605) -- so any fixture standing in for a SUCCEEDING
# slippage run has to write the whole set.
SLIPPAGE_OUTPUTS = (
    "slippage_ranked.csv",
    "missing_fills_report.csv",
    "slippage_and_coverage_report.md",
    "latest_run_summary.json",
)


def _stamp_to_trading_day(path, trading_day=None):
    """Backdate ``path`` (file or directory tree) to noon ET on ``trading_day``.

    _is_current_day_artifact accepts an artifact either because its PATH carries
    the day stamp or because its MTIME falls on the trading day in ET. A fixture
    writing to a path without the day stamp therefore depended on the wall-clock
    date: it passed only while the suite ran on the pinned TRADING_DAY, and
    started failing at the following ET midnight with no code change. Stamping
    the mtime makes such a fixture hermetic (Codex review, PR #219).
    """
    when = datetime.fromisoformat(f"{trading_day or TRADING_DAY}T12:00:00").replace(
        tzinfo=ZoneInfo("America/New_York")
    )
    stamp = when.timestamp()
    path = Path(path)
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    for target in targets:
        os.utime(target, (stamp, stamp))
    return path


def _write_slippage_outputs(out_dir, names=SLIPPAGE_OUTPUTS):
    """Write ``names`` into ``out_dir`` and return it, for a 67aa802b run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (out_dir / name).write_text("{}", encoding="utf-8")
    return out_dir


def test_failed_consumer_attempt_survives_a_successful_retry(tmp_path):
    # Codex review discussion_r3660021373: 67aa802b appends its model_usage row
    # from a finally block, so a failed attempt logs one too. Merging every
    # match and asking any(...) whether SOME output exists let the retry's file
    # vouch for the failed attempt: the pair read as one clean scheduled run,
    # no failed-start row was emitted, and retry_count stayed 0.
    failed_out = tmp_path / "reports" / "attempt1"
    failed_out.mkdir(parents=True)  # created before the raise, left empty
    # The retry writes the COMPLETE documented output set -- anything less is
    # itself a failed invocation now (discussion_r3660192605).
    good_out = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    _write_model_usage(
        tmp_path,
        [
            _consumer_entry("slip_attempt1", failed_out, "2026-07-27T19:35:00Z"),
            _consumer_entry("slip_retry", good_out, "2026-07-27T19:36:00Z"),
        ],
    )

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    # The retry did produce output, so the chain row is still valid evidence.
    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert chain["classification"] == "valid_scheduled_evidence"
    assert chain["run_id"] == "slip_retry"
    # ...but the failed attempt is preserved as its own recovery row...
    failed_rows = [
        r
        for r in report["recovery_and_escalation_runs"]
        if r["task_id"] == "67aa802b" and r["run_id"] == "slip_attempt1"
    ]
    assert len(failed_rows) == 1
    assert failed_rows[0]["classification"] == "failed_start_no_evidence"
    # ...and it counts as a retry.
    assert report["usage_counts"]["retry_count"] >= 1


def test_consumer_artifacts_exclude_the_failed_attempt_path(tmp_path):
    # Only the succeeding invocation's artifacts are reported: the failed
    # attempt produced nothing that exists.
    failed_out = tmp_path / "reports" / "attempt1"
    failed_out.mkdir(parents=True)
    good_out = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    _write_model_usage(
        tmp_path,
        [
            _consumer_entry("slip_attempt1", failed_out, "2026-07-27T19:35:00Z"),
            _consumer_entry("slip_retry", good_out, "2026-07-27T19:36:00Z"),
        ],
    )

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert sorted(chain["artifacts_written"]) == sorted(str(good_out / n) for n in SLIPPAGE_OUTPUTS)


def test_all_consumer_attempts_failing_still_emits_one_row(tmp_path):
    # With no successful invocation the behaviour is unchanged: a single
    # failed-start row, and no retry credited.
    first = tmp_path / "reports" / "attempt1"
    first.mkdir(parents=True)
    _write_model_usage(tmp_path, [_consumer_entry("slip_attempt1", first, "2026-07-27T19:35:00Z")])

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert chain["classification"] == "failed_start_no_evidence"
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "67aa802b"]
    assert len(rows) == 1


def test_consumer_retry_reusing_the_same_output_dir_is_not_credited_as_success(tmp_path):
    # A retry commonly reuses the SAME output_dir as the attempt before it
    # (dca_reporting.generate_scheduled_reports() is handed one output_dir per
    # trading day). Evaluating each row only against its own artifact_path
    # cannot tell a later failed retry from the earlier successful attempt
    # that already wrote the complete set into that shared directory -- a
    # later invocation that raised before writing anything new still saw the
    # earlier invocation's output and would be wrongly credited with it --
    # codex review discussion_r3660464510.
    out_dir = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    written_at = datetime(2026, 7, 27, 19, 34, 30, tzinfo=timezone.utc).timestamp()
    for name in SLIPPAGE_OUTPUTS:
        os.utime(out_dir / name, (written_at, written_at))
    _write_model_usage(
        tmp_path,
        [
            # The first invocation actually wrote the complete set above.
            _consumer_entry("slip_attempt1", out_dir, "2026-07-27T19:35:00Z"),
            # The retry reuses the SAME directory but raised before writing
            # anything new -- the files on disk are still attempt1's.
            _consumer_entry("slip_retry", out_dir, "2026-07-27T19:40:00Z"),
        ],
    )

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    recovery = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "67aa802b"]
    assert chain["classification"] == "valid_scheduled_evidence"
    assert chain["run_id"] == "slip_attempt1"
    assert chain["model_usage_rows"] == 1
    failed_rows = [r for r in recovery if r["run_id"] == "slip_retry"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["classification"] == "failed_start_no_evidence"
    assert report["usage_counts"]["retry_count"] >= 1


def test_consumer_second_genuine_success_in_the_same_dir_is_still_credited(tmp_path):
    # The mirror case: a SECOND invocation that legitimately rewrites the same
    # output_dir with a fresh complete set (e.g. an intentional same-day
    # re-run) must still be credited -- the freshness check must not reject
    # every repeat, only ones that left the directory unchanged.
    out_dir = tmp_path / "reports" / "20260727"
    _write_slippage_outputs(out_dir)
    stale_at = datetime(2026, 7, 27, 19, 34, 30, tzinfo=timezone.utc).timestamp()
    for name in SLIPPAGE_OUTPUTS:
        os.utime(out_dir / name, (stale_at, stale_at))
    _write_model_usage(
        tmp_path,
        [_consumer_entry("slip_attempt1", out_dir, "2026-07-27T19:35:00Z")],
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert chain["classification"] == "valid_scheduled_evidence"

    # Now a second invocation genuinely rewrites the whole set afterward.
    fresh_at = datetime(2026, 7, 27, 19, 41, 0, tzinfo=timezone.utc).timestamp()
    for name in SLIPPAGE_OUTPUTS:
        (out_dir / name).write_text("{}", encoding="utf-8")
        os.utime(out_dir / name, (fresh_at, fresh_at))
    _write_model_usage(
        tmp_path,
        [
            _consumer_entry("slip_attempt1", out_dir, "2026-07-27T19:35:00Z"),
            _consumer_entry("slip_rerun", out_dir, "2026-07-27T19:42:00Z"),
        ],
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert chain["classification"] == "valid_scheduled_evidence"
    # Both the original attempt and the later genuine rewrite are credited --
    # the freshness check only disqualifies a repeat that left the shared
    # directory unchanged, not every repeat.
    assert chain["model_usage_rows"] == 2


def test_cron_tracking_root_honours_the_env_override(monkeypatch, tmp_path):
    # Codex review discussion_r3660021363: the registered command hardcoded the
    # production tree, bypassing the configurable root the entry point already
    # supported. The default is now env-aware so a relocated deployment works
    # even if the scheduler drops the flag.
    from scripts.generate_credit_usage_proxy_report import (
        CRON_TRACKING_ROOT_ENV,
        _default_cron_tracking_root,
        default_report_paths,
    )

    monkeypatch.delenv(CRON_TRACKING_ROOT_ENV, raising=False)
    bundled = _default_cron_tracking_root()
    assert bundled == str(Path(__file__).resolve().parents[2] / "cron_tracking")

    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, str(tmp_path))
    assert _default_cron_tracking_root() == str(tmp_path)
    # Outputs move with the tree they are derived from.
    paths = default_report_paths(TRADING_DAY)
    assert paths["json"].is_relative_to(tmp_path)
    assert paths["md"].is_relative_to(tmp_path)
    assert paths["json"].name == "credit_usage_proxy_report_20260727.json"

    # An empty value is treated as unset, not as the root directory.
    monkeypatch.setenv(CRON_TRACKING_ROOT_ENV, "")
    assert _default_cron_tracking_root() == bundled


def test_registered_command_derives_the_root_with_no_shell_hardcoding():
    # Codex review: "Keep the report command on the shared artifact root".
    # The command previously expanded a shell ${DCA_CRON_TRACKING_ROOT:-
    # /home/user/workspace/cron_tracking} fallback for --cron-tracking-root/
    # --json-out/--md-out, which diverges from dca_paths.cron_tracking_root()'s
    # own checkout-relative unset-default -- the same root every other
    # in-repo producer/consumer uses -- so an unconfigured checkout would
    # read/write a different tree than every other task. The manifest
    # command must not hardcode any root or path at all; --write-defaults
    # lets the Python entry point derive both via cron_tracking_root()/
    # default_report_paths() instead, which can never drift from the shared
    # root the way a static shell fallback could.
    repo_root = Path(__file__).resolve().parents[2]
    manifest = json.loads((repo_root / "config" / "scheduled_tasks.json").read_bytes())
    command = next(
        t["command"] for t in manifest["tasks"] if t.get("id", "").startswith("onetime-1630-credit")
    )
    assert "--write-defaults" in command
    assert "DCA_CRON_TRACKING_ROOT" not in command
    assert "/home/user/workspace/cron_tracking" not in command
    assert "--cron-tracking-root" not in command
    assert "--json-out" not in command
    assert "--md-out" not in command


def test_redaction_handles_quoted_json_credential_keys(tmp_path):
    # A connector exception carrying a JSON fragment puts the closing quote
    # BETWEEN the key and the colon, which a bare `key\s*[:=]` pattern cannot
    # match -- and "access_token" contains no word boundary before "token", so
    # the \btoken\b alternative missed the commonest credential key there is.
    # The helper returned JSON-formatted credentials unchanged into both report
    # formats -- codex review discussion_r3660192592 (P1).
    from generate_credit_usage_proxy_report import _redact_secrets

    for raw, leaked in [
        ('{"access_token":"tok_live_abc"}', "tok_live_abc"),
        ('{"access_token": "tok_live_abc", "user": "bob"}', "tok_live_abc"),
        ("{'refresh_token': 'rt_live_9x'}", "rt_live_9x"),
        ('{"client_secret":"cs_abc"}', "cs_abc"),
        ("X-Api-Key: k_live_991", "k_live_991"),
        ("db_password=hunter2", "hunter2"),
    ]:
        out = _redact_secrets(raw)
        assert leaked not in out, raw
        assert "[REDACTED]" in out, raw
    # Benign status text with no credential shape is left alone.
    for benign in (
        "order_submit_status=not_submitted_error",
        "blocking_reason=market closed early",
        "usage_proxy.errors_count=2",
    ):
        assert _redact_secrets(benign) == benign, benign


def test_quoted_json_credential_is_redacted_end_to_end(tmp_path):
    # The helper is only useful if the free-text paths actually run it: assert
    # against both generated formats, not just the helper.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(blocking_reason='broker refused: {"access_token":"tok_live_abc"}'),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert "tok_live_abc" not in json.dumps(report)
    assert "tok_live_abc" not in render_markdown(report)


def test_redaction_handles_credentials_embedded_in_url_paths(tmp_path):
    # A Slack webhook (https://hooks.slack.com/services/T.../B.../<secret>)
    # or an API token placed directly in a URL path carries no recognizable
    # KEY name at all ("token"/"secret"/...), so the key=value/JSON rule
    # never matches it and the complete credential passed through unredacted
    # into both report formats -- codex review discussion_r3660858022 (P1).
    from generate_credit_usage_proxy_report import _redact_secrets

    slack_tail = "X" * 24
    stripe_like = "sk_" + "live_" + "abcdefghijklmnopqrstuvwxyz"
    for raw, leaked in [
        (
            f"broker rejected: https://hooks.slack.com/services/T00000000/B00000000/{slack_tail}",
            slack_tail,
        ),
        (
            f"connector call failed for https://api.example.com/v1/reports/{stripe_like}",
            stripe_like,
        ),
    ]:
        out = _redact_secrets(raw)
        assert leaked not in out, raw
        assert "[REDACTED]" in out, raw
    # An ordinary reference URL with no long opaque path segment is left alone.
    benign = "see https://github.com/badMade/all-stock-dca/pull/213 for details"
    assert _redact_secrets(benign) == benign


def test_url_embedded_credential_is_redacted_end_to_end(tmp_path):
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(
            blocking_reason=(
                "broker rejected: https://hooks.slack.com/services/"
                "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
            )
        ),
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    assert "XXXXXXXXXXXXXXXXXXXXXXXX" not in json.dumps(report)
    assert "XXXXXXXXXXXXXXXXXXXXXXXX" not in render_markdown(report)


def test_partial_slippage_output_set_is_a_failed_invocation(tmp_path):
    # dca_reporting.generate_reports() writes all four post-trade files, and
    # logs its model_usage row from a finally block -- so an invocation that
    # wrote the first report and then raised must not pass as successful on
    # the strength of that one file -- codex review discussion_r3660192605.
    out_dir = _write_slippage_outputs(
        tmp_path / "reports" / "20260727", names=("slippage_ranked.csv",)
    )
    _write_model_usage(tmp_path, [_consumer_entry("slip", out_dir, "2026-07-27T19:35:00Z")])
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "failed_start_no_evidence"


def test_complete_slippage_output_set_is_successful(tmp_path):
    out_dir = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    _write_model_usage(tmp_path, [_consumer_entry("slip", out_dir, "2026-07-27T19:35:00Z")])
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    assert row["classification"] == "valid_scheduled_evidence"


def test_consumer_retry_does_not_double_count_the_failed_log_row(tmp_path):
    # The failed attempt gets its own recovery row with model_usage_rows=1, so
    # the chain row counting every match billed that entry twice -- three rows
    # for two real entries -- codex review discussion_r3660192611.
    failed_out = tmp_path / "reports" / "attempt1"
    failed_out.mkdir(parents=True)
    good_out = _write_slippage_outputs(tmp_path / "reports" / "20260727")
    _write_model_usage(
        tmp_path,
        [
            _consumer_entry("slip_attempt1", failed_out, "2026-07-27T19:35:00Z"),
            _consumer_entry("slip_retry", good_out, "2026-07-27T19:36:00Z"),
        ],
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    chain = next(r for r in report["scheduled_runs"] if r["task_id"] == "67aa802b")
    recovery = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "67aa802b"]
    assert chain["model_usage_rows"] == 1
    assert sum(r["model_usage_rows"] for r in recovery) == 1
    # Two log entries on disk, two counted -- not three.
    assert chain["model_usage_rows"] + sum(r["model_usage_rows"] for r in recovery) == 2


def test_latest_marker_merges_after_a_clean_append_failure(tmp_path):
    # write_failed_start_marker appends to the history FIRST and writes the
    # single marker second, so an append that raises OSError leaves a history
    # that parses perfectly while the newest failure lives only in
    # <stream>.json. Gating the merge on visible lossiness dropped it in
    # exactly that scenario -- codex review discussion_r3660192619.
    day_dir = tmp_path / "attempt_log" / "20260727"
    day_dir.mkdir(parents=True)
    older = {
        "stream": "morning", "error_class": "ConnectionError",
        "error_message": "first attempt", "triggered_at_utc": "2026-07-27T13:31:00Z",
    }
    newest = {
        "stream": "morning", "error_class": "ValueError",
        "error_message": "second attempt", "triggered_at_utc": "2026-07-27T14:05:00Z",
    }
    # A perfectly valid history -- no truncation, nothing malformed.
    (day_dir / "morning.attempts.jsonl").write_text(json.dumps(older) + "\n", encoding="utf-8")
    (day_dir / "morning.json").write_text(json.dumps(newest), encoding="utf-8")

    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    rows = [r for r in report["recovery_and_escalation_runs"] if r["task_id"] == "42173154"]
    assert len(rows) == 2
    issues = " ".join(r["triggering_issue"] for r in rows)
    assert "first attempt" in issues and "second attempt" in issues
    assert report["usage_counts"]["failed_start_escalation_count"] == 2


def test_preflight_missing_validation_csv_is_an_actionable_issue(tmp_path):
    # also_runs_required_preflight_writer.preflight_artifacts.files declares
    # all four of run_state.json, validation.csv, signals.csv, run_summary.json
    # -- validation.csv was the one this map omitted, so an incomplete
    # preflight reported no missing artifacts -- codex review
    # discussion_r3660192627.
    run_dir = tmp_path / "morning_dca_preflight" / "20260727_0945ET"
    _write_json(
        run_dir / "run_state.json",
        {
            "run_id": "morning_preflight_20260727_0945ET",
            "run_classification": {
                "run_origin": "scheduled", "manual_run": False, "cron_id": "bb4f6ef1",
                "scheduled_for_et": "09:45",
                "run_id": "morning_preflight_20260727_0945ET",
                "run_type": "morning_dca_preflight",
                "triggered_at_utc": "2026-07-27T13:45:00Z", "state_schema_version": "test-v1",
            },
        },
    )
    (run_dir / "signals.csv").write_text("ticker\n", encoding="utf-8")
    (run_dir / "run_summary.json").write_text("{}", encoding="utf-8")
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    row = next(r for r in report["scheduled_runs"] if r["stream"] == "preflight")
    assert row["missing_required_artifacts"] == ["validation.csv"]
    assert any("validation.csv" in issue for issue in report["actionable_issues"])


def test_cross_reference_recovery_is_not_ranked_as_a_second_driver(tmp_path):
    # An in-place scheduled recovery appears twice -- as the substantive
    # scheduled row and as its recovery-section cross-reference -- under the
    # SAME run_id, so ranking both let one underlying run take two of the five
    # driver slots -- codex review discussion_r3660192640.
    _write_json(
        tmp_path / "market_open_dca" / "20260727_0935ET" / "run_state.json",
        _run_state(recovery_rerun=True, recovery={"trigger_reason": "internal_persistence_failure"}),
    )
    model_usage_dir = tmp_path / "model_usage" / "202607"
    model_usage_dir.mkdir(parents=True)
    (model_usage_dir / "model_usage.jsonl").write_text(
        json.dumps({"run_id": "market_open_live_20260727_0935ET", "model_routing_mode": "premium"}) + "\n",
        encoding="utf-8",
    )
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    run_ids = [r["run_id"] for r in report["biggest_usage_drivers"]]
    assert run_ids.count("market_open_live_20260727_0935ET") == 1
    # The recovery row is still reported and still counted -- only its
    # duplicate RANKING is suppressed.
    assert any(r["source"] == "recovery_rerun" for r in report["recovery_and_escalation_runs"])
    assert report["usage_counts"]["retry_count"] == 1


def test_unstattable_artifact_bytes_are_disclosed_in_caveats(tmp_path):
    # _scan_run_dir deliberately propagates not_available into
    # artifact_bytes_approx, and artifact_load follows, but neither named a
    # reason -- an unexplained not_available violates unavailable_data_rule --
    # codex review discussion_r3660192649.
    import unittest.mock as mock

    run_dir = tmp_path / "market_open_dca" / "20260727_0935ET"
    _write_json(run_dir / "run_state.json", _run_state())
    (run_dir / "vanishes.bin").write_text("x" * 32, encoding="utf-8")
    real_stat = Path.stat

    def flaky_stat(self, *a, **kw):
        if self.name == "vanishes.bin":
            raise OSError("vanished mid-scan")
        return real_stat(self, *a, **kw)

    with mock.patch.object(Path, "stat", flaky_stat):
        report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)

    assert report["usage_counts"]["artifact_bytes_approx"] == "not_available"
    assert report["proxy_bands"]["artifact_load"]["band"] == "not_available"
    assert any(c.startswith("artifact_bytes_approx: not_available") for c in report["caveats"])
    assert any(c.startswith("artifact_load: not_available") for c in report["caveats"])


def test_recovery_stream_keys_match_the_canonical_names(tmp_path):
    # The contract's recovery_counts_by_stream_rule named `post_trade` while
    # every other part of the contract -- and the report -- says `slippage`,
    # so the promised key never appeared -- codex review discussion_r3660192634.
    report = generate_report(trading_day=TRADING_DAY, cron_tracking_root=tmp_path)
    by_stream = report["usage_counts"]["recovery_run_count_by_stream"]
    assert "slippage" in by_stream
    assert "post_trade" not in by_stream
    assert set(by_stream) == set(ALL_STREAMS)


def test_contract_and_report_agree_on_recovery_stream_names():
    # The contract text itself must name the canonical streams, or a consumer
    # reading it cannot tell an omitted stream from a confirmed zero.
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "scheduled_tasks.json").read_text(encoding="utf-8")
    )
    rule = manifest["credit_usage_proxy_report_2026_07_27"]["recovery_counts_by_stream_rule"]
    # Only the DECLARED list matters; the trailing note legitimately mentions
    # the old post_trade name to explain why it was replaced.
    declared = rule.split("This rule previously", 1)[0]
    assert "slippage" in declared
    assert "post_trade" not in declared
    for stream in ALL_STREAMS:
        assert stream in declared, stream
