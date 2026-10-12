"""Tests for wiring ALL six stream slots into the backup planner's safety gate (stage 1).

Written test-first, no implementation yet. Companion to ``test_backup_planner_gate.py`` (slot 1530,
``scripts.generate_buy_candidates``). This file covers the slots whose Gate 5 is ``scripts/stream_candidates.py``:
market-open 0935, morning 1030, intraday 1130/1330/1430 and midday 1230.

Owner direction (2026-10-08): there is nothing special about 1030 or 1230. They differ from the others only in time.
No slot may depend on the retired 09:45 pre-flight, on the Finance connector, on Yahoo, or on a per-ticker source
exception. So the backup scores every one of the six from the Robinhood data the Perplexity task fetches:

* 0935 and the three intraday slots already do.
* 1030 is ``MORNING_SPEC`` without its pre-flight ``universe_filter``, and the backup never consults the pre-flight.
* 1230 is ``MIDDAY_SPEC`` as Robinhood-primary, the same change 09:35 already went through on 2026-09-09: default
  5-minute bars and trend length, no ``ticker_source_overrides`` (so GFUZ is scored like any other ticker).
  Stage 2 changes the primary's own specs to match; these tests keep passing unchanged when it lands.

Design (checked against the real generators when the tests were written):

* The backup runs the primary's own stream CLI path (``stream_candidates._run_with_args``) on the payload, with the
  primary's default decision anchor, and plans exactly the BUY rows of its ``cands`` list, minus rows the generator
  withholds as planner-incompatible. Nothing is re-derived, so it cannot size differently from the primary.
* An empty ``finance`` placeholder (what ``assemble_market_data`` writes) is dropped; a non-empty one is refused.
  Yahoo is never required: no payload key, prompt line or error depends on it.
* Kill-switch figure: slots whose spec requires it (``requires_deployed_last_7d_usd``, today 1230) pass it to the
  stream CLI and are capped at 1x when it trips; the others ignore it, as the primary does.
* ``universe`` prints the buyable symbols from the packaged config, so no prompt has to name a stock.
* Placement is unchanged: a computed plan is ``generator: backup`` and ``finalize`` stays ``SHADOW_ONLY``.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_data_fixtures import DAY, SLOT_TIMES, market_data, raw_bundle, snapshot  # noqa: E402

import dca_config  # noqa: E402
import dca_order_plan as dop  # noqa: E402
import dca_runner  # noqa: E402
import scripts.generate_intraday_candidates as intraday  # noqa: E402
import scripts.generate_market_open_candidates as market_open  # noqa: E402
import scripts.generate_midday_candidates as midday  # noqa: E402
import scripts.generate_morning_candidates as morning  # noqa: E402
from near_close_dca import SourceName  # noqa: E402
from scripts import assemble_market_data as amd  # noqa: E402
from scripts import backup_planner, stream_candidates  # noqa: E402

ACCOUNT = "999000111"  # synthetic; conftest assigns the same value
STREAM_SLOTS = ["0935", "1030", "1130", "1230", "1330", "1430"]
NOW = {"0935": "2026-10-07T09:56:00-04:00", "1030": "2026-10-07T10:51:00-04:00", "1130": "2026-10-07T11:51:00-04:00",
       "1230": "2026-10-07T12:51:00-04:00", "1330": "2026-10-07T13:51:00-04:00", "1430": "2026-10-07T14:51:00-04:00",
       "1530": "2026-10-07T15:46:00-04:00"}
TAPE = {"VT": (100.0, 97.0), "MSFT": (100.0, 98.5), "AAPL": (100.0, 100.0)}  # VT -3%, MSFT -1.5%, AAPL flat


def _spec(slot):
    """The primary's own spec for each slot. Stage 2 made 10:30 and 12:30 plain Robinhood-sourced specs, so the
    backup needs no adaptation of its own."""
    if slot == "0935":
        return market_open.MARKET_OPEN_SPEC
    if slot == "1030":
        return morning.MORNING_SPEC
    if slot == "1230":
        return midday.MIDDAY_SPEC
    return intraday.spec_for_slot(slot)


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)


def _pages(rows=()):
    return {"account_number": ACCOUNT, "trading_day": DAY, "pages": [{"orders": list(rows)}]}


def _raw_buy(symbol, *, amount="2.000000", created=f"{DAY}T13:40:00+00:00"):
    return {"id": f"id-{symbol}-{created}", "ref_id": None, "symbol": symbol, "side": "buy", "type": "market", "state": "filled",
            "dollar_based_amount": {"amount": amount, "currency_code": "USD"}, "time_in_force": "gfd",
            "market_hours": "regular_hours", "trigger": "immediate", "placed_agent": "agentic",
            "created_at": created}


def _plan(tmp_path, capsys, slot, data, *, rows=(), deployed="0", bp="1000"):
    argv = ["compute", "--trading-day", DAY, "--slot", slot, "--market-data", _write(tmp_path, "market.json", data),
            "--orders-pages", _write(tmp_path, "pages.json", _pages(rows)), "--now", NOW[slot], "--buying-power", bp,
            "--deployed-last-7d-usd", deployed]
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _orders(report):
    return {o["symbol"]: o for o in report["plan"]["orders"]}


def _primary_cands(tmp_path, slot, data):
    """What the primary's own stream CLI decides on the same payload, run exactly as the primary runs it."""
    stream_only = {"sources": {"robinhood": data["sources"]["robinhood"]}, "source_errors": data["source_errors"],
                   "ticker_errors": {"robinhood": data["ticker_errors"]["robinhood"]}}
    path = _write(tmp_path, "primary.json", stream_only)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        extra = ["--deployed-last-7d-usd", "0"] if _spec(slot).requires_deployed_last_7d_usd else []
        stream_candidates.run_cli(_spec(slot), ["--trading-day", DAY, "--market-data", path, *extra])
    return {row["ticker"]: row for row in json.loads(out.getvalue())["cands"]}


