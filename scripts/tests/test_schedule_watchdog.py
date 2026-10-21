"""Tests for scripts/schedule_watchdog.py: the independent, non-LLM-judgment
alerting layer over scripts.verify_schedule.

Reproduces the 2026-09-22 silent-roster incident (scripts/tests/test_verify_schedule.py's
own reproduction case) end to end through this module's CLI, and pins that
the watchdog never depends on the failing session's own cooperation: it
writes its durable alert file(s) from evidence alone.
"""

import json
import os
import sys
import unittest
import urllib.error
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import schedule_watchdog  # noqa: E402
from scripts.schedule_watchdog import (  # noqa: E402
    WATCHDOG_GATE_TOKEN,
    alert_body,
    failing_rows,
    main,
    send_slack_alert,
    write_watchdog_alert_files,
)
from scripts.verify_schedule import verify_day  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 22)  # the day docs/schedule_verification.md documents as roster-silent
AFTER_CLOSE = datetime(2026, 9, 22, 18, 0, tzinfo=ET)


class FailingRowsTests(unittest.TestCase):
    def test_empty_root_reproduces_every_slot_failing(self):
        """The 2026-09-22 incident: every slot's run directory absent."""
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            rows = failing_rows(report)
            self.assertEqual(
                {row["slot_id"] for row in rows},
                {"0935", "1030", "1130", "1230", "1330", "1430", "1530"},
            )
            for row in rows:
                self.assertEqual(row["status"], "no_evidence")

    def test_no_failing_rows_when_not_due(self):
        """Nothing has fired yet -- not_due is not a failure."""
        with _tmp_root() as root:
            not_due = datetime(2026, 9, 22, 8, 0, tzinfo=ET)
            report = verify_day(DAY, not_due, root=root)
            self.assertEqual(failing_rows(report), [])


class SlotDidNotFireTagTests(unittest.TestCase):
    """The `slot_did_not_fire` triage Key marks a slot that never started, not a late or half-finished one."""

    def test_no_evidence_row_is_tagged_slot_did_not_fire(self):
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            rows = failing_rows(report)
            self.assertEqual(len(rows), 7)
            for row in rows:
                self.assertEqual(row["failure_key"], "slot_did_not_fire")

    def test_late_slot_inside_grace_is_not_tagged(self):
        """12:30 slot, 12:50 ET: past nominal but inside the grace deadline -> awaiting, not a miss."""
        with _tmp_root() as root:
            report = verify_day(DAY, datetime(2026, 9, 22, 12, 50, tzinfo=ET), root=root)
            row = next(r for r in report["slots"] if r["slot_id"] == "1230")
            self.assertEqual(row["status"], "awaiting_evidence")
            self.assertIsNone(row["failure_key"])

    def test_started_but_unfinalized_slot_is_not_tagged(self):
        """A run directory with no run_state.json is the 2026-09-22 Gate 9 failure, a different Key."""
        with _tmp_root() as root:
            (root / "midday_dca" / "20260922_1230ET").mkdir(parents=True)
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            row = next(r for r in report["slots"] if r["slot_id"] == "1230")
            self.assertEqual(row["status"], "evidence_incomplete")
            self.assertIsNone(row["failure_key"])

    def test_alert_body_names_the_key_only_for_a_missed_slot(self):
        with _tmp_root() as root:
            (root / "midday_dca" / "20260922_1230ET").mkdir(parents=True)
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            missed = next(r for r in report["slots"] if r["slot_id"] == "1030")
            started = next(r for r in report["slots"] if r["slot_id"] == "1230")
            self.assertIn("Key: slot_did_not_fire", alert_body(report, missed))
            self.assertNotIn("slot_did_not_fire", alert_body(report, started))


