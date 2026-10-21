"""Gate 5 of the 12:30 live-mirror prompt: the abort message must say WHY each pair is missing.

2026-10-20: the gate aborted naming five ``(day, slot)`` pairs, but the message could not
tell "no run_state.json was ever written" from "one was written and the pair check
rejected it", so the operator had to dig through cron_tracking by hand. These tests run
the prompt's own heredoc against a temp tree, the same code the live executor runs.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

import pytest

import dca_paths

# Imported before any patch so the heredoc's own imports cannot capture _FrozenDateTime or the
# temp stream_root lambda into these modules and leak them into later tests (review round 1).
import dca_risk  # noqa: F401
import dca_runner  # noqa: F401

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "claude/dca-executor-prompts/live-mirror/1230ET-1230-pm-midday-buying-run.txt"
)
TODAY = datetime.date(2026, 10, 20)  # Tuesday; Oct 14-19 are all full market days


def _gate5_script() -> str:
    text = PROMPT.read_text(encoding="utf-8")
    match = re.search(r"python3 - <<'PY'\n(import datetime, glob, json, zoneinfo.*?)\nPY\n", text, re.S)
    assert match, "Gate 5 deployment heredoc not found in the 12:30 prompt"
    return match.group(1)


class _FrozenDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 10, 20, 12, 30, tzinfo=tz)


def _run_gate5(monkeypatch, tmp_path, states: dict[str, dict]) -> str:
    """Run the heredoc; return the SystemExit message (fails the test if it exits clean)."""
    scheduled = tmp_path / "scheduled_runs"
    intraday = tmp_path / "intraday_dca"
    scheduled.mkdir()
    intraday.mkdir()
    for name, state in states.items():
        run_dir = intraday / name
        run_dir.mkdir()
        (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    # Import dependencies before they can capture the patched globals.
    for module_name in ("dca_risk", "dca_runner"):
        __import__(module_name)
    monkeypatch.setattr(dca_paths, "stream_root", lambda cron, *rest: scheduled if cron == "ce16ba6b" else intraday)
    monkeypatch.setattr(datetime, "datetime", _FrozenDateTime)
    with pytest.raises(SystemExit) as excinfo:
        exec(compile(_gate5_script(), "<gate5>", "exec"), {"__name__": "__gate5__"})
    return str(excinfo.value)


def test_abort_names_pairs_with_no_run_state_at_all(monkeypatch, tmp_path):
    message = _run_gate5(monkeypatch, tmp_path, {})
    assert "missing scheduled fires" in message  # existing contract stays
    for day in ("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19"):
        for slot in ("14:30", "15:30"):
            assert f"{day} {slot}: no run_state.json" in message


def test_abort_distinguishes_a_rejected_run_state_from_an_absent_one(monkeypatch, tmp_path):
    # A 14:30 record exists for Oct 16, but its top-level trading_day disagrees with its
    # own run_classification, so the pair check rejects it.
    rejected = {
        "trading_day": "2026-10-15",
        "run_classification": {
            "trading_day": "2026-10-16",
            "scheduled_for_et": "14:30",
            "stream": "afternoon",
        },
    }
    message = _run_gate5(monkeypatch, tmp_path, {"20261016_1430ET": rejected})
    # Day, slot and reason are asserted together so a mislabelled pair cannot pass.
    assert "2026-10-16 14:30: trading_day mismatch" in message
    assert "2026-10-16 15:30: no run_state.json" in message
    assert "2026-10-14 14:30: no run_state.json" in message
    assert message.count("trading_day mismatch") == 1