# ---------------------------------------------------------------------------
# It is the primary's decision, slot by slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_every_planned_order_is_exactly_the_primarys_own_candidate(tmp_path, capsys, slot):
    data = market_data(TAPE, slot=slot)
    code, report = _plan(tmp_path, capsys, slot, data)
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert report["plan"]["generator"] == "backup" and report["plan"]["slot"] == slot
    primary = _primary_cands(tmp_path, slot, data)
    assert set(_orders(report)) == set(primary) and primary
    for symbol, order in _orders(report).items():
        row = primary[symbol]
        assert order["notional_usd"] == row["desired_usd"]
        assert order["multiplier"] == pytest.approx(row["multiplier"])
        assert order["drop_pct"] == pytest.approx(row["drop_pct"])
        assert order["decision_price"] == pytest.approx(row["current_price"])
        assert order["prior_close"] == row["prior_close"]
        assert order["prior_low"] == row["prior_low"]  # None when the prior low is unknown: never fabricated
        assert order["ref_id"] == f"dca-runner-{slot}-{DAY}-{symbol}"


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_flat_ticker_is_not_an_order_and_priority_is_the_primarys(tmp_path, capsys, slot):
    _, report = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot))
    symbols = [o["symbol"] for o in report["plan"]["orders"]]
    assert "AAPL" not in symbols and symbols.index("VT") < symbols.index("MSFT")


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_per_ticker_error_marker_blocks_that_ticker_only(tmp_path, capsys, slot):
    data = market_data(TAPE, slot=slot)
    data["ticker_errors"]["robinhood"] = ["VT"]
    _, report = _plan(tmp_path, capsys, slot, data)
    assert "VT" not in _orders(report) and "MSFT" in _orders(report)


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_failed_robinhood_fetch_plans_nothing(tmp_path, capsys, slot):
    data = market_data(TAPE, slot=slot)
    data["source_errors"] = ["robinhood"]
    code, report = _plan(tmp_path, capsys, slot, data)
    assert code in (0, 2) and (report["plan"] is None or report["plan"]["orders"] == [])


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_ticker_with_stale_or_missing_bars_is_not_bought(tmp_path, capsys, slot):
    data = market_data(TAPE, slot=slot)
    data["sources"]["robinhood"]["VT"]["intraday_bars"] = data["sources"]["robinhood"]["VT"]["intraday_bars"][:2]
    _, report = _plan(tmp_path, capsys, slot, data)
    assert "VT" not in (_orders(report) if report["plan"] else {})


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_buy_the_primarys_gate_6_would_withhold_is_not_planned(tmp_path, capsys, monkeypatch, slot):
    """`planner_incompatible_tickers`: the generator holds back rows `dca_runner.plan_orders` would refuse."""
    monkeypatch.setattr(dca_runner, "_candidate_multiplier_notional_mismatch", lambda cand: cand.ticker == "VT")
    _, report = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot))
    assert "VT" not in _orders(report) and "MSFT" in _orders(report)


