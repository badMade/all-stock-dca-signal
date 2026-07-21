"""Tests for scripts/drift_detection.py.

Runnable with ``pytest scripts/tests`` or ``python scripts/tests/test_drift_detection.py``.

Fixture scenarios:
  * clean matched orders (no drift)
  * top-up-blocked rows (deliberate $0 buys, not drift-with-unknown-cause)
  * auto-buy rows without orders (the drift case)
  * missing raw/ directory (Jul 16 2026 failure mode)
  * empty submitted_orders.csv
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drift_detection import (  # noqa: E402
    CAUSE_ORDERS_EMPTY,
    CAUSE_RAW_MISSING,
    CAUSE_ROW_BLOCK,
    DEFAULT_SLACK_WEBHOOK_ENV,
    build_slack_payload,
    detect_drift,
    main,
    parse_amount,
    parse_bool,
    resolve_webhook,
    send_slack_alert,
)

SIGNAL_FIELDS = [
    "ticker",
    "auto_buy_signal",
    "incremental_order_amount",
    "afternoon_desired_amount",
    "top_up_guard_result",
    "block_reason",
    "status",
    "data_confidence",
    "spread",
]
ORDER_FIELDS = ["ticker", "status", "amount_usd", "order_id"]


def _write_csv(path: Path, fields, rows) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _signal(ticker, auto_buy="true", incremental=0.0, desired=0.0, guard="", block="", status="", confidence="", spread=""):
    return {
        "ticker": ticker,
        "auto_buy_signal": auto_buy,
        "incremental_order_amount": incremental,
        "afternoon_desired_amount": desired,
        "top_up_guard_result": guard,
        "block_reason": block,
        "status": status,
        "data_confidence": confidence,
        "spread": spread,
    }


def _order(ticker, amount=3.0, status="submitted", order_id="oid-1"):
    return {"ticker": ticker, "status": status, "amount_usd": amount, "order_id": order_id}


class DriftDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _raw(self):
        (self.run_dir / "raw").mkdir()

    # --- clean matched orders ------------------------------------------------
    def test_clean_matched_orders_has_no_drift(self):
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("MSFT", desired=6.0)],
        )
        _write_csv(
            self.run_dir / "submitted_orders.csv",
            ORDER_FIELDS,
            [_order("AAPL"), _order("MSFT", amount=6.0)],
        )
        report = detect_drift(self.run_dir)
        self.assertFalse(report.drift_detected)
        self.assertEqual(report.affected_symbols, [])
        self.assertEqual(report.expected_symbol_count, 2)
        self.assertEqual(report.run_suspected_cause, "none")

    # --- top-up-blocked rows -------------------------------------------------
    def test_top_up_blocked_rows_are_classified_as_row_block(self):
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [
                _signal("AAPL", incremental=3.0),  # will be ordered
                _signal("NVDA", desired=6.0, guard="top_up_no_incremental", status="skipped"),
            ],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL")])
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        self.assertEqual(len(report.affected_symbols), 1)
        drift = report.affected_symbols[0]
        self.assertEqual(drift.symbol, "NVDA")
        self.assertEqual(drift.suspected_cause, CAUSE_ROW_BLOCK)
        self.assertTrue(any("top_up" in e for e in drift.evidence))
        self.assertEqual(report.run_suspected_cause, CAUSE_ROW_BLOCK)

    # --- auto-buy rows without orders ---------------------------------------
    def test_auto_buy_without_orders_flags_drift(self):
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("MSFT", desired=6.0)],
        )
        # submitted_orders.csv present with an unrelated symbol only.
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("TSLA")])
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        affected = {s.symbol for s in report.affected_symbols}
        self.assertEqual(affected, {"AAPL", "MSFT"})

    def test_case_insensitive_auto_buy_tokens(self):
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [
                _signal("AAPL", auto_buy="TRUE", incremental=3.0),
                _signal("MSFT", auto_buy="Yes", desired=6.0),
                _signal("NVDA", auto_buy="1", incremental=9.0),
                _signal("AMD", auto_buy="false", incremental=9.0),  # not an auto-buy
            ],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        report = detect_drift(self.run_dir)
        affected = {s.symbol for s in report.affected_symbols}
        self.assertEqual(affected, {"AAPL", "MSFT", "NVDA"})
        self.assertNotIn("AMD", affected)

    # --- missing raw/ directory (Jul 16 2026 failure mode) ------------------
    def test_missing_raw_dir_jul16_failure_mode(self):
        # 8 auto-buy rows with positive incremental amounts, empty orders, no raw/.
        symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMD", "NFLX", "LUNR", "GLW"]
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal(sym, incremental=3.0 * (i + 1)) for i, sym in enumerate(symbols)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])  # empty
        # raw/ intentionally NOT created
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        self.assertEqual(len(report.affected_symbols), 8)
        self.assertFalse(report.raw_dir_present)
        # raw-missing takes priority over empty-orders as the run cause.
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING)
        self.assertIn("raw/", report.recommended_action)

    # --- empty submitted_orders.csv (raw present) ---------------------------
    def test_empty_submitted_orders_with_raw_present(self):
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        self.assertTrue(report.submitted_orders_empty)
        self.assertEqual(report.run_suspected_cause, CAUSE_ORDERS_EMPTY)

    # --- output formats ------------------------------------------------------
    def test_json_and_markdown_outputs(self):
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        report = detect_drift(self.run_dir)
        payload = json.loads(report.to_json(compact=True))
        self.assertEqual(payload["affected_symbols"][0]["symbol"], "AAPL")
        self.assertEqual(payload["affected_symbols"][0]["expected_amount_usd"], 3.0)
        md = report.to_markdown()
        self.assertIn("DRIFT DETECTED", md)
        self.assertIn("| AAPL |", md)

    # --- CLI + files + exit codes -------------------------------------------
    def test_cli_writes_outputs_and_fail_on_drift(self):
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        json_out = self.run_dir / "drift.json"
        md_out = self.run_dir / "drift.md"
        code = main(
            [
                "--run-dir",
                str(self.run_dir),
                "--json-out",
                str(json_out),
                "--md-out",
                str(md_out),
                "--fail-on-drift",
            ]
        )
        self.assertEqual(code, 2)
        self.assertTrue(json_out.exists())
        self.assertTrue(md_out.exists())
        self.assertTrue(json.loads(json_out.read_text())["drift_detected"])

    def test_cli_clean_run_exit_zero(self):
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL")])
        code = main(["--run-dir", str(self.run_dir), "--fail-on-drift"])
        self.assertEqual(code, 0)

    def test_cli_missing_run_dir_errors(self):
        code = main(["--run-dir", str(self.run_dir / "does-not-exist")])
        self.assertEqual(code, 1)

    # --- afternoon action=buy without reconciled candidate -------------------
    def test_afternoon_action_buy_without_candidate_is_not_drift(self):
        """Finance-alone action=buy with no buy_candidates.csv entry must not alert.

        When reconciliation filters out a Finance-only signal (e.g. Robinhood
        disagreed), no buy_candidates.csv entry is written and no order is ever
        placed. The detector must not count this as drift — absence of a buy
        candidate is the evidence that the signal was legitimately filtered.
        """
        self._raw()
        afternoon_signal_fields = ["ticker", "action", "buy_multiplier"]
        with open(self.run_dir / "signals.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=afternoon_signal_fields)
            writer.writeheader()
            writer.writerow({"ticker": "AAPL", "action": "buy", "buy_multiplier": "1.0"})
        # No buy_candidates.csv — reconciliation produced no candidate for AAPL.
        report = detect_drift(self.run_dir)
        self.assertFalse(report.drift_detected)
        self.assertEqual(report.expected_symbol_count, 0)

    def test_afternoon_action_buy_with_candidate_and_no_order_is_drift(self):
        """Afternoon action=buy with a matching buy_candidates.csv entry but no
        submitted order IS real drift."""
        self._raw()
        afternoon_signal_fields = ["ticker", "action", "buy_multiplier"]
        with open(self.run_dir / "signals.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=afternoon_signal_fields)
            writer.writeheader()
            writer.writerow({"ticker": "AAPL", "action": "buy", "buy_multiplier": "1.0"})
        with open(self.run_dir / "buy_candidates.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["ticker", "desired_amount_usd", "status"])
            writer.writeheader()
            writer.writerow({"ticker": "AAPL", "desired_amount_usd": "7.50", "status": "buy"})
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        self.assertEqual(report.expected_symbol_count, 1)
        self.assertEqual(report.affected_symbols[0].symbol, "AAPL")


class SlackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.run_dir / "raw").mkdir()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])

    def test_resolve_webhook_prefers_arg_then_env(self):
        self.assertEqual(resolve_webhook("https://arg", "SOME_ENV"), "https://arg")

    def test_resolve_webhook_reads_env(self):
        import os

        os.environ["TEST_DRIFT_HOOK"] = "https://env"
        self.addCleanup(os.environ.pop, "TEST_DRIFT_HOOK", None)
        self.assertEqual(resolve_webhook(None, "TEST_DRIFT_HOOK"), "https://env")

    def test_resolve_webhook_none_when_unset(self):
        self.assertIsNone(resolve_webhook(None, "DEFINITELY_UNSET_HOOK_VAR"))

    def test_send_slack_returns_false_without_webhook(self):
        report = detect_drift(self.run_dir)
        self.assertFalse(send_slack_alert(report, webhook=None, env_var="DEFINITELY_UNSET_HOOK_VAR"))

    def test_send_slack_rejects_invalid_scheme(self):
        import urllib.error

        report = detect_drift(self.run_dir)
        with self.assertRaises(urllib.error.URLError):
            send_slack_alert(report, webhook="file:///etc/passwd")

    def test_slack_payload_shape(self):
        report = detect_drift(self.run_dir)
        payload = build_slack_payload(report)
        self.assertIn("text", payload)
        self.assertIn("AAPL", payload["text"])
        self.assertIn("DCA drift detected", payload["text"])

    def test_no_hardcoded_webhook_in_default_env(self):
        self.assertTrue(DEFAULT_SLACK_WEBHOOK_ENV.isupper())


class ParsingTests(unittest.TestCase):
    def test_parse_bool(self):
        for token in ("true", "True", "1", "yes", "Y", "t"):
            self.assertTrue(parse_bool(token))
        for token in ("false", "0", "no", "", None):
            self.assertFalse(parse_bool(token))

    def test_parse_amount(self):
        self.assertEqual(parse_amount("$1,234.50"), 1234.5)
        self.assertEqual(parse_amount(""), 0.0)
        self.assertEqual(parse_amount("bad"), 0.0)
        self.assertEqual(parse_amount(9), 9.0)


if __name__ == "__main__":
    unittest.main()