def _write_attempt(root, stream, triggered_utc, phase="before_run_artifact", day="20260922"):
    """One line in <attempt_log>/<day>/<stream>.attempts.jsonl, the shape dca_alerts writes."""
    day_dir = root / "attempt_log" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"stream": stream, "triggered_at_utc": triggered_utc, "failure_phase": phase})
    with (day_dir / f"{stream}.attempts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class AttemptLogConditionTests(unittest.TestCase):
    """A failed-start marker inside the slot's window means it fired and crashed, so it did not 'not fire'."""

    def _row(self, root, slot_id):
        with patch.dict(os.environ, {"DCA_ATTEMPT_LOG_ROOT": ""}):
            report = verify_day(DAY, AFTER_CLOSE, root=root)
        return next(r for r in report["slots"] if r["slot_id"] == slot_id)

    def test_failed_start_marker_in_window_is_not_tagged_but_still_fails(self):
        with _tmp_root() as root:
            _write_attempt(root, "midday", "2026-09-22T16:31:00Z")  # 12:31 ET
            row = self._row(root, "1230")
            self.assertEqual(row["status"], "no_evidence")
            self.assertIsNone(row["failure_key"])
            self.assertTrue(any("failed-start marker" in reason for reason in row["reasons"]))

    def test_marker_for_another_stream_does_not_excuse_the_slot(self):
        with _tmp_root() as root:
            _write_attempt(root, "morning", "2026-09-22T16:31:00Z")
            self.assertEqual(self._row(root, "1230")["failure_key"], "slot_did_not_fire")

    def test_marker_outside_the_slot_window_does_not_excuse_it(self):
        with _tmp_root() as root:
            _write_attempt(root, "midday", "2026-09-22T13:00:00Z")  # 09:00 ET, hours early
            self.assertEqual(self._row(root, "1230")["failure_key"], "slot_did_not_fire")

    def test_intraday_marker_excuses_only_its_own_slot(self):
        with _tmp_root() as root:
            _write_attempt(root, "intraday", "2026-09-22T17:31:00Z")  # 13:31 ET
            self.assertIsNone(self._row(root, "1330")["failure_key"])
            self.assertEqual(self._row(root, "1130")["failure_key"], "slot_did_not_fire")
            self.assertEqual(self._row(root, "1430")["failure_key"], "slot_did_not_fire")

    def test_after_run_artifact_marker_is_not_a_failed_start(self):
        with _tmp_root() as root:
            _write_attempt(root, "midday", "2026-09-22T16:31:00Z", phase="after_run_artifact")
            self.assertEqual(self._row(root, "1230")["failure_key"], "slot_did_not_fire")

    def test_unreadable_attempt_log_fails_toward_tagging(self):
        with _tmp_root() as root:
            day_dir = root / "attempt_log" / "20260922"
            day_dir.mkdir(parents=True)
            (day_dir / "midday.attempts.jsonl").write_text("not json\n{broken", encoding="utf-8")
            self.assertEqual(self._row(root, "1230")["failure_key"], "slot_did_not_fire")


class AlertBodyTests(unittest.TestCase):
    def test_body_names_slot_stream_status_and_reasons(self):
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            row = next(r for r in report["slots"] if r["slot_id"] == "1230")
            body = alert_body(report, row)
            self.assertIn("12:30 ET (midday)", body)
            self.assertIn("no_evidence", body)
            self.assertIn("no run directory for this slot", body)
            self.assertIn("midday_dca/20260922_1230ET", body)


class WriteAlertFilesTests(unittest.TestCase):
    def test_writes_one_durable_file_per_failing_slot(self):
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            with patch.dict(os.environ, {"DCA_CRON_TRACKING_ROOT": str(root)}):
                written = write_watchdog_alert_files(report)
            self.assertEqual(len(written), 7)
            for path in written:
                self.assertTrue(path.is_file())
                self.assertIn(f"-{WATCHDOG_GATE_TOKEN}.txt", path.name)
                self.assertIn("schedule_watchdog", str(path))

    def test_no_files_written_when_everything_verified(self):
        with _tmp_root() as root:
            not_due = datetime(2026, 9, 22, 8, 0, tzinfo=ET)
            report = verify_day(DAY, not_due, root=root)
            with patch.dict(os.environ, {"DCA_CRON_TRACKING_ROOT": str(root)}):
                self.assertEqual(write_watchdog_alert_files(report), [])

    def test_root_argument_is_honored_independently_of_the_env_var(self):
        """A caller-supplied root must win over $DCA_CRON_TRACKING_ROOT, not be
        shadowed by it (PR #1535 review: the first version always resolved the
        alert-file root from the env var even when --root pointed elsewhere)."""
        with _tmp_root() as evidence_root, _tmp_root() as alert_root, _tmp_root() as decoy_env_root:
            report = verify_day(DAY, AFTER_CLOSE, root=evidence_root)
            with patch.dict(os.environ, {"DCA_CRON_TRACKING_ROOT": str(decoy_env_root)}):
                written = write_watchdog_alert_files(report, root=alert_root)
            self.assertEqual(len(written), 7)
            for path in written:
                self.assertTrue(str(path).startswith(str(alert_root)))
            self.assertFalse((decoy_env_root / "schedule_watchdog").exists())

    def test_rerunning_for_the_same_unresolved_day_overwrites_not_numbers(self):
        """A recurring fire for a still-open absence must not multiply alert
        files (PR #1535 review: dca_alerts.write_alert_file's collision
        numbering, designed for distinct executor events, inflates
        alert_digest counts when reused here for one ongoing absence)."""
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            first = write_watchdog_alert_files(report, root=root)
            second = write_watchdog_alert_files(report, root=root)
            self.assertEqual(sorted(p.name for p in first), sorted(p.name for p in second))
            alerts_dir = root / "schedule_watchdog" / "alerts"
            self.assertEqual(len(list(alerts_dir.glob("*.txt"))), 7)  # not 14


class SlackTests(unittest.TestCase):
    def setUp(self):
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
            old_value = os.environ.pop(name, None)
            if old_value is not None:
                self.addCleanup(os.environ.__setitem__, name, old_value)

    def test_returns_false_without_webhook(self):
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            self.assertFalse(
                send_slack_alert(report, webhook=None, env_var="DEFINITELY_UNSET_WATCHDOG_HOOK")
            )

    def test_returns_false_when_nothing_failing_even_with_webhook(self):
        """A clean report never POSTs, regardless of webhook configuration."""
        with _tmp_root() as root:
            not_due = datetime(2026, 9, 22, 8, 0, tzinfo=ET)
            report = verify_day(DAY, not_due, root=root)
            self.assertFalse(send_slack_alert(report, webhook="https://hooks.example/alert"))

    def test_rejects_invalid_scheme(self):
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            with self.assertRaises(urllib.error.URLError):
                send_slack_alert(report, webhook="file:///etc/passwd")

    def test_primary_env_var_falls_back_to_the_shared_alert_webhooks(self):
        """DCA_WATCHDOG_SLACK_WEBHOOK unset but DCA_ALERT_SLACK_WEBHOOK set still resolves."""
        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            with patch.dict(os.environ, {"DCA_ALERT_SLACK_WEBHOOK": "file:///etc/passwd"}, clear=False):
                os.environ.pop("DCA_WATCHDOG_SLACK_WEBHOOK", None)
                with self.assertRaises(urllib.error.URLError):
                    send_slack_alert(report)  # resolves to the fallback, then fails destination validation


class CliTests(unittest.TestCase):
    def test_exit_1_and_alert_files_on_silent_roster(self):
        with _tmp_root() as root:
            with patch.dict(os.environ, {"DCA_CRON_TRACKING_ROOT": str(root)}):
                with patch.object(schedule_watchdog, "datetime") as mock_dt:
                    mock_dt.now.return_value = AFTER_CLOSE
                    code = main(["--date", "2026-09-22", "--root", str(root)])
            self.assertEqual(code, 1)
            alerts_dir = root / "schedule_watchdog" / "alerts"
            self.assertTrue(alerts_dir.is_dir())
            self.assertEqual(len(list(alerts_dir.glob("*.txt"))), 7)

    def test_no_alert_file_flag_skips_the_durable_writer(self):
        with _tmp_root() as root:
            code = main(["--date", "2026-09-22", "--root", str(root), "--no-alert-file"])
            self.assertEqual(code, 1)
            self.assertFalse((root / "schedule_watchdog").exists())

    def test_json_output_is_the_verify_day_report(self):
        with _tmp_root() as root:
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = main(["--date", "2026-09-22", "--root", str(root), "--json", "--no-alert-file"])
            self.assertEqual(code, 1)
            report = json.loads(buf.getvalue())
            self.assertEqual(report["date"], "2026-09-22")
            self.assertFalse(report["ok"])

    def test_slack_without_webhook_warns_and_exit_code_still_reflects_failures(self):
        with _tmp_root() as root:
            code = main(
                [
                    "--date", "2026-09-22", "--root", str(root), "--no-alert-file",
                    "--slack", "--slack-webhook-env", "DEFINITELY_UNSET_WATCHDOG_HOOK",
                ]
            )
            self.assertEqual(code, 1)  # slack config is independent of the audit's own exit code

    def test_slack_send_failure_is_a_warning_not_a_crash(self):
        with _tmp_root() as root:
            with patch.object(schedule_watchdog, "send_slack_alert", side_effect=urllib.error.URLError("boom")):
                code = main(
                    [
                        "--date", "2026-09-22", "--root", str(root), "--no-alert-file",
                        "--slack", "--slack-webhook", "https://hooks.example/alert",
                    ]
                )
            self.assertEqual(code, 1)

    def test_clean_day_exits_zero(self):
        with _tmp_root() as root:
            not_due = datetime(2026, 9, 22, 8, 0, tzinfo=ET)
            with patch.object(schedule_watchdog, "datetime") as mock_dt:
                mock_dt.now.return_value = not_due
                code = main(["--date", "2026-09-22", "--root", str(root)])
            self.assertEqual(code, 0)
            self.assertFalse((root / "schedule_watchdog").exists())


class AlertDigestIntegrationTests(unittest.TestCase):
    def test_alert_digest_recognizes_the_gate_token(self):
        """The files this module writes must not load as gate=None (PR #1535
        review): SCHEDULE_WATCHDOG has to be a token
        scripts.alert_digest.detect_gate_evidence actually recognizes, not
        just a filename convention this module invented on its own."""
        from scripts.alert_digest import load_alert_files

        with _tmp_root() as root:
            report = verify_day(DAY, AFTER_CLOSE, root=root)
            write_watchdog_alert_files(report, root=root)
            records = load_alert_files(root / "schedule_watchdog" / "alerts")
            self.assertEqual(len(records), 7)
            for record in records:
                self.assertEqual(record.gate, "SCHEDULE_WATCHDOG")
                self.assertIsNotNone(record.slot)


def _tmp_root():
    import tempfile

    class _Ctx:
        def __enter__(self):
            self._tmp = tempfile.TemporaryDirectory()
            return Path(self._tmp.name)

        def __exit__(self, *exc):
            self._tmp.cleanup()
            return False

    return _Ctx()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
