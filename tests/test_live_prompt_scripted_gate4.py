"""Gate 4 runs as one scripted command in the live prompts (written before the edit).

The prompts told the model to fetch the day's order history, classify every order in the slot's window by
``placed_agent`` and decide. ``primary_gates gate4`` does that classification from the saved day baseline and prints
one verdict (CLEAR, COMPATIBLE_CONTINUE or ABORT). The prose rules stay in each prompt as the manual fallback, exactly
as for the scripted clock and budget, and the 12:30 prompt keeps its own gate script.

Scope: the six prompts with the standard gate layout (9:35, 10:30, 11:30, 1:30, 2:30, 3:30).
"""

import re
from pathlib import Path

import pytest

LIVE_MIRROR = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
STANDARD = ("0935", "1030", "1130", "1330", "1430", "1530")


def _text(slot: str) -> str:
    (path,) = sorted(LIVE_MIRROR.glob(f"{slot}ET-*.txt"))
    return path.read_text(encoding="utf-8")


def _gate4(slot: str) -> str:
    text = _text(slot)
    return text[text.index("GATE 4 - SOLE-EXECUTOR INTERLOCK") : text.index("GATE 5 - BUY LIST")]


def _command_line(slot: str) -> str:
    (line,) = [ln for ln in _text(slot).splitlines() if "primary_gates gate4" in ln]
    return line


@pytest.mark.parametrize("slot", STANDARD)
def test_gate_4_runs_as_one_scripted_command_for_this_slot(slot):
    gate4 = _gate4(slot)
    assert f"python3 -m scripts.primary_gates gate4 --slot {slot}" in gate4
    for verdict in ("CLEAR", "COMPATIBLE_CONTINUE", "ABORT"):
        assert verdict in gate4


@pytest.mark.parametrize("slot", STANDARD)
def test_the_command_reads_the_saved_day_baseline_and_takes_no_account_flag(slot):
    line = _command_line(slot)
    assert "--orders-pages" in line and "--trading-day" in line
    assert "--account" not in line


@pytest.mark.parametrize("slot", STANDARD)
def test_the_covered_tickers_feed_gate_5_s_exclusion_flag(slot):
    gate4 = _gate4(slot)
    assert "already_covered_tickers" in gate4 and "--already-covered-tickers" in gate4


@pytest.mark.parametrize("slot", STANDARD)
def test_the_manual_classification_rules_stay_as_the_fallback(slot):
    gate4 = _gate4(slot)
    assert "rogue" in gate4 and "placed_agent" in gate4 and "agentic" in gate4
    assert "This gate runs on every single execution" in gate4


@pytest.mark.parametrize("slot", STANDARD)
def test_an_abort_verdict_places_nothing_and_alerts(slot):
    scripted = _command_line(slot)
    assert re.search(r"ABORT[^.]*place nothing|place nothing[^.]*ABORT", scripted, flags=re.I), scripted


def test_12_30_keeps_its_own_gate_script():
    assert "primary_gates gate4" not in _text("1230")


@pytest.mark.parametrize("slot", STANDARD)
def test_no_other_slot_s_id_leaks_into_the_command_line(slot):
    line = _command_line(slot)
    for other in STANDARD:
        if other != slot:
            assert f"--slot {other}" not in line


@pytest.mark.parametrize("slot", STANDARD)
def test_gate_4_still_sits_before_gate_5_and_the_scripted_clock_before_it(slot):
    text = _text(slot)
    assert text.index("primary_gates clock") < text.index("primary_gates gate4") < text.index("GATE 5 - BUY LIST")
