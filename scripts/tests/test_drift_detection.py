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
import http.client
import ipaddress
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import scripts.drift_detection as drift_detection  # noqa: E402
except ModuleNotFoundError as _import_exc:  # pragma: no cover - fallback when running directly in scripts/
    # Only a failure to resolve the "scripts" package itself -- e.g. running
    # this file directly as `python scripts/tests/test_drift_detection.py`
    # without the repo root on sys.path -- means package-style import
    # doesn't apply here and the flat, run-from-scripts/ import style is
    # needed instead. A ModuleNotFoundError for anything else (a real
    # missing dependency inside drift_detection.py's OWN imports) must not
    # be silently reinterpreted as "package layout doesn't resolve" and
    # retried through a duplicate module instance under a different import
    # path, which would obscure the real failure and could test against a
    # second, separately-initialized copy of the module (Codex review:
    # "Narrow the direct-execution import fallback"; AGENTS.md: "Do not
    # wrap imports in broad try/except blocks").
    if _import_exc.name not in ("scripts", "scripts.drift_detection"):
        raise
    import drift_detection  # type: ignore # noqa: E402

import dca_net_utils  # noqa: E402

# Pulled as attributes off the already-resolved module (rather than a
# second `from <module> import (...)` statement duplicated per branch
# above) so there is only ONE place that can raise on a genuinely missing
# symbol, and it raises a plain, honest AttributeError instead of being
# eligible for the same broad-except reinterpretation this fix removes.
CAUSE_BROKER_PLACEMENT_AMBIGUOUS = drift_detection.CAUSE_BROKER_PLACEMENT_AMBIGUOUS
CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED = (
    drift_detection.CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED
)
CAUSE_ORDERS_EMPTY = drift_detection.CAUSE_ORDERS_EMPTY
CAUSE_RAW_MISSING = drift_detection.CAUSE_RAW_MISSING
CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION = drift_detection.CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION
CAUSE_ROW_BLOCK = drift_detection.CAUSE_ROW_BLOCK
DEFAULT_SLACK_WEBHOOK_ENV = drift_detection.DEFAULT_SLACK_WEBHOOK_ENV
build_slack_payload = drift_detection.build_slack_payload
detect_drift = drift_detection.detect_drift
load_run_artifacts = drift_detection.load_run_artifacts
main = drift_detection.main
parse_amount = drift_detection.parse_amount
parse_bool = drift_detection.parse_bool
resolve_webhook = drift_detection.resolve_webhook
send_slack_alert = drift_detection.send_slack_alert

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

    # --- confirmation-blocked rows are not real submissions ------------------
    def test_confirmation_required_row_does_not_count_as_submitted(self):
        # Codex review r3623249074: scripts/scheduled_artifacts.py's
        # submitted_orders.csv writer emits a row for a
        # not_submitted_confirmation_required outcome (order review passed but
        # OrderConfirmationRequiredError blocked the actual broker call), so a
        # row existing for a ticker must not by itself mean it was submitted --
        # otherwise a genuinely blocked buy is silently reconciled as reported
        # and the drift/Slack alert never fires.
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0)],
        )
        _write_csv(
            self.run_dir / "submitted_orders.csv",
            ORDER_FIELDS,
            [_order("AAPL", status="not_submitted_confirmation_required", order_id="")],
        )
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        affected = {s.symbol for s in report.affected_symbols}
        self.assertEqual(affected, {"AAPL"})

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

    # --- header-only signals files (Feb 2026 failure mode) ---------------------
    def test_detect_drift_header_only_signals_with_run_state_orders(self):
        # Regression test: header-only signals.csv files used to return
        # drift_detected=False (false-clean) instead of triggering an alert.
        # _has_usable_header() returned True for a file with only headers,
        # _first_usable() selected it, then read_csv_rows() returned [].
        # The detector saw no expected buys (signals.csv was empty) and reported
        # no drift, even though run_state["broker_outcomes"] confirmed that
        # orders were actually placed. This is a genuine drift condition:
        # a placed order with no matching signals file to document intent.
        # Now _first_usable() verifies at least one data row exists; a
        # header-only file is treated as unusable, and run_state-confirmed
        # orders still surface as drift (Codex review: "Validate row count to
        # reject header-only files").
        self._raw()
        # Create a header-only signals.csv with no data rows.
        signals_path = self.run_dir / "signals.csv"
        with signals_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SIGNAL_FIELDS)
            writer.writeheader()
            # Intentionally write no rows -- header only.

        # run_state.json shows that an order WAS actually placed for AAPL,
        # even though signals.csv is empty. This is the anomaly we're testing:
        # a placed order with no corresponding signal/intent record.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "afternoon",
                    "status": "completed",
                    "broker_outcomes": {"AAPL": {"status": "submitted", "order_id": "oid-1"}},
                }
            ),
            encoding="utf-8",
        )

        # submitted_orders.csv is absent or empty, so the canonical order
        # record is missing. run_state proves the order reached the broker.
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])

        # The detector must recognize the header-only signals file as unusable
        # (no signals with intent) and the run_state-confirmed order as drift:
        # a placed order with no canonical record AND no signal intent.
        _write_csv(
            self.run_dir / "morning_signals.csv", SIGNAL_FIELDS, [_signal("AAPL", auto_buy="false")]
        )
        artifacts = drift_detection.load_run_artifacts(self.run_dir)
        self.assertEqual(artifacts.signals_path, self.run_dir / "morning_signals.csv")
        report = detect_drift(self.run_dir)

        # Drift should be detected: run_state shows AAPL was submitted but
        # submitted_orders.csv does not record it (and signals.csv has no
        # expected buys to document the intent).
        self.assertTrue(report.drift_detected, "Order confirmed by run_state but not in signals or CSV should trigger drift alert")
        self.assertEqual(len(report.affected_symbols), 1)
        self.assertEqual(report.affected_symbols[0].symbol, "AAPL")
        # The cause is that the canonical order record is missing, run-state confirmed.
        self.assertEqual(
            report.affected_symbols[0].suspected_cause,
            CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED,
        )

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

    def test_current_schema_morning_run_requires_raw_dir_despite_stream_exemption(self):
        # Codex review on PR #538 ("Require raw evidence for current morning
        # and midday runs"): _STREAMS_WITHOUT_RAW exists for LEGACY morning/
        # midday artifacts written before the raw-persistence contract, which
        # genuinely never wrote a raw/ directory. A current-schema artifact
        # stamps raw_input_dir unconditionally on every success path (see
        # morning_dca.py's _RawInputRecorder wiring) -- if ONE of these has
        # auto-buy signals with missing orders and its raw/ directory was
        # deleted or never created, without any raw_persistence_error/
        # error_class stamped, that must still surface as CAUSE_RAW_MISSING
        # rather than being waved through to CAUSE_ORDERS_EMPTY just because
        # the stream name is in the legacy exemption set.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "raw_input_dir": str(self.run_dir / "raw"),
                    "raw_inputs_saved": ["quote_AAPL_1.json"],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        # raw/ intentionally NOT created on disk, despite raw_input_dir being stamped.
        report = detect_drift(self.run_dir)
        self.assertFalse(report.raw_dir_present)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING)

    def test_legacy_morning_run_without_raw_input_dir_key_is_exempt(self):
        # The companion case: a run_state with NO raw_input_dir key at all
        # predates the raw-persistence contract entirely, so the legacy
        # exemption still applies and empty orders fall through to the
        # ordinary orders-empty cause instead of CAUSE_RAW_MISSING.
        (self.run_dir / "run_state.json").write_text(
            json.dumps({"stream": "morning", "status": "blocked"}),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        report = detect_drift(self.run_dir)
        self.assertFalse(report.raw_dir_present)
        self.assertEqual(report.run_suspected_cause, CAUSE_ORDERS_EMPTY)

    # --- explicit morning/midday raw-persistence error_class ----------------
    def test_morning_raw_persistence_error_class_takes_priority(self):
        # Codex review on PR #420 ("Classify broker raw-write failures
        # correctly in drift reports"): morning/midday are excluded from the
        # raw_dir_present check below (they didn't always write raw/), but
        # _blocked_live_result() now stamps an explicit
        # error_class="missing_or_invalid_raw_inputs" onto a blocked
        # run_state when a broker-side raw write failed mid-loop -- e.g. a
        # tradability check that failed to serialize AFTER an earlier
        # candidate was already submitted, so raw/ exists and orders are
        # non-empty, yet the real cause is still the raw-persistence failure.
        #
        # Superseded by PR #538 discussion_r3809442207 ("Match drift recovery
        # guidance to partial submissions"): this exact partial-submission
        # scenario (AAPL already submitted, NVDA's raw write failed) now maps
        # to the distinct CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION cause,
        # whose guidance tells operators to reconcile the existing submission
        # first rather than blindly re-run -- CAUSE_RAW_MISSING's "raw/ is
        # missing... re-run the live task" text was factually wrong here,
        # since raw/ genuinely exists and AAPL already has a real order.
        self._raw()
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "error_class": "missing_or_invalid_raw_inputs",
                    "raw_persistence_error": "failed to persist tradability_NVDA_2.json: [Errno 17] File exists",
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL")])
        report = detect_drift(self.run_dir)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION)
        self.assertIn("reconcile", report.recommended_action)

    def test_morning_partial_submission_detected_from_buy_candidates_without_orders_csv(self):
        # Codex review, PR #538 ("Consult run-state submissions before
        # recommending a rerun"): when submitted_orders.csv is missing/empty/
        # damaged, submitted_symbols previously only looked at broker_outcomes
        # (and only the exact "submitted" string) -- an AAPL row that reached
        # the broker but is recorded ONLY in run_state["buy_candidates"] (e.g.
        # with status "filled") was invisible, so this fell through to
        # CAUSE_RAW_MISSING's "re-run the live task" guidance instead of the
        # reconcile-first CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION guidance
        # -- risking a duplicate resubmission of an order already accepted.
        self._raw()
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "error_class": "missing_or_invalid_raw_inputs",
                    "raw_persistence_error": "failed to persist tradability_NVDA_2.json: [Errno 17] File exists",
                    "buy_candidates": [
                        {"ticker": "AAPL", "status": "filled", "order_id": "o1"},
                        {"ticker": "NVDA", "status": "not_submitted"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])  # missing/empty
        report = detect_drift(self.run_dir)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION)
        self.assertIn("reconcile", report.recommended_action)

    def test_raw_missing_action_describes_run_state_only_evidence_accurately(self):
        # Codex review discussion_r3840378334 ("Describe run-state-only
        # submission evidence accurately"): CAUSE_RAW_MISSING's partial-
        # submission guidance used to hard-code "submitted_orders.csv is
        # non-empty" whenever has_submitted_order_evidence was true --
        # factually wrong when the evidence comes ONLY from run_state.json's
        # buy_candidates/broker_outcomes and submitted_orders.csv does not
        # exist on disk at all (art.orders_path is None), sending on-call to
        # inspect a file that was never written.
        #
        # This scenario reaches CAUSE_RAW_MISSING as the REPORT-level cause
        # (not the AFTER_PARTIAL_SUBMISSION variant above, and not the purely
        # per-symbol CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED cause)
        # via the current-schema raw-dir-missing check: raw_input_dir is
        # stamped (so the legacy morning/midday exemption does not apply) but
        # the raw/ directory itself was never created on disk, and neither
        # error_class nor raw_persistence_error is set -- so _infer_run_cause
        # reaches the raw_dir_present check directly rather than the earlier
        # submitted-symbols branch. NVDA is included, still unsubmitted and
        # with no run-state evidence of its own, so the report-level cause
        # falls through to run_cause (CAUSE_RAW_MISSING) instead of every
        # affected symbol sharing the run-state-confirmed cause -- AAPL still
        # supplies the run-state-only submitted evidence this action text
        # must describe accurately.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "raw_input_dir": str(self.run_dir / "raw"),
                    "raw_inputs_saved": ["quote_AAPL_1.json"],
                    "buy_candidates": [{"ticker": "AAPL", "status": "filled", "order_id": "o1"}],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        # submitted_orders.csv is intentionally never written at all -- the
        # only submission evidence here is run_state.json's buy_candidates.
        report = detect_drift(self.run_dir)
        self.assertFalse(report.raw_dir_present)
        self.assertFalse(report.orders_present)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING)
        self.assertNotIn("submitted_orders.csv is non-empty", report.recommended_action)
        self.assertIn("submitted_orders.csv is missing", report.recommended_action)
        self.assertIn("run_state.json records a submitted order", report.recommended_action)

    def test_raw_missing_action_stays_neutral_about_why_raw_is_absent(self):
        # Codex review, PR #593 ("Keep missing-raw guidance neutral"): the
        # CAUSE_RAW_MISSING-with-submission-evidence action text asserted a
        # sequence it cannot know -- "an earlier candidate was already
        # submitted before a LATER candidate's raw write failed" -- and sent
        # on-call to investigate why that write failed. An EXPLICIT
        # persistence failure carrying submission evidence has its own cause,
        # CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION, so the only runs that
        # reach this branch are COMPLETED runs whose raw/ archive is simply
        # gone. The guidance must point at deletion/corruption of the archive
        # instead of a candidate failure that never happened.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "completed",
                    "raw_input_dir": str(self.run_dir / "raw"),
                    "raw_inputs_saved": ["quote_AAPL_1.json"],
                    # No error_class, no raw_persistence_error: nothing failed
                    # to write. The raw/ directory below is simply absent.
                    "buy_candidates": [{"ticker": "AAPL", "status": "filled", "order_id": "o1"}],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        report = detect_drift(self.run_dir)

        self.assertFalse(report.raw_dir_present)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING)
        action = report.recommended_action
        # The unprovable sequence is gone...
        self.assertNotIn("raw write failed", action)
        self.assertNotIn("LATER candidate", action)
        self.assertNotIn("already submitted before", action)
        # ...replaced by what is actually known, plus a pointer at the cause
        # that DOES describe a mid-run persistence failure.
        self.assertIn("deletion", action)
        self.assertIn(CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION, action)
        # And the do-not-resubmit protection this branch exists for survives.
        self.assertIn("Do NOT re-run", action)

    def test_raw_missing_after_partial_submission_keeps_its_own_narrative(self):
        # Companion guard for the fix above: the partial-submission narrative
        # is not deleted, only relocated to the cause that can actually prove
        # it. An explicit raw-persistence failure WITH an already-submitted
        # order still selects CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION and
        # still tells on-call that persistence failed after a real order.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "error_class": "missing_or_invalid_raw_inputs",
                    "raw_persistence_error": "disk full while writing quote_NVDA_2.json",
                    "raw_input_dir": str(self.run_dir / "raw"),
                    "buy_candidates": [{"ticker": "AAPL", "status": "submitted", "order_id": "o1"}],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        report = detect_drift(self.run_dir)

        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING_AFTER_PARTIAL_SUBMISSION)
        self.assertIn("Raw-input persistence failed AFTER", report.recommended_action)

    def test_raw_missing_action_does_not_claim_csv_evidence_for_unsubmitted_rows(self):
        # Codex review, PR #593 discussion_r3840429735 ("Report the actual
        # submission-evidence source"): branching the action text on
        # `art.orders_path is not None` only proved SOME order CSV exists, not
        # that it holds submission evidence. A submitted_orders.csv whose only
        # row is "not_submitted_confirmation_required" (that order never
        # reached the broker -- csv_submitted_symbols filters it out) still
        # made the guidance claim the CSV was non-empty submission evidence,
        # sending on-call to a file that proves nothing while the real
        # evidence sits in run_state.json.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "raw_input_dir": str(self.run_dir / "raw"),
                    "raw_inputs_saved": ["quote_AAPL_1.json"],
                    "buy_candidates": [{"ticker": "AAPL", "status": "filled", "order_id": "o1"}],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        # The CSV exists but carries no ACTUALLY-submitted row.
        _write_csv(
            self.run_dir / "submitted_orders.csv",
            ORDER_FIELDS,
            [_order("NVDA", status="not_submitted_confirmation_required")],
        )
        report = detect_drift(self.run_dir)
        self.assertTrue(report.orders_present)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING)
        self.assertNotIn("submitted_orders.csv is non-empty", report.recommended_action)
        self.assertIn("submitted_orders.csv records no submitted order", report.recommended_action)
        self.assertIn("run_state.json does", report.recommended_action)

    def test_raw_missing_action_names_the_fallback_order_csv_actually_used(self):
        # Same Codex review finding, second case: when submitted_orders.csv is
        # absent, _first_existing() falls back to orders.csv/filled_orders.csv
        # -- naming submitted_orders.csv in the guidance then points on-call at
        # a file that does not exist. The message must name the record that
        # actually holds the evidence.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "raw_input_dir": str(self.run_dir / "raw"),
                    "raw_inputs_saved": ["quote_AAPL_1.json"],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("NVDA", incremental=3.0)],
        )
        # submitted_orders.csv is intentionally absent; the fallback record is
        # what carries AAPL's real submission.
        _write_csv(self.run_dir / "orders.csv", ORDER_FIELDS, [_order("AAPL")])
        report = detect_drift(self.run_dir)
        self.assertEqual(report.run_suspected_cause, CAUSE_RAW_MISSING)
        self.assertIn("orders.csv records a submitted order", report.recommended_action)
        self.assertNotIn("submitted_orders.csv is non-empty", report.recommended_action)
        self.assertNotIn("submitted_orders.csv records", report.recommended_action)
        self.assertNotIn("submitted_orders.csv is missing, but", report.recommended_action)

    def test_run_state_confirmed_order_still_surfaces_as_drift_when_csv_missing(self):
        # Codex review, PR #538 ("Keep missing order artifacts visible after
        # reconciliation"): the fix above (letting run_state["buy_candidates"]
        # count as submitted evidence) must NOT make the affected symbol
        # reconcile away entirely -- submitted_orders.csv is still genuinely
        # missing/empty for AAPL, which is itself a real drift condition an
        # operator must reconcile (regenerate the CSV artifact), even though
        # run_state.json proves the order reached the broker and must never
        # be resubmitted.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "buy_candidates": [{"ticker": "AAPL", "status": "filled", "order_id": "o1"}],
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])  # missing/empty
        report = detect_drift(self.run_dir)
        self.assertTrue(report.drift_detected)
        self.assertEqual(len(report.affected_symbols), 1)
        affected = report.affected_symbols[0]
        self.assertEqual(affected.symbol, "AAPL")
        self.assertEqual(affected.suspected_cause, CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertEqual(report.run_suspected_cause, CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertIn("reconcile", report.recommended_action.lower())
        self.assertIn("do not resubmit", report.recommended_action.lower())

    def test_settlement_adjustment_row_excluded_from_run_state_only_submitted_symbols(self):
        # Codex review ("Exclude adjustment rows from drift detection too"):
        # a whole_share_settlement_adjustment row is a bookkeeping-only
        # dollar correction for an order ALREADY recorded -- near_close_dca's
        # own submitted_orders.csv writer deliberately excludes this same
        # marker, so it is genuinely absent from that CSV by design.
        # run_state_only_submitted_symbols must not treat that absence as
        # "confirmed only by run_state, not any CSV" (which would fire
        # CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED and tell an
        # operator not to resubmit an order that was never actually placed
        # by this row).
        from scripts.drift_detection import load_run_artifacts

        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "buy_candidates": [
                        {
                            "ticker": "BYDDY",
                            "status": "submitted",
                            "order_id": "RH-ADJ-1",
                            "whole_share_settlement_adjustment": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        art = load_run_artifacts(self.run_dir)
        self.assertNotIn("BYDDY", art.run_state_only_submitted_symbols)

    def test_blank_outcome_ticker_falls_back_to_the_mapping_key(self):
        # Codex review, PR #593 ("Fall back from blank outcome tickers"): the
        # composite-key fallback used dict.get("ticker", key), which only
        # reaches the key when the field is MISSING. An outcome explicitly
        # carrying "ticker": null resolved to the literal "NONE" (and "" to
        # nothing at all), so run_state evidence recorded under the key
        # "AAPL" stopped reconciling with the "AAPL" signal row and a real
        # submitted order was reported as missing. The canonical writer,
        # scripts.scheduled_artifacts.submitted_order_rows(), resolves the
        # very same shape with a TRUTHY fallback -- outcome.get("ticker") or
        # key -- so anything else guarantees the two disagree.
        from scripts.drift_detection import load_run_artifacts

        for blank in (None, ""):
            with self.subTest(blank=blank):
                (self.run_dir / "run_state.json").write_text(
                    json.dumps(
                        {
                            "stream": "morning",
                            "status": "blocked",
                            "broker_outcomes": {
                                "AAPL": {"ticker": blank, "status": "submitted", "order_id": "o1"},
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                art = load_run_artifacts(self.run_dir)
                self.assertEqual(art.run_state_only_submitted_symbols, {"AAPL"})
                self.assertNotIn("NONE", art.run_state_only_submitted_symbols)

                _write_csv(
                    self.run_dir / "signals.csv",
                    SIGNAL_FIELDS,
                    [_signal("AAPL", incremental=3.0)],
                )
                # The canonical order record really is missing, so this still
                # surfaces as drift -- but as the reconcile-only, never-
                # resubmit cause, because run_state proves AAPL reached the
                # broker.
                _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
                report = detect_drift(self.run_dir)
                self.assertTrue(report.drift_detected)
                self.assertEqual(len(report.affected_symbols), 1)
                self.assertEqual(report.affected_symbols[0].symbol, "AAPL")
                self.assertEqual(
                    report.affected_symbols[0].suspected_cause,
                    CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED,
                )

    def test_broker_placement_failure_takes_priority_over_raw_persistence(self):
        # Codex review on PR #538 ("Prioritize broker failures over
        # raw-persistence metadata"): _PersistingBrokerAdapter persists a
        # broker connector exception (e.g. an ambiguous timeout after the
        # broker may have already accepted the order) onto raw_recorder.error
        # -- the SAME failure that _place_broker_orders() already recorded as
        # a "failed" broker_outcomes entry with real error detail. Blindly
        # classifying this as CAUSE_RAW_MISSING/CAUSE_RAW_MISSING_AFTER_
        # PARTIAL_SUBMISSION told operators to just re-run, risking a
        # duplicate resubmission of an order the broker may have accepted.
        self._raw()
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "status": "blocked",
                    "error_class": "missing_or_invalid_raw_inputs",
                    "raw_persistence_error": "ambiguous timeout placing order for NVDA",
                    "broker_outcomes": {
                        "NVDA": {
                            "status": "failed",
                            "skip_reason": "ambiguous timeout placing order for NVDA",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("NVDA", incremental=3.0)],
        )
        report = detect_drift(self.run_dir)
        self.assertEqual(report.run_suspected_cause, CAUSE_BROKER_PLACEMENT_AMBIGUOUS)
        self.assertIn("reconcil", report.recommended_action.lower())
        self.assertIn("broker's own order history", report.recommended_action)

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

    def test_state_only_submission_makes_mixed_empty_orders_reconcile_first(self):
        # A run-state-confirmed order beside a genuinely missing one: the
        # report-level cause must not surface CAUSE_ORDERS_EMPTY's "re-run the
        # live task" guidance, which would duplicate the order run_state
        # already records (Codex review: "Fix mixed drift retry guidance").
        self._raw()
        _write_csv(
            self.run_dir / "signals.csv",
            SIGNAL_FIELDS,
            [_signal("AAPL", incremental=3.0), _signal("MSFT", incremental=4.0)],
        )
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        (self.run_dir / "run_state.json").write_text(
            json.dumps({"broker_outcomes": {"AAPL": {"status": "submitted"}}}),
            encoding="utf-8",
        )

        report = detect_drift(self.run_dir)

        causes = {drift.symbol: drift.suspected_cause for drift in report.affected_symbols}
        self.assertEqual(causes["AAPL"], CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertEqual(causes["MSFT"], CAUSE_ORDERS_EMPTY)
        self.assertEqual(report.run_suspected_cause, CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertIn("Reconcile broker order history for every affected symbol", report.recommended_action)
        self.assertIn("do not re-run the live task first", report.recommended_action)

    def test_every_placed_outcome_status_counts_as_submission_evidence(self):
        # A strict status == "submitted" test omitted a real filled/pending/
        # unconfirmed order from the evidence sets, so with an empty canonical
        # CSV detect_drift() called it genuinely missing and recommended
        # re-running the live task -- duplicating an order that already exists
        # or is still in flight (Codex review: "Treat all placed outcomes as
        # submission evidence").
        self._raw()
        for status in ("submitted", "filled", "pending", "unconfirmed"):
            with self.subTest(status=status):
                _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
                _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
                (self.run_dir / "run_state.json").write_text(
                    json.dumps({"broker_outcomes": {"AAPL": {"status": status}}}), encoding="utf-8"
                )
                report = detect_drift(self.run_dir)
                causes = {d.symbol: d.suspected_cause for d in report.affected_symbols}
                self.assertEqual(causes.get("AAPL"), CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
                self.assertIn("Do NOT resubmit", report.recommended_action)

        # An order that never reached the broker is still NOT evidence.
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {"broker_outcomes": {"AAPL": {"status": "not_submitted_confirmation_required"}}}
            ),
            encoding="utf-8",
        )
        report = detect_drift(self.run_dir)
        causes = {d.symbol: d.suspected_cause for d in report.affected_symbols}
        self.assertEqual(causes.get("AAPL"), CAUSE_ORDERS_EMPTY)

    def test_a_completely_missing_order_csv_is_reported_for_a_current_artifact(self):
        # "run_state records a placed order and no order CSV exists at all" is
        # the most severe form of the artifact disagreement, not an exemption:
        # every live stream writes submitted_orders.csv today, so a missing one
        # must never declare the run reconciled (Codex review: "Report a
        # completely missing canonical order CSV").
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        # No order CSV written at all.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "stream": "morning",
                    "raw_input_dir": str(self.run_dir / "raw"),  # the current contract
                    "broker_outcomes": {"AAPL": {"status": "submitted", "order_id": "O1"}},
                }
            ),
            encoding="utf-8",
        )

        report = detect_drift(self.run_dir)

        causes = {d.symbol: d.suspected_cause for d in report.affected_symbols}
        self.assertEqual(causes.get("AAPL"), CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertIn("Do NOT resubmit", report.recommended_action)

    def test_a_current_failure_artifact_without_raw_fields_is_still_reported(self):
        # MorningDCAExecutor's failure writer synthesizes a complete current
        # run_state -- including broker_outcomes for orders it had ALREADY
        # placed -- without either raw field, because the run died before raw
        # persistence was summarized. That artifact (order placed, run failed,
        # companion CSVs then discarded as stale) is exactly the one whose
        # state-only submission must surface, raw fields or not (Codex review:
        # "Recognize current failure artifacts without raw fields").
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        # No order CSV, and no raw_input_dir/raw_persistence_error -- exactly
        # what _write_failure_artifacts produces.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "run_classification": {
                        "run_origin": "scheduled",
                        "stream": "morning",
                        "run_id": "morning_live_20260629_1030ET",
                    },
                    "run_classification_outcome": "failed_after_submission",
                    "orders_submitted_count": 1,
                    "placement_evidence_incomplete": True,
                    "broker_outcomes": {"AAPL": {"status": "submitted", "order_id": "O1"}},
                }
            ),
            encoding="utf-8",
        )

        report = detect_drift(self.run_dir)

        causes = {d.symbol: d.suspected_cause for d in report.affected_symbols}
        self.assertEqual(causes.get("AAPL"), CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertIn("Do NOT resubmit", report.recommended_action)

    def test_a_state_only_order_is_reported_even_with_no_signal_rows(self):
        # detect_drift's loop walks `expected`, which is built entirely from
        # signals.csv -- so a run with NO signal rows never examined the
        # state-only set at all. _write_failure_artifacts synthesizes exactly
        # that shape (signal_rows: [] alongside the broker_outcomes for orders
        # already placed), which defeated the state-only reporting in precisely
        # the case it exists for (Codex review: "Report state-only orders
        # without signal rows").
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [])  # header only
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "status": "failed",
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "signal_rows": [],
                    "broker_outcomes": {"AAPL": {"status": "submitted", "order_id": "O1"}},
                }
            ),
            encoding="utf-8",
        )

        report = detect_drift(self.run_dir)

        self.assertTrue(report.drift_detected)
        causes = {d.symbol: d.suspected_cause for d in report.affected_symbols}
        self.assertEqual(causes.get("AAPL"), CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertIn("Do NOT resubmit", report.recommended_action)

    def test_a_state_only_order_is_not_reported_twice(self):
        # When a signal row DOES exist for the symbol, it is reported once
        # through the expected loop, not again by the signal-independent sweep.
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "broker_outcomes": {"AAPL": {"status": "submitted", "order_id": "O1"}},
                }
            ),
            encoding="utf-8",
        )

        report = detect_drift(self.run_dir)

        self.assertEqual([d.symbol for d in report.affected_symbols], ["AAPL"])

    def test_a_second_order_for_one_ticker_needs_its_own_canonical_row(self):
        # A retry can leave TWO distinct physical orders for one ticker in
        # run_state (near_close_dca keys the second "AAPL:<order-id>" precisely
        # so both survive the merge). Comparing symbol SETS called the ticker
        # reconciled as soon as the CSV carried any row for it, so the second
        # order's missing canonical record went unreported (Codex review:
        # "Compare state and CSV orders by order ID").
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL", order_id="O1")])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "broker_outcomes": {
                        "AAPL": {"ticker": "AAPL", "status": "submitted", "order_id": "O1"},
                        "AAPL:O2": {"ticker": "AAPL", "status": "filled", "order_id": "O2"},
                    },
                }
            ),
            encoding="utf-8",
        )

        artifacts = load_run_artifacts(self.run_dir)

        self.assertEqual(artifacts.run_state_only_submitted_symbols, {"AAPL"})
        # The capital is real either way: the symbol still counts as submitted,
        # so the guidance is reconcile-and-rebuild, never resubmit.
        self.assertEqual(artifacts.submitted_symbols, {"AAPL"})
        report = detect_drift(self.run_dir)
        causes = {d.symbol: d.suspected_cause for d in report.affected_symbols}
        self.assertEqual(causes.get("AAPL"), CAUSE_ORDER_ARTIFACT_MISSING_RUN_STATE_CONFIRMED)
        self.assertIn("Do NOT resubmit", report.recommended_action)

    def test_matching_order_ids_reconcile_and_conflicting_ones_do_not(self):
        # The per-order comparison must not fire on a healthy artifact set, and
        # must still catch a CSV row that names a DIFFERENT order than the one
        # run_state records -- a disagreement equal counts alone would miss.
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        state = {
            "run_classification": {"run_origin": "scheduled", "stream": "morning"},
            "broker_outcomes": {"AAPL": {"ticker": "AAPL", "status": "submitted", "order_id": "O1"}},
        }

        for csv_order_id, expected in (("O1", set()), ("O9", {"AAPL"})):
            with self.subTest(csv_order_id=csv_order_id):
                _write_csv(
                    self.run_dir / "submitted_orders.csv",
                    ORDER_FIELDS,
                    [_order("AAPL", order_id=csv_order_id)],
                )
                (self.run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
                artifacts = load_run_artifacts(self.run_dir)
                self.assertEqual(artifacts.run_state_only_submitted_symbols, expected)

    def test_a_repeated_order_id_is_not_mistaken_for_a_blank_id_row(self):
        # Deriving the blank-ID count as total - len(ids) turned a damaged CSV
        # carrying the same order twice into "one identified row plus one
        # unidentified row". That phantom spare row then covered a genuinely
        # missing order: state {O1, O2} against a CSV of {O1, O1} reconciled
        # while O2's canonical record was absent (Codex review: "Do not count
        # duplicate IDs as unidentified orders").
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(
            self.run_dir / "submitted_orders.csv",
            ORDER_FIELDS,
            [_order("AAPL", order_id="O1"), _order("AAPL", order_id="O1")],
        )
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "broker_outcomes": {
                        "AAPL": {"ticker": "AAPL", "status": "submitted", "order_id": "O1"},
                        "AAPL:O2": {"ticker": "AAPL", "status": "filled", "order_id": "O2"},
                    },
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(load_run_artifacts(self.run_dir).run_state_only_submitted_symbols, {"AAPL"})

    def test_an_order_row_without_an_id_still_accounts_for_a_state_order(self):
        # The canonical writer copies order_id through, but an id-less row is a
        # DAMAGED record rather than proof of some other order. Treating the
        # mismatch as a missing record would report drift for every legacy row,
        # so a spare id-less row covers one otherwise-unmatched state order --
        # and stops covering once the state outnumbers the CSV.
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL", order_id="")])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "broker_outcomes": {"AAPL": {"ticker": "AAPL", "status": "submitted", "order_id": "O1"}},
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(load_run_artifacts(self.run_dir).run_state_only_submitted_symbols, set())

        # A second state order has nothing left to cover it.
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "broker_outcomes": {
                        "AAPL": {"ticker": "AAPL", "status": "submitted", "order_id": "O1"},
                        "AAPL:O2": {"ticker": "AAPL", "status": "filled", "order_id": "O2"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(load_run_artifacts(self.run_dir).run_state_only_submitted_symbols, {"AAPL"})

    def test_a_placed_candidate_row_does_not_double_count_its_own_outcome(self):
        # The afternoon stream records one order on BOTH sides of run_state
        # (buy_candidates and broker_outcomes), and the candidate row need not
        # repeat the order id. Tallying it as a second, unidentified order
        # would report a missing canonical record for a CSV that carries the
        # order exactly once.
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL", order_id="O1")])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "afternoon"},
                    "buy_candidates": [{"ticker": "AAPL", "status": "submitted"}],
                    "broker_outcomes": {"AAPL": {"ticker": "AAPL", "status": "submitted", "order_id": "O1"}},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(load_run_artifacts(self.run_dir).run_state_only_submitted_symbols, set())
        self.assertFalse(detect_drift(self.run_dir).drift_detected)

    def test_a_collision_suffixed_outcome_key_resolves_to_its_real_ticker(self):
        # A retried artifact can key a second order for one ticker as
        # "AAPL:<order-id>". That key is an identity, not a symbol, so the
        # evidence set must read the outcome's own ticker (and strip the
        # suffix when it has none) or the symbol looks unreconciled.
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "broker_outcomes": {
                        "AAPL:ORDER-EARLIER": {"status": "submitted", "order_id": "ORDER-EARLIER"}
                    }
                }
            ),
            encoding="utf-8",
        )

        artifacts = load_run_artifacts(self.run_dir)

        self.assertEqual(artifacts.state_submitted_symbols, {"AAPL"})

    def test_a_lease_recovery_outcome_key_resolves_to_its_real_ticker(self):
        # near_close_dca keys a recovered inverse-martingale lease
        # "AAPL#imart-recovery". Today's producer puts the real ticker in the
        # outcome, so the key is only read for an artifact that omitted it --
        # where "#" must be stripped exactly as ":" is, or the symbol looks
        # unreconciled (Antigravity review: "Robust ticker normalization for
        # lease recovery keys in drift detection").
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {"broker_outcomes": {"AAPL#imart-recovery": {"status": "submitted", "order_id": "O1"}}}
            ),
            encoding="utf-8",
        )

        self.assertEqual(load_run_artifacts(self.run_dir).state_submitted_symbols, {"AAPL"})

    def test_a_suffixed_ticker_matches_between_run_state_and_the_order_csv(self):
        # submitted_order_rows() writes the ticker column with the SAME
        # "outcome.get('ticker') or key" fallback, so a legacy artifact can
        # carry the synthetic suffix on both sides. Normalizing only run_state
        # would turn that matched pair into a false missing-record report; both
        # sides get the same treatment.
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(
            self.run_dir / "submitted_orders.csv",
            ORDER_FIELDS,
            [_order("AAPL#imart-recovery", order_id="O1")],
        )
        (self.run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_classification": {"run_origin": "scheduled", "stream": "morning"},
                    "broker_outcomes": {
                        "AAPL#imart-recovery": {"status": "submitted", "order_id": "O1"}
                    },
                }
            ),
            encoding="utf-8",
        )

        artifacts = load_run_artifacts(self.run_dir)

        self.assertEqual(artifacts.csv_submitted_symbols, {"AAPL"})
        self.assertEqual(artifacts.state_submitted_symbols, {"AAPL"})
        self.assertEqual(artifacts.run_state_only_submitted_symbols, set())

    def test_invalid_utf8_run_state_is_ignored(self):
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        (self.run_dir / "run_state.json").write_bytes(b"\xff\xfe\x00")

        artifacts = load_run_artifacts(self.run_dir)

        self.assertIsNone(artifacts.run_state)
        self.assertEqual(artifacts.signal_rows[0]["ticker"], "AAPL")

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
        self.assertTrue(json.loads(json_out.read_bytes())["drift_detected"])

    def test_cli_clean_run_exit_zero(self):
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [_order("AAPL")])
        code = main(["--run-dir", str(self.run_dir), "--fail-on-drift"])
        self.assertEqual(code, 0)

    def test_cli_missing_run_dir_errors(self):
        code = main(["--run-dir", str(self.run_dir / "does-not-exist")])
        self.assertEqual(code, 1)

    def test_cli_slack_without_webhook_warns_and_still_exits_zero(self):
        """--slack with no webhook configured is a warning, not a hard failure."""
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        code = main(
            [
                "--run-dir",
                str(self.run_dir),
                "--slack",
                "--slack-webhook-env",
                "DEFINITELY_UNSET_DRIFT_HOOK_VAR",
            ]
        )
        self.assertEqual(code, 0)

    def test_cli_slack_send_failure_is_a_warning_not_a_crash(self):
        """A Slack transport failure must not take down the CLI or mask the drift exit code."""
        self._raw()
        _write_csv(self.run_dir / "signals.csv", SIGNAL_FIELDS, [_signal("AAPL", incremental=3.0)])
        _write_csv(self.run_dir / "submitted_orders.csv", ORDER_FIELDS, [])
        with patch.object(
            drift_detection, "send_slack_alert", side_effect=urllib.error.URLError("boom")
        ):
            code = main(
                [
                    "--run-dir",
                    str(self.run_dir),
                    "--slack",
                    "--slack-webhook",
                    "https://hooks.example/alert",
                    "--fail-on-drift",
                ]
            )
        self.assertEqual(code, 2)  # --fail-on-drift still governs the exit code

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
        # drift_detection now special-cases webhook destinations routed
        # through a configured forward proxy (see _proxy_configured_for) so
        # proxy-only deployments don't need local DNS resolution to
        # succeed. These tests must exercise the direct (non-proxied) path
        # regardless of whatever proxy happens to be configured in the
        # environment the suite itself runs in.
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
            old_value = os.environ.pop(name, None)
            if old_value is not None:
                self.addCleanup(os.environ.__setitem__, name, old_value)

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

    def test_send_slack_blocks_private_destination(self):
        report = detect_drift(self.run_dir)
        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("127.0.0.1")}
        ):
            with self.assertRaises(urllib.error.URLError):
                send_slack_alert(report, webhook="https://hooks.example/alert")

    def test_send_slack_blocks_cloud_metadata_destination(self):
        report = detect_drift(self.run_dir)
        with patch.object(
            dca_net_utils,
            "resolve_ips",
            lambda hostname: {ipaddress.ip_address("169.254.169.254")},
        ):
            with self.assertRaises(urllib.error.URLError):
                send_slack_alert(report, webhook="https://hooks.example/alert")

    def test_send_slack_allows_public_destination(self):
        report = detect_drift(self.run_dir)
        calls = []

        class _FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(self, request, data=None, timeout=None):
            calls.append(request)
            return _FakeResponse()

        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("93.184.216.34")}
        ), patch.object(urllib.request.OpenerDirector, "open", fake_urlopen):
            self.assertTrue(send_slack_alert(report, webhook="https://hooks.example/alert"))
        self.assertEqual(len(calls), 1)


    def test_send_slack_blocks_file_scheme(self):
        report = detect_drift(self.run_dir)
        with self.assertRaises(urllib.error.URLError) as ctx:
            send_slack_alert(report, webhook="file:///etc/passwd")
        self.assertIn("file", str(ctx.exception).lower())

    def test_send_slack_blocks_ftp_scheme(self):
        report = detect_drift(self.run_dir)
        with self.assertRaises(urllib.error.URLError) as ctx:
            send_slack_alert(report, webhook="ftp://example.com/file")
        self.assertIn("ftp", str(ctx.exception).lower())

    def test_safe_redirect_handler_blocks_private_redirect_target(self):
        handler = dca_net_utils.SafeRedirectHandler()
        req = urllib.request.Request("https://hooks.example/start")
        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("127.0.0.1")}
        ):
            with self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(req, None, 302, "Found", {}, "https://internal.example/secret")

    def test_send_slack_blocks_shared_address_space_destination(self):
        """100.64.0.0/10 (carrier-grade NAT) is neither private nor reserved
        but is not publicly routable either -- is_private/is_reserved alone
        let it through.
        """
        report = detect_drift(self.run_dir)
        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("100.64.0.1")}
        ):
            with self.assertRaises(urllib.error.URLError):
                send_slack_alert(report, webhook="https://hooks.example/alert")

    def test_send_slack_blocks_deprecated_site_local_destination(self):
        report = detect_drift(self.run_dir)
        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("fec0::1")}
        ):
            with self.assertRaises(urllib.error.URLError):
                send_slack_alert(report, webhook="https://hooks.example/alert")

    def test_send_slack_rejects_malformed_port(self):
        report = detect_drift(self.run_dir)
        with self.assertRaises(urllib.error.URLError):
            send_slack_alert(report, webhook="https://hooks.example:notaport/alert")

    def test_send_slack_skips_local_resolution_behind_a_proxy(self):
        """A proxy-only deployment's webhook host may intentionally not
        resolve on this host (the proxy resolves it); a genuine local
        resolution FAILURE for that hostname must not block the request
        when a proxy is configured (codex review discussion_r3679190507).
        """
        report = detect_drift(self.run_dir)
        calls = []

        class _FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(self, request, data=None, timeout=None):
            calls.append(request)
            return _FakeResponse()

        def boom(hostname):
            raise OSError("name resolution failed")

        os.environ["https_proxy"] = "http://proxy.internal:3128"
        self.addCleanup(os.environ.pop, "https_proxy", None)
        with patch.object(dca_net_utils, "resolve_ips", boom), patch.object(
            urllib.request.OpenerDirector, "open", fake_urlopen
        ):
            self.assertTrue(send_slack_alert(report, webhook="https://internal-only.example/alert"))
        self.assertEqual(len(calls), 1)

    def test_send_slack_still_blocks_literal_disallowed_ip_behind_a_proxy(self):
        """A literal IP host (e.g. the cloud-metadata address) needs no DNS
        resolution at all, so the proxy-bypass branch above must not skip
        validating it -- an attacker-influenced webhook naming
        169.254.169.254 directly must still be rejected even when a proxy is
        configured (codex review on this PR).
        """
        report = detect_drift(self.run_dir)
        os.environ["https_proxy"] = "http://proxy.internal:3128"
        self.addCleanup(os.environ.pop, "https_proxy", None)
        with self.assertRaises(urllib.error.URLError):
            send_slack_alert(report, webhook="https://169.254.169.254/latest/meta-data/")

    def test_send_slack_still_blocks_resolvable_disallowed_hostname_behind_a_proxy(self):
        """A hostname (not a literal IP) that DOES resolve locally to a
        disallowed address must still be rejected even when a proxy is
        configured for the scheme -- unconditionally skipping validation
        whenever any proxy exists would let an attacker-controlled hostname
        resolve via the proxy to a private/link-local address completely
        unchecked (codex review on this PR).
        """
        report = detect_drift(self.run_dir)
        os.environ["https_proxy"] = "http://proxy.internal:3128"
        self.addCleanup(os.environ.pop, "https_proxy", None)
        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("169.254.169.254")}
        ):
            with self.assertRaises(urllib.error.URLError):
                send_slack_alert(report, webhook="https://attacker.example/alert")

    def test_connect_pinned_connects_to_the_validated_address(self):
        """The address _connect_pinned validates must be the exact address
        it connects to -- not a hostname handed to a second, independent
        lookup that could answer differently (DNS rebinding).
        """
        validated_ip = ipaddress.ip_address("93.184.216.34")
        calls = []

        def fake_create_connection(address, timeout, source_address):
            calls.append(address)
            return object()

        class _FakeConn:
            _create_connection = staticmethod(fake_create_connection)
            host = "hooks.example"
            port = 443
            timeout = 10.0
            source_address = None
            sock: Any = None

        conn = _FakeConn()
        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {validated_ip}
        ), patch.object(dca_net_utils.socket, "create_connection", fake_create_connection):
            dca_net_utils.connect_pinned(cast(Any, conn))
        self.assertEqual(calls, [(str(validated_ip), 443)])
        self.assertIsNotNone(conn.sock)

    def test_pinned_https_handler_constructs_connection_without_crashing(self):
        """https_open() must be able to construct its connection object
        without raising. Every other test here mocks urllib.request.urlopen
        directly, which bypasses this handler entirely and let a Python-
        version-specific bug ship undetected: on 3.12+, HTTPSHandler no
        longer sets self._check_hostname at all, and
        http.client.HTTPSConnection.__init__ no longer accepts a
        check_hostname argument either, so forwarding it here raised
        AttributeError/TypeError on every real HTTPS request (Codex review
        on this PR).
        """

        class _ReachedConnection(Exception):
            pass

        def fake_request(self, *a, **kw):
            raise _ReachedConnection("construction succeeded")

        handler = dca_net_utils.PinnedHTTPSHandler()
        req = urllib.request.Request("https://hooks.example/alert")
        req.timeout = 10.0  # normally set by OpenerDirector.open() before dispatch
        with patch.object(dca_net_utils.PinnedHTTPSConnection, "request", fake_request):
            with self.assertRaises(_ReachedConnection):
                handler.https_open(req)

    def test_pinned_https_handler_proxied_branch_constructs_connection_without_crashing(self):
        class _ReachedConnection(Exception):
            pass

        def fake_request(self, *a, **kw):
            raise _ReachedConnection("construction succeeded")

        handler = dca_net_utils.PinnedHTTPSHandler()
        req = urllib.request.Request("https://hooks.example/alert")
        req.timeout = 10.0  # normally set by OpenerDirector.open() before dispatch
        cast(Any, req)._tunnel_host = "hooks.example"
        with patch.object(http.client.HTTPSConnection, "request", fake_request):
            with self.assertRaises(_ReachedConnection):
                handler.https_open(req)

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


