"""Tests for the backup planner's safety gate: ``compute --market-data`` (slot 1530 first).

Written test-first, no implementation yet. Companion to ``test_backup_planner.py``, which pins the
quotes-only ``compute`` that this gate replaces for go-live.

The gap: ``compute --quotes`` applies only the price ladder, so it can plan a ticker the primary would
have rejected (``docs/adr-plan-first-backup-executor.md``, design gap 2), and ``finalize`` therefore never
lets a computed plan place. The fix is to run the primary's OWN Gate 5 on the same kind of payload, not to
write a second signal. For 15:30 that is ``scripts.generate_buy_candidates`` (the stream whose engine
``near_close_dca.evaluate_source_signal`` really is). The six other slots use the executor classes in
``scripts/stream_candidates.py``; they are a follow-up slice, so those slots fail closed here instead of
being scored by the wrong engine (the generator's own docstring warns against exactly that).

Contract:

* ``compute --market-data FILE --deployed-last-7d-usd N`` for slot 1530. ``--quotes`` and ``--market-data``
  are mutually exclusive and one is required. The market-data file is what ``assemble_market_data`` writes.
* A ticker becomes an order only if the generator reconciles it to BUY and ``dca_runner.plan_orders`` would
  accept it. MANUAL_REVIEW, NO_DATA, provider errors, planner-incompatible rows and tickers already bought in
  this slot's window never become orders.
* Notional, multiplier and drop come from the generator's decision, not recomputed here, so the backup can
  never size differently from the primary.
* The kill switch is the primary's: a large trailing-7-day figure caps the multiplier at 1x. The figure is
  required (a missing one would silently assume zero), finite and non-negative.
* Caps are all-or-nothing against a fresh pull, same as the quotes path (D4).
* The plan is still ``generator: backup`` and ``finalize`` still returns ``SHADOW_ONLY``: this change adds the
  gate, it does not switch placement on.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_data_fixtures import DAY, market_data, snapshot  # noqa: E402

import dca_config  # noqa: E402
from scripts import backup_planner  # noqa: E402
from scripts import generate_buy_candidates as gbc  # noqa: E402

SLOT = "1530"
NOW = "2026-10-07T15:31:00-04:00"
ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
DECISION = datetime(2026, 10, 7, 15, 30, tzinfo=ZoneInfo("America/New_York"))
TAPE = {"VT": (100.0, 97.0), "MSFT": (100.0, 98.5), "AAPL": (100.0, 100.0)}  # VT -3%, MSFT -1.5%, AAPL flat


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)


def _pages(rows=()):
    return {"account_number": ACCOUNT, "trading_day": DAY, "pages": [{"orders": list(rows)}]}


def _raw_buy(symbol, *, amount="2.000000", created=f"{DAY}T15:31:00+00:00", ref=None):
    return {"id": f"id-{symbol}-{created}", "ref_id": ref, "symbol": symbol, "side": "buy", "type": "market", "state": "filled",
            "dollar_based_amount": {"amount": amount, "currency_code": "USD"}, "time_in_force": "gfd",
            "market_hours": "regular_hours", "trigger": "immediate", "placed_agent": "agentic", "created_at": created}


def _gate(tmp_path, capsys, data, *, rows=(), slot=SLOT, deployed="0", bp="1000", extra=()):
    argv = ["compute", "--trading-day", DAY, "--slot", slot, "--market-data", _write(tmp_path, "market.json", data),
            "--orders-pages", _write(tmp_path, "pages.json", _pages(rows)), "--now", NOW, "--buying-power", bp,
            "--deployed-last-7d-usd", deployed, *extra]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _orders(report):
    return {o["symbol"]: o for o in report["plan"]["orders"]}


def _primary_buys(config, data, **kwargs):
    """What the primary's generator itself decides on the same payload."""
    rows = gbc.generate_candidates(
        slot="1530ET", now=DECISION, config=config, market_data_by_source=data["sources"],
        source_errors=data["source_errors"], ticker_errors=data["ticker_errors"], **kwargs,
    )
    return {r["ticker"]: r for r in rows if r["action"] == "BUY"}


# ---------------------------------------------------------------------------
# It is the primary's decision, not a second one
# ---------------------------------------------------------------------------