# ---------------------------------------------------------------------------
# Same guards as the quotes and 15:30 paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_ticker_already_bought_today_is_excluded_not_resized(tmp_path, capsys, slot):
    _, report = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), rows=[_raw_buy("VT")])
    assert "VT" not in _orders(report) and "MSFT" in _orders(report)


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_cap_breach_plans_nothing(tmp_path, capsys, slot):
    spent = _raw_buy("NVDA", amount="179.500000")
    code, report = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), rows=[spent])
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


_NEEDS_FIGURE = [slot for slot in STREAM_SLOTS if _spec(slot).requires_deployed_last_7d_usd]
_IGNORES_FIGURE = [slot for slot in STREAM_SLOTS if not _spec(slot).requires_deployed_last_7d_usd]


def test_1230_is_the_only_stream_slot_whose_producer_contract_needs_the_kill_switch_figure():
    assert _NEEDS_FIGURE == ["1230"]


@pytest.mark.parametrize("slot", _IGNORES_FIGURE)
def test_slots_whose_spec_does_not_use_the_kill_switch_figure_size_the_same_at_any_value(tmp_path, capsys, slot):
    _, low = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), deployed="0")
    _, high = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), deployed="1e6")
    assert low["plan"]["orders"] == high["plan"]["orders"]


@pytest.mark.parametrize("slot", _NEEDS_FIGURE)
def test_a_slot_whose_spec_uses_the_figure_is_capped_at_1x_when_it_trips(tmp_path, capsys, slot):
    _, normal = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), deployed="0")
    _, tripped = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), deployed="1e6")
    assert _orders(normal)["VT"]["multiplier"] > 1.0
    assert tripped["plan"]["orders"] and all(o["multiplier"] <= 1.0 for o in tripped["plan"]["orders"])


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_the_figure_is_required_on_every_market_data_run(tmp_path, slot):
    with pytest.raises(SystemExit) as exc:
        backup_planner.main(["compute", "--trading-day", DAY, "--slot", slot, "--market-data", _write(tmp_path, "m.json", market_data(TAPE, slot=slot)),
                             "--orders-pages", _write(tmp_path, "p.json", _pages()), "--now", NOW[slot], "--buying-power", "1000"])
    assert exc.value.code == 2


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_buying_power_is_still_checked(tmp_path, capsys, slot):
    code, report = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot), bp="0.50")
    assert (code, report["plan"]) == (2, None)


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_the_empty_finance_placeholder_from_the_assembler_is_accepted(tmp_path, capsys, slot):
    data = market_data(TAPE, slot=slot)
    assert data["sources"]["finance"] == {}
    code, report = _plan(tmp_path, capsys, slot, data)
    assert code == 0 and report["plan"]["orders"]


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_non_empty_finance_block_is_refused_like_the_stream_cli_refuses_it(tmp_path, capsys, slot):
    data = market_data(TAPE, slot=slot, finance={"VT": snapshot(100.0, 97.0, slot=slot)})
    code, report = _plan(tmp_path, capsys, slot, data)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None) and report["errors"]


