"""Tests for ``scripts/backup_planner.py``, the command the Perplexity backup prompts call.

Written test-first. The contract is the table in
``claude/dca-executor-prompts/perplexity/README.md``: three subcommands (``plan``, ``compute``,
``finalize``), each printing ONE JSON document and exiting 0 when the result is usable and 2 on
anything else, in which case the prompt places nothing.

It is a thin I/O wrapper. All sizing, cap, reconciliation and validation logic lives in
``dca_order_plan`` and is tested there; these tests pin only the wiring, the fail-closed input
handling for saved broker pages, and that no secret or broker code is involved.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from pathlib import Path

import pytest

import dca_config
import dca_order_plan as dop
import dca_paths
from scripts import backup_planner

DAY = "2026-10-07"
SLOT = "1130"
NOW = "2026-10-07T11:49:30-04:00"
ACCOUNT = "999000111"  # synthetic; conftest assigns the same value


def _uuid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"backup-planner-test/{label}"))


def _raw(symbol, *, ref=None, agent="agentic", state="filled", amount="2.000000", side="buy",
         created=f"{DAY}T15:31:00+00:00"):
    """A raw ``get_equity_orders`` row, shaped like the live capture."""
    return {
        "id": _uuid(f"id/{symbol}/{created}"),
        "ref_id": ref,
        "symbol": symbol,
        "side": side,
        "type": "market",
        "state": state,
        "dollar_based_amount": {"amount": amount, "currency_code": "USD"},
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
        "trigger": "immediate",
        "placed_agent": agent,
        "created_at": created,
    }


def _envelope(rows=(), *, account=ACCOUNT, day=DAY, pages=None):
    return {"account_number": account, "trading_day": day, "pages": pages if pages is not None else [{"orders": list(rows)}]}


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)


def _survivor(ticker, buy_usd, multiplier, drop_pct):
    return {"ticker": ticker, "buy_usd": buy_usd, "multiplier": multiplier, "drop_pct": drop_pct,
            "current_price": 100.0, "prior_close": 101.2, "prior_low": 100.5}


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


def _plan(config, survivors):
    result = dop.plan_from_survivors(
        survivors, config=config, trading_day=date.fromisoformat(DAY), slot=SLOT,
        generated_at_et=datetime.fromisoformat(NOW), ladder_version=dop.current_ladder_version(),
    )
    assert result.errors == ()
    return result.plan


def _run(capsys, argv):
    code = backup_planner.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _spreads(tmp_path, spreads=None, name="spreads.json"):
    """A fresh bid/ask file; every symbol these tests use defaults to a tight, valid spread."""
    default = {s: {"bid": 99.9, "ask": 100.1} for s in ("MSFT", "AAPL", "VT", "AMD", "NVDA")}
    return _write(tmp_path, name, default if spreads is None else spreads)


def _final_inputs(tmp_path, *, bp="1000", spreads=None):
    """The buying-power and spread inputs `finalize` now requires (review gaps 3 and 4)."""
    return ["--buying-power", bp, "--fresh-spreads", _spreads(tmp_path, spreads)]


def _common(extra=()):
    return ["--trading-day", DAY, "--slot", SLOT, *extra]


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_returns_a_valid_mac_plan(tmp_path, capsys, monkeypatch, config):
    monkeypatch.setattr(dca_paths, "cron_tracking_root", lambda: str(tmp_path))
    plan = _plan(config, [_survivor("MSFT", 4.0, 2.0, 1.2), _survivor("AAPL", 2.0, 1.0, 0.7)])
    dop.write_plan(dop.order_plan_path(date.fromisoformat(DAY), SLOT), plan) if dop.order_plan_path(
        date.fromisoformat(DAY), SLOT).parent.mkdir(parents=True, exist_ok=True) is None else None
    code, report = _run(capsys, ["plan", "--source", "mac_plan", *_common()])
    assert code == 0
    assert report["status"] == "VALID_PLAN"
    assert report["errors"] == []
    assert [o["symbol"] for o in report["plan"]["orders"]] == ["MSFT", "AAPL"]


def test_plan_reads_an_explicit_plan_file(tmp_path, capsys, config):
    path = tmp_path / "plan.json"
    dop.write_plan(path, _plan(config, [_survivor("MSFT", 4.0, 2.0, 1.2)]))
    code, report = _run(capsys, ["plan", "--source", "mac_plan", *_common(["--plan-file", str(path)])])
    assert (code, report["status"]) == (0, "VALID_PLAN")


def test_plan_with_no_file_reports_no_valid_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dca_paths, "cron_tracking_root", lambda: str(tmp_path))
    code, report = _run(capsys, ["plan", "--source", "mac_plan", *_common()])
    assert (code, report["status"], report["plan"]) == (2, "NO_VALID_PLAN", None)
    assert [e["code"] for e in report["errors"]] == ["plan_missing"]


@pytest.mark.parametrize(
    ("change", "code"),
    [({"trading_day": "2026-10-06"}, "wrong_trading_day"), ({"slot": "1230"}, "wrong_slot"),
     ({"universe_hash": "0" * 64}, "universe_hash_mismatch"), ({"ladder_version": "old"}, "stale_ladder_version")],
)
def test_plan_that_fails_validation_reports_no_valid_plan(tmp_path, capsys, config, change, code):
    plan = {**_plan(config, [_survivor("MSFT", 4.0, 2.0, 1.2)]), **change}
    path = _write(tmp_path, "plan.json", plan)
    exit_code, report = _run(capsys, ["plan", "--source", "mac_plan", *_common(["--plan-file", path])])
    assert (exit_code, report["status"]) == (2, "NO_VALID_PLAN")
    assert code in {e["code"] for e in report["errors"]}


def test_plan_only_accepts_the_mac_plan_source():
    with pytest.raises(SystemExit):
        backup_planner.main(["plan", "--source", "cloud", *_common()])


@pytest.mark.parametrize(("slot", "day"), [("9999", DAY), ("1130ET", DAY), (SLOT, "10/07/2026"), (SLOT, "")])
def test_bad_slot_or_day_fails_closed(capsys, slot, day):
    code, report = _run(capsys, ["plan", "--source", "mac_plan", "--trading-day", day, "--slot", slot])
    assert (code, report["status"]) == (2, "FAILED")


# ---------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------


def _quote(symbol, drop_pct, prior_close=1000.0, prior_low=None):
    return {"symbol": symbol, "prior_close": prior_close, "prior_low": prior_low,
            "decision_price": round(prior_close * (1 - drop_pct / 100.0), 6)}


def _compute(tmp_path, capsys, quotes, pages, *, quotes_text=None, bp="1000"):
    q = _write(tmp_path, "quotes.json", quotes_text if quotes_text is not None else {"quotes": quotes})
    p = _write(tmp_path, "pages.json", pages)
    return _run(capsys, ["compute", *_common(["--quotes", q, "--orders-pages", p, "--now", NOW, "--buying-power", bp])])


def test_compute_returns_a_backup_plan_under_the_normal_tier(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0), _quote("MSFT", 1.2)], _envelope())
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert report["plan"]["generator"] == "backup"
    assert report["plan"]["ladder_version"] == dop.current_ladder_version()
    assert {o["symbol"] for o in report["plan"]["orders"]} == {"VT", "MSFT"}


def test_compute_buys_nothing_when_the_fresh_history_leaves_no_daily_headroom(tmp_path, capsys):
    pages = _envelope([_raw("NVDA", amount="179.500000", ref=f"dca-runner-1030-{DAY}-NVDA")])
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0)], pages)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


def test_compute_counts_the_whole_week_not_just_today(tmp_path, capsys):
    monday = _raw("NVDA", amount="899.500000", created="2026-10-05T15:31:00+00:00")
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0)], _envelope([monday]))
    assert (code, report["status"]) == (2, "FAILED")
    assert "over_weekly_cap" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("text", ["", "{oops", "{}", "[]", "null"])
def test_compute_with_corrupt_quotes_fails_closed(tmp_path, capsys, text):
    code, report = _compute(tmp_path, capsys, None, _envelope(), quotes_text=text)
    assert (code, report["status"]) == (2, "FAILED")
    assert [e["code"] for e in report["errors"]] == ["inputs_corrupt"]


def test_compute_with_missing_quotes_fails_closed(tmp_path, capsys):
    pages = _write(tmp_path, "pages.json", _envelope())
    code, report = _run(capsys, ["compute", *_common(["--quotes", str(tmp_path / "no.json"), "--orders-pages", pages, "--now", NOW, "--buying-power", "1000"])])
    assert (code, report["status"]) == (2, "FAILED")
    assert [e["code"] for e in report["errors"]] == ["inputs_missing"]


# -- saved broker pages are evidence, so they are proven before use ---------


@pytest.mark.parametrize(
    ("pages", "reason"),
    [
        ({"pages": [{"orders": []}]}, "orders_pages_missing_query_metadata"),
        ([{"orders": []}], "orders_pages_missing_query_metadata"),
        (_envelope(day="2026-10-06"), "orders_pages_trading_day_mismatch"),
        (_envelope(account=""), "orders_pages_missing_query_metadata"),
        (_envelope(pages=[]), "orders_pages_incomplete"),
        (_envelope(pages=[{"orders": [], "next": "https://api.example/orders/?cursor=P2"}]), "orders_pages_incomplete"),
        (_envelope(pages=[{"orders": []}, {"orders": []}]), "orders_pages_incomplete"),
    ],
)
def test_unproven_or_mismatched_order_pages_fail_closed_on_every_subcommand(tmp_path, capsys, config, pages, reason):
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0)], pages)
    assert (code, report["status"]) == (2, "FAILED")
    assert reason in {e["code"] for e in report["errors"]}
    plan = _write(tmp_path, "plan.json", _plan(config, [_survivor("MSFT", 4.0, 2.0, 1.2)]))
    fresh = _write(tmp_path, "fresh.json", {"MSFT": 100.0})
    pages_file = _write(tmp_path, "pages2.json", pages)
    code, report = _run(capsys, ["finalize", *_common(["--plan", plan, "--orders-pages", pages_file, "--fresh-quotes", fresh, *_final_inputs(tmp_path)])])
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert reason in {e["code"] for e in report["errors"]}


def test_a_malformed_order_row_fails_closed(tmp_path, capsys):
    bad = _raw("MSFT")
    del bad["symbol"]
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0)], _envelope([bad]))
    assert (code, report["status"]) == (2, "FAILED")
    assert "history_unusable" in {e["code"] for e in report["errors"]}


def test_a_two_page_chain_with_its_continuation_is_accepted(tmp_path, capsys):
    pages = _envelope(pages=[{"orders": [], "next": "https://api.example/orders/?cursor=P2"}, {"orders": [_raw("MSFT")]}])
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0)], pages)
    assert (code, report["status"]) == (0, "VALID_PLAN")


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _finalize(tmp_path, capsys, plan, rows=(), fresh=None, *, pages=None, bp="1000", spreads=None):
    plan_file = _write(tmp_path, "plan.json", plan)
    pages_file = _write(tmp_path, "pages.json", pages if pages is not None else _envelope(rows))
    fresh_file = _write(tmp_path, "fresh.json", fresh if fresh is not None else {"MSFT": 100.0, "AAPL": 100.0})
    return _run(capsys, ["finalize", *_common(["--plan", plan_file, "--orders-pages", pages_file, "--fresh-quotes", fresh_file, *_final_inputs(tmp_path, bp=bp, spreads=spreads)])])


@pytest.fixture
def two_order_plan(config):
    return _plan(config, [_survivor("MSFT", 4.0, 2.0, 1.2), _survivor("AAPL", 2.0, 1.0, 0.7)])


def test_finalize_returns_exactly_what_to_place(tmp_path, capsys, two_order_plan):
    code, report = _finalize(tmp_path, capsys, two_order_plan)
    assert (code, report["status"]) == (0, "PROCEED")
    assert report["to_place"] == [
        {"symbol": "MSFT", "notional_usd": 4.0, "ref_id": f"dca-runner-{SLOT}-{DAY}-MSFT"},
        {"symbol": "AAPL", "notional_usd": 2.0, "ref_id": f"dca-runner-{SLOT}-{DAY}-AAPL"},
    ]


def test_finalize_completes_a_partly_finished_claude_batch(tmp_path, capsys, two_order_plan):
    claude_msft = _raw("MSFT", ref=f"dca-runner-{SLOT}-{DAY}-MSFT", amount="4.000000")
    code, report = _finalize(tmp_path, capsys, two_order_plan, [claude_msft])
    assert (code, report["status"]) == (0, "PROCEED")
    assert [o["symbol"] for o in report["to_place"]] == ["AAPL"]
    assert {"symbol": "MSFT", "reason": "already_placed"} in report["skipped"]


def test_finalize_with_everything_placed_proceeds_with_nothing(tmp_path, capsys, two_order_plan):
    rows = [_raw("MSFT", ref=f"dca-runner-{SLOT}-{DAY}-MSFT"), _raw("AAPL", ref=f"dca-runner-{SLOT}-{DAY}-AAPL")]
    code, report = _finalize(tmp_path, capsys, two_order_plan, rows)
    assert (code, report["status"], report["to_place"]) == (0, "PROCEED", [])


def test_finalize_skips_a_ticker_whose_fresh_quote_is_not_below_the_prior_close(tmp_path, capsys, two_order_plan):
    code, report = _finalize(tmp_path, capsys, two_order_plan, fresh={"MSFT": 101.2, "AAPL": 100.0})
    assert code == 0
    assert [o["symbol"] for o in report["to_place"]] == ["AAPL"]
    assert {"symbol": "MSFT", "reason": "quote_not_below_prior_close"} in report["skipped"]


def test_finalize_aborts_the_whole_batch_on_a_cap_breach_in_the_fresh_pull(tmp_path, capsys, two_order_plan):
    rows = [_raw("NVDA", amount="176.000000", ref=f"dca-runner-1030-{DAY}-NVDA")]  # 176 + 4 + 2 > 180
    code, report = _finalize(tmp_path, capsys, two_order_plan, rows)
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "over_daily_cap" in {e["code"] for e in report["errors"]}


def test_finalize_aborts_on_an_invalid_plan(tmp_path, capsys, two_order_plan):
    code, report = _finalize(tmp_path, capsys, {**two_order_plan, "universe_hash": "0" * 64})
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "universe_hash_mismatch" in {e["code"] for e in report["errors"]}


def test_finalize_aborts_when_the_plan_file_is_missing(tmp_path, capsys):
    pages = _write(tmp_path, "pages.json", _envelope())
    fresh = _write(tmp_path, "fresh.json", {})
    code, report = _run(capsys, ["finalize", *_common(["--plan", str(tmp_path / "no.json"), "--orders-pages", pages, "--fresh-quotes", fresh, *_final_inputs(tmp_path)])])
    assert (code, report["status"]) == (2, "ABORT")
    assert "plan_missing" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("text", ["", "{oops", "[]", '{"MSFT": "100"}'])
def test_finalize_aborts_on_corrupt_fresh_quotes(tmp_path, capsys, two_order_plan, text):
    plan = _write(tmp_path, "plan.json", two_order_plan)
    pages = _write(tmp_path, "pages.json", _envelope())
    fresh = _write(tmp_path, "fresh.json", text)
    code, report = _run(capsys, ["finalize", *_common(["--plan", plan, "--orders-pages", pages, "--fresh-quotes", fresh, *_final_inputs(tmp_path)])])
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])


# ---------------------------------------------------------------------------
# hygiene
# ---------------------------------------------------------------------------


def test_output_never_contains_the_account_number(tmp_path, capsys, monkeypatch, two_order_plan):
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", ACCOUNT)
    rows = [_raw("MSFT", ref=f"dca-runner-{SLOT}-{DAY}-MSFT")]
    plan_file = _write(tmp_path, "plan.json", two_order_plan)
    pages = _write(tmp_path, "pages.json", _envelope(rows))
    fresh = _write(tmp_path, "fresh.json", {"AAPL": 100.0})
    backup_planner.main(["finalize", *_common(["--plan", plan_file, "--orders-pages", pages, "--fresh-quotes", fresh, *_final_inputs(tmp_path)])])
    assert ACCOUNT not in capsys.readouterr().out


def test_the_planner_carries_no_broker_or_placement_code():
    for name in ("execute_plan", "RobinhoodBroker", "place_equity_order", "place_order"):
        assert not hasattr(backup_planner, name), name
    source = (Path(backup_planner.__file__)).read_text()
    assert "dca_live_adapters" not in source
    assert "place_equity_order" not in source


def test_every_invocation_prints_exactly_one_json_document(tmp_path, capsys, two_order_plan):
    _finalize(tmp_path, capsys, two_order_plan)  # parsed by json.loads inside _run: one document, nothing else
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Review gaps found 2026-10-07 (ADR "Open design gaps")
# ---------------------------------------------------------------------------


def _earlier_slot_buy(symbol="MSFT"):
    """A 09:40 ET buy from the 09:35 slot: legitimately followed by a resized 11:30 top-up."""
    return _raw(symbol, ref=f"dca-runner-0935-{DAY}-{symbol}", created=f"{DAY}T13:40:00+00:00")


# -- gap 1: cross-slot top-ups (config/budgets.json one_order_per_ticker_per_day is false) ----


def test_a_claude_plan_still_buys_a_ticker_an_earlier_slot_bought(tmp_path, capsys, two_order_plan):
    code, report = _finalize(tmp_path, capsys, two_order_plan, [_earlier_slot_buy("MSFT")])
    assert (code, report["status"]) == (0, "PROCEED")
    assert [o["symbol"] for o in report["to_place"]] == ["MSFT", "AAPL"]


def test_a_claude_plan_still_drops_a_ticker_bought_in_this_slots_window(tmp_path, capsys, two_order_plan):
    in_window = _raw("MSFT", ref="another-executor-ref", created=f"{DAY}T15:45:00+00:00")  # 11:45 ET
    code, report = _finalize(tmp_path, capsys, two_order_plan, [in_window])
    assert code == 0
    assert [o["symbol"] for o in report["to_place"]] == ["AAPL"]
    assert {"symbol": "MSFT", "reason": "already_placed"} in report["skipped"]


# -- gap 2: the compute path is not the Gate 5 signal path, so it never places -------------


def _backup_plan(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0), _quote("MSFT", 1.2)], _envelope())
    assert code == 0
    return report["plan"]


def test_a_compute_derived_plan_is_shadow_only_and_places_nothing(tmp_path, capsys):
    plan = _backup_plan(tmp_path, capsys)
    fresh = {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]}
    code, report = _finalize(tmp_path, capsys, plan, fresh=fresh)
    assert (code, report["status"], report["to_place"]) == (0, "SHADOW_ONLY", [])
    assert {o["symbol"] for o in report["would_place"]} == {"VT", "MSFT"}
    assert backup_planner.COMPUTE_PLACEMENT_ENABLED is False


def test_the_shadow_gate_is_one_switch_and_a_claude_plan_never_needs_it(tmp_path, capsys, monkeypatch, two_order_plan):
    code, report = _finalize(tmp_path, capsys, two_order_plan)
    assert (code, report["status"]) == (0, "PROCEED")  # a Claude plan is unaffected
    assert "would_place" not in report or report["would_place"] == []
    plan = _backup_plan(tmp_path, capsys)
    monkeypatch.setattr(backup_planner, "COMPUTE_PLACEMENT_ENABLED", True)
    fresh = {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]}
    code, report = _finalize(tmp_path, capsys, plan, fresh=fresh)
    assert (code, report["status"]) == (0, "PROCEED")
    assert {o["symbol"] for o in report["to_place"]} == {"VT", "MSFT"}


def test_a_compute_derived_plan_keeps_the_day_wide_exclusion_when_placement_is_enabled(tmp_path, capsys, monkeypatch):
    """compute cannot resize a top-up, so a ticker bought in ANY earlier slot stays excluded."""
    monkeypatch.setattr(backup_planner, "COMPUTE_PLACEMENT_ENABLED", True)
    plan = _backup_plan(tmp_path, capsys)
    fresh = {o["symbol"]: o["prior_close"] * 0.99 for o in plan["orders"]}
    code, report = _finalize(tmp_path, capsys, plan, [_earlier_slot_buy("VT")], fresh=fresh)
    assert code == 0
    assert "VT" not in {o["symbol"] for o in report["to_place"]}
    assert {"symbol": "VT", "reason": "already_placed"} in report["skipped"]


# -- gap 3: available buying power is an input, and a missing one fails closed -------------


def test_compute_and_finalize_require_a_buying_power_argument(tmp_path, two_order_plan):
    quotes = _write(tmp_path, "q.json", {"quotes": []})
    pages = _write(tmp_path, "p.json", _envelope())
    with pytest.raises(SystemExit):
        backup_planner.main(["compute", *_common(["--quotes", quotes, "--orders-pages", pages, "--now", NOW])])
    plan = _write(tmp_path, "plan.json", two_order_plan)
    fresh = _write(tmp_path, "f.json", {})
    with pytest.raises(SystemExit):
        backup_planner.main(["finalize", *_common(["--plan", plan, "--orders-pages", pages, "--fresh-quotes", fresh,
                                                    "--fresh-spreads", _spreads(tmp_path)])])


@pytest.mark.parametrize("bad", ["abc", "", "nan", "inf", "-5", "0"])
def test_an_unusable_buying_power_value_fails_closed(tmp_path, capsys, two_order_plan, bad):
    code, report = _finalize(tmp_path, capsys, two_order_plan, bp=bad)
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "invalid_buying_power" in {e["code"] for e in report["errors"]}
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0)], _envelope(), bp=bad)
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "invalid_buying_power" in {e["code"] for e in report["errors"]}


def test_finalize_aborts_when_the_batch_exceeds_buying_power(tmp_path, capsys, two_order_plan):
    code, report = _finalize(tmp_path, capsys, two_order_plan, bp="5.99")  # $4 + $2 = $6
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "insufficient_buying_power" in {e["code"] for e in report["errors"]}


def test_finalize_allows_a_batch_exactly_equal_to_buying_power(tmp_path, capsys, two_order_plan):
    code, report = _finalize(tmp_path, capsys, two_order_plan, bp="6.00")
    assert (code, report["status"]) == (0, "PROCEED")


def test_compute_buys_nothing_when_the_plan_exceeds_buying_power(tmp_path, capsys):
    code, report = _compute(tmp_path, capsys, [_quote("VT", 5.0), _quote("MSFT", 1.2)], _envelope(), bp="0.50")
    assert (code, report["status"], report["plan"]) == (2, "FAILED", None)
    assert "insufficient_buying_power" in {e["code"] for e in report["errors"]}


# -- gap 4: Gate 6.7 spread, fail closed per ticker ------------------------------------------


def test_finalize_requires_a_fresh_spreads_file(tmp_path, capsys, two_order_plan):
    plan = _write(tmp_path, "plan.json", two_order_plan)
    pages = _write(tmp_path, "p.json", _envelope())
    fresh = _write(tmp_path, "f.json", {"MSFT": 100.0, "AAPL": 100.0})
    with pytest.raises(SystemExit):
        backup_planner.main(["finalize", *_common(["--plan", plan, "--orders-pages", pages, "--fresh-quotes", fresh,
                                                    "--buying-power", "1000"])])
    code, report = _run(capsys, ["finalize", *_common(["--plan", plan, "--orders-pages", pages, "--fresh-quotes", fresh,
                                                        "--buying-power", "1000", "--fresh-spreads",
                                                        str(tmp_path / "none.json")])])
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "fresh_spreads_missing" in {e["code"] for e in report["errors"]}


@pytest.mark.parametrize("text", ["", "{oops", "[]", '{"MSFT": [99, 101]}', '{"MSFT": {"bid": "99", "ask": 101}}'])
def test_corrupt_fresh_spreads_abort(tmp_path, capsys, two_order_plan, text):
    plan = _write(tmp_path, "plan.json", two_order_plan)
    pages = _write(tmp_path, "p.json", _envelope())
    fresh = _write(tmp_path, "f.json", {"MSFT": 100.0, "AAPL": 100.0})
    spreads = _write(tmp_path, "s.json", text)
    code, report = _run(capsys, ["finalize", *_common(["--plan", plan, "--orders-pages", pages, "--fresh-quotes", fresh,
                                                        "--buying-power", "1000", "--fresh-spreads", spreads])])
    assert (code, report["status"], report["to_place"]) == (2, "ABORT", [])
    assert "fresh_spreads_corrupt" in {e["code"] for e in report["errors"]}


def test_a_wide_spread_skips_only_that_ticker_in_finalize(tmp_path, capsys, two_order_plan):
    spreads = {"MSFT": {"bid": 99.0, "ask": 101.0}, "AAPL": {"bid": 99.9, "ask": 100.1}}  # 2.0% and 0.2%
    code, report = _finalize(tmp_path, capsys, two_order_plan, spreads=spreads)
    assert (code, report["status"]) == (0, "PROCEED")
    assert [o["symbol"] for o in report["to_place"]] == ["AAPL"]
    assert {"symbol": "MSFT", "reason": "spread_not_ok"} in report["skipped"]


def test_a_missing_or_crossed_spread_skips_the_ticker_in_finalize(tmp_path, capsys, two_order_plan):
    spreads = {"AAPL": {"bid": 101.0, "ask": 99.0}}  # MSFT missing, AAPL crossed
    code, report = _finalize(tmp_path, capsys, two_order_plan, spreads=spreads)
    assert (code, report["to_place"]) == (0, [])
    assert {"symbol": "MSFT", "reason": "spread_unavailable"} in report["skipped"]
    assert {"symbol": "AAPL", "reason": "spread_unavailable"} in report["skipped"]
