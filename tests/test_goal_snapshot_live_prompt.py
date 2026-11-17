"""Wording tests for the live goal-snapshot prompts (Phase 2 live wiring, tests first).

Written FIRST. The prompts are not written yet, so these fail until they are. Approved scope:

1. The 07:31 ET task prompt (``claude/goal-snapshot/0731ET-goal-snapshot.md``): trading days only,
   records the PREVIOUS trading day's close through the read-only Robinhood tools via
   ``dca_goal_snapshot.py``, places no orders, is marked "Requires this computer", runs
   Sonnet 5.5 at medium effort with a 30-minute budget, and finishes before 3:40 PM ET.
2. The complete Friday 041d9d90 Notion refresh prompt (``claude/goal-snapshot/friday-041d9d90-weekly-dashboard-refresh.md``):
   calls ``dca_goal_progress.goal_progress_section`` on Fridays only, period return only under one year.

The live scheduled task bodies are not in this repo; the operator pastes these files in, as with the
executor mirrors. The mirror directory ``live-mirror/`` is NOT used, because ``sync_live_prompts.py``
expects every file there to map to an executor slot.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import dca_goal_progress
import dca_goal_snapshot

ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "claude" / "goal-snapshot"
SNAPSHOT_PROMPT = PROMPT_DIR / "0731ET-goal-snapshot.md"
FRIDAY_PROMPT = PROMPT_DIR / "friday-041d9d90-weekly-dashboard-refresh.md"

READ_ONLY_TOOLS = {"get_equity_positions", "get_equity_historicals", "get_accounts"}
ORDER_TOOL = re.compile(
    r"(?<![A-Za-z0-9_])(place|cancel|review|preview|exercise|decline|accept)_(equity|option|crypto|advanced|order|trade)"
)
ACCOUNT_LITERAL = re.compile(r"(?<!\d)\d{9,12}(?!\d)")


def _snapshot_prompt() -> str:
    return SNAPSHOT_PROMPT.read_text(encoding="utf-8")


def _friday_prompt() -> str:
    return FRIDAY_PROMPT.read_text(encoding="utf-8")


# ---- 07:31 ET snapshot prompt -----------------------------------------------------------------

def test_snapshot_prompt_file_exists():
    assert SNAPSHOT_PROMPT.is_file()


SNAPSHOT_SETUP = ROOT / "claude" / "GOAL-SNAPSHOT-0731-TASK-PROMPT.md"


def _snapshot_setup() -> str:
    return SNAPSHOT_SETUP.read_text(encoding="utf-8")


def test_snapshot_registration_doc_matches_the_task_spec():
    # Registration facts (schedule, model, budget) belong to the task's settings, so they live in the
    # setup doc; the prompt file is the whole prompt and nothing else.
    text = _snapshot_setup()
    spec = dca_goal_snapshot.scheduled_task_spec()
    assert "07:31" in text and "America/New_York" in text
    assert spec["time"].strftime("%H:%M") == "07:31"
    assert spec["timezone"] == "America/New_York"
    assert "Requires this computer" in text
    assert re.search(r"Sonnet 5\.5", text) and re.search(r"medium", text, re.I)
    assert re.search(r"30[- ]minute", text, re.I)
    assert re.search(r"before 3:40 PM ET", text)
    assert "0731ET-goal-snapshot.md" in text, "setup doc must point at the single prompt file"


def test_snapshot_prompt_is_one_whole_file_with_no_setup_notes():
    text = _snapshot_prompt()
    assert text.startswith("You are the 07:31 ET goal progress snapshot task")
    assert "```" not in text and "## Registration" not in text


def test_snapshot_prompt_runs_only_on_trading_days_as_the_code_decides():
    text = _snapshot_prompt()
    assert "should_run" in text and "previous_trading_day" in text
    assert dca_goal_snapshot.should_run(date(2026, 11, 14), None) is False  # Saturday
    assert dca_goal_snapshot.should_run(date(2026, 11, 16), None) is True   # Monday


def test_snapshot_prompt_routes_capture_through_the_module():
    text = _snapshot_prompt()
    assert "capture_previous_close" in text
    assert "missed_snapshot" in text.lower() or "MISSED SNAPSHOT" in text
    assert re.search(r"no rows|missed snapshot", text, re.I)


def test_snapshot_prompt_names_only_read_only_tools():
    text = _snapshot_prompt()
    named = set(re.findall(r"\bget_[a-z_]+\b", text))
    assert named <= READ_ONLY_TOOLS, f"non-allowlisted read tools: {sorted(named - READ_ONLY_TOOLS)}"


def test_snapshot_prompt_states_no_trading_and_names_no_order_tool():
    text = _snapshot_prompt()
    assert re.search(r"never trade|no orders?", text, re.I)
    assert [m.group(0) for m in ORDER_TOOL.finditer(text)] == []


def test_snapshot_prompt_carries_no_account_literal():
    assert not ACCOUNT_LITERAL.search(_snapshot_prompt())


# ---- Friday 041d9d90 section ------------------------------------------------------------------

def test_friday_prompt_file_exists():
    assert FRIDAY_PROMPT.is_file()


def test_friday_prompt_calls_the_section_function_with_today():
    text = _friday_prompt()
    assert "dca_goal_progress.goal_progress_section(" in text
    assert "today=" in text
    assert "Goal progress" in text


def test_friday_prompt_is_for_the_5_30_pm_friday_refresh_only():
    text = _friday_prompt()
    assert "041d9d90" in text
    assert "5:30 PM ET" in text
    assert "Fridays" in text
    assert dca_goal_progress.friday_refresh_sections(date(2026, 11, 20)) == ["Goal progress"]
    assert dca_goal_progress.friday_refresh_sections(date(2026, 11, 19)) == []


def test_friday_prompt_shows_period_return_under_one_year_only():
    text = _friday_prompt()
    assert re.search(r"under (one|1) year", text, re.I)
    assert re.search(r"period return", text, re.I)


def test_friday_prompt_names_no_order_tool_and_carries_no_account_literal():
    text = _friday_prompt()
    # The complete prompt names order tools exactly once, in its "NEVER call" prohibition.
    hits = [ln for ln in text.splitlines() if ORDER_TOOL.search(ln)]
    assert all("NEVER call" in ln for ln in hits)
    assert not ACCOUNT_LITERAL.search(text)
