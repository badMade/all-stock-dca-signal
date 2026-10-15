"""Tests for the Gate 5 / same-day top-up-gate abort diagnostics.

Context: the 2026-09-15 1230ET midday run aborted for two independent,
documented, fail-closed reasons -- neither is a bug:

  1. ``dca_risk.is_afternoon_scheduled_evidence`` found 0 qualifying afternoon
     run_state.json records in the trailing 7-day window (docs/midday_signal_entry.md:
     "a count of 0 means abort, not a deployment of $0.00").
  2. ``scripts.same_day_top_up_gate``'s ``_bind_saved_pages`` rejected a
     reconstructed ``--orders-pages`` file for missing the
     pages/account_number/trading_day envelope it requires
     (docs/reliability_spec.md section 3, "the raw-input contract").

This module is diagnostic-only: it explains WHY a gate produced that verdict
in more actionable detail than the bare abort, so an operator can tell "the
tree is genuinely empty" from "the tree exists but nothing in it qualifies"
from "the file was reconstructed instead of captured raw". It never changes
either gate's verdict, never retries a placement, and never fabricates
evidence -- it would be unsafe for it to do any of those things, so these
tests only assert on the diagnostic's OWN output shape, never on it producing
a different trading outcome.

Every case here is derived from the documented behavior of the functions it
wraps (``dca_risk.is_afternoon_scheduled_evidence``,
``scripts.same_day_top_up_gate._bind_saved_pages``), not from this module's
own implementation.
"""

from __future__ import annotations

import json
from datetime import date

from run_dca import _slot_classification_overrides
from scripts import gate_failure_diagnostics as gfd
from scripts.run_classification import build_scheduled_classification

AS_OF = date(2026, 9, 15)
ACCOUNT = "999000111"
DAY = "2026-09-15"


# --- fixtures -----------------------------------------------------------------


def _afternoon_state(trading_day: str, *, et_time: str = "15:20:00") -> dict:
    """A valid scheduled-afternoon run_state, same shape build_run_artifacts writes."""
    return {
        "trading_day": trading_day,
        "generated_at": f"{trading_day}T{et_time}-04:00",
        "run_classification": build_scheduled_classification(trading_day),
    }


def _midday_state(trading_day: str, *, et_time: str = "12:30:00") -> dict:
    """A valid scheduled-midday run_state -- STREAM_SCHEDULED, but never afternoon evidence."""
    overrides = _slot_classification_overrides("midday", "1230ET", trading_day)
    return {
        "trading_day": trading_day,
        "generated_at": f"{trading_day}T{et_time}-04:00",
        "run_classification": build_scheduled_classification(trading_day, **overrides),
    }


# --- diagnose_afternoon_evidence_gap ------------------------------------------


def test_missing_root_reports_root_missing(tmp_path):
    missing = tmp_path / "does_not_exist"
    result = gfd.diagnose_afternoon_evidence_gap([str(missing)], as_of=AS_OF)
    assert result["likely_cause"] == gfd.AFTERNOON_ROOT_MISSING
    assert result["qualifying_in_window_total"] == 0
    assert result["roots"][0]["exists"] is False


def test_root_exists_but_empty_reports_no_files(tmp_path):
    root = tmp_path / "ce16ba6b" / "scheduled_runs"
    root.mkdir(parents=True)
    result = gfd.diagnose_afternoon_evidence_gap([str(root)], as_of=AS_OF)
    assert result["likely_cause"] == gfd.AFTERNOON_NO_FILES
    assert result["roots"][0]["run_state_files_found"] == 0


def test_qualifying_recent_afternoon_state_is_healthy(tmp_path):
    run_dir = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260914_1530ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps(_afternoon_state("2026-09-14")))
    root = tmp_path / "ce16ba6b" / "scheduled_runs"
    result = gfd.diagnose_afternoon_evidence_gap([str(root)], as_of=AS_OF)
    assert result["likely_cause"] == gfd.AFTERNOON_HEALTHY
    assert result["qualifying_in_window_total"] == 1
    assert result["roots"][0]["reason_counts"]["qualifying_in_window"] == 1


