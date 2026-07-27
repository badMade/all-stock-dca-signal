"""Repo-wide pytest configuration.

``near_close_dca.py`` reads ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` from the
environment at import time (falling back to a clearly-invalid placeholder
when unset) instead of hardcoding the real account number in source, per the
security fix in this branch. Tests that assert on the account number
(e.g. ``test_morning_dca.py::test_run_state_path_and_classification``) need a
deterministic value to compare against, so set it here before any test module
gets collected/imported -- this keeps the fixture value out of production
source while still giving the test suite a stable, known account number to
assert on.
"""

import os

import pytest

os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "562057042")


@pytest.fixture(autouse=True)
def _isolate_attempt_log_root(monkeypatch, tmp_path):
    """Force every test's dca_alerts.write_failed_start_marker calls into a
    throwaway directory instead of the real cron_tracking tree.

    dca_alerts.DEFAULT_ATTEMPT_LOG_ROOT is a hardcoded absolute path (mirroring
    this repo's CRON_TRACKING_ROOT convention). A test that exercises
    guarded_run's failure path without explicitly overriding
    DCA_ATTEMPT_LOG_ROOT -- e.g. test_dca_alerts.py's
    test_guarded_run_failure_alerts_pings_fail_and_reraises and
    test_guarded_run_alerting_bug_does_not_mask_failure, reproduced writing a
    real midday.json marker under today's actual cron_tracking/attempt_log/
    trading-day directory -- would otherwise leave a synthetic failed-start
    marker for generate_credit_usage_proxy_report.py's _read_attempt_marker to
    pick up as if a real scheduled task had crashed (codex review
    discussion_r3658334906). Autouse so this isolation applies to every test,
    present and future, without each one remembering to set it; a test that
    wants a specific path (test_write_failed_start_marker_uses_env_root) still
    overrides it explicitly afterward.
    """
    monkeypatch.setenv("DCA_ATTEMPT_LOG_ROOT", str(tmp_path / "attempt_log"))
