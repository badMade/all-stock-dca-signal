"""Tests for ``scripts/primary_gates.py`` (written before the implementation; derived from expected behavior).

Why: the 2026-11-13 11:30 primary run started at 11:39:25 ET and placed its first order at about 11:44:49,
roughly five minutes of gates, because every gate was walked by the model one tool turn at a time. The delayed
backup, which is script-driven, then reached the same tickers first and CAT was bought twice. The gates that need
only a clock, two config files and already-fetched order rows are deterministic, so one command should settle
them in well under a second and print one JSON verdict the prompt acts on.

Scope (operator approval, 2026-11-13): Gates 0, 1 and 3 (``clock``), the account pin, and the Gate 6 cap
arithmetic (``budget``). Gate 4's rogue/compatible classification stays in the prompt; it needs judgement the
prompt owns and is not part of this change. Per-order revalidation is KEPT as is (operator decision).

Contract:
  * ``evaluate_clock(slot_id, now)`` -> dict with ``status`` in PROCEED / END_SILENTLY / ABORT, ``gate``
    (0, 1, 3 or None), ``reason`` and ``now_et`` (offset-aware ISO). Gate order is 0, then 1, then 3, as in the
    prompt. A naive ``now`` raises ValueError. A date the calendar does not cover is an ABORT, never a PROCEED.
  * ``evaluate_budget(...)`` -> dict with remaining dollars and orders, ``spendable_usd`` and a status. Never
    negative, never guesses buying power: an unconfirmed value is an ABORT.
  * ``account_pin(raw)`` -> dict; the unexpanded placeholder, an empty value and a non-digit value fail closed.
  * CLI: exit 0 for PROCEED and END_SILENTLY, 1 for ABORT, 2 for a usage error (including a naive ``--now``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, datetime
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_runner
from dca_runner import BrokerOrder
from scripts import primary_gates

REPO = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

MONDAY = "2026-10-19"  # inside the approved window, a normal weekday
WEDNESDAY = "2026-10-21"


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET)


# --------------------------------------------------------------------------------------------------------------
# Gates 0, 1, 3
# --------------------------------------------------------------------------------------------------------------


def test_clock_proceeds_inside_the_1130_window():
    out = primary_gates.evaluate_clock("1130", at(MONDAY, "11:35"))
    assert out["status"] == "PROCEED" and out["gate"] is None


def test_clock_proceeds_for_the_actual_late_start_of_2026_11_13():
    # 11:39:25 ET is nine minutes after nominal and still inside the 11:30-12:00 acceptance window.
    now = datetime.fromisoformat(f"{MONDAY}T11:39:25").replace(tzinfo=ET)
    assert primary_gates.evaluate_clock("1130", now)["status"] == "PROCEED"


def test_clock_converts_a_utc_instant_to_et_before_judging_the_hour():
    # 15:39Z on 2026-10-19 is 11:39 EDT. The prompt's CONVERSION rule: never trust the host wall clock.
    now = datetime.fromisoformat(f"{MONDAY}T15:39:00").replace(tzinfo=UTC_TZ)
    out = primary_gates.evaluate_clock("1130", now)
    assert out["status"] == "PROCEED"
    assert out["now_et"].endswith("-04:00")


@pytest.mark.parametrize("hhmm", ["11:24", "12:00", "12:01", "03:00"])
def test_clock_aborts_gate_1_outside_the_window_and_the_close_is_exclusive(hhmm):
    out = primary_gates.evaluate_clock("1130", at(MONDAY, hhmm))
    assert (out["status"], out["gate"]) == ("ABORT", 1)
    assert out["reason"]


def test_clock_ends_silently_on_a_weekend_at_gate_0():
    out = primary_gates.evaluate_clock("1130", at("2026-10-17", "11:35"))
    assert (out["status"], out["gate"]) == ("END_SILENTLY", 0)


def test_clock_ends_silently_on_a_listed_holiday_at_gate_0():
    out = primary_gates.evaluate_clock("1130", at("2026-11-26", "11:35"))  # Thanksgiving
    assert (out["status"], out["gate"]) == ("END_SILENTLY", 0)


def test_clock_gate_0_is_decided_before_gate_1():
    # A Saturday at 03:00 is out of window AND closed; the prompt orders market day first.
    out = primary_gates.evaluate_clock("1130", at("2026-10-17", "03:00"))
    assert (out["status"], out["gate"]) == ("END_SILENTLY", 0)


def test_clock_early_close_day_does_not_stop_the_1130_slot():
    out = primary_gates.evaluate_clock("1130", at("2026-11-27", "11:35"))  # early close 13:00, slot is before it
    assert out["status"] == "PROCEED"


def test_clock_early_close_day_ends_the_1330_slot_silently():
    out = primary_gates.evaluate_clock("1330", at("2026-11-27", "13:35"))
    assert (out["status"], out["gate"]) == ("END_SILENTLY", 0)


def test_clock_aborts_gate_3_outside_the_approved_trading_window(monkeypatch):
    real = dca_runner._read_config

    def fake(name: str) -> dict:
        cfg = dict(real(name))
        if name == "trading_window.json":
            cfg.update(start_date="2026-09-01", end_date="2026-10-16")
        return cfg

    monkeypatch.setattr(dca_runner, "_read_config", fake)
    out = primary_gates.evaluate_clock("1130", at(MONDAY, "11:35"))
    assert (out["status"], out["gate"]) == ("ABORT", 3)


def test_clock_aborts_when_the_calendar_does_not_cover_the_year():
    out = primary_gates.evaluate_clock("1130", at("2029-03-05", "11:35"))
    assert (out["status"], out["gate"]) == ("ABORT", 0)
    assert "calendar" in out["reason"].lower()


def test_clock_rejects_a_naive_now():
    with pytest.raises(ValueError):
        primary_gates.evaluate_clock("1130", datetime.fromisoformat(f"{MONDAY}T11:35:00"))


def test_clock_rejects_an_unknown_slot():
    with pytest.raises(ValueError):
        primary_gates.evaluate_clock("9999", at(MONDAY, "11:35"))


def test_clock_agrees_with_dca_runner_for_every_slot_boundary():
    # One source of truth: the script may not carry its own copy of the window rules.
    for slot_id, spec in dca_runner.SLOTS.items():
        for hhmm in (spec.window_start_et, spec.window_end_et):
            now = datetime.combine(date.fromisoformat(MONDAY), datetime.min.time().replace(hour=hhmm[0], minute=hhmm[1]), tzinfo=ET)
            expected = dca_runner.gate_time_sanity(now, spec)
            assert (primary_gates.evaluate_clock(slot_id, now)["status"] == "PROCEED") is expected


def test_clock_settles_all_three_gates_in_well_under_a_second():
    start = time.perf_counter()
    primary_gates.evaluate_clock("1130", at(MONDAY, "11:35"))
    assert time.perf_counter() - start < 1.0


# --------------------------------------------------------------------------------------------------------------
# Account pin
# --------------------------------------------------------------------------------------------------------------


def test_account_pin_accepts_a_digit_account_number():
    out = primary_gates.account_pin("999000111")  # conftest.py's synthetic value
    assert out["ok"] is True and out["account_number"] == "999000111"


@pytest.mark.parametrize("raw", [None, "", "   ", "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", "not-an-account", "12 34"])
def test_account_pin_fails_closed_on_anything_else(raw):
    out = primary_gates.account_pin(raw)
    assert out["ok"] is False and "account_number" not in out and out["reason"]


# --------------------------------------------------------------------------------------------------------------
# Gate 6 cap arithmetic
# --------------------------------------------------------------------------------------------------------------


def _buy(day: str, hhmm: str, amount: float, state: str = "filled", executed: float | None = None) -> BrokerOrder:
    return BrokerOrder(
        symbol="CAT",
        side="buy",
        dollar_amount=amount,
        created_at=at(day, hhmm),
        placed_agent="agentic",
        state=state,
        executed_dollar_amount=executed,
    )


CAPS = dict(daily_cap_usd=180.0, weekly_cap_usd=900.0, max_orders_per_day=120)


def test_budget_reports_remaining_headroom_from_broker_rows():
    orders = [_buy(MONDAY, "09:40", 50.0), _buy(WEDNESDAY, "09:40", 20.0), _buy(WEDNESDAY, "10:40", 30.0)]
    out = primary_gates.evaluate_budget(orders, date.fromisoformat(WEDNESDAY), buying_power_usd=500.0, **CAPS)
    assert out["status"] == "OK"
    assert out["day_remaining_usd"] == 130.0
    assert out["week_remaining_usd"] == 800.0
    assert out["orders_remaining"] == 118
    assert out["spendable_usd"] == 130.0


def test_budget_counts_a_cancelled_attempt_toward_orders_but_not_dollars():
    orders = [_buy(WEDNESDAY, "09:40", 10.0), _buy(WEDNESDAY, "09:41", 10.0, state="cancelled", executed=0.0)]
    out = primary_gates.evaluate_budget(orders, date.fromisoformat(WEDNESDAY), buying_power_usd=500.0, **CAPS)
    assert out["orders_remaining"] == 118
    assert out["day_remaining_usd"] == 170.0


def test_budget_spendable_is_capped_by_buying_power():
    out = primary_gates.evaluate_budget([], date.fromisoformat(WEDNESDAY), buying_power_usd=42.5, **CAPS)
    assert out["spendable_usd"] == 42.5


def test_budget_never_goes_negative_and_reports_no_headroom():
    orders = [_buy(WEDNESDAY, "09:40", 190.0)]
    out = primary_gates.evaluate_budget(orders, date.fromisoformat(WEDNESDAY), buying_power_usd=500.0, **CAPS)
    assert out["status"] == "NO_HEADROOM"
    assert out["day_remaining_usd"] == 0.0 and out["spendable_usd"] == 0.0


def test_budget_reports_no_headroom_when_the_order_count_is_spent():
    orders = [_buy(WEDNESDAY, "09:40", 0.01) for _ in range(120)]
    out = primary_gates.evaluate_budget(orders, date.fromisoformat(WEDNESDAY), buying_power_usd=500.0, **CAPS)
    assert out["status"] == "NO_HEADROOM" and out["orders_remaining"] == 0


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1.0, "12"])
def test_budget_aborts_when_buying_power_cannot_be_confirmed(bad):
    out = primary_gates.evaluate_budget([], date.fromisoformat(WEDNESDAY), buying_power_usd=bad, **CAPS)
    assert out["status"] == "ABORT" and "buying power" in out["reason"].lower()


# --------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.primary_gates", *args], cwd=REPO, capture_output=True, text=True, timeout=30
    )


def test_cli_clock_prints_one_json_document_and_exits_0_on_proceed():
    done = _run("clock", "--slot", "1130", "--now", f"{MONDAY}T11:35:00-04:00")
    assert done.returncode == 0
    assert json.loads(done.stdout)["status"] == "PROCEED"


def test_cli_clock_exits_0_on_a_silent_end_so_the_shell_does_not_alert():
    done = _run("clock", "--slot", "1130", "--now", "2026-10-17T11:35:00-04:00")
    assert done.returncode == 0
    assert json.loads(done.stdout)["status"] == "END_SILENTLY"


def test_cli_clock_exits_1_on_abort():
    done = _run("clock", "--slot", "1130", "--now", f"{MONDAY}T12:30:00-04:00")
    assert done.returncode == 1
    assert json.loads(done.stdout)["status"] == "ABORT"


def test_cli_clock_exits_2_on_a_naive_now_and_prints_no_verdict():
    done = _run("clock", "--slot", "1130", "--now", f"{MONDAY}T11:35:00")
    assert done.returncode == 2
    assert "PROCEED" not in done.stdout


def test_cli_never_prints_the_account_number(monkeypatch):
    done = subprocess.run(
        [sys.executable, "-m", "scripts.primary_gates", "clock", "--slot", "1130", "--now", f"{MONDAY}T11:35:00-04:00"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER": "999000111"},
    )
    assert "999000111" not in done.stdout + done.stderr


# --------------------------------------------------------------------------------------------------------------
# --latest-start (rollout, 2026-11-14): the 3:30 prompt text closes at 3:55 PM while dca_runner.SLOTS['1530']
# runs to 4:00 PM. The script must not loosen that, so a caller may state a LATEST start that can only tighten the
# window; it can never widen it past dca_runner's own gate.
# --------------------------------------------------------------------------------------------------------------

LATEST = clock_time(15, 55)


def test_latest_start_aborts_at_and_after_the_limit_and_proceeds_before_it():
    before = primary_gates.evaluate_clock("1530", at(MONDAY, "15:54"), latest_start=LATEST)
    at_limit = primary_gates.evaluate_clock("1530", at(MONDAY, "15:55"), latest_start=LATEST)
    assert before["status"] == "PROCEED"
    assert (at_limit["status"], at_limit["gate"]) == ("ABORT", 1)
    assert "latest" in at_limit["reason"].lower()


def test_latest_start_cannot_widen_the_slot_window():
    # 16:00 is outside SLOTS['1530'] (exclusive end); a generous latest_start must not rescue it.
    out = primary_gates.evaluate_clock("1530", at(MONDAY, "16:00"), latest_start=clock_time(16, 30))
    assert (out["status"], out["gate"]) == ("ABORT", 1)


def test_without_latest_start_the_slot_window_alone_decides():
    assert primary_gates.evaluate_clock("1530", at(MONDAY, "15:57"))["status"] == "PROCEED"


def test_latest_start_does_not_override_a_closed_market():
    out = primary_gates.evaluate_clock("1530", at("2026-10-17", "15:56"), latest_start=LATEST)
    assert (out["status"], out["gate"]) == ("END_SILENTLY", 0)


def test_cli_latest_start_flag_tightens_the_window():
    ok = _run("clock", "--slot", "1530", "--now", f"{MONDAY}T15:54:00-04:00", "--latest-start", "15:55")
    late = _run("clock", "--slot", "1530", "--now", f"{MONDAY}T15:56:00-04:00", "--latest-start", "15:55")
    assert ok.returncode == 0 and json.loads(ok.stdout)["status"] == "PROCEED"
    assert late.returncode == 1 and json.loads(late.stdout)["status"] == "ABORT"


@pytest.mark.parametrize("bad", ["3:55pm", "25:00", "15:61", "", "15"])
def test_cli_rejects_a_malformed_latest_start_with_exit_2(bad):
    done = _run("clock", "--slot", "1530", "--now", f"{MONDAY}T15:30:00-04:00", "--latest-start", bad)
    assert done.returncode == 2 and "PROCEED" not in done.stdout
