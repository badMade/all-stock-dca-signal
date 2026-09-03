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

import drift_detection  # noqa: E402
from drift_detection import (  # noqa: E402
    CAUSE_ORDERS_EMPTY,
    CAUSE_RAW_MISSING,
    CAUSE_ROW_BLOCK,
    DEFAULT_SLACK_WEBHOOK_ENV,
    build_slack_payload,
    detect_drift,
    load_run_artifacts,
    main,
    parse_amount,
    parse_bool,
    resolve_webhook,
    send_slack_alert,
)

import dca_net_utils

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

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return _FakeResponse()

        with patch.object(
            dca_net_utils, "resolve_ips", lambda hostname: {ipaddress.ip_address("93.184.216.34")}
        ), patch.object(urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(send_slack_alert(report, webhook="https://hooks.example/alert"))
        self.assertEqual(len(calls), 1)

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

        def fake_urlopen(request, timeout=None):
            calls.append(request)
            return _FakeResponse()

        def boom(hostname):
            raise OSError("name resolution failed")

        os.environ["https_proxy"] = "http://proxy.internal:3128"
        self.addCleanup(os.environ.pop, "https_proxy", None)
        with patch.object(dca_net_utils, "resolve_ips", boom), patch.object(
            urllib.request, "urlopen", fake_urlopen
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