def test_invalid_source_names_are_not_echoed_in_errors(tmp_path, capsys):
    data = market_data(TAPE, slot="1130")
    marker = "untrusted-response-content"
    data["sources"][marker] = {}
    code, report = _plan(tmp_path, capsys, "1130", data)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert marker not in json.dumps(report)
    assert report["errors"] == [{"code": "market_data_invalid", "detail": "ValueError"}]


def test_a_config_override_cannot_bypass_the_runner_config_binding(tmp_path, capsys):
    directory = tmp_path / "config"
    shutil.copytree(dca_config.get_config_dir(), directory)
    code = backup_planner.main([
        "compute", "--trading-day", DAY, "--slot", "1130", "--config-dir", str(directory),
        "--market-data", _write(tmp_path, "market.json", market_data(TAPE, slot="1130")),
        "--orders-pages", _write(tmp_path, "pages.json", _pages()), "--now", NOW["1130"],
        "--buying-power", "1000", "--deployed-last-7d-usd", "0",
    ])
    report = json.loads(capsys.readouterr().out)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert report["errors"] == [{"code": "market_data_invalid", "detail": "ValueError"}]


@pytest.mark.parametrize("slot", ["1130", "0935", "1030", "1230"])
def test_raw_responses_assemble_and_plan_end_to_end_for_a_stream_slot(tmp_path, capsys, slot):
    raw = _write(tmp_path, "raw.json", raw_bundle(TAPE, slot=slot))
    out = tmp_path / "assembled.json"
    assert amd.main(["--trading-day", DAY, "--raw", raw, "--out", str(out)]) == 0
    capsys.readouterr()
    argv = ["compute", "--trading-day", DAY, "--slot", slot, "--market-data", str(out), "--orders-pages",
            _write(tmp_path, "pages.json", _pages()), "--now", NOW[slot], "--buying-power", "1000", "--deployed-last-7d-usd", "auto"]
    code = backup_planner.main(argv)
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and "VT" in _orders(report)


# ---------------------------------------------------------------------------
# Nothing special about 1030 or 1230
# ---------------------------------------------------------------------------


def test_the_backup_has_no_preflight_to_consult(tmp_path, capsys):
    """The 09:45 pre-flight is retired: its helper no longer exists, and 10:30 plans from the payload alone."""
    import morning_dca

    assert not hasattr(morning_dca, "morning_preflight_eligible_tickers")
    code, report = _plan(tmp_path, capsys, "1030", market_data(TAPE, slot="1030"))
    assert (code, report["status"]) == (0, "VALID_PLAN") and "VT" in _orders(report)


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_no_ticker_is_special_cased_gfuz_is_scored_like_any_other(tmp_path, capsys, slot):
    """The old midday spec routed one ticker to a different source; no ticker is named in the specs now."""
    data = market_data({**TAPE, "GFUZ": (100.0, 97.0)}, slot=slot)
    code, report = _plan(tmp_path, capsys, slot, data)
    primary = _primary_cands(tmp_path, slot, data)
    assert code == 0 and "GFUZ" in primary and "GFUZ" in _orders(report)
    assert _orders(report)["GFUZ"]["notional_usd"] == primary["GFUZ"]["desired_usd"]


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_every_slot_is_scored_from_robinhood_with_no_per_ticker_exception(slot):
    """Checked on the planner's real registry, not only on this file's own copy of the spec."""
    from scripts import backup_stream_specs

    for spec in (_spec(slot), backup_stream_specs.stream_spec(slot)):
        assert spec.source == SourceName.ROBINHOOD and not spec.ticker_source_overrides
    assert backup_stream_specs.STREAM_SLOTS == tuple(STREAM_SLOTS)


