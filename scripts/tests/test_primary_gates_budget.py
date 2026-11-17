"""Tests for ``primary_gates budget``: account from the environment, and ``--tier`` (written before the change).

Two follow-ups to the 2026-11-14 rollout, both inside ``scripts/primary_gates.py``:

  * KEEP THE ACCOUNT NUMBER OFF THE COMMAND LINE (AGENTS.md secrets policy). ``budget`` takes it from
    ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``, as ``clock`` already does. ``--account`` stays accepted so an existing
    caller keeps working, but the prompts stop using it. Every path fails closed and none prints the number.
  * ``--tier`` so the 3:30 prompt, which escalates through dynamic budget tiers, can use the script too. The tier is
    still CHOSEN in the prompt (by today's count of 3x-or-greater signals); the script only turns a chosen tier into
    caps, reusing ``dca_runner``'s own stress-tier rollover-reserve rule. The default stays ``normal``, so the four
    NORMAL-tier prompts do not change.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import primary_gates

REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "999000111"  # conftest.py's synthetic value
# Derived, never written as a literal: tests/test_config_invariants.py takes no account-shaped literal other than
# the synthetic ACCOUNT above (use conftest.FIXTURE_ACCOUNT_NUMBER or keep a number to 5 digits or fewer).
OTHER_ACCOUNT = str(int(ACCOUNT) + 1)
MONDAY = "2026-11-16"
WEDNESDAY = "2026-11-18"


def _row(index: int, amount: str, day: str = WEDNESDAY, state: str = "filled") -> dict:
    return {
        "id": f"order-{index}",
        "symbol": "CAT",
        "side": "buy",
        "state": state,
        "created_at": f"{day}T14:{index % 60:02d}:00Z",
        "dollar_based_amount": {"amount": amount},
        "placed_agent": "agentic",
    }


def _envelope(tmp_path: Path, rows: list[dict], account: str = ACCOUNT) -> Path:
    path = tmp_path / f"orders_pages_week_{MONDAY}_1530.json"
    payload = {"account_number": account, "trading_day": MONDAY, "pages": [{"orders": rows, "next": None}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_budget(tmp_path: Path, rows: list[dict], *extra: str, env_account: str | None = ACCOUNT,
                envelope_account: str = ACCOUNT, buying_power: str = "1000") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"}
    env["DCA_DISABLE_DOTENV"] = "1"  # hermetic: never read the operator's .env
    if env_account is not None:
        env["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] = env_account
    return subprocess.run(
        [sys.executable, "-m", "scripts.primary_gates", "budget", "--orders-pages",
         str(_envelope(tmp_path, rows, envelope_account)), "--trading-day", WEDNESDAY,
         "--buying-power", buying_power, *extra],
        cwd=REPO, capture_output=True, text=True, timeout=30, env=env,
    )


def _doc(done: subprocess.CompletedProcess) -> dict:
    return json.loads(done.stdout)


# --------------------------------------------------------------------------------------------------------------
# Account from the environment
# --------------------------------------------------------------------------------------------------------------


def test_budget_reads_the_account_from_the_environment_when_the_flag_is_omitted(tmp_path):
    done = _run_budget(tmp_path, [_row(1, "10.00")])
    assert done.returncode == 0 and _doc(done)["status"] == "OK"


def test_the_account_flag_still_works_for_an_existing_caller(tmp_path):
    done = _run_budget(tmp_path, [_row(1, "10.00")], "--account", ACCOUNT, env_account=None)
    assert done.returncode == 0 and _doc(done)["status"] == "OK"


def test_no_flag_and_no_environment_account_aborts_and_names_the_account(tmp_path):
    done = _run_budget(tmp_path, [], env_account=None)
    assert done.returncode == 2
    assert "no usable account number" in done.stderr


@pytest.mark.parametrize(
    ("bad", "exit_code"),
    # 2026-11-15 (#2323): a malformed value is now refused by scripts/account_arg.py's shape check at the command
    # line (exit 2, like an unset one) instead of reaching account_pin and printing ABORT (exit 1). Either way the
    # budget is never computed.
    [("${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", 2), ("not-an-account", 2), ("", 2), ("12 34", 2)],
)
def test_an_unusable_environment_account_aborts(tmp_path, bad, exit_code):
    done = _run_budget(tmp_path, [], env_account=bad)
    assert done.returncode == exit_code
    if exit_code == 2:
        assert "no usable account number" in done.stderr
    else:
        assert _doc(done)["status"] == "ABORT"


def test_an_envelope_for_another_account_aborts(tmp_path):
    done = _run_budget(tmp_path, [_row(1, "10.00")], envelope_account=OTHER_ACCOUNT)
    doc = _doc(done)
    assert done.returncode == 1 and doc["status"] == "ABORT"
    assert doc["reason"] == "orders_pages account does not match the environment account"


def test_budget_rejects_duplicate_json_keys_in_orders_pages(tmp_path):
    spent_row = json.dumps(_row(1, "180.00"))
    path = tmp_path / f"orders_pages_week_{MONDAY}_1530.json"
    path.write_text(
        '{"account_number": "' + ACCOUNT + '", "trading_day": "' + MONDAY + '", '
        '"pages": [{"orders": [' + spent_row + '], "next": null}], '
        '"pages": [{"orders": [], "next": null}]}',
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"}
    env["DCA_DISABLE_DOTENV"] = "1"
    env["ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"] = ACCOUNT
    done = subprocess.run(
        [sys.executable, "-m", "scripts.primary_gates", "budget", "--orders-pages", str(path),
         "--trading-day", WEDNESDAY, "--buying-power", "1000"],
        cwd=REPO, capture_output=True, text=True, timeout=30, env=env,
    )
    assert done.returncode == 1 and _doc(done)["status"] == "ABORT"



@pytest.mark.parametrize(
    ("kwargs", "extra"),
    [({}, ()), ({"envelope_account": OTHER_ACCOUNT}, ()), ({"env_account": None}, ()), ({"env_account": None}, ("--account", ACCOUNT))],
)
def test_neither_account_number_is_ever_printed(tmp_path, kwargs, extra):
    done = _run_budget(tmp_path, [_row(1, "10.00")], *extra, **kwargs)
    for text in (done.stdout, done.stderr):
        assert ACCOUNT not in text and OTHER_ACCOUNT not in text


# --------------------------------------------------------------------------------------------------------------
# --tier: resolve_tier_caps
# --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "daily", "weekly"),
    [("normal", 180.0, 900.0), ("elevated", 240.0, 1200.0), ("high_volatility", 300.0, 1500.0)],
)
def test_the_non_reserve_tiers_resolve_to_their_nominal_caps(tier, daily, weekly):
    assert primary_gates.resolve_tier_caps(tier) == (daily, weekly)


@pytest.mark.parametrize(("reserve", "weekly"), [(0, 900.0), (500, 1400.0), (900, 1800.0), (5000, 1800.0)])
def test_the_stress_tier_weekly_cap_is_min_of_nominal_and_normal_plus_reserve(reserve, weekly):
    assert primary_gates.resolve_tier_caps("stress", reserve) == (360.0, weekly)


def test_a_tier_without_a_reserve_ignores_one_that_is_given():
    assert primary_gates.resolve_tier_caps("elevated", 500) == (240.0, 1200.0)


def test_the_stress_tier_without_a_reserve_fails_closed():
    with pytest.raises(ValueError, match="rollover"):
        primary_gates.resolve_tier_caps("stress")


@pytest.mark.parametrize("bad", [-1, math.nan, math.inf, True, "5", 10**400])
def test_a_malformed_stress_reserve_is_refused(bad):
    with pytest.raises(ValueError):
        primary_gates.resolve_tier_caps("stress", bad)


def test_evaluate_budget_aborts_on_oversized_int_buying_power():
    from datetime import date

    out = primary_gates.evaluate_budget(
        [],
        date.fromisoformat(WEDNESDAY),
        buying_power_usd=10**400,
        daily_cap_usd=180.0,
        weekly_cap_usd=900.0,
        max_orders_per_day=120,
    )
    assert out["status"] == "ABORT" and "buying power" in out["reason"].lower()



@pytest.mark.parametrize("bad", ["bogus", "Stress", "NORMAL", "", "high-volatility"])
def test_an_unknown_tier_name_is_refused(bad):
    with pytest.raises(ValueError):
        primary_gates.resolve_tier_caps(bad)


# --------------------------------------------------------------------------------------------------------------
# --tier: CLI
# --------------------------------------------------------------------------------------------------------------


def test_the_default_tier_is_normal_so_the_four_normal_tier_prompts_do_not_change(tmp_path):
    doc = _doc(_run_budget(tmp_path, []))
    assert doc["tier"] == "normal" and doc["day_remaining_usd"] == 180.0 and doc["week_remaining_usd"] == 900.0


def test_the_stress_tier_uses_the_rollover_reserve(tmp_path):
    done = _run_budget(tmp_path, [], "--tier", "stress", "--weekly-rollover-reserve", "500")
    doc = _doc(done)
    assert done.returncode == 0
    assert (doc["tier"], doc["day_remaining_usd"], doc["week_remaining_usd"]) == ("stress", 360.0, 1400.0)


def test_the_stress_tier_without_a_reserve_aborts(tmp_path):
    done = _run_budget(tmp_path, [], "--tier", "stress")
    assert done.returncode == 1
    doc = _doc(done)
    assert doc["status"] == "ABORT" and "rollover" in doc["reason"].lower()


def test_an_unknown_tier_is_a_usage_error(tmp_path):
    done = _run_budget(tmp_path, [], "--tier", "bogus")
    assert done.returncode == 2 and "OK" not in done.stdout


def test_a_higher_tier_gives_headroom_where_normal_has_none(tmp_path):
    spent = [_row(i, "20.00") for i in range(1, 11)]  # $200 today: over NORMAL's $180, under ELEVATED's $240
    normal = _doc(_run_budget(tmp_path, spent))
    elevated = _doc(_run_budget(tmp_path, spent, "--tier", "elevated"))
    assert normal["status"] == "NO_HEADROOM" and normal["day_remaining_usd"] == 0.0
    assert elevated["status"] == "OK" and elevated["day_remaining_usd"] == 40.0
