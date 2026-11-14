"""Goal-progress alert triggers (docs/adr-goals-and-loss-hedge-shadow.md, Phase 2).

Written FIRST. Skipped until ``dca_goal_progress.py`` lands. Two separate triggers, approved
2026-11-14:

(a) Missed snapshot = one missed TRADING day. Weekends and configured holidays are exempt
    (``dca_runner.market_day_status``, i.e. ``config/market_calendar.json``). A missed trading day
    writes NO ``goal_progress`` row for that day and raises a ``missed_snapshot`` alert.
    One missed day is enough; there is no grace period.

(b) Stale or invalid goal = ``goals.json`` holds goals, but ``dca_goals.load_goals`` returns the
    neutral set because the data is invalid, unreadable or ambiguous. Alert at once.
    A goal that is not yet effective (before 2026-12-01 ET) must NOT alert.

Judgement calls made here, to be reviewed: a ``status`` other than ``active`` is an intentional
pause, not an invalid file, so it does not alert; an empty ``goals`` list means "no goals", so it
does not alert; while the goal config is invalid, no ``goal_progress`` row is written (fail closed).
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

dca_goal_progress = pytest.importorskip(
    "dca_goal_progress", reason="dca_goal_progress.py lands with Phase 2 (ADR tests 16-17)"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_GOALS = json.loads((REPO_ROOT / "config" / "goals.json").read_text())

BEFORE_EFFECTIVE = datetime(2026, 11, 14, 14, 20, tzinfo=timezone.utc)  # the handoff's date
AFTER_EFFECTIVE = datetime(2026, 12, 2, 15, 0, tzinfo=timezone.utc)
SNAPSHOT_ROW = {"symbol": "CHE", "quantity": "0.004453", "equity": "6.25"}
MONDAY = date(2026, 11, 16)  # ordinary trading day
SATURDAY = date(2026, 11, 14)
THANKSGIVING = date(2026, 11, 26)  # configured holiday in config/market_calendar.json


def _is_trading_day(day: date) -> bool:
    # Hermetic calendar for the cases that are not about holidays.
    return day.weekday() < 5


def _write_goals(config_dir: Path, document) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else json.dumps(document)
    (config_dir / "goals.json").write_text(text, encoding="utf-8")
    return config_dir


def _kinds(result) -> list[str]:
    return [alert.kind for alert in result.alerts]


# ---- trigger (a): missed snapshot on a trading day ----------------------------------------

def test_a_missed_trading_day_writes_no_row_and_alerts(tmp_path):
    result = dca_goal_progress.close_day(
        MONDAY, snapshot=None, config_dir=_write_goals(tmp_path, SHIPPED_GOALS),
        now=BEFORE_EFFECTIVE, is_trading_day=_is_trading_day,
    )
    assert result.progress_rows == []
    assert _kinds(result) == ["missed_snapshot"]
    assert result.alerts[0].day == MONDAY


def test_a_single_missed_day_is_enough_to_alert(tmp_path):
    # No grace period: the first miss alerts. (Two-day tolerance was considered and rejected.)
    result = dca_goal_progress.close_day(
        MONDAY, snapshot=None, config_dir=_write_goals(tmp_path, SHIPPED_GOALS),
        now=BEFORE_EFFECTIVE, is_trading_day=_is_trading_day,
    )
    assert "missed_snapshot" in _kinds(result)


def test_a_missed_weekend_day_is_exempt(tmp_path):
    result = dca_goal_progress.close_day(
        SATURDAY, snapshot=None, config_dir=_write_goals(tmp_path, SHIPPED_GOALS),
        now=BEFORE_EFFECTIVE, is_trading_day=_is_trading_day,
    )
    assert result.progress_rows == []
    assert "missed_snapshot" not in _kinds(result)


def test_a_missed_holiday_is_exempt_using_the_real_calendar(tmp_path):
    # Uses config/market_calendar.json through dca_runner, not the hermetic stub.
    result = dca_goal_progress.close_day(
        THANKSGIVING, snapshot=None, config_dir=_write_goals(tmp_path, SHIPPED_GOALS),
        now=BEFORE_EFFECTIVE, is_trading_day=None,
    )
    assert "missed_snapshot" not in _kinds(result)


def test_a_present_snapshot_writes_one_row_and_no_alert(tmp_path):
    result = dca_goal_progress.close_day(
        MONDAY, snapshot=[SNAPSHOT_ROW], config_dir=_write_goals(tmp_path, SHIPPED_GOALS),
        now=BEFORE_EFFECTIVE, is_trading_day=_is_trading_day,
    )
    assert len(result.progress_rows) == 1
    assert result.progress_rows[0]["day"] == MONDAY
    assert result.alerts == []


# ---- trigger (b): stale or invalid goal config ----------------------------------------------

def test_b_shipped_config_before_effective_date_does_not_alert(tmp_path):
    # The real file: goals exist but are not effective until 2026-12-01. Neutral, and quiet.
    assert dca_goal_progress.goal_config_alert(_write_goals(tmp_path, SHIPPED_GOALS), BEFORE_EFFECTIVE) is None


def test_b_shipped_config_after_effective_date_does_not_alert(tmp_path):
    assert dca_goal_progress.goal_config_alert(_write_goals(tmp_path, SHIPPED_GOALS), AFTER_EFFECTIVE) is None


def test_b_invalid_json_alerts_at_once(tmp_path):
    alert = dca_goal_progress.goal_config_alert(_write_goals(tmp_path, '{"status": "active", "goals": ['),
                                                BEFORE_EFFECTIVE)
    assert alert is not None and alert.kind == "goal_config_invalid"


def test_b_unknown_top_level_field_alerts_at_once(tmp_path):
    doc = copy.deepcopy(SHIPPED_GOALS)
    doc["surprise"] = True
    alert = dca_goal_progress.goal_config_alert(_write_goals(tmp_path, doc), AFTER_EFFECTIVE)
    assert alert is not None and alert.kind == "goal_config_invalid"


def test_b_unreadable_file_alerts_at_once(tmp_path):
    (tmp_path / "goals.json").mkdir()  # a directory where the file should be
    alert = dca_goal_progress.goal_config_alert(tmp_path, BEFORE_EFFECTIVE)
    assert alert is not None and alert.kind == "goal_config_invalid"


def test_b_two_in_effect_growth_goals_are_ambiguous_and_alert(tmp_path):
    doc = copy.deepcopy(SHIPPED_GOALS)
    twin = copy.deepcopy(doc["goals"][0])
    twin["goal_id"] = "growth-twr-5y-b"
    doc["goals"].append(twin)
    alert = dca_goal_progress.goal_config_alert(_write_goals(tmp_path, doc), AFTER_EFFECTIVE)
    assert alert is not None and alert.kind == "goal_config_ambiguous"


def test_b_broken_goal_entry_alerts_at_once_even_before_effective(tmp_path):
    doc = copy.deepcopy(SHIPPED_GOALS)
    doc["goals"][0]["target"] = 7.5  # out of range; invalid whether or not it is effective yet
    alert = dca_goal_progress.goal_config_alert(_write_goals(tmp_path, doc), BEFORE_EFFECTIVE)
    assert alert is not None and alert.kind == "goal_config_invalid"


def test_b_paused_status_is_intentional_and_does_not_alert(tmp_path):
    doc = copy.deepcopy(SHIPPED_GOALS)
    doc["status"] = "paused"
    assert dca_goal_progress.goal_config_alert(_write_goals(tmp_path, doc), AFTER_EFFECTIVE) is None


def test_b_empty_goal_list_means_no_goals_and_does_not_alert(tmp_path):
    doc = copy.deepcopy(SHIPPED_GOALS)
    doc["goals"] = []
    assert dca_goal_progress.goal_config_alert(_write_goals(tmp_path, doc), AFTER_EFFECTIVE) is None


def test_b_invalid_config_writes_no_goal_progress_row(tmp_path):
    # Fail closed: a day with a good snapshot still writes no progress row while the goal is broken.
    result = dca_goal_progress.close_day(
        MONDAY, snapshot=[SNAPSHOT_ROW], config_dir=_write_goals(tmp_path, '{"goals": ['),
        now=BEFORE_EFFECTIVE, is_trading_day=_is_trading_day,
    )
    assert result.progress_rows == []
    assert "goal_config_invalid" in _kinds(result)