def test_yahoo_is_never_required_a_plain_robinhood_payload_is_enough_for_every_slot(tmp_path, capsys):
    for slot in STREAM_SLOTS:
        data = market_data(TAPE, slot=slot)
        assert set(data["sources"]) == {"finance", "robinhood"}  # no yahoo_finance_yfinance block anywhere
        code, report = _plan(tmp_path, capsys, slot, data)
        assert code == 0 and report["plan"]["orders"], slot


# ---------------------------------------------------------------------------
# The universe lives in config; the prompts ask the planner for it
# ---------------------------------------------------------------------------


def _universe(capsys, *extra):
    code = backup_planner.main(["universe", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_universe_prints_exactly_the_buyable_symbols_from_config_with_the_plan_stamp(capsys):
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    code, report = _universe(capsys)
    assert (code, report["status"]) == (0, "OK")
    assert report["symbols"] == sorted(set(config.tickers) - set(config.excluded_tickers))
    assert report["count"] == len(report["symbols"]) > 0
    assert report["universe_hash"] == dop.universe_hash(config.tickers)  # the same stamp every plan carries


def test_universe_never_lists_an_excluded_or_whole_share_symbol(capsys):
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    _, report = _universe(capsys)
    assert not set(report["symbols"]) & (set(config.excluded_tickers) | set(getattr(config, "whole_share_tickers", ()) or ()))
    assert config.excluded_tickers, "the config under test should exclude at least one ticker"


def test_universe_is_deterministic_and_needs_no_other_input(capsys):
    assert _universe(capsys) == _universe(capsys)


def test_universe_fails_closed_on_an_unusable_config_dir(capsys, tmp_path):
    code, report = _universe(capsys, "--config-dir", str(tmp_path / "missing"))
    assert (code, report["status"]) == (2, "FAILED") and report["errors"] and "symbols" not in report


def test_the_gated_slots_are_all_seven():
    assert set(backup_planner.GATED_SLOTS) == {"0935", "1030", "1130", "1230", "1330", "1430", "1530"}


def test_every_wired_slot_has_timings_for_the_fixtures_so_the_tests_cover_it():
    assert set(SLOT_TIMES) == set(backup_planner.GATED_SLOTS)


# ---------------------------------------------------------------------------
# Placement stays off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_a_stream_plan_is_still_shadow_only_at_finalize(tmp_path, capsys, slot):
    _, report = _plan(tmp_path, capsys, slot, market_data(TAPE, slot=slot))
    plan = report["plan"]
    fresh = {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]}
    spreads = {s: {"bid": p * 0.999, "ask": p * 1.001} for s, p in fresh.items()}
    code = backup_planner.main([
        "finalize", "--trading-day", DAY, "--slot", slot, "--plan", _write(tmp_path, "plan.json", plan),
        "--orders-pages", _write(tmp_path, "pages2.json", _pages()), "--fresh-quotes", _write(tmp_path, "fresh.json", fresh),
        "--buying-power", "1000", "--fresh-spreads", _write(tmp_path, "spreads.json", spreads), "--now", NOW[slot],
    ])
    out = json.loads(capsys.readouterr().out)
    assert (code, out["status"], out["to_place"]) == (0, "SHADOW_ONLY", [])
    assert backup_planner.COMPUTE_PLACEMENT_ENABLED is False


@pytest.mark.parametrize("slot", STREAM_SLOTS)
def test_the_backup_scores_each_slot_with_the_primarys_own_spec(slot):
    """No adaptation layer: the backup cannot size differently from the primary because it is the same object."""
    from scripts import backup_stream_specs

    assert backup_stream_specs.stream_spec(slot) is _spec(slot)


def test_the_backup_has_no_pre_flight_workaround_left():
    from scripts import backup_stream_specs

    assert not hasattr(backup_stream_specs, "NoPreflightMorningExecutor")
    assert "NoPreflightMorningExecutor" not in backup_stream_specs.__all__