def test_a_buy_becomes_an_order_sized_exactly_as_the_primarys_generator_sized_it(tmp_path, capsys, config):
    data = market_data(TAPE)
    code, report = _gate(tmp_path, capsys, data)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert report["plan"]["generator"] == "backup" and report["plan"]["slot"] == SLOT
    primary = _primary_buys(config, data)
    assert set(_orders(report)) == set(primary) and primary
    for symbol, order in _orders(report).items():
        assert order["notional_usd"] == primary[symbol]["suggested_amount_usd"]
        assert order["multiplier"] == primary[symbol]["buy_multiplier"]


def test_a_flat_ticker_is_not_an_order(tmp_path, capsys):
    _, report = _gate(tmp_path, capsys, market_data(TAPE))
    assert "AAPL" not in _orders(report)


def test_orders_are_in_the_primarys_allocation_priority(tmp_path, capsys):
    _, report = _gate(tmp_path, capsys, market_data(TAPE))
    symbols = [o["symbol"] for o in report["plan"]["orders"]]
    assert symbols.index("VT") < symbols.index("MSFT")  # bigger drop and multiplier first


# ---------------------------------------------------------------------------
# What the quotes-only path could not catch
# ---------------------------------------------------------------------------


def test_a_provider_error_on_a_ticker_blocks_its_buy_while_others_proceed(tmp_path, capsys):
    data = market_data(TAPE)
    data["ticker_errors"]["robinhood"] = ["VT"]
    _, report = _gate(tmp_path, capsys, data)
    assert "VT" not in _orders(report) and "MSFT" in _orders(report)


def test_a_failed_robinhood_fetch_buys_nothing(tmp_path, capsys):
    data = market_data(TAPE)
    data["source_errors"] = ["robinhood"]
    code, report = _gate(tmp_path, capsys, data)
    assert report["plan"] is None or report["plan"]["orders"] == []
    assert code in (0, 2)


def test_finance_buy_with_robinhood_not_buying_is_manual_review_not_an_order(tmp_path, capsys):
    data = market_data({"VT": (100.0, 100.0)}, finance={"VT": snapshot(100.0, 97.0)})
    _, report = _gate(tmp_path, capsys, data)
    assert report["plan"] is None or "VT" not in _orders(report)


def test_incomplete_bars_are_a_data_error_not_a_buy(tmp_path, capsys):
    data = market_data(TAPE)
    entry = data["sources"]["robinhood"]["VT"]
    entry["intraday_bars"] = entry["intraday_bars"][-2:]  # no lookback bar, no coverage from 09:30
    _, report = _gate(tmp_path, capsys, data)
    assert "VT" not in _orders(report)


def test_a_stale_quote_falls_back_to_the_bar_close_rather_than_being_trusted(tmp_path, capsys, config):
    data = market_data(TAPE)
    data["sources"]["robinhood"]["VT"]["quote"]["as_of"] = "2026-10-07T11:00:00-04:00"
    data["sources"]["robinhood"]["VT"]["quote"]["price"] = 50.0  # a stale, absurd quote must not size the buy
    _, report = _gate(tmp_path, capsys, data)
    assert _orders(report)["VT"]["decision_price"] != 50.0
    assert _orders(report)["VT"]["notional_usd"] == _primary_buys(config, data)["VT"]["suggested_amount_usd"]


# ---------------------------------------------------------------------------
# The primary's risk controls carry over
# ---------------------------------------------------------------------------


def test_the_kill_switch_caps_every_multiplier_at_1x(tmp_path, capsys):
    _, normal = _gate(tmp_path, capsys, market_data(TAPE), deployed="0")
    _, tripped = _gate(tmp_path, capsys, market_data(TAPE), deployed="1e6")
    assert _orders(normal)["VT"]["multiplier"] > 1.0
    assert all(o["multiplier"] <= 1.0 for o in tripped["plan"]["orders"]) and tripped["plan"]["orders"]


@pytest.mark.parametrize("value", ["", "abc", "-1", "nan", "inf"])
def test_a_missing_or_unusable_deployment_figure_fails_closed(tmp_path, capsys, value):
    code, report = _gate(tmp_path, capsys, market_data(TAPE), deployed=value)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)