class LoadRunArtifactsSignalSelectionTest(unittest.TestCase):
    """The detector must read the signals file the artifact contract validated.

    The contract accepts any recognized name with a usable header, so a reused
    run directory can hold a stale signals.csv beside a valid stream-specific
    file. Picking the first file that merely EXISTS would read the malformed one,
    see no symbols, and report no expected orders while the contract advertises
    the run as valid (Codex review).
    """

    def test_prefers_a_usable_sibling_over_a_malformed_higher_priority_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "signals.csv").write_text("", encoding="utf-8")
            (run_dir / "morning_signals.csv").write_text(
                "ticker,auto_buy_signal\nAAA,true\n", encoding="utf-8"
            )

            artifacts = load_run_artifacts(run_dir)

            self.assertIsNotNone(artifacts.signals_path)
            assert artifacts.signals_path is not None
            self.assertEqual(artifacts.signals_path.name, "morning_signals.csv")
            self.assertEqual(len(artifacts.signal_rows), 1)

    def test_prefers_a_usable_sibling_over_a_corrupt_body(self):
        """A valid header over an unreadable body is not usable. Selecting on
        the header alone picked the corrupt canonical file, then read_csv_rows()
        raised and the wrapper logged a detector failure with no drift
        reconciled (Codex review)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "signals.csv").write_text(
                "ticker,auto_buy_signal\nAAA," + "x" * (csv.field_size_limit() + 1024) + "\n",
                encoding="utf-8",
            )
            (run_dir / "morning_signals.csv").write_text(
                "ticker,auto_buy_signal\nBBB,true\n", encoding="utf-8"
            )

            artifacts = load_run_artifacts(run_dir)

            self.assertIsNotNone(artifacts.signals_path)
            assert artifacts.signals_path is not None
            self.assertEqual(artifacts.signals_path.name, "morning_signals.csv")
            self.assertEqual(len(artifacts.signal_rows), 1)

    def test_prefers_the_canonical_name_when_it_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "signals.csv").write_text(
                "ticker,auto_buy_signal\nAAA,true\n", encoding="utf-8"
            )
            (run_dir / "morning_signals.csv").write_text(
                "ticker,auto_buy_signal\nBBB,true\n", encoding="utf-8"
            )

            artifacts = load_run_artifacts(run_dir)
            self.assertIsNotNone(artifacts.signals_path)
            assert artifacts.signals_path is not None
            self.assertEqual(artifacts.signals_path.name, "signals.csv")

    def test_falls_back_to_the_first_existing_when_none_are_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "signals.csv").write_text("", encoding="utf-8")

            artifacts = load_run_artifacts(run_dir)

            self.assertIsNotNone(artifacts.signals_path)
            assert artifacts.signals_path is not None
            self.assertEqual(artifacts.signals_path.name, "signals.csv")
            self.assertEqual(artifacts.signal_rows, [])

    def test_invalid_utf8_run_state_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "run_state.json").write_bytes(b"\xff\xfe")

            artifacts = load_run_artifacts(run_dir)

            self.assertIsNone(artifacts.run_state)

    def test_submitted_symbols_recognizes_every_in_flight_broker_outcome_status(self):
        """Codex review ("Recognize all in-flight outcomes before recommending
        retries"): morning/midday's broker_outcomes fallback used to recognize
        only status="submitted". "filled"/"pending"/"unconfirmed" are equally
        placed-or-in-flight per scripts.scheduled_artifacts.ORDER_ATTEMPT_
        STATUSES and already produce a canonical submitted_orders.csv row --
        missing them here left a genuine partial-submission incident with the
        generic "re-run" guidance instead of the partial-submission warning.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "run_state.json").write_text(
                json.dumps(
                    {
                        "broker_outcomes": {
                            "AAA": {"status": "filled"},
                            "BBB": {"status": "pending"},
                            "CCC": {"status": "unconfirmed"},
                            "DDD": {"status": "not_submitted_confirmation_required"},
                            "EEE": {"status": "blocked"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            artifacts = load_run_artifacts(run_dir)

            self.assertEqual(artifacts.submitted_symbols, {"AAA", "BBB", "CCC"})

    def test_submitted_symbols_resolves_composite_outcome_key_to_its_ticker(self):
        """Codex review ("Use the outcome ticker for fallback reconciliation"):
        a same-day retry's second order can be keyed under a composite
        "<ticker>:<order-id>" mapping key (scripts.scheduled_artifacts.
        submitted_order_rows() supports this), with the real symbol carried
        in outcome["ticker"]. Using the raw mapping key left the composite
        string unmatched against the plain-ticker signal row, producing a
        false missing-order alert despite a real submission.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "run_state.json").write_text(
                json.dumps(
                    {
                        "broker_outcomes": {
                            "AAA": {"ticker": "AAA", "status": "submitted"},
                            "AAA:retry-order-2": {"ticker": "AAA", "status": "submitted"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            artifacts = load_run_artifacts(run_dir)

            self.assertEqual(artifacts.submitted_symbols, {"AAA"})


class SanitizeExceptionTests(unittest.TestCase):
    """dca_net_utils.sanitize_exception never exposes webhook tokens or URL-derived content."""

    def test_plain_string_reason_hides_content(self):
        exc = urllib.error.URLError("connection to https://hooks.example/secret-token failed")
        rendered = dca_net_utils.sanitize_exception(exc)
        assert "secret-token" not in rendered
        assert "URLError" in rendered

    def test_oserror_reason_reports_errno_only(self):
        exc = urllib.error.URLError(OSError(111, "connection refused"))
        rendered = dca_net_utils.sanitize_exception(exc)
        assert "URLError" in rendered
        assert "111" in rendered
        assert "connection refused" not in rendered

    def test_base_exception_reason_without_errno_reports_type_only(self):
        exc = urllib.error.URLError(ValueError("unexpected scheme https://hooks.example/secret-token"))
        rendered = dca_net_utils.sanitize_exception(exc)
        assert "secret-token" not in rendered
        assert "URLError" in rendered
        assert "ValueError" in rendered

    def test_http_error_reports_status_only(self):
        exc = urllib.error.HTTPError(
            "https://hooks.example/secret-token", 503, "blocked", Message(), None
        )
        rendered = dca_net_utils.sanitize_exception(exc)
        assert "secret-token" not in rendered
        assert "503" in rendered

    def test_generic_exception_reports_type_only(self):
        exc = RuntimeError("contains https://hooks.example/secret-token in message")
        rendered = dca_net_utils.sanitize_exception(exc)
        assert "secret-token" not in rendered
        assert "RuntimeError" in rendered


if __name__ == "__main__":
    unittest.main()
