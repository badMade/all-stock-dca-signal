"""Failing-first tests: the watchdog flags slots that ran but ended in an error outcome.

Today the watchdog only alerts on missing or incomplete evidence. A slot that fired, wrote a complete,
valid run_state.json and finalized as ``not_submitted_error`` (the classifier refusals of 2026-10-26 and
2026-10-27, the market_data_unavailable aborts) verifies cleanly, so the day looks green to the one process that
is supposed to catch what the executor session does not report. Contract pinned here:

* ``verify_day`` rows carry an additive ``error_class`` (None when absent); the audit verdict is unchanged.
* ``schedule_watchdog.error_outcome_rows`` returns VERIFIED rows whose ``order_submit_status`` is an error status
  (``not_submitted_error``, ``blocked_placement_phase_unreachable``) and nothing else: quiet outcomes
  (no orders, no candidates, budget exhausted, confirmation required) are not errors, and a row that already
  fails the evidence audit is reported by ``failing_rows``, never twice.
* An error row gets its own durable alert file (one per day and slot, same filename shape) and an alert body that
  says the slot ended in an error outcome, not that evidence is missing.
* ``main`` exits 1 and reports ``error_slots`` in its JSON; a clean day still exits 0 with ``error_slots == []``.
* The desktop banner and Slack text name slot ids only, never run_state values such as ``error_class``.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import schedule_watchdog  # noqa: E402
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative, verify_day  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 22)
AFTER_CLOSE = datetime(2026, 9, 22, 18, 0, tzinfo=ET)
ERROR_STATUSES = ("not_submitted_error", "blocked_placement_phase_unreachable")
QUIET_STATUSES = (
    "submitted",
    "no_orders_submitted",
    "not_submitted_no_candidates",
    "not_submitted_budget_exhausted",
    "not_submitted_confirmation_required",
)


def write_complete_day(root, *, overrides=None):
    """Every slot verified; ``overrides`` maps slot_id -> extra run_state fields."""
    overrides = overrides or {}
    for slot_id, (stream, _) in SLOT_EVIDENCE_LAYOUT.items():
        run_dir = root / slot_run_dir_relative(slot_id, DAY)
        run_dir.mkdir(parents=True)
        rc = {"run_origin": "scheduled", "manual_run": False, "stream": stream,
              "scheduled_for_et": f"{slot_id[:2]}:{slot_id[2:]}"}
        state = {"trading_day": DAY.isoformat(), "run_classification": rc,
                 "order_submit_status": "no_orders_submitted", **overrides.get(slot_id, {})}
        (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
        (run_dir / "run_summary.json").write_text(json.dumps({"run_classification": rc}), encoding="utf-8")
        (run_dir / "signals.csv").write_text("ticker\n", encoding="utf-8")
        (run_dir / "submitted_orders.csv").write_text("ticker,order_id,submitted_amount_usd,status\n", encoding="utf-8")


def error_day(root, slot_id="1330", status="not_submitted_error", error_class="platform_refusal"):
    extra = {"order_submit_status": status}
    if error_class:
        extra["error_class"] = error_class
    write_complete_day(root, overrides={slot_id: extra})
    return verify_day(DAY, AFTER_CLOSE, root=root)


def row_for(report, slot_id):
    return next(row for row in report["slots"] if row["slot_id"] == slot_id)


def test_rows_carry_the_error_class_and_the_audit_verdict_is_unchanged(tmp_path):
    report = error_day(tmp_path)
    assert row_for(report, "1330")["status"] == "verified"
    assert row_for(report, "1330")["error_class"] == "platform_refusal"
    assert row_for(report, "1030")["error_class"] is None
    assert report["ok"] is True


@pytest.mark.parametrize("status", ERROR_STATUSES)
def test_error_statuses_are_flagged(tmp_path, status):
    report = error_day(tmp_path, status=status)
    assert [row["slot_id"] for row in schedule_watchdog.error_outcome_rows(report)] == ["1330"]


@pytest.mark.parametrize("status", QUIET_STATUSES)
def test_quiet_outcomes_are_not_errors(tmp_path, status):
    report = error_day(tmp_path, status=status, error_class=None)
    assert schedule_watchdog.error_outcome_rows(report) == []


def test_a_row_that_already_fails_the_audit_is_not_reported_twice(tmp_path):
    report = error_day(tmp_path)
    row_for(report, "1330")["status"] = "evidence_incomplete"
    assert schedule_watchdog.error_outcome_rows(report) == []
    assert [r["slot_id"] for r in schedule_watchdog.failing_rows(report)] == ["1330"]


def test_the_alert_body_says_error_outcome_not_missing_evidence(tmp_path):
    report = error_day(tmp_path)
    body = schedule_watchdog.alert_body(report, row_for(report, "1330"))
    assert "error outcome" in body and "not_submitted_error" in body and "platform_refusal" in body
    assert "13:30" in body and "left no valid scheduled evidence" not in body


def test_an_error_slot_gets_one_durable_alert_file(tmp_path):
    report = error_day(tmp_path)
    written = schedule_watchdog.write_watchdog_alert_files(report, root=tmp_path)
    assert [p.name for p in written] == [f"2026-09-22-1330-{schedule_watchdog.WATCHDOG_GATE_TOKEN}.txt"]
    assert "error outcome" in written[0].read_text(encoding="utf-8")


def test_cli_exits_1_and_lists_error_slots(tmp_path, capsys):
    error_day(tmp_path)
    assert schedule_watchdog.main(["--date", "2026-09-22", "--root", str(tmp_path), "--json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["error_slots"] == ["1330"]


def test_cli_stays_green_on_a_clean_day(tmp_path, capsys):
    write_complete_day(tmp_path)
    assert schedule_watchdog.main(["--date", "2026-09-22", "--root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["error_slots"] == []


def test_the_banner_names_slot_ids_and_never_quotes_run_state_values(tmp_path):
    error_day(tmp_path)
    with patch.object(schedule_watchdog, "notify_macos") as notify:
        schedule_watchdog.main(["--date", "2026-09-22", "--root", str(tmp_path), "--notify"])
    notify.assert_called_once()
    message = notify.call_args.args[1]
    assert "1330" in message and "platform_refusal" not in message and "not_submitted_error" not in message


def test_slack_text_names_the_error_slot(tmp_path):
    report = error_day(tmp_path)
    sent = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, request, timeout=None):
            sent["body"] = json.loads(request.data)["text"]
            return _Response()

    with patch.object(schedule_watchdog, "_assert_safe_destination"), \
            patch.object(schedule_watchdog.urllib.request, "build_opener", return_value=_Opener()):
        assert schedule_watchdog.send_slack_alert(report, webhook="https://hooks.example.test/x") is True
    assert "13:30" in sent["body"] and "error outcome" in sent["body"]
