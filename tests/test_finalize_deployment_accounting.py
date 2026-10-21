"""A run_state written by ``finalize_run_evidence`` must carry deployment the risk code can count.

2026-10-21: a state written by the finalizer with a placed order contributed $0.00 to the 7-day
kill-switch total. ``dca_risk.deployed_usd`` reads ``buy_candidates`` (or ``budget.day_notional_usd``) and the
finalizer wrote neither, only ``orders_submitted_count`` and the submitted-orders CSV. The seven live prompts
finalize through this command, so deployment from prompt-driven slots never reached the total the 12:30
prompt's Gate 5 hands to the signal generator. These tests pin that the finalizer records each placed order
as a countable candidate, rebuilds them from the union of order rows on a retry (never double counting), and
leaves a state an executor wrote alone.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_risk
from scripts import finalize_run_evidence as fre
from scripts.scheduled_artifacts import ORDER_ATTEMPT_STATUSES

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 14)
BUDGET = {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900}
AFTERNOON = ["1430", "1530"]
ALL_SLOTS = ["0935", "1030", "1130", "1230", "1330", "1430", "1530"]


@pytest.fixture(autouse=True)
def synthetic_account(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")


def _order(order_id, amount, ticker="MA", status="filled"):
    return {"ticker": ticker, "order_id": order_id, "submitted_amount_usd": amount, "status": status}


def _finalize(root: Path, slot: str, orders, *, status="submitted", budget=BUDGET, now=None):
    start = datetime(2026, 10, 14, int(slot[:2]), int(slot[2:]) + 2, tzinfo=ET)
    return fre.finalize(
        slot, start, outcome="ORDERS_PLACED" if orders else "NO_ORDERS", order_submit_status=status,
        root=root, orders=orders, budget=budget if orders else None, now=now or start + timedelta(minutes=20),
    )


def _state(root: Path, result: dict) -> dict:
    return json.loads((root / result["run_dir"] / "run_state.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("slot", ALL_SLOTS)
def test_a_placed_order_counts_as_deployed(tmp_path, slot):
    result = _finalize(tmp_path, slot, [_order("ord-1", 2.5)])

    assert result["verified"] is True, result["errors"]
    assert dca_risk.deployed_usd(_state(tmp_path, result)) == 2.5


@pytest.mark.parametrize("slot", AFTERNOON)
def test_the_deployment_lands_on_the_fire_day_in_the_afternoon_total(tmp_path, slot):
    result = _finalize(tmp_path, slot, [_order("ord-1", 2.5), _order("ord-2", 1.24, ticker="DFTX")])

    state = _state(tmp_path, result)
    assert dca_risk.deployed_by_day([state]) == {DAY: 3.74}
    assert dca_risk.seven_day_deployed(dca_risk.deployed_by_day([state]), date(2026, 10, 20)) == 3.74


@pytest.mark.parametrize("status", sorted(ORDER_ATTEMPT_STATUSES - {"not_submitted_confirmation_required"}))
def test_every_placed_or_in_flight_status_the_finalizer_accepts_is_counted(tmp_path, status):
    result = _finalize(tmp_path, "1530", [_order("ord-1", 2.0, status=status)])

    assert dca_risk.deployed_usd(_state(tmp_path, result)) == 2.0


def test_a_confirmation_required_row_never_counts_as_deployed(tmp_path):
    result = _finalize(tmp_path, "1530", [_order("ord-1", 2.0, status="not_submitted_confirmation_required")])

    assert dca_risk.deployed_usd(_state(tmp_path, result)) == 0.0


def test_a_retry_with_the_same_orders_does_not_double_count(tmp_path):
    first = _finalize(tmp_path, "1530", [_order("ord-1", 2.5)])
    second = _finalize(tmp_path, "1530", [_order("ord-1", 2.5)])

    assert second["run_dir"] == first["run_dir"]
    assert dca_risk.deployed_usd(_state(tmp_path, second)) == 2.5


def test_a_retry_that_adds_an_order_keeps_the_first_attempts_order_too(tmp_path):
    _finalize(tmp_path, "1530", [_order("ord-1", 2.5)])
    second = _finalize(tmp_path, "1530", [_order("ord-2", 1.0, ticker="TRIN")])

    state = _state(tmp_path, second)
    assert dca_risk.deployed_usd(state) == 3.5
    assert sorted(c["order_id"] for c in state["buy_candidates"]) == ["ord-1", "ord-2"]


def test_a_run_with_no_orders_deploys_nothing_even_when_the_budget_names_a_day_notional(tmp_path):
    budget = {**BUDGET, "day_notional_usd": 48.38}  # prior same-day capital seeded into the budget block

    result = _finalize(tmp_path, "1530", None, status="no_orders_submitted", budget=None)
    state = _state(tmp_path, result)
    state["budget"] = budget  # what a Gate 6 that ran and placed nothing would carry

    assert state["buy_candidates"] == []
    assert dca_risk.deployed_usd(state) == 0.0


def test_the_candidate_row_names_the_order_the_ticker_and_the_amount(tmp_path):
    result = _finalize(tmp_path, "1530", [_order("ord-9", 1.24, ticker="DFTX")])

    [candidate] = _state(tmp_path, result)["buy_candidates"]
    assert candidate["ticker"] == "DFTX"
    assert candidate["order_id"] == "ord-9"
    assert candidate["status"] == "filled"
    assert candidate["planned_amount_usd"] == 1.24
    assert candidate["buy_multiplier"] is None  # schema-required, but the ladder multiplier is not the finalizer's


def test_a_state_an_executor_wrote_is_left_untouched(tmp_path):
    first = _finalize(tmp_path, "1530", None, status="no_orders_submitted", budget=None)
    path = tmp_path / first["run_dir"] / "run_state.json"
    executor_state = _state(tmp_path, first)
    executor_state.pop("finalized_by")  # now it looks like an executor's own run() wrote it
    executor_state["buy_candidates"] = [
        {"ticker": "GS", "order_id": "exec-1", "status": "filled", "planned_amount_usd": 9.0}
    ]
    path.write_text(json.dumps(executor_state), encoding="utf-8")

    _finalize(tmp_path, "1530", [_order("ord-1", 2.5)])

    kept = json.loads(path.read_text(encoding="utf-8"))
    assert kept["buy_candidates"] == [
        {"ticker": "GS", "order_id": "exec-1", "status": "filled", "planned_amount_usd": 9.0}
    ]


def test_the_cli_cannot_record_placed_orders_without_a_gate_6_budget(tmp_path, capsys):
    """Reconstructing orders for a fire that aborted before Gate 6 is a Python-only path for the backfill.

    The command line is what a live session can reach, and its refusal ("orders were placed, so Gate 6
    ran") is what stops a session writing false evidence. No combination of flags may reopen it, however
    the outcome, error class, report and budget are spelled.
    """
    orders = tmp_path / "orders.json"
    orders.write_text(json.dumps([_order("ord-1", 2.5)]), encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"reconstruction": {
        "backfilled": True, "attributed_by": "operator", "attributed_order_ids": ["ord-1"]}}), encoding="utf-8")

    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps({"budget_evaluated": False}), encoding="utf-8")

    code = fre.main([
        "--slot", "1530", "--started-at", "2026-10-14T15:32:00-04:00",
        "--outcome", "BACKFILL_GATE2_ABORT_ATTRIBUTED_ORDERS", "--order-submit-status", "submitted",
        "--orders-json", str(orders), "--error-class", "backfill_gate2_abort", "--report-json", str(report),
        "--budget-json", str(budget), "--root", str(tmp_path / "root"),
    ])

    assert code == 2
    assert "Gate 6 ran" in capsys.readouterr().out
    assert not (tmp_path / "root").exists() or not list((tmp_path / "root").rglob("run_state.json"))
