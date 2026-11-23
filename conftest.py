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
import sys
from typing import TYPE_CHECKING

import pytest

# Synthetic. Nine digits so it satisfies the account_number shape pinned by
# schemas/morning_run_classification.schema.json.
FIXTURE_ACCOUNT_NUMBER = "999000111"

os.environ["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] = FIXTURE_ACCOUNT_NUMBER

# dca_paths loads the operator's local .env at import (see dca_env). Switch that off
# for the whole suite, before any module is imported, so a real operator file can
# never leak a live account number or alert address into a test.
os.environ["DCA_DISABLE_DOTENV"] = "1"

# local_heartbeat start/finish publish the public slot-status signal by git push (scripts/slot_status.py). Keep every
# test local, before any module is imported, so the suite can never push to the real remote.
os.environ["DCA_DISABLE_SLOT_STATUS_PUBLISH"] = "1"

# deploy/launchd/robinhood_payload.sh exports DCA_NOTIFY_REFUSAL=1 unless the caller already set it, and the
# launcher tests in test_robinhood_payload.py run that real script against a stubbed Codex that refuses the fake
# 2026-10-20 09:35 slot at 0/122 -- so every suite run on the operator's Mac raised real "Payload refused" macOS
# notifications (2026-11-14). Pin it off for the whole suite; tests of the notifier opt back in with monkeypatch.
os.environ["DCA_NOTIFY_REFUSAL"] = "0"



def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print the fix for a test file that cannot import its module at collection.

    A test written before its module (test-first) fails collection with ModuleNotFoundError,
    and one collection error stops the WHOLE core run, not just that file (2026-11-13: #2266's
    tests/test_dca_goals.py and tests/test_dca_console_goals.py turned every PR red until
    #2287/#2288 gated them). pytest's own output names the import but not the repo's fix, so
    this adds it once per offending file. Read-only: it never changes the exit status.
    """
    for report in terminalreporter.stats.get("error", []):
        if getattr(report, "when", None) != "collect" or not getattr(report, "failed", False):
            continue
        text = getattr(report, "longreprtext", "")
        if "ModuleNotFoundError" in text or "ImportError" in text:
            terminalreporter.write_line(
                f"HINT: {report.nodeid} cannot import a module at collection. If it is test-first, gate it with "
                '`pytest.importorskip("<module>")` (see tests/test_dca_goals.py) so the rest of the suite still runs.',
                yellow=True,
                bold=True,
            )

@pytest.fixture(autouse=True)
def _isolate_ticker_history_and_intraday_archive(monkeypatch, tmp_path):
    """Keep signal inputs and publication out of the operator's managed archive.

    Do not create the history directory: missing-history behavior must stay
    distinguishable from an available archive. Individual tests may override
    either location explicitly after this fixture, as with the cron roots.
    """
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(tmp_path / "ticker_history"))
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(tmp_path / "intraday_archive"))


@pytest.fixture
def historical_live_clock(monkeypatch, request):
    """Opt-in clock for historical tests that simulate live broker placement.

    Freeze ONLY the clock source at each entry point's explicit scan/context
    timestamp. The real execution path, placement checks, and NYSE calendar
    still run. The dedicated session-boundary regressions do not use this
    fixture: they drive their own clocks, including changes during placement.
    ContextVar scopes nested and concurrent calls without sharing a scan's
    timestamp with another thread or leaving it set after an exception.
    """
    import inspect
    from contextvars import ContextVar
    from datetime import datetime, time
    from functools import wraps

    import morning_dca
    import near_close_dca

    # Some recovery tests undo their own temporary failure injection mid-test.
    # Keep this fixture's lifecycle independent of that monkeypatch stack.
    test_patches = monkeypatch
    monkeypatch = pytest.MonkeyPatch()

    def restore():
        # A test may have patched one of our wrappers. Restore its snapshot
        # first, then ours, so fixture wrappers cannot leak into the next test.
        test_patches.undo()
        monkeypatch.undo()

    request.addfinalizer(restore)
    clock: ContextVar[datetime | None] = ContextVar("historical_live_clock", default=None)
    real_clock = near_close_dca._live_placement_now
    monkeypatch.setattr(near_close_dca, "_live_placement_now", lambda: clock.get() or real_clock())

    def freeze_call(function):
        signature = inspect.signature(function)

        @wraps(function)
        def invoke(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            now = bound.arguments.get("now")
            ctx = bound.arguments.get("ctx")
            if ctx is not None:
                now = ctx.now
            # Direct placement tests need an in-slot clock for scheduled
            # executors; manual and other callers keep the synthetic noon.
            if now is None and "trading_day" in bound.arguments:
                executor = bound.arguments.get("self")
                decision_time = time(12)
                if isinstance(executor, morning_dca.MorningDCAExecutor) and not bound.arguments.get("manual", False):
                    decision_time = executor.profile["decision_time_et"]
                now = datetime.combine(bound.arguments["trading_day"], decision_time, tzinfo=near_close_dca.EASTERN)
            if clock.get() is not None or not isinstance(now, datetime):
                return function(*args, **kwargs)
            token = clock.set(now)
            try:
                return function(*args, **kwargs)
            finally:
                clock.reset(token)

        return invoke

    for owner, name in (
        (near_close_dca, "_run_once_impl"),
        (near_close_dca, "_process_buy_candidate"),
        (near_close_dca, "_process_whole_share_candidate"),
        (morning_dca.MorningDCAExecutor, "_run_impl"),
        (morning_dca.MorningDCAExecutor, "_place_broker_orders"),
    ):
        original = getattr(owner, name)
        wrapped = freeze_call(original)
        monkeypatch.setattr(owner, name, wrapped)
        # Tests importing a function directly keep that binding even after
        # the module attribute changes; freeze the same call in either form.
        for alias, value in vars(request.module).copy().items():
            if value is original:
                monkeypatch.setattr(request.module, alias, wrapped)


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


def _clear_in_flight_candidate_outcomes() -> None:
    """Drop ``near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]`` if that module is loaded.

    The dict is process-global; dozens of tests and several near_close_dca paths fill it and nothing
    used to reset it, so it stayed populated for every later test in the same process. Serially the
    two tests asserting it starts empty happened to run while it was clean; under ``pytest -n auto``
    (PR 2315) the order changes and they failed in 1 of 2 full runs. Pops the key rather than
    rebinding the attribute, the same move near_close_dca makes when a run finishes, so references
    already held to the dict stay live. Looks the module up in ``sys.modules`` instead of importing
    it so tests that never touch it do not pay for a ~40k-line import.
    """
    module = sys.modules.get("near_close_dca")
    if module is not None:
        module._IN_FLIGHT_CANDIDATE_OUTCOMES.pop("outcomes", None)


@pytest.fixture(autouse=True)
def _reset_in_flight_candidate_outcomes():
    """Per-test isolation for module state, as the env fixtures above do for paths
    (tests/test_conftest_isolation.py)."""
    _clear_in_flight_candidate_outcomes()


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


@pytest.fixture(autouse=True)
def _pin_no_buy_clock_outside_every_window(monkeypatch):
    """Pin dca_config's "today" past every dated ``no_buy`` window in config/tickers.json.

    The shipped config pauses buys of tickers sold at a loss for a wash-sale window (docs/adr-no-buy-window.md).
    Many tests build a config from the real universe and count its tickers or orders; without this they would
    pass or fail depending on the host's date. Tests of the window itself pass explicit dates, or monkeypatch
    ``dca_config._today_et`` again, which overrides this.
    """
    import datetime as _datetime

    import dca_config

    monkeypatch.setattr(dca_config, "_today_et", lambda: _datetime.date(2099, 1, 1))