def test_the_deployment_figure_is_required(tmp_path):
    argv = ["compute", "--trading-day", DAY, "--slot", SLOT, "--market-data", _write(tmp_path, "m.json", market_data(TAPE)),
            "--orders-pages", _write(tmp_path, "p.json", _pages()), "--now", NOW, "--buying-power", "1000"]
    with pytest.raises(SystemExit) as exc:
        backup_planner.main(argv)
    assert exc.value.code == 2


def test_a_ticker_bought_in_this_slots_window_is_excluded_not_resized(tmp_path, capsys):
    in_window = _raw_buy("VT", created=f"{DAY}T19:31:00+00:00")  # 15:31 ET
    _, report = _gate(tmp_path, capsys, market_data(TAPE), rows=[in_window])
    assert "VT" not in _orders(report) and "MSFT" in _orders(report)


def test_a_breach_of_the_daily_cap_plans_nothing(tmp_path, capsys):
    spent = _raw_buy("NVDA", amount="179.500000", created=f"{DAY}T13:40:00+00:00")
    code, report = _gate(tmp_path, capsys, market_data(TAPE), rows=[spent])
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


def test_buying_power_is_still_required_and_checked(tmp_path, capsys):
    code, report = _gate(tmp_path, capsys, market_data(TAPE), bp="0.50")
    assert (code, report["plan"]) == (2, None)


# ---------------------------------------------------------------------------
# Scope: 15:30 here; the stream slots are covered in test_backup_planner_streams.py
# ---------------------------------------------------------------------------


def test_a_buy_the_primarys_gate_6_would_withhold_is_not_planned(tmp_path, capsys, monkeypatch):
    """`planner_incompatible_tickers`: the 15:30 generator holds back rows `dca_runner.plan_orders` would refuse."""
    import dca_runner

    monkeypatch.setattr(dca_runner, "_candidate_multiplier_notional_mismatch", lambda cand: cand.ticker == "VT")
    _, report = _gate(tmp_path, capsys, market_data(TAPE))
    assert "VT" not in _orders(report) and "MSFT" in _orders(report)


def test_calibrated_buys_are_not_falsely_withheld_the_check_sees_their_calibration_bounds(tmp_path, capsys):
    """A bare 4-field row is judged against the global ladder and would flag every calibrated ticker; the planner
    passes the bounds, as the stream documents do, so a normal calibrated BUY is planned."""
    _, report = _gate(tmp_path, capsys, market_data(TAPE))
    assert {"VT", "MSFT"} <= set(_orders(report))


def test_quotes_and_market_data_are_mutually_exclusive_and_one_is_required(tmp_path, capsys):
    base = ["compute", "--trading-day", DAY, "--slot", SLOT, "--orders-pages", _write(tmp_path, "p.json", _pages()),
            "--now", NOW, "--buying-power", "1000"]
    both = base + ["--quotes", _write(tmp_path, "q.json", {"quotes": []}), "--market-data", _write(tmp_path, "m.json", market_data(TAPE)),
                   "--deployed-last-7d-usd", "0"]
    for argv in (both, base):
        with pytest.raises(SystemExit) as exc:
            backup_planner.main(argv)
        assert exc.value.code == 2
        capsys.readouterr()


@pytest.mark.parametrize("text", ["", "{oops", "[]", "null", "{}", '{"sources": []}'])
def test_corrupt_market_data_fails_closed_with_one_json_document(tmp_path, capsys, text):
    code, report = _gate(tmp_path, capsys, text)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None) and report["errors"]


# ---------------------------------------------------------------------------
# The gate does not switch placement on
# ---------------------------------------------------------------------------


def test_a_gated_plan_is_still_shadow_only_at_finalize(tmp_path, capsys):
    _, report = _gate(tmp_path, capsys, market_data(TAPE))
    plan = report["plan"]
    fresh = {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]}
    spreads = {s: {"bid": p * 0.999, "ask": p * 1.001} for s, p in fresh.items()}
    code = backup_planner.main([
        "finalize", "--trading-day", DAY, "--slot", SLOT, "--plan", _write(tmp_path, "plan.json", plan),
        "--orders-pages", _write(tmp_path, "pages.json", _pages()), "--fresh-quotes", _write(tmp_path, "fresh.json", fresh),
        "--buying-power", "1000", "--fresh-spreads", _write(tmp_path, "spreads.json", spreads), "--now", NOW,
    ])
    out = json.loads(capsys.readouterr().out)
    assert (code, out["status"], out["to_place"]) == (0, "SHADOW_ONLY", [])
    assert {o["symbol"] for o in out["would_place"]} == {o["symbol"] for o in plan["orders"]}
    assert backup_planner.COMPUTE_PLACEMENT_ENABLED is False


