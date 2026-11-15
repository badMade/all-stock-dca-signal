"""Tests for ``primary_gates gate4``: the sole-executor interlock's classification as code (written before the code).

Why: Gate 4 is the last gate the live prompts still walk by hand. Each prompt tells the model to fetch the day's order
history, "classify EVERY order in the window" by ``placed_agent``, and decide. That is deterministic, so it should be one
command. The rules are the PROMPT's (the 11:30 text and its siblings), and two other in-repo implementations already
follow them (``backup_planner._USER_AGENTS`` and ``plan_first_slot``):

  * an order is IN THE WINDOW when its ``created_at`` (submission, not fill time) falls inside the slot's interlock
    window on the trading day, both ends inclusive, from ``dca_runner.SLOTS`` (the same bounds ``clock`` uses);
  * ``placed_agent`` is ``user``, ``recurring`` or ``drip``: the account owner's own activity, never counted;
  * ``placed_agent`` is exactly ``agentic``: the recognized compatible executor (docs/safeguards.md);
  * anything else, including a missing value and a different case or stray whitespace: ROGUE (unknown provenance is
    rogue, never compatible);
  * any rogue order => ABORT, even when a compatible one is also present;
  * no rogue and at least one compatible => COMPATIBLE_CONTINUE, carrying the tickers that executor already ordered, so
    Gate 5 can exclude them (``--already-covered-tickers``);
  * otherwise CLEAR.

ONE DISCREPANCY TO CONFIRM: ``dca_runner.gate_sole_executor`` deliberately ignores ``placed_agent`` and treats even a
manual order in the window as a second writer. The prompt (and the two in-repo helpers above) say the owner's own
activity never blocks. These tests follow the prompt, because it is what actually runs.

Decisions pinned here that the prompt does not spell out: classification is by provenance only, so it ignores an
order's side and state (a cancelled or rejected attempt, or a sell, still counts as that executor's activity); matching
is EXACT, with no case or whitespace folding (fail safe); and an envelope saved for a different trading day or account
fails closed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_runner
from dca_runner import BrokerOrder
from scripts import primary_gates

REPO = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
ACCOUNT = "999000111"  # conftest.py's synthetic value
# Derived, never written as a literal: tests/test_config_invariants.py takes no account-shaped literal other than ACCOUNT.
OTHER_ACCOUNT = str(int(ACCOUNT) + 1)
WEDNESDAY = date(2026, 11, 18)  # a normal weekday after the 2026-11-01 DST change (EST, UTC-5)
ALLOWED_KEYS = {
    "status", "reason", "orders_in_window", "own_activity", "compatible", "rogue", "already_covered_tickers",
}


def at(hhmm: str, day: date = WEDNESDAY, seconds: int = 0) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, seconds, tzinfo=ET)


def order(symbol: str, hhmm: str, agent, *, state: str = "filled", side: str = "buy", day: date = WEDNESDAY,
          seconds: int = 0) -> BrokerOrder:
    return BrokerOrder(symbol=symbol, side=side, dollar_amount=2.0, created_at=at(hhmm, day, seconds),
                       placed_agent=agent, state=state)


def classify(orders, slot: str = "1130") -> dict:
    return primary_gates.classify_window_orders(orders, slot, WEDNESDAY)


# --------------------------------------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------------------------------------


def test_an_empty_window_is_clear():
    out = classify([])
    assert out["status"] == "CLEAR"
    assert (out["orders_in_window"], out["own_activity"], out["compatible"], out["rogue"]) == (0, 0, 0, 0)
    assert out["already_covered_tickers"] == []


@pytest.mark.parametrize("agent", ["user", "recurring", "drip"])
def test_the_owners_own_activity_never_blocks(agent):
    out = classify([order("CAT", "11:30", agent)])
    assert out["status"] == "CLEAR" and out["own_activity"] == 1 and out["rogue"] == 0


def test_all_three_owner_values_together_are_still_clear():
    out = classify([order("AAA", "11:30", "user"), order("BBB", "11:31", "recurring"), order("CCC", "11:32", "drip")])
    assert out["status"] == "CLEAR" and out["own_activity"] == 3 and out["orders_in_window"] == 3


def test_the_compatible_executor_continues_and_reports_the_tickers_it_already_ordered():
    out = classify([order("CAT", "11:30", "agentic"), order("anet", "11:31", "agentic"), order("CAT", "11:40", "agentic")])
    assert out["status"] == "COMPATIBLE_CONTINUE" and out["compatible"] == 3 and out["rogue"] == 0
    assert out["already_covered_tickers"] == ["ANET", "CAT"]  # unique, upper-case, sorted


def test_a_compatible_attempt_that_was_cancelled_or_rejected_still_covers_its_ticker():
    out = classify([order("CAT", "11:30", "agentic", state="cancelled"), order("ANET", "11:31", "agentic", state="rejected")])
    assert out["already_covered_tickers"] == ["ANET", "CAT"]


def test_a_compatible_sell_is_still_that_executors_activity():
    out = classify([order("CAT", "11:30", "agentic", side="sell")])
    assert out["status"] == "COMPATIBLE_CONTINUE" and out["already_covered_tickers"] == ["CAT"]


@pytest.mark.parametrize("agent", [None, "", "robinhood_app", "Agentic", " agentic", "agentic ", "USER", "user "])
def test_unknown_provenance_is_rogue_with_no_case_or_whitespace_folding(agent):
    out = classify([order("CAT", "11:30", agent)])
    assert out["status"] == "ABORT" and out["rogue"] == 1 and out["reason"]


def test_a_rogue_sell_still_aborts():
    assert classify([order("CAT", "11:30", None, side="sell")])["status"] == "ABORT"


def test_one_rogue_order_cannot_be_hidden_by_a_compatible_one():
    out = classify([order("CAT", "11:30", "agentic"), order("ANET", "11:31", "mystery")])
    assert out["status"] == "ABORT" and (out["compatible"], out["rogue"]) == (1, 1)


def test_a_rogue_order_still_aborts_alongside_the_owners_own_activity():
    out = classify([order("CAT", "11:30", "user"), order("ANET", "11:31", None)])
    assert out["status"] == "ABORT" and (out["own_activity"], out["rogue"]) == (1, 1)


def test_the_abort_output_carries_counts_and_a_reason_but_no_order_detail():
    out = classify([order("CAT", "11:30", None)])
    assert set(out) <= ALLOWED_KEYS


# --------------------------------------------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------------------------------------------


def test_orders_outside_the_window_or_on_another_day_are_ignored():
    before, after = at("11:25", seconds=0) - timedelta(seconds=1), at("12:00") + timedelta(seconds=1)
    other_day = order("CAT", "11:30", None, day=WEDNESDAY - timedelta(days=1))
    early = BrokerOrder("CAT", "buy", 2.0, before, placed_agent=None)
    late = BrokerOrder("CAT", "buy", 2.0, after, placed_agent=None)
    assert classify([early, late, other_day])["status"] == "CLEAR"


@pytest.mark.parametrize("slot", sorted(dca_runner.SLOTS))
def test_both_window_ends_are_inclusive_for_every_slot(slot):
    start, end = dca_runner.SLOTS[slot].window_bounds_utc(WEDNESDAY)
    on_start = BrokerOrder("CAT", "buy", 2.0, start, placed_agent="agentic")
    on_end = BrokerOrder("ANET", "buy", 2.0, end, placed_agent="agentic")
    just_before = BrokerOrder("AAA", "buy", 2.0, start - timedelta(seconds=1), placed_agent=None)
    just_after = BrokerOrder("BBB", "buy", 2.0, end + timedelta(seconds=1), placed_agent=None)
    out = classify([on_start, on_end, just_before, just_after], slot)
    assert out["status"] == "COMPATIBLE_CONTINUE" and out["already_covered_tickers"] == ["ANET", "CAT"]


def test_the_window_comes_from_dca_runner_not_a_copy():
    # 11:26 is inside the 11:30 slot's window only because dca_runner.SLOTS opens it at 11:25.
    assert dca_runner.SLOTS["1130"].window_start_et == (11, 25)
    assert classify([order("CAT", "11:26", "agentic")])["status"] == "COMPATIBLE_CONTINUE"


def test_an_unknown_slot_is_refused():
    with pytest.raises(ValueError):
        primary_gates.classify_window_orders([], "9999", WEDNESDAY)


def test_a_utc_timestamp_is_judged_by_its_et_instant():
    # 16:30Z on 2026-11-18 is 11:30 EST, inside the 11:30 slot's window.
    utc_order = BrokerOrder("CAT", "buy", 2.0, datetime(2026, 11, 18, 16, 30, tzinfo=timezone.utc), placed_agent="agentic")
    assert classify([utc_order])["status"] == "COMPATIBLE_CONTINUE"


# --------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------


def _row(index: int, symbol: str, hhmm: str, agent, *, state: str = "filled") -> dict:
    created = at(hhmm).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"id": f"order-{index}", "symbol": symbol, "side": "buy", "state": state, "created_at": created,
           "dollar_based_amount": {"amount": "2.00"}}
    if agent is not None:
        row["placed_agent"] = agent
    return row


def _envelope(tmp_path: Path, rows: list, *, account: str = ACCOUNT, day: date = WEDNESDAY) -> Path:
    path = tmp_path / f"orders_pages_{day.isoformat()}_1130.json"
    path.write_text(json.dumps({"account_number": account, "trading_day": day.isoformat(),
                                "pages": [{"orders": rows, "next": None}]}), encoding="utf-8")
    return path


def _run(tmp_path: Path, rows: list, *extra: str, env_account: str | None = ACCOUNT, envelope_account: str = ACCOUNT,
         envelope_day: date = WEDNESDAY, slot: str = "1130") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"}
    env["DCA_DISABLE_DOTENV"] = "1"  # hermetic: never read the operator's .env
    if env_account is not None:
        env["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] = env_account
    return subprocess.run(
        [sys.executable, "-m", "scripts.primary_gates", "gate4", "--slot", slot, "--trading-day", WEDNESDAY.isoformat(),
         "--orders-pages", str(_envelope(tmp_path, rows, account=envelope_account, day=envelope_day)), *extra],
        cwd=REPO, capture_output=True, text=True, timeout=30, env=env,
    )


def _doc(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stdout)


def test_cli_clear_exits_zero(tmp_path):
    done = _run(tmp_path, [_row(1, "CAT", "11:30", "user")])
    assert done.returncode == 0 and _doc(done)["status"] == "CLEAR"


def test_cli_compatible_continue_exits_zero_and_prints_the_covered_tickers(tmp_path):
    done = _run(tmp_path, [_row(1, "CAT", "11:30", "agentic"), _row(2, "ANET", "11:31", "agentic")])
    doc = _doc(done)
    assert done.returncode == 0 and doc["status"] == "COMPATIBLE_CONTINUE"
    assert doc["already_covered_tickers"] == ["ANET", "CAT"]


def test_cli_rogue_aborts_with_exit_one(tmp_path):
    done = _run(tmp_path, [_row(1, "CAT", "11:30", None)])
    assert done.returncode == 1 and _doc(done)["status"] == "ABORT"


def test_cli_an_envelope_saved_for_another_day_fails_closed(tmp_path):
    done = _run(tmp_path, [], envelope_day=WEDNESDAY - timedelta(days=1))
    assert done.returncode == 1 and _doc(done)["status"] == "ABORT"


def test_cli_an_envelope_for_another_account_fails_closed_and_prints_neither_number(tmp_path):
    done = _run(tmp_path, [_row(1, "CAT", "11:30", "user")], envelope_account=OTHER_ACCOUNT)
    assert done.returncode == 1 and _doc(done)["status"] == "ABORT"
    assert ACCOUNT not in done.stdout + done.stderr and OTHER_ACCOUNT not in done.stdout + done.stderr


@pytest.mark.parametrize("bad", [None, "", "not-an-account", "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"])
def test_cli_an_unusable_environment_account_aborts_naming_the_account(tmp_path, bad):
    done = _run(tmp_path, [], env_account=bad)
    doc = _doc(done)
    assert done.returncode == 1 and doc["status"] == "ABORT" and "account" in doc["reason"].lower()


def test_cli_a_malformed_row_fails_closed(tmp_path):
    bad = _row(1, "CAT", "11:30", "agentic")
    del bad["state"]
    done = _run(tmp_path, [bad])
    assert done.returncode == 1 and _doc(done)["status"] == "ABORT"


def test_cli_the_clear_path_never_prints_the_account_number(tmp_path):
    done = _run(tmp_path, [_row(1, "CAT", "11:30", "agentic")])
    assert ACCOUNT not in done.stdout + done.stderr


def test_cli_an_unknown_slot_is_a_usage_error(tmp_path):
    done = _run(tmp_path, [], slot="9999")
    assert done.returncode == 2 and "CLEAR" not in done.stdout