def test_stale_qualifying_state_outside_window_reports_stale(tmp_path):
    run_dir = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260801_1530ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps(_afternoon_state("2026-08-01")))
    root = tmp_path / "ce16ba6b" / "scheduled_runs"
    result = gfd.diagnose_afternoon_evidence_gap([str(root)], as_of=AS_OF)
    assert result["likely_cause"] == gfd.AFTERNOON_STALE
    assert result["qualifying_in_window_total"] == 0
    assert result["roots"][0]["newest_qualifying_date"] == "2026-08-01"


def test_midday_stream_states_are_excluded_not_counted(tmp_path):
    run_dir = tmp_path / "midday_dca" / "20260914_1230ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps(_midday_state("2026-09-14")))
    root = tmp_path / "midday_dca"
    result = gfd.diagnose_afternoon_evidence_gap([str(root)], as_of=AS_OF)
    assert result["likely_cause"] == gfd.AFTERNOON_STREAM_EXCLUDED
    assert result["roots"][0]["reason_counts"]["morning_or_midday_stream_excluded"] == 1
    assert result["qualifying_in_window_total"] == 0


def test_before_floor_timestamp_is_excluded(tmp_path):
    run_dir = tmp_path / "intraday_dca" / "20260914_1330ET"
    run_dir.mkdir(parents=True)
    state = _afternoon_state("2026-09-14", et_time="13:30:00")
    (run_dir / "run_state.json").write_text(json.dumps(state))
    root = tmp_path / "intraday_dca"
    result = gfd.diagnose_afternoon_evidence_gap([str(root)], as_of=AS_OF)
    assert result["roots"][0]["reason_counts"]["before_afternoon_floor"] == 1
    assert result["qualifying_in_window_total"] == 0


def test_unreadable_file_is_counted_without_crashing(tmp_path):
    run_dir = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260914_1530ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text("{not valid json")
    root = tmp_path / "ce16ba6b" / "scheduled_runs"
    result = gfd.diagnose_afternoon_evidence_gap([str(root)], as_of=AS_OF)
    assert result["roots"][0]["files_unreadable"] == 1
    assert result["roots"][0]["run_state_files_found"] == 1


def test_multiple_roots_merge_totals(tmp_path):
    run_a = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260914_1530ET"
    run_a.mkdir(parents=True)
    (run_a / "run_state.json").write_text(json.dumps(_afternoon_state("2026-09-14")))
    run_b = tmp_path / "intraday_dca" / "20260913_1430ET"
    run_b.mkdir(parents=True)
    (run_b / "run_state.json").write_text(json.dumps(_afternoon_state("2026-09-13", et_time="14:30:00")))
    result = gfd.diagnose_afternoon_evidence_gap(
        [str(tmp_path / "ce16ba6b" / "scheduled_runs"), str(tmp_path / "intraday_dca")],
        as_of=AS_OF,
    )
    assert result["qualifying_in_window_total"] == 2
    assert len(result["roots"]) == 2


def test_reports_cron_tracking_root_context(tmp_path):
    result = gfd.diagnose_afternoon_evidence_gap([str(tmp_path)], as_of=AS_OF)
    assert "cron_tracking_root" in result
    assert "cron_tracking_root_env_override" in result


# --- diagnose_orders_pages_rejection -------------------------------------------


def test_bare_list_payload_is_rejected_as_missing_metadata():
    result = gfd.diagnose_orders_pages_rejection([{"id": "1"}], account_number=ACCOUNT, trading_day=DAY)
    assert result["rejected"] is True
    assert result["reason_code"] == "orders_pages_missing_query_metadata"
    assert result["recommendation"]
    assert result["payload_shape"]["type"] == "list"


def test_dict_missing_pages_key_is_rejected():
    payload = {"account_number": ACCOUNT, "trading_day": DAY}
    result = gfd.diagnose_orders_pages_rejection(payload, account_number=ACCOUNT, trading_day=DAY)
    assert result["reason_code"] == "orders_pages_missing_query_metadata"
    assert result["payload_shape"]["has_pages_key"] is False


def test_dict_missing_account_metadata_is_rejected():
    payload = {"pages": [], "trading_day": DAY}
    result = gfd.diagnose_orders_pages_rejection(payload, account_number=ACCOUNT, trading_day=DAY)
    assert result["reason_code"] == "orders_pages_missing_query_metadata"