def test_the_quotes_only_path_is_unchanged(tmp_path, capsys):
    quotes = {"quotes": [{"symbol": "VT", "prior_close": 1000.0, "prior_low": None, "decision_price": 950.0}]}
    code = backup_planner.main(["compute", "--trading-day", DAY, "--slot", "1130", "--quotes", _write(tmp_path, "q.json", quotes),
                                "--orders-pages", _write(tmp_path, "p.json", _pages()), "--now", NOW, "--buying-power", "1000"])
    assert (code, json.loads(capsys.readouterr().out)["status"]) == (0, "VALID_PLAN")


# ---------------------------------------------------------------------------
# The 7-day figure derived from the same broker history (so the task never sums anything itself)
# ---------------------------------------------------------------------------

KILL_SWITCH_THRESHOLD_USD = 1440.0  # 0.4 x the $3,600 monthly allocation (config/budgets.json risk_guard)


def test_auto_derives_the_figure_from_the_order_history_and_trips_the_kill_switch_above_the_threshold(tmp_path, capsys):
    """$1,500 bought six days ago is inside the trailing 7 days but outside this ISO week, so it caps nothing else."""
    big = _raw_buy("NVDA", amount="1500.000000", created="2026-10-01T15:00:00+00:00")
    _, tripped = _gate(tmp_path, capsys, market_data(TAPE), rows=[big], deployed="auto")
    assert tripped["plan"]["orders"] and all(o["multiplier"] <= 1.0 for o in tripped["plan"]["orders"])


def test_auto_leaves_the_kill_switch_off_below_the_threshold(tmp_path, capsys):
    small = _raw_buy("NVDA", amount=f"{KILL_SWITCH_THRESHOLD_USD - 500:.6f}", created="2026-10-01T15:00:00+00:00")
    _, report = _gate(tmp_path, capsys, market_data(TAPE), rows=[small], deployed="auto")
    assert _orders(report)["VT"]["multiplier"] > 1.0


def test_auto_ignores_buys_older_than_seven_days(tmp_path, capsys):
    old = _raw_buy("NVDA", amount="5000.000000", created="2026-09-29T15:00:00+00:00")
    _, report = _gate(tmp_path, capsys, market_data(TAPE), rows=[old], deployed="auto")
    assert _orders(report)["VT"]["multiplier"] > 1.0


def test_auto_matches_the_same_figure_passed_explicitly(tmp_path, capsys):
    big = _raw_buy("NVDA", amount="1500.000000", created="2026-10-01T15:00:00+00:00")
    _, automatic = _gate(tmp_path, capsys, market_data(TAPE), rows=[big], deployed="auto")
    _, explicit = _gate(tmp_path, capsys, market_data(TAPE), rows=[big], deployed="1500")
    assert automatic["plan"]["orders"] == explicit["plan"]["orders"]


def test_auto_with_an_unusable_committed_amount_fails_closed(tmp_path, capsys):
    broken = _raw_buy("NVDA", amount="NaN", created="2026-10-01T15:00:00+00:00")
    code, report = _gate(tmp_path, capsys, market_data(TAPE), rows=[broken], deployed="auto")
    assert (code, report["plan"]) == (2, None) and report["errors"]


def test_the_prior_week_buys_do_not_count_toward_this_weeks_cap_but_do_toward_the_figure(tmp_path, capsys):
    """Both facts at once: the fetch window is now seven days wide, the weekly cap is still the ISO week."""
    last_week = _raw_buy("NVDA", amount="899.000000", created="2026-10-01T15:00:00+00:00")
    code, report = _gate(tmp_path, capsys, market_data(TAPE), rows=[last_week], deployed="auto")
    assert code == 0 and report["plan"]["orders"]


