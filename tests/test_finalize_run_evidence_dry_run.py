"""Contract tests for a dry-run mode in scripts.finalize_run_evidence.

Why: the evidence writer refuses any --started-at outside the slot's acceptance
window, which is correct for scheduled fires but makes an off-hours smoke test of
Gate 9 impossible. The dry-run mode must (a) accept an off-window start,
(b) never write into the scheduled run directory, (c) never create or refresh the
latest-run pointer, (d) never accept orders, and (e) mark its output as a dry run.

dca_runner.execute_plan already has its own dry_run gate (test_dca_runner.py), so
placement is out of scope here. Tests are derived from the behavior above, not from
any implementation.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts import finalize_run_evidence as fre

ET = ZoneInfo("America/New_York")
OFF_WINDOW = datetime(2026, 10, 16, 22, 21, 34, tzinfo=ET)  # Friday night
IN_WINDOW = datetime(2026, 10, 16, 9, 36, 0, tzinfo=ET)


def _all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock"))


def test_off_window_start_is_still_refused_without_dry_run(tmp_path):
    with pytest.raises(fre.FinalizeRefused, match="acceptance window"):
        fre.finalize("0935", OFF_WINDOW, outcome="X", order_submit_status="no_orders_submitted", root=tmp_path)
    assert _all_files(tmp_path) == []


def test_dry_run_accepts_an_off_window_start(tmp_path):
    result = fre.finalize(
        "0935", OFF_WINDOW, outcome="DRY_RUN", order_submit_status="no_orders_submitted",
        root=tmp_path, dry_run=True,
    )
    assert result["dry_run"] is True


def test_dry_run_never_writes_the_scheduled_run_directory(tmp_path):
    fre.finalize(
        "0935", IN_WINDOW, outcome="DRY_RUN", order_submit_status="no_orders_submitted",
        root=tmp_path, dry_run=True,
    )
    scheduled = tmp_path / fre.slot_run_dir_relative("0935", IN_WINDOW.date())
    assert not scheduled.exists()


def test_dry_run_never_creates_or_refreshes_a_latest_pointer(tmp_path):
    fre.finalize(
        "0935", OFF_WINDOW, outcome="DRY_RUN", order_submit_status="no_orders_submitted",
        root=tmp_path, dry_run=True,
    )
    assert [p for p in _all_files(tmp_path) if p.name.startswith("latest_")] == []


def test_dry_run_output_lives_under_manual_runs_and_is_marked(tmp_path):
    fre.finalize(
        "0935", OFF_WINDOW, outcome="DRY_RUN", order_submit_status="no_orders_submitted",
        root=tmp_path, dry_run=True,
    )
    states = [p for p in _all_files(tmp_path) if p.name == "run_state.json"]
    assert len(states) == 1
    assert "manual_runs" in states[0].relative_to(tmp_path).parts
    assert json.loads(states[0].read_text())["dry_run"] is True


def test_dry_run_refuses_any_orders(tmp_path):
    order = {"ticker": "TSM", "order_id": "abc", "submitted_amount_usd": 4.01, "status": "filled"}
    with pytest.raises(fre.FinalizeRefused, match="dry"):
        fre.finalize(
            "0935", OFF_WINDOW, outcome="DRY_RUN", order_submit_status="submitted",
            root=tmp_path, orders=[order], dry_run=True,
        )
    assert _all_files(tmp_path) == []


def test_dry_run_persists_supplied_gate9_metadata(tmp_path):
    report = {"eligible_count": 2}
    budget = {"budget_tier": "normal", "daily_cap_usd": 10, "weekly_cap_usd": 20}
    code_version = {"git_sha": "a" * 40, "tag": "v1"}
    fre.finalize(
        "0935",
        OFF_WINDOW,
        outcome="DRY_RUN",
        order_submit_status="no_orders_submitted",
        root=tmp_path,
        report=report,
        budget=budget,
        error_class="SmokeTestError",
        error_detail="diagnostic",
        code_version=code_version,
        dry_run=True,
    )
    state_path = next(tmp_path.rglob("run_state.json"))
    state = json.loads(state_path.read_text())
    assert state["gate9_report"] == report
    assert state["budget"] == budget
    assert state["error_class"] == "SmokeTestError"
    assert state["error_detail"] == "diagnostic"
    assert state["code_version"] == code_version


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"budget": {"budget_tier": "unknown"}}, "budget_tier"),
        ({"code_version": {"git_sha": "not-a-sha"}}, "git_sha"),
    ],
)
def test_dry_run_refuses_invalid_metadata_before_writing(tmp_path, metadata, message):
    with pytest.raises(fre.FinalizeRefused, match=message):
        fre.finalize(
            "0935",
            OFF_WINDOW,
            outcome="DRY_RUN",
            order_submit_status="no_orders_submitted",
            root=tmp_path,
            dry_run=True,
            **metadata,
        )
    assert _all_files(tmp_path) == []


def test_cli_exposes_a_dry_run_flag(capsys, tmp_path):
    code = fre.main([
        "--slot", "0935", "--started-at", OFF_WINDOW.isoformat(), "--outcome", "DRY_RUN",
        "--order-submit-status", "no_orders_submitted", "--root", str(tmp_path), "--dry-run",
    ])
    assert code in (0, 1)  # never 2 (refused)
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
