"""The 09:45 morning watchlist scan (cron bb4f6ef1) is retired, and nothing in the live program depends on it.

Owner direction (2026-10-14): the "Weekday 9:45 AM morning DCA watchlist early-warning scan (read-only)" is no longer
in use; deprecate and remove it "so that the confusion cannot happen again". Chosen scope is "B2": remove the
watchlist mode from ``morning_dca`` and make ``mode`` a REQUIRED argument rather than defaulting to live, so a caller
that omits it fails loudly instead of silently switching from a no-orders scan to live buying.

Deliberately NOT retired here (and not tested for removal):
  * ``schemas/morning_run_classification.schema.json`` keeps the ``morning_dca_watchlist_scan`` run type, because
    run classification and reporting still read historical artifacts of that type.
  * ``scripts/generate_credit_usage_proxy_report.py`` keeps the ``watchlist`` stream bucket for the same reason
    (it already stops counting the scan as a chain slot).

These tests were written before the removal and fail until it lands. ``test_live_mode_still_works`` passes
today and must keep passing: it is the guard against over-deletion.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import morning_dca
from morning_dca import MORNING_LIVE_MODE, MorningConfig, MorningDCAExecutor, load_morning_config

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "bb4f6ef1"


def _scheduled_tasks() -> dict:
    return json.loads((ROOT / "config" / "scheduled_tasks.json").read_text())


def _task() -> dict:
    tasks = [t for t in _scheduled_tasks()["tasks"] if t.get("id") == TASK_ID]
    assert len(tasks) == 1, "the retired task object is kept as the historical record, exactly once"
    return tasks[0]


def _reconciliation() -> dict:
    return next(
        v for v in _scheduled_tasks().values() if isinstance(v, dict) and "canonical_schedule_et" in v
    )


# --- the scheduled task and the manifest ------------------------------------------------------------------------


def test_the_task_is_retired_not_active():
    task = _task()
    assert task["status"] == "disabled"
    assert task.get("in_live_scheduler") is False
    assert task.get("scheduler_registered") is False
    assert not task.get("cron_utc"), "a retired task has no cron, so it cannot be re-registered from its manifest entry"


def test_the_task_is_not_in_the_canonical_schedule():
    recon = _reconciliation()
    rows = recon["canonical_schedule_et"]
    assert TASK_ID not in {row.get("task_id") for row in rows}
    assert recon["canonical_schedule_et_row_count"] == len(rows)


def test_the_task_is_listed_as_retired():
    recon = _reconciliation()
    retired_ids = {row.get("task_id") for row in recon["retired_not_in_canonical_schedule"]}
    assert TASK_ID in retired_ids


def _id_lists(node, path=""):
    """Yield (path, task_ids) for every list of task ids under the active machine-readable directories."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("daily_compact_chain_et", "applies_to_daily_tasks_et") and isinstance(value, list):
                yield f"{path}/{key}", [e if isinstance(e, str) else e.get("task_id") for e in value]
            else:
                yield from _id_lists(value, f"{path}/{key}")


def test_the_task_is_in_no_active_machine_readable_directory():
    # Copilot review of PR 1668: operating_mode.daily_compact_chain_et and the two applies_to_daily_tasks_et lists
    # (model_usage_log is consumed by scripts/model_usage_log.py as the single source of covered tasks) are active
    # directories, so a retired task must not stay in them.
    lists = dict(_id_lists(_scheduled_tasks()))
    assert len(lists) >= 3, sorted(lists)
    offenders = sorted(path for path, ids in lists.items() if TASK_ID in ids)
    assert offenders == []


# --- morning_dca: no watchlist mode, and no silent default ------------------------------------------------------


def test_morning_dca_has_no_watchlist_identity():
    leftovers = sorted(
        name for name in dir(morning_dca) if "WATCHLIST" in name.upper() or name.upper().startswith("DEFAULT_MORNING_STATE")
    )
    assert leftovers == []


def test_the_unqualified_morning_aliases_are_gone():
    # These resolved to the WATCHLIST values (MORNING_CRON_ID was bb4f6ef1), which is exactly the kind of
    # silent default that caused the confusion. Callers use the MORNING_LIVE_* names.
    for name in (
        "MORNING_CRON_ID",
        "MORNING_RUN_TYPE",
        "MORNING_SCHEDULED_FOR_ET",
        "MORNING_WINDOW_START_ET",
        "MORNING_WINDOW_END_ET",
        "STATE_SCHEMA_VERSION",
    ):
        assert not hasattr(morning_dca, name), name


def test_the_only_morning_profile_is_live():
    assert set(morning_dca.MORNING_PROFILES) == {MORNING_LIVE_MODE}
    assert morning_dca.MORNING_PROFILES[MORNING_LIVE_MODE]["trades"] is True


def test_mode_has_no_default_on_the_executor_or_the_config_loader():
    for func in (MorningDCAExecutor.__init__, load_morning_config):
        param = inspect.signature(func).parameters["mode"]
        assert param.default is inspect.Parameter.empty, f"{func.__qualname__} must require mode"


def test_building_the_executor_without_a_mode_fails_loudly():
    with pytest.raises(TypeError):
        MorningDCAExecutor(MorningConfig(tickers=("AAPL",)))


def test_loading_the_config_without_a_mode_fails_loudly(tmp_path):
    with pytest.raises(TypeError):
        load_morning_config(str(tmp_path))


def test_the_watchlist_mode_is_rejected():
    with pytest.raises(ValueError):
        MorningDCAExecutor(MorningConfig(tickers=("AAPL",)), mode="watchlist")
    with pytest.raises(ValueError):
        load_morning_config(None, mode="watchlist")


def test_live_mode_still_works():
    executor = MorningDCAExecutor(MorningConfig(tickers=("AAPL",)), mode=MORNING_LIVE_MODE)
    assert executor.mode == MORNING_LIVE_MODE
    assert executor.trades is True
    assert executor.profile["cron_id"] == "42173154"


# --- config -----------------------------------------------------------------------------------------------------


def test_morning_config_has_no_watchlist_task_or_root():
    text = (ROOT / "config" / "morning_dca.json").read_text()
    config = json.loads(text)
    assert "watchlist_task" not in config
    for needle in (TASK_ID, "morning_dca_watchlist", "morning-watchlist-v1"):
        assert needle not in text, needle


def _find_key(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from _find_key(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _find_key(value, key)


def test_the_default_morning_run_root_is_the_live_root():
    # morning_run_state_root used to point at the watchlist root as a legacy default.
    config = json.loads((ROOT / "config" / "morning_dca.json").read_text())
    roots = list(_find_key(config, "morning_run_state_root"))
    assert roots, "morning_run_state_root must still be declared"
    for root in roots:
        assert str(root).rstrip("/").endswith("/morning_dca"), root


def _find_lists(node, key):
    return [value for value in _find_key(node, key) if isinstance(value, list)]


def test_the_scan_is_not_in_the_must_cover_daily_chain():
    # The credit-usage report must list every id in this chain even on a day it did not fire, so a retired task left
    # here would show as a permanent did_not_run (Copilot review of PR 1668).
    chains = _find_lists(_scheduled_tasks(), "must_cover_active_daily_chain")
    assert chains, "must_cover_active_daily_chain must still be declared"
    for chain in chains:
        assert TASK_ID not in chain
