"""Repo-wide pytest configuration.

The live brokerage account number is never committed. ``near_close_dca.py``
reads ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` from the environment at import time
(falling back to a clearly-invalid placeholder when unset), and
``config/*.json`` stores it as the ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}``
reference that ``dca_config.expand_config_env`` resolves at load time.

Tests need a deterministic value to assert against, so one is set here before
any test module is collected/imported.

``FIXTURE_ACCOUNT_NUMBER`` is a synthetic, obviously-fake number, and it is
assigned UNCONDITIONALLY rather than via ``setdefault``. That is deliberate and
does two things at once:

  * the suite is hermetic -- it passes identically on a developer machine whose
    shell exports a real account number, instead of failing with a confusing
    account-mismatch diff;
  * a real account number in the ambient environment can never be picked up by a
    test and written into a fixture artifact, an assertion message, or CI logs.

Any test that needs a *different* account (the account-mismatch and
unpinned-account cases) sets it explicitly on the config object it builds.
"""

import os

import pytest

# Synthetic. Nine digits so it satisfies the account_number shape pinned by
# schemas/morning_run_classification.schema.json.
FIXTURE_ACCOUNT_NUMBER = "999000111"

os.environ["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] = FIXTURE_ACCOUNT_NUMBER


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