def test_account_mismatch_is_a_distinct_reason():
    # "123456789" is one of test_config_invariants.py's own approved synthetic
    # account values -- an arbitrary account-shaped literal here would trip
    # that invariant even though it's plainly a fixture, not a real account.
    payload = {"pages": [], "account_number": "123456789", "trading_day": DAY}
    result = gfd.diagnose_orders_pages_rejection(payload, account_number=ACCOUNT, trading_day=DAY)
    assert result["reason_code"] == "orders_pages_account_mismatch"


def test_trading_day_mismatch_is_a_distinct_reason():
    payload = {"pages": [], "account_number": ACCOUNT, "trading_day": "2026-09-14"}
    result = gfd.diagnose_orders_pages_rejection(payload, account_number=ACCOUNT, trading_day=DAY)
    assert result["reason_code"] == "orders_pages_trading_day_mismatch"


def test_valid_payload_is_not_rejected():
    payload = {"pages": [{"orders": []}], "account_number": ACCOUNT, "trading_day": DAY}
    result = gfd.diagnose_orders_pages_rejection(payload, account_number=ACCOUNT, trading_day=DAY)
    assert result["rejected"] is False
    assert result["reason_code"] is None
    assert result["pages_found"] == 1


def test_diagnostic_never_echoes_the_actual_account_number_or_day():
    """AGENTS.md's secrets policy applies to the account number wherever it is
    persisted, including a local diagnostics log -- so the diagnostic reports
    only whether the values matched, never the values themselves."""
    payload = {"pages": [], "account_number": "123456789", "trading_day": "2026-09-14"}
    result = gfd.diagnose_orders_pages_rejection(payload, account_number=ACCOUNT, trading_day=DAY)
    dumped = json.dumps(result)
    assert "123456789" not in dumped
    assert ACCOUNT not in dumped
    assert "2026-09-14" not in dumped


# --- log_gate_failure_diagnostic -----------------------------------------------


def test_log_gate_failure_diagnostic_appends_jsonl(tmp_path):
    log_path = tmp_path / "gate_diagnostics" / "gate_failures.jsonl"
    gfd.log_gate_failure_diagnostic(
        {"gate": "afternoon_evidence", "likely_cause": gfd.AFTERNOON_STALE}, log_path=log_path
    )
    gfd.log_gate_failure_diagnostic(
        {"gate": "same_day_top_up", "reason_code": "orders_pages_account_mismatch"}, log_path=log_path
    )
    with log_path.open("r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["gate"] == "afternoon_evidence"
    assert "logged_at" in first


def test_log_gate_failure_diagnostic_creates_parent_dirs(tmp_path):
    log_path = tmp_path / "nested" / "dir" / "gate_failures.jsonl"
    gfd.log_gate_failure_diagnostic({"gate": "x"}, log_path=log_path)
    assert log_path.exists()


# --- CLI ------------------------------------------------------------------------


def test_cli_afternoon_evidence_subcommand(tmp_path, capsys):
    run_dir = tmp_path / "ce16ba6b" / "scheduled_runs" / "20260914_1530ET"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text(json.dumps(_afternoon_state("2026-09-14")))
    log_path = tmp_path / "gate_failures.jsonl"
    exit_code = gfd.main(
        [
            "afternoon-evidence",
            "--root",
            str(tmp_path / "ce16ba6b" / "scheduled_runs"),
            "--as-of",
            "2026-09-15",
            "--log-path",
            str(log_path),
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["likely_cause"] == gfd.AFTERNOON_HEALTHY
    assert log_path.exists()


def test_cli_orders_pages_subcommand(tmp_path, capsys):
    payload_path = tmp_path / "orders_pages.json"
    payload_path.write_text(json.dumps([{"id": "1"}]))
    log_path = tmp_path / "gate_failures.jsonl"
    exit_code = gfd.main(
        [
            "orders-pages",
            "--orders-pages-file",
            str(payload_path),
            "--account",
            ACCOUNT,
            "--trading-day",
            DAY,
            "--log-path",
            str(log_path),
        ]
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rejected"] is True
    assert log_path.exists()
