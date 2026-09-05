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
from typing import TYPE_CHECKING

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


@pytest.fixture(autouse=True)
def _isolate_cron_tracking_root(monkeypatch, tmp_path):
    """Point every call-time ``dca_paths.stream_root()`` consumer at a
    throwaway tree instead of the checkout's own ``cron_tracking/``.

    Same reasoning as ``_isolate_attempt_log_root`` above, one level up.
    ``DEFAULT_CRON_TRACKING_ROOT`` is a repo-relative ``cron_tracking``
    directory, so anything resolving the shared root AT CALL TIME writes into
    the working copy when the variable is unset. The scheduled-live slot lock
    (``MorningDCAExecutor._slot_lock_path``, added by Codex review, PR #593:
    "Place the slot lock outside the replaceable run directory") is exactly
    that: a per-stream, per-dated-slot lock file under ``slot_locks/``,
    resolved per call so ``$DCA_CRON_TRACKING_ROOT`` can relocate it. Without
    this isolation every test using the same stream and simulated date shares
    ONE lock file across the whole session, so a file left behind by an
    earlier test is visible to a later one asserting that no lock was taken --
    and the suite quietly litters the checkout besides.

    Autouse, and a ``tmp_path``-derived value, so the isolation is per-test.
    A test that wants a specific root still overrides it afterward (dozens
    already do), and the handful in ``tests/test_dca_paths.py`` that assert the
    UNSET behaviour ``delenv`` it explicitly. The MODULE-LEVEL constants
    resolved at import time are unaffected either way -- that asymmetry is
    documented in ``dca_paths`` and is itself under test.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))


if TYPE_CHECKING:
    from pathlib import Path

    import pandas as pd

    def make_synthetic_ohlcv(
        *,
        n: int = 260,
        seed: int = 0,
        start: str = "2021-01-04",
        drift: float = 0.0003,
        vol: float = 0.018,
        with_volume: bool = True,
    ) -> pd.DataFrame: ...

    def write_ticker_csv(history_dir: Path, ticker: str, frame: pd.DataFrame) -> Path: ...
