"""The 07:31 ET snapshot task and the Friday Goal progress section (Phase 2, ADR tests 25-28).

Written FIRST. Skipped until ``dca_goal_snapshot.py`` and ``dca_goal_progress.py`` gain these
functions. Approved 2026-11-14:

* The snapshot task runs at 07:31 America/New_York on trading days only, and records the PREVIOUS
  trading day's close. It must finish well before the afternoon chain; the test pins the run time
  before 3:40 PM ET. It runs on the Mac with "Requires this computer".
* The Friday Notion "Goal progress" section sits in the existing Friday refresh. Under one year of
  window, it shows the period return only, never an annualized figure.
* A paused goal (status not active) or an empty goal list is shown as INFO on the dashboard, and
  does not alert.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

dca_goal_snapshot = pytest.importorskip("dca_goal_snapshot", reason="Phase 2 snapshot task (ADR tests 25-26)")
dca_goal_progress = pytest.importorskip("dca_goal_progress", reason="Phase 2 section and info (ADR tests 27-28)")

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_trading_day(day: date) -> bool:
    return day.weekday() < 5


# ---- 07:31 ET previous-close task (tests 25-26) -----------------------------------------------

def test_task_spec_is_0731_et_on_trading_days_recording_the_previous_close():
    spec = dca_goal_snapshot.scheduled_task_spec()
    assert spec["time"] == time(7, 31)
    assert spec["timezone"] == "America/New_York"
    assert spec["days"] == "trading"
    assert spec["records"] == "previous_trading_day_close"
    assert spec["requires_this_computer"] is True


def test_task_finishes_well_before_the_3_40_pm_afternoon_chain():
    spec = dca_goal_snapshot.scheduled_task_spec()
    # Run start plus a generous 30-minute budget must still end before the 3:40 PM watchdog.
    assert datetime.combine(date(2026, 11, 16), spec["time"], ET).hour < 15
    assert spec["max_runtime_minutes"] <= 60


def test_task_does_not_run_on_weekends_or_holidays():
    assert dca_goal_snapshot.should_run(date(2026, 11, 14), None) is False
    assert dca_goal_snapshot.should_run(date(2026, 11, 26), None) is False  # Thanksgiving
    assert dca_goal_snapshot.should_run(date(2026, 11, 16), None) is True


def test_task_manifest_row_is_documented():
    manifest = (REPO_ROOT / "docs" / "schedule_manifest.md").read_text(encoding="utf-8")
    assert "07:31" in manifest and "previous trading day" in manifest


# ---- Friday section (tests 27-28) -----------------------------------------------------------

def test_section_shows_period_return_and_no_annualized_figure_under_one_year():
    section = dca_goal_progress.goal_progress_section(
        [{"goal_id": "growth-twr-5y", "window_days": 9, "period_return": 0.0846, "annualized_return": None}],
        today=date(2026, 11, 20),
    )
    text = section["text"]
    assert "period return" in text.lower()
    assert "8.46%" in text
    assert "annualized" not in text.lower()


def test_section_shows_annualized_only_at_one_year_or_more():
    section = dca_goal_progress.goal_progress_section(
        [{"goal_id": "growth-twr-5y", "window_days": 400, "period_return": 0.25, "annualized_return": 0.2}],
        today=date(2027, 12, 20),
    )
    assert "annualized" in section["text"].lower()
    assert "20.00%" in section["text"]


def test_section_is_part_of_the_friday_refresh_only():
    assert dca_goal_progress.in_friday_refresh(date(2026, 11, 20)) is True   # Friday
    assert dca_goal_progress.in_friday_refresh(date(2026, 11, 19)) is False  # Thursday


def test_inactive_goal_is_info_not_an_alert(tmp_path):
    import json

    doc = json.loads((REPO_ROOT / "config" / "goals.json").read_text())
    doc["status"] = "paused"
    (tmp_path / "goals.json").write_text(json.dumps(doc))
    now = datetime(2026, 12, 2, 15, 0, tzinfo=ET)
    assert dca_goal_progress.goal_config_alert(tmp_path, now) is None
    info = dca_goal_progress.goal_status_info(tmp_path, now)
    assert info is not None and info["kind"] == "goal_inactive" and info["level"] == "info"
    assert "paused" in info["reason"]


def test_empty_goal_list_is_info_not_an_alert(tmp_path):
    import json

    doc = json.loads((REPO_ROOT / "config" / "goals.json").read_text())
    doc["goals"] = []
    (tmp_path / "goals.json").write_text(json.dumps(doc))
    now = datetime(2026, 12, 2, 15, 0, tzinfo=ET)
    assert dca_goal_progress.goal_config_alert(tmp_path, now) is None
    info = dca_goal_progress.goal_status_info(tmp_path, now)
    assert info is not None and info["level"] == "info" and "no goals" in info["reason"]


def test_active_goal_has_no_inactive_info(tmp_path):
    import json

    doc = json.loads((REPO_ROOT / "config" / "goals.json").read_text())
    (tmp_path / "goals.json").write_text(json.dumps(doc))
    assert dca_goal_progress.goal_status_info(tmp_path, datetime(2026, 12, 2, 15, 0, tzinfo=ET)) is None