# ---------------------------------------------------------------------------
# Poll 2 re-validates the kill switch (Copilot's second review on #1624, finding 3; written test-first)
# ---------------------------------------------------------------------------


def _finalize(tmp_path, capsys, plan, *, rows=(), now=NOW):
    """finalize on a computed plan with healthy fresh quotes and spreads, so only the poll-2 guard can differ."""
    fresh = {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]}
    spreads = {s: {"bid": p * 0.999, "ask": p * 1.001} for s, p in fresh.items()}
    argv = [
        "finalize", "--trading-day", DAY, "--slot", SLOT, "--plan", _write(tmp_path, "plan.json", plan),
        "--orders-pages", _write(tmp_path, "pages2.json", _pages(rows)), "--fresh-quotes", _write(tmp_path, "fresh.json", fresh),
        "--buying-power", "1000", "--fresh-spreads", _write(tmp_path, "spreads.json", spreads), "--now", now,
    ]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_spend_added_between_the_polls_that_trips_the_kill_switch_aborts_an_over_1x_plan(tmp_path, capsys):
    """Poll 1 had no spend, so the plan was sized above 1x; poll 2 shows $1,500 inside the trailing seven days."""
    _, planned = _gate(tmp_path, capsys, market_data(TAPE), deployed="auto")
    assert any(o["multiplier"] > 1.0 for o in planned["plan"]["orders"])
    big = _raw_buy("NVDA", amount="1500.000000", created="2026-10-01T15:00:00+00:00")
    code, report = _finalize(tmp_path, capsys, planned["plan"], rows=[big])
    assert code == 2 and report["status"] == "ABORT" and report["to_place"] == []
    assert [e["code"] for e in report["errors"]] == ["kill_switch_engaged_since_plan"]


def test_an_unchanged_poll_2_still_finalizes_as_shadow_only(tmp_path, capsys):
    _, planned = _gate(tmp_path, capsys, market_data(TAPE), deployed="auto")
    code, report = _finalize(tmp_path, capsys, planned["plan"])
    assert (code, report["status"], report["to_place"]) == (0, "SHADOW_ONLY", [])


def test_spend_below_the_threshold_between_the_polls_does_not_abort(tmp_path, capsys):
    _, planned = _gate(tmp_path, capsys, market_data(TAPE), deployed="auto")
    small = _raw_buy("NVDA", amount=f"{KILL_SWITCH_THRESHOLD_USD - 500:.6f}", created="2026-10-01T15:00:00+00:00")
    code, report = _finalize(tmp_path, capsys, planned["plan"], rows=[small])
    assert (code, report["status"]) == (0, "SHADOW_ONLY")


def test_a_plan_already_capped_at_1x_is_not_aborted_when_the_switch_was_tripped_at_poll_1_too(tmp_path, capsys):
    big = _raw_buy("NVDA", amount="1500.000000", created="2026-10-01T15:00:00+00:00")
    _, planned = _gate(tmp_path, capsys, market_data(TAPE), rows=[big], deployed="auto")
    assert planned["plan"]["orders"] and all(o["multiplier"] <= 1.0 for o in planned["plan"]["orders"])
    code, report = _finalize(tmp_path, capsys, planned["plan"], rows=[big])
    assert (code, report["status"]) == (0, "SHADOW_ONLY")


def test_an_unusable_amount_in_poll_2_fails_closed_instead_of_reading_as_no_spend(tmp_path, capsys):
    _, planned = _gate(tmp_path, capsys, market_data(TAPE), deployed="auto")
    broken = _raw_buy("NVDA", amount="NaN", created="2026-10-01T15:00:00+00:00")
    code, report = _finalize(tmp_path, capsys, planned["plan"], rows=[broken])
    assert code == 2 and report["status"] == "ABORT" and report["to_place"] == []


def test_finalize_of_a_computed_plan_refuses_a_now_on_another_et_day(tmp_path, capsys):
    """The window's upper bound is --now: a --now on an earlier day would classify today's orders as future."""
    _, planned = _gate(tmp_path, capsys, market_data(TAPE), deployed="auto")
    code, report = _finalize(tmp_path, capsys, planned["plan"], now="2026-10-06T10:00:00-04:00")
    assert code == 2 and [e["code"] for e in report["errors"]] == ["now_not_trading_day"]
