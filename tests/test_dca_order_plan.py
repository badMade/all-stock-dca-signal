"""Tests for ``dca_order_plan`` -- plan-first backup executor (ADR docs/adr-plan-first-backup-executor.md).

Written test-first (Phase 2): derived from the ADR's expected behavior, before any
implementation exists, so every test here must fail red until Phase 3. The module under
test does not exist yet; the contract these tests define is:

* ``validate_plan`` / ``load_plan``   -- reject-the-whole-plan validation, never raises
* ``reconcile``                       -- broker history minus planned orders, D2
* ``compute_plan``                    -- the compute fallback, reusing the repo's own ladder
* ``revalidate_for_placement``        -- the pre-placement re-check run on EVERY path
* ``choose_plan_path``                -- mac_plan | mac_compute | cloud_compute, D8
* ``canonical_plan_json`` / ``write_plan`` -- byte-deterministic serialization

Behavior notes the reviewer should know:

* Live sizing uses ``config/per_ticker_sizing.json`` (interpolated, $1 base) for every active
  ticker; the global 15-tier ladder (``config/sizing_ladder.json``, $1/$2 base) is only the
  fallback. The ladder-edge and $1-vs-$2 tests therefore run against a config with the
  per-ticker calibration removed, and a separate test pins the calibrated path.
* D4 is "literal": a plan that would breach a cap buys NOTHING; it is never trimmed.
* A rejected or cancelled buy still counts as "placed" for reconciliation (one order per ticker
  per day; over-counting is the safe direction). Phase 2 assumption, flagged for confirmation.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import dca_config
import dca_order_plan as dop
import dca_runner
import near_close_dca
from dca_runner import BrokerOrder
from scripts.validate_native_placement import native_ref_id

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 7)  # a Wednesday
MONDAY = date(2026, 10, 5)
SLOT = "1130"
LADDER_VERSION = "2026-10-04+2026-10-05"  # sizing_ladder.json + per_ticker_sizing.json (composite, approved)
NOW = datetime(2026, 10, 7, 11, 30, 5, tzinfo=ET)
NORMAL_TIER = near_close_dca.BUDGET_TIERS[0]  # $180/day, $900/week


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config():
    """The repo's real StrategyConfig (per-ticker calibration active, as live)."""
    return dca_config.load_strategy_config(dca_config.get_config_dir())


@pytest.fixture(scope="module")
def global_config(config):
    """Same config with per-ticker calibration removed: the global sizing_ladder.json applies."""
    return dataclasses.replace(config, per_ticker_sizing={})


def _expected_universe_hash(tickers) -> str:
    # Independent of dop.universe_hash: the schema defines it as sha256 over the sorted,
    # newline-joined active tickers with a trailing newline.
    return hashlib.sha256(("\n".join(sorted(tickers)) + "\n").encode()).hexdigest()


def _order(symbol="MSFT", notional=4.0, multiplier=2.0, drop=1.2, **over):
    row = {
        "symbol": symbol,
        "notional_usd": notional,
        "multiplier": multiplier,
        "drop_pct": drop,
        "decision_price": 100.0,
        "prior_close": 101.2,
        "prior_low": 100.5,
        "ref_id": native_ref_id(SLOT, DAY.isoformat(), symbol),
    }
    row.update(over)
    return row


def _plan(config, orders=None, **over):
    plan = {
        "schema_version": 1,
        "trading_day": DAY.isoformat(),
        "slot": SLOT,
        "generated_at_et": NOW.isoformat(),
        "generator": "claude",
        "ladder_version": LADDER_VERSION,
        "universe_hash": _expected_universe_hash(config.tickers),
        "orders": [_order("MSFT"), _order("AAPL", notional=2.0, multiplier=1.0, drop=0.3)]
        if orders is None
        else orders,
    }
    plan.update(over)
    return plan


@pytest.fixture
def ctx(config):
    """Validation context: the reader's own view of day, slot, ladder, universe, cap."""
    return {
        "trading_day": DAY,
        "slot": SLOT,
        "ladder_version": LADDER_VERSION,
        "active_tickers": tuple(config.tickers),
        "excluded_tickers": frozenset(config.excluded_tickers),
        "max_per_order_usd": dca_runner.MAX_PER_ORDER_USD,
    }


def _codes(result):
    return [e.code for e in result.errors]


def _broker_order(symbol, *, side="buy", amount=2.0, day=DAY, ref=None, state="filled",
                  agent="agentic", hour=11):
    return BrokerOrder(
        symbol=symbol,
        side=side,
        dollar_amount=amount,
        created_at=datetime(day.year, day.month, day.day, hour, 31, tzinfo=ET),
        placed_agent=agent,
        state=state,
        client_order_id=ref,
    )


def _quote(symbol, drop_pct, *, prior_low=None, prior_close=1000.0):
    return {
        "symbol": symbol,
        "prior_close": prior_close,
        "prior_low": prior_low,
        "decision_price": round(prior_close * (1 - drop_pct / 100.0), 6),
    }


def _compute(config, quotes, *, orders_today=(), tier=NORMAL_TIER):
    return dop.compute_plan(
        quotes,
        config=config,
        trading_day=DAY,
        slot=SLOT,
        generated_at_et=NOW,
        ladder_version=LADDER_VERSION,
        orders_today=orders_today,
        tier=tier,
    )


# ---------------------------------------------------------------------------
# Plan validation: any defect rejects the WHOLE plan (empty orders + structured error)
# ---------------------------------------------------------------------------


def test_valid_plan_is_accepted_and_returns_its_orders_in_order(config, ctx):
    result = dop.validate_plan(_plan(config), **ctx)
    assert result.errors == ()
    assert [o["symbol"] for o in result.orders] == ["MSFT", "AAPL"]


def test_current_ladder_version_is_the_composite_of_both_sizing_files():
    cfg_dir = dca_config.get_config_dir()
    ladder = json.loads((cfg_dir / "sizing_ladder.json").read_bytes())["version"]
    per_ticker = json.loads((cfg_dir / "per_ticker_sizing.json").read_bytes())["version"]
    assert dop.current_ladder_version() == f"{ladder}+{per_ticker}"


def test_universe_hash_matches_the_schema_definition(config):
    assert dop.universe_hash(config.tickers) == _expected_universe_hash(config.tickers)
    assert dop.universe_hash(reversed(list(config.tickers))) == _expected_universe_hash(config.tickers)


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"trading_day": "2026-10-06"}, "wrong_trading_day"),
        ({"slot": "1230"}, "wrong_slot"),
        ({"schema_version": 2}, "schema_version_mismatch"),
        ({"ladder_version": "2026-08-02"}, "stale_ladder_version"),
        ({"universe_hash": "0" * 64}, "universe_hash_mismatch"),
        ({"generator": "somebody_else"}, "schema_violation"),
    ],
)
def test_header_defects_reject_the_whole_plan(config, ctx, override, expected_code):
    result = dop.validate_plan(_plan(config, **override), **ctx)
    assert result.orders == ()
    assert expected_code in _codes(result)


def test_tampered_universe_hash_rejects_even_when_orders_are_otherwise_valid(config, ctx):
    plan = _plan(config)
    plan["universe_hash"] = _expected_universe_hash([*config.tickers, "ZZZZ"])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "universe_hash_mismatch" in _codes(result)


@pytest.mark.parametrize(
    ("symbol", "expected_code"),
    [("ZZZZ", "unknown_symbol"), ("QBUT", "excluded_symbol"), ("BYDDY", "unknown_symbol")],
)
def test_unknown_excluded_and_whole_share_symbols_reject_the_plan(config, ctx, symbol, expected_code):
    plan = _plan(config, orders=[_order("MSFT"), _order(symbol)])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()  # one bad order rejects the batch, MSFT is not salvaged
    assert expected_code in _codes(result)


def test_notional_over_the_per_order_cap_rejects_the_plan(config, ctx):
    cap = dca_runner.MAX_PER_ORDER_USD
    plan = _plan(config, orders=[_order("MSFT", notional=cap + 0.01)])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "notional_over_cap" in _codes(result)


def test_notional_exactly_at_the_cap_is_accepted(config, ctx):
    plan = _plan(config, orders=[_order("MSFT", notional=dca_runner.MAX_PER_ORDER_USD, multiplier=8.0, drop=17.0)])
    assert dop.validate_plan(plan, **ctx).errors == ()


@pytest.mark.parametrize("bad", [0.0, -2.0, 1.234, float("nan")])
def test_non_positive_or_non_cent_notional_rejects_the_plan(config, ctx, bad):
    result = dop.validate_plan(_plan(config, orders=[_order("MSFT", notional=bad)]), **ctx)
    assert result.orders == ()
    assert result.errors


def test_duplicate_symbol_rejects_the_plan(config, ctx):
    plan = _plan(config, orders=[_order("MSFT"), _order("MSFT", notional=2.0)])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "duplicate_symbol" in _codes(result)


def test_ref_id_must_name_this_slot_day_and_symbol(config, ctx):
    plan = _plan(config, orders=[_order("MSFT", ref_id="dca-runner-1230-2026-10-07-MSFT")])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "bad_ref_id" in _codes(result)


@pytest.mark.parametrize("extra", [{"placed": True}, {"status": "filled"}, {"filled_usd": 4.0}])
def test_a_plan_cannot_carry_purchase_claims(config, ctx, extra):
    """Mac content is never purchase evidence: unknown per-order fields are a schema violation."""
    plan = _plan(config, orders=[_order("MSFT", **extra)])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "schema_violation" in _codes(result)


def test_a_plan_cannot_carry_top_level_purchase_claims(config, ctx):
    result = dop.validate_plan(_plan(config, placed_symbols=["MSFT"]), **ctx)
    assert result.orders == ()
    assert "schema_violation" in _codes(result)


def test_an_empty_orders_list_is_a_valid_plan(config, ctx):
    result = dop.validate_plan(_plan(config, orders=[]), **ctx)
    assert result.errors == ()
    assert result.orders == ()


@pytest.mark.parametrize("garbage", [None, [], "plan", 7, {}, {"orders": "x"}])
def test_garbage_input_returns_a_structured_error_never_raises(ctx, garbage):
    result = dop.validate_plan(garbage, **ctx)
    assert result.orders == ()
    assert result.errors


# ---------------------------------------------------------------------------
# Fail closed on file I/O
# ---------------------------------------------------------------------------


def test_missing_plan_file_returns_empty_plan_and_structured_error(tmp_path, ctx):
    result = dop.load_plan(tmp_path / "order_plan_1130.json", **ctx)
    assert result.orders == ()
    assert _codes(result) == ["plan_missing"]


@pytest.mark.parametrize("text", ["", "{not json", "[]", '"str"', "null", '{"a": 1, "a": 2}'])
def test_corrupt_plan_file_returns_empty_plan_and_structured_error(tmp_path, ctx, text):
    path = tmp_path / "order_plan_1130.json"
    path.write_text(text)
    result = dop.load_plan(path, **ctx)
    assert result.orders == ()
    assert result.errors  # plan_corrupt (duplicate keys count as corrupt, like config loading)


def test_a_directory_in_place_of_the_plan_file_does_not_raise(tmp_path, ctx):
    result = dop.load_plan(tmp_path, **ctx)
    assert result.orders == ()
    assert result.errors


def test_load_plan_round_trips_a_written_plan(tmp_path, config, ctx):
    path = tmp_path / "order_plan_1130.json"
    dop.write_plan(path, _plan(config))
    result = dop.load_plan(path, **ctx)
    assert result.errors == ()
    assert [o["symbol"] for o in result.orders] == ["MSFT", "AAPL"]


# ---------------------------------------------------------------------------
# Reconciliation: only planned orders the BROKER shows as unplaced (D2)
# ---------------------------------------------------------------------------


def _planned(*symbols):
    return [_order(s) for s in symbols]


def test_reconcile_with_empty_history_returns_every_planned_order(config):
    # A plan is never purchase evidence: with no broker history nothing is "already placed".
    orders = _planned("MSFT", "AAPL", "AMD")
    assert dop.reconcile(orders, [], trading_day=DAY) == orders


def test_reconcile_drops_orders_matched_by_ref_id(config):
    orders = _planned("MSFT", "AAPL")
    history = [_broker_order("MSFT", ref=orders[0]["ref_id"])]
    assert [o["symbol"] for o in dop.reconcile(orders, history, trading_day=DAY)] == ["AAPL"]


def test_reconcile_also_matches_by_symbol_when_the_ref_id_differs(config):
    # e.g. the other executor's own reference: one order per ticker per day still holds.
    orders = _planned("MSFT", "AAPL")
    history = [_broker_order("MSFT", ref="some-other-executor-ref", agent="agentic")]
    assert [o["symbol"] for o in dop.reconcile(orders, history, trading_day=DAY)] == ["AAPL"]


def test_reconcile_matches_a_ref_id_found_in_the_ref_id_field_too(config):
    orders = _planned("MSFT", "AAPL")
    row = dataclasses.replace(_broker_order("MSFT"), ref_id=orders[0]["ref_id"])
    assert [o["symbol"] for o in dop.reconcile(orders, [row], trading_day=DAY)] == ["AAPL"]


def test_reconcile_ignores_sells_and_other_days(config):
    orders = _planned("MSFT", "AAPL")
    history = [
        _broker_order("MSFT", side="sell"),
        _broker_order("AAPL", day=date(2026, 10, 6)),
    ]
    assert dop.reconcile(orders, history, trading_day=DAY) == orders


@pytest.mark.parametrize("state", ["cancelled", "rejected", "failed"])
def test_reconcile_counts_a_dead_buy_attempt_as_placed(config, state):
    """Conservative: a dead attempt still consumed the ticker's order for the day."""
    orders = _planned("MSFT", "AAPL")
    history = [_broker_order("MSFT", state=state)]
    assert [o["symbol"] for o in dop.reconcile(orders, history, trading_day=DAY)] == ["AAPL"]


def test_reconcile_preserves_plan_priority_order(config):
    orders = _planned("MSFT", "AAPL", "AMD", "NVDA")
    history = [_broker_order("AAPL")]
    assert [o["symbol"] for o in dop.reconcile(orders, history, trading_day=DAY)] == ["MSFT", "AMD", "NVDA"]


def test_reconcile_everything_placed_returns_nothing(config):
    orders = _planned("MSFT", "AAPL")
    history = [_broker_order("MSFT"), _broker_order("AAPL")]
    assert dop.reconcile(orders, history, trading_day=DAY) == []


# ---------------------------------------------------------------------------
# Compute fallback: reuses the repo's ladder, base selection, and plan_orders caps
# ---------------------------------------------------------------------------

# (drop %, expected multiplier) from config/sizing_ladder.json: min inclusive, max exclusive.
_GLOBAL_LADDER = [
    (0.1, 1.0), (0.4999, 1.0), (0.5, 1.5), (0.9999, 1.5), (1.0, 2.0), (1.4999, 2.0), (1.5, 2.5),
    (2.0, 3.0), (2.75, 3.5), (3.5, 4.0), (4.5, 4.5), (5.5, 5.0), (7.0, 5.5), (8.5, 6.0),
    (10.5, 6.5), (12.5, 7.0), (14.5, 7.5), (15.9999, 7.5), (16.0, 8.0), (30.0, 8.0),
]


@pytest.mark.parametrize(("drop", "multiplier"), _GLOBAL_LADDER)
def test_global_ladder_band_edges_min_inclusive_max_exclusive(global_config, drop, multiplier):
    result = _compute(global_config, [_quote("VT", drop, prior_low=None)])
    assert result.errors == ()
    assert [o["multiplier"] for o in result.orders] == [multiplier]
    assert result.orders[0]["notional_usd"] == pytest.approx(1.0 * multiplier)  # prior low unknown => $1 base


@pytest.mark.parametrize("drop", [0.0, 0.05, 0.0999])
def test_drop_below_a_tenth_of_a_percent_is_no_buy_on_the_global_ladder(global_config, drop):
    result = _compute(global_config, [_quote("VT", drop)])
    assert result.errors == ()
    assert result.orders == ()


@pytest.mark.parametrize("decision_above_close", [1000.0, 1000.5, 1100.0])
def test_price_at_or_above_prior_close_is_never_a_buy(global_config, decision_above_close):
    quote = {"symbol": "VT", "prior_close": 1000.0, "prior_low": None, "decision_price": decision_above_close}
    assert _compute(global_config, [quote]).orders == ()


def test_base_is_two_dollars_below_close_and_low_and_one_dollar_below_close_only(global_config):
    below_both = {"symbol": "VT", "prior_close": 1000.0, "prior_low": 996.0, "decision_price": 995.0}
    below_close_only = {"symbol": "VTI", "prior_close": 1000.0, "prior_low": 990.0, "decision_price": 995.0}
    unknown_low = {"symbol": "VT", "prior_close": 1000.0, "prior_low": None, "decision_price": 995.0}
    cfg = dataclasses.replace(global_config, tickers=(*global_config.tickers, "VTI"))
    by_symbol = {o["symbol"]: o for o in _compute(cfg, [below_both, below_close_only]).orders}
    assert by_symbol["VT"]["notional_usd"] == pytest.approx(2.0 * 1.5)
    assert by_symbol["VTI"]["notional_usd"] == pytest.approx(1.0 * 1.5)
    assert _compute(cfg, [unknown_low]).orders[0]["notional_usd"] == pytest.approx(1.0 * 1.5)


def test_eight_x_at_the_two_dollar_base_is_sixteen_and_is_not_refused(global_config):
    result = _compute(global_config, [_quote("VT", 20.0, prior_low=900.0)])
    assert result.errors == ()
    assert result.orders[0]["notional_usd"] == pytest.approx(16.0)


def test_calibrated_ticker_uses_its_own_sizing_not_the_global_ladder(config):
    """Live behavior: per_ticker_sizing.json ($1 base, buys from 0.01%) governs every active ticker."""
    quote = _quote("VT", 0.1, prior_low=999.5)  # below close AND low: the global ladder would use $2
    result = _compute(config, [quote])
    expected = near_close_dca.graded_buy_multiplier(0.001, config, "VT")
    assert len(result.orders) == 1
    order = result.orders[0]
    assert order["multiplier"] == pytest.approx(expected, abs=1e-4)
    assert order["notional_usd"] == pytest.approx(1.0 * expected, abs=0.005)  # $1 base, never $2
    assert round(order["notional_usd"], 2) == order["notional_usd"]
    tiny = _compute(config, [_quote("VT", 0.0200)])  # 0.02% drop: calibrated buys, the global ladder does not
    assert len(tiny.orders) == 1


def test_orders_are_prioritized_by_multiplier_then_drop_then_alphabetical(global_config):
    quotes = [
        _quote("QUBT", 0.7),
        _quote("COHR", 0.7),
        _quote("CIEN", 0.9),  # same 1.5x tier as the other two but a deeper drop
        _quote("VT", 3.6),  # 4.0x
    ]
    result = _compute(global_config, quotes)
    assert [o["symbol"] for o in result.orders] == ["VT", "CIEN", "COHR", "QUBT"]


def test_excluded_unknown_and_whole_share_symbols_are_never_planned(global_config):
    quotes = [_quote("QBUT", 5.0), _quote("ZZZZ", 5.0), _quote("BYDDY", 5.0), _quote("VT", 5.0)]
    result = _compute(global_config, quotes)
    assert [o["symbol"] for o in result.orders] == ["VT"]
    assert {symbol for symbol, _ in result.skipped} >= {"QBUT", "ZZZZ", "BYDDY"}


def test_duplicate_quote_rows_produce_one_order(global_config):
    result = _compute(global_config, [_quote("VT", 2.0), _quote("VT", 2.0)])
    assert [o["symbol"] for o in result.orders] == ["VT"]


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "VT", "prior_close": 0.0, "prior_low": None, "decision_price": 99.0},
        {"symbol": "VT", "prior_close": float("nan"), "prior_low": None, "decision_price": 99.0},
        {"symbol": "VT", "prior_close": 100.0, "prior_low": None, "decision_price": -1.0},
        {"symbol": "VT", "prior_low": None, "decision_price": 99.0},  # prior_close missing
        {"symbol": "VT", "prior_close": "100", "prior_low": None, "decision_price": 99.0},
    ],
)
def test_a_malformed_quote_row_is_skipped_and_never_planned(global_config, bad):
    result = _compute(global_config, [bad, _quote("AAPL", 2.0)])
    assert [o["symbol"] for o in result.orders] == ["AAPL"]
    assert "VT" in {symbol for symbol, _ in result.skipped}


def test_the_computed_plan_document_is_schema_valid_and_round_trips(global_config, ctx):
    result = _compute(global_config, [_quote("VT", 2.0), _quote("AAPL", 1.2)])
    assert result.plan["generator"] == "backup"
    assert result.plan["ladder_version"] == LADDER_VERSION
    assert result.plan["universe_hash"] == _expected_universe_hash(global_config.tickers)
    assert dop.validate_plan(result.plan, **ctx).errors == ()


# -- D4 literal: a cap breach buys nothing, never a trimmed batch ------------


def test_plan_that_would_breach_the_daily_cap_buys_nothing(global_config):
    spent = [_broker_order("MSFT", amount=179.0)]
    result = _compute(global_config, [_quote("VT", 0.7)], orders_today=spent)  # $1.50 -> $180.50 > $180
    assert result.orders == ()
    assert "over_daily_cap" in _codes(result)


def test_literal_cap_rule_does_not_keep_the_part_that_fits(global_config):
    spent = [_broker_order("MSFT", amount=177.0)]
    quotes = [_quote("VT", 1.2), _quote("AAPL", 1.2)]  # $2.00 + $2.00: 179.0 fits, 181.0 does not
    result = _compute(global_config, quotes, orders_today=spent)
    assert result.orders == ()
    assert "over_daily_cap" in _codes(result)


def test_spend_landing_exactly_on_the_daily_cap_is_allowed(global_config):
    spent = [_broker_order("MSFT", amount=176.0)]
    result = _compute(global_config, [_quote("VT", 1.2), _quote("AAPL", 1.2)], orders_today=spent)
    assert result.errors == ()
    assert len(result.orders) == 2


def test_plan_that_would_breach_the_weekly_cap_buys_nothing(global_config):
    earlier_in_week = [_broker_order("MSFT", amount=899.0, day=MONDAY)]
    result = _compute(global_config, [_quote("VT", 1.2)], orders_today=earlier_in_week)
    assert result.orders == ()
    assert "over_weekly_cap" in _codes(result)


# -- compute inputs file: a JSON object {"quotes": [...]}; fail closed on anything else ---


def test_quotes_file_happy_path_matches_the_in_memory_compute(tmp_path, global_config):
    quotes = [_quote("VT", 2.0), _quote("AAPL", 1.2)]
    path = tmp_path / "quotes.json"
    path.write_text(json.dumps({"quotes": quotes}))
    from_file = dop.compute_plan_from_file(
        path, config=global_config, trading_day=DAY, slot=SLOT,
        generated_at_et=NOW, ladder_version=LADDER_VERSION, orders_today=(), tier=NORMAL_TIER,
    )
    assert from_file.errors == ()
    assert from_file.plan == _compute(global_config, quotes).plan



def test_missing_quotes_file_returns_empty_plan_and_structured_error(tmp_path, global_config):
    result = dop.compute_plan_from_file(
        tmp_path / "quotes.json", config=global_config, trading_day=DAY, slot=SLOT,
        generated_at_et=NOW, ladder_version=LADDER_VERSION, orders_today=(), tier=NORMAL_TIER,
    )
    assert result.orders == ()
    assert _codes(result) == ["inputs_missing"]


@pytest.mark.parametrize("text", ["", "{oops", "{}", '"x"', "null"])
def test_corrupt_quotes_file_returns_empty_plan_and_structured_error(tmp_path, global_config, text):
    path = tmp_path / "quotes.json"
    path.write_text(text)
    result = dop.compute_plan_from_file(
        path, config=global_config, trading_day=DAY, slot=SLOT,
        generated_at_et=NOW, ladder_version=LADDER_VERSION, orders_today=(), tier=NORMAL_TIER,
    )
    assert result.orders == ()
    assert _codes(result) == ["inputs_corrupt"]


# ---------------------------------------------------------------------------
# Pre-placement re-validation, run on every path (fresh pull, fresh quote)
# ---------------------------------------------------------------------------


def _revalidate(config, orders, *, history=(), quotes=None, tier=NORMAL_TIER):
    fresh = {o["symbol"]: o["decision_price"] for o in orders} if quotes is None else quotes
    return dop.revalidate_for_placement(
        orders, broker_orders=history, fresh_quotes=fresh, trading_day=DAY, tier=tier, config=config,
    )


def test_revalidation_passes_clean_orders_through_unchanged(config):
    orders = _planned("MSFT", "AAPL")
    result = _revalidate(config, orders)
    assert result.errors == ()
    assert result.orders == tuple(orders)


@pytest.mark.parametrize("fresh", [101.2, 101.3, 120.0])
def test_fresh_quote_at_or_above_prior_close_skips_that_order_only(config, fresh):
    orders = _planned("MSFT", "AAPL")
    result = _revalidate(config, orders, quotes={"MSFT": fresh, "AAPL": 100.0})
    assert [o["symbol"] for o in result.orders] == ["AAPL"]
    assert ("MSFT", "quote_not_below_prior_close") in result.skipped


def test_missing_fresh_quote_skips_that_order_fail_closed(config):
    orders = _planned("MSFT", "AAPL")
    result = _revalidate(config, orders, quotes={"AAPL": 100.0})
    assert [o["symbol"] for o in result.orders] == ["AAPL"]
    assert ("MSFT", "no_fresh_quote") in result.skipped


def test_claudes_notional_is_kept_when_the_fresh_quote_moves_but_stays_below_close(config):
    orders = [_order("MSFT", notional=4.0, multiplier=2.0)]
    result = _revalidate(config, orders, quotes={"MSFT": 95.0})  # deeper dip: still Claude's $4.00
    assert result.orders[0]["notional_usd"] == 4.0


def test_ticker_already_bought_today_is_skipped(config):
    orders = _planned("MSFT", "AAPL")
    result = _revalidate(config, orders, history=[_broker_order("MSFT")])
    assert [o["symbol"] for o in result.orders] == ["AAPL"]
    assert ("MSFT", "already_bought") in result.skipped


def test_cap_breach_found_in_the_fresh_pull_buys_nothing(config):
    orders = _planned("MSFT", "AAPL")  # $4 + $4 = $8
    result = _revalidate(config, orders, history=[_broker_order("NVDA", amount=175.0)])
    assert result.orders == ()
    assert "over_daily_cap" in _codes(result)


def test_revalidation_refuses_a_symbol_outside_the_universe(config):
    result = _revalidate(config, [_order("QBUT")])
    assert result.orders == ()
    assert result.errors or result.skipped


# ---------------------------------------------------------------------------
# Path selection (D8)
# ---------------------------------------------------------------------------


@pytest.fixture
def valid_result(config, ctx):
    return dop.validate_plan(_plan(config), **ctx)


@pytest.fixture
def invalid_result(config, ctx):
    return dop.validate_plan(_plan(config, trading_day="2026-10-06"), **ctx)


def test_timeouts_match_the_adr():
    assert dop.MAC_REACHABILITY_TIMEOUT_S == 60
    assert dop.MAC_PATH_BUDGET_S == 180


def test_mac_online_with_a_valid_plan_uses_the_mac_plan(valid_result):
    assert dop.choose_plan_path("online", plan=valid_result, elapsed_s=5) == "mac_plan"


@pytest.mark.parametrize("plan_fixture", ["invalid_result", None])
def test_mac_online_without_a_valid_plan_computes_on_the_mac(request, plan_fixture):
    plan = request.getfixturevalue(plan_fixture) if plan_fixture else None
    assert dop.choose_plan_path("online", plan=plan, elapsed_s=5) == "mac_compute"


@pytest.mark.parametrize("status", ["offline", "not_found", "timeout", "device_error"])
def test_mac_unreachable_falls_back_to_cloud_compute_even_if_a_plan_is_present(status, valid_result):
    assert dop.choose_plan_path(status, plan=valid_result, elapsed_s=5) == "cloud_compute"


def test_an_unrecognized_mac_status_falls_back_to_cloud_compute(valid_result):
    assert dop.choose_plan_path("banana", plan=valid_result, elapsed_s=5) == "cloud_compute"


@pytest.mark.parametrize("plan_fixture", ["valid_result", "invalid_result"])
def test_exhausting_the_mac_path_budget_falls_back_to_cloud_compute(request, plan_fixture):
    plan = request.getfixturevalue(plan_fixture)
    assert dop.choose_plan_path("online", plan=plan, elapsed_s=dop.MAC_PATH_BUDGET_S + 1) == "cloud_compute"


def test_the_chosen_path_is_always_one_of_the_three_recorded_sources(valid_result):
    allowed = {"mac_plan", "mac_compute", "cloud_compute"}
    for status in ("online", "offline", "not_found", "timeout", "device_error", "weird"):
        assert dop.choose_plan_path(status, plan=valid_result, elapsed_s=1) in allowed


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_inputs_give_byte_identical_plan_files(tmp_path, global_config):
    quotes = [_quote("VT", 2.0), _quote("AAPL", 1.2), _quote("COHR", 0.7)]
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    dop.write_plan(first, _compute(global_config, quotes).plan)
    dop.write_plan(second, _compute(global_config, list(reversed(quotes))).plan)
    assert first.read_bytes() == second.read_bytes()


def test_canonical_json_ignores_key_order_and_ends_with_a_newline(config):
    plan = _plan(config)
    shuffled = dict(reversed(list(plan.items())))
    assert dop.canonical_plan_json(plan) == dop.canonical_plan_json(shuffled)
    assert dop.canonical_plan_json(plan).endswith("\n")
    assert json.loads(dop.canonical_plan_json(plan)) == plan


def test_write_plan_is_atomic_and_leaves_no_temp_files(tmp_path, config):
    dop.write_plan(tmp_path / "order_plan_1130.json", _plan(config))
    assert [p.name for p in Path(tmp_path).iterdir()] == ["order_plan_1130.json"]


# ---------------------------------------------------------------------------
# Primary-side emit (phase 4): the resolved buy list becomes a plan file BEFORE placement
# ---------------------------------------------------------------------------


def _survivor(ticker="MSFT", buy_usd=4.0, multiplier=2.0, drop_pct=1.2, **over):
    """A Gate 6.5/6.7 survivor row as the live prompts carry it (extra evidence fields included)."""
    row = {
        "ticker": ticker,
        "buy_usd": buy_usd,
        "desired_usd": buy_usd,
        "multiplier": multiplier,
        "drop_pct": drop_pct,
        "current_price": 100.0,
        "prior_close": 101.2,
        "prior_low": 100.5,
        "unknown_prior_low": False,
        "selected_base_amount_usd": 2.0,
        "sizing_base_usd": 2.0,
        "reason": "first_buy_of_day",
    }
    row.update(over)
    return row


def _from_survivors(config, rows):
    return dop.plan_from_survivors(
        rows,
        config=config,
        trading_day=DAY,
        slot=SLOT,
        generated_at_et=NOW,
        ladder_version=LADDER_VERSION,
    )


def test_order_plan_path_is_under_the_configured_cron_root(monkeypatch, tmp_path):
    import dca_paths

    monkeypatch.setattr(dca_paths, "cron_tracking_root", lambda: str(tmp_path))
    assert dop.order_plan_path(DAY, SLOT) == tmp_path / "order_plans" / "2026-10-07" / "order_plan_1130.json"


def test_survivors_become_a_valid_claude_plan(config, ctx):
    result = _from_survivors(config, [_survivor("MSFT", 4.0), _survivor("AAPL", 2.0, 1.0, 0.7)])
    assert result.errors == ()
    assert result.plan["generator"] == "claude"
    assert result.plan["ladder_version"] == LADDER_VERSION
    assert result.plan["universe_hash"] == _expected_universe_hash(config.tickers)
    assert dop.validate_plan(result.plan, **ctx).errors == ()


def test_the_planned_notional_is_the_resolved_buy_usd_not_the_desired_amount(config):
    """Gate 6.5 may shrink a top-up; the plan must carry what Claude will actually place."""
    row = _survivor("MSFT", buy_usd=1.5, desired_usd=4.0)
    order = _from_survivors(config, [row]).orders[0]
    assert order["notional_usd"] == 1.5
    assert order["decision_price"] == 100.0
    assert order["prior_close"] == 101.2
    assert order["prior_low"] == 100.5
    assert order["ref_id"] == native_ref_id("1130", "2026-10-07", "MSFT")


def test_zero_buy_rows_are_dropped_and_an_all_zero_list_is_a_valid_empty_plan(config, ctx):
    result = _from_survivors(config, [_survivor("MSFT", 0.0), _survivor("AAPL", 0.0)])
    assert result.errors == ()
    assert result.orders == ()
    assert dop.validate_plan(result.plan, **ctx).errors == ()


def test_unknown_prior_low_survives_as_null(config):
    row = _survivor("MSFT", prior_low=None, unknown_prior_low=True)
    assert _from_survivors(config, [row]).orders[0]["prior_low"] is None


def test_survivors_are_ordered_by_allocation_priority(config):
    rows = [
        _survivor("QUBT", 1.5, 1.5, 0.7),
        _survivor("COHR", 1.5, 1.5, 0.7),
        _survivor("CIEN", 1.5, 1.5, 0.9),
        _survivor("VT", 4.0, 4.0, 3.6),
    ]
    assert [o["symbol"] for o in _from_survivors(config, rows).orders] == ["VT", "CIEN", "COHR", "QUBT"]


def test_survivor_order_does_not_change_the_plan_bytes(config):
    rows = [_survivor("MSFT", 4.0), _survivor("AAPL", 2.0, 1.0, 0.7), _survivor("AMD", 3.0, 1.5, 0.6)]
    forward = dop.canonical_plan_json(_from_survivors(config, rows).plan)
    backward = dop.canonical_plan_json(_from_survivors(config, list(reversed(rows))).plan)
    assert forward == backward



def test_survivor_ticker_is_stripped_and_uppercased(config, ctx):
    row = _survivor("  msft\n", 4.0)
    result = _from_survivors(config, [row])
    assert result.errors == ()
    assert result.orders[0]["symbol"] == "MSFT"


def test_survivor_with_malformed_numeric_priority_keys_fails_validation_cleanly(config):
    # This row has NaN for multiplier and drop_pct.
    # It should sort after AAPL and VT without crashing, and then fail validate_plan.
    bad_row = _survivor("MSFT", 4.0, multiplier=float('nan'), drop_pct=float('inf'))
    good_row = _survivor("AAPL", 2.0, multiplier=1.0, drop_pct=0.7)

    result = _from_survivors(config, [bad_row, good_row])
    assert result.plan is None
    assert "schema_violation" in _codes(result)


def test_survivor_with_extra_ignored_keys_succeeds(config):
    row = _survivor("MSFT", 4.0)
    row["extra_key"] = "ignored"
    row["another_key"] = 123

    result = _from_survivors(config, [row])
    assert result.errors == ()
    assert result.orders[0]["symbol"] == "MSFT"
    assert "extra_key" not in result.orders[0]


def test_survivor_missing_keys_fails_validation_cleanly(config):
    row = _survivor("MSFT", 4.0)
    del row["current_price"]

    result = _from_survivors(config, [row])
    assert result.plan is None
    assert "schema_violation" in _codes(result)


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (_survivor("QBUT"), "excluded_symbol"),
        (_survivor("ZZZZ"), "unknown_symbol"),
        (_survivor("BYDDY"), "unknown_symbol"),
        (_survivor("MSFT", buy_usd=dca_runner.MAX_PER_ORDER_USD + 0.01), "notional_over_cap"),
        (_survivor("MSFT", buy_usd=1.234), "invalid_notional"),
        (_survivor("MSFT", buy_usd=-1.0), "invalid_notional"),
        (_survivor("MSFT", buy_usd=float("nan")), "invalid_notional"),
        (_survivor("MSFT", prior_close=None), "schema_violation"),
        (_survivor("MSFT", current_price="100"), "schema_violation"),
        (_survivor("MSFT", multiplier=0.0), "schema_violation"),
    ],
)
def test_one_bad_survivor_fails_the_whole_emit_and_produces_no_plan(config, row, code):
    result = _from_survivors(config, [_survivor("AAPL", 2.0, 1.0, 0.7), row])
    assert result.plan is None
    assert result.orders == ()
    assert code in _codes(result)


def test_duplicate_survivor_tickers_fail_the_emit(config):
    result = _from_survivors(config, [_survivor("MSFT"), _survivor("MSFT")])
    assert result.plan is None
    assert "duplicate_symbol" in _codes(result)


@pytest.mark.parametrize("garbage", [None, {}, "x", 7, [None], ["MSFT"], [[]]])
def test_garbage_survivor_input_returns_a_structured_error_never_raises(config, garbage):
    result = _from_survivors(config, garbage)
    assert result.plan is None
    assert result.errors


# ---------------------------------------------------------------------------
# Review gaps found 2026-10-07 (ADR "Open design gaps"): cross-slot top-ups, buying power, spread
# ---------------------------------------------------------------------------

_WINDOWS = {
    "0935": ("09:35", "10:30"), "1030": ("10:30", "11:30"), "1130": ("11:30", "12:30"),
    "1230": ("12:30", "13:30"), "1330": ("13:30", "14:30"), "1430": ("14:30", "15:30"),
    "1530": ("15:30", "16:00"),
}


def _at(symbol, hh, mm, *, ref=None, amount=2.0, side="buy", state="filled"):
    row = _broker_order(symbol, side=side, amount=amount, ref=ref, state=state)
    return dataclasses.replace(row, created_at=datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=ET))


def test_latest_placement_times_are_the_next_slots_start_and_the_close():
    assert {slot: (hhmm[0], hhmm[1]) for slot, hhmm in _WINDOWS.items()} == {
        slot: tuple(t.strftime("%H:%M") for t in dop.placement_window(slot, DAY)) for slot in _WINDOWS
    }
    start, end = dop.placement_window("1130", DAY)
    assert start.tzinfo is not None and end.tzinfo is not None


def test_latest_placement_table_matches_the_window_function():
    assert dop.LATEST_PLACEMENT_ET == {slot: end for slot, (_, end) in _WINDOWS.items()}


# -- cross-slot top-ups: config/budgets.json one_order_per_ticker_per_day is false ---------


def test_slot_scope_keeps_a_ticker_bought_in_an_earlier_slot(config):
    orders = _planned("MSFT", "AAPL")
    history = [_at("MSFT", 9, 40, ref=f"dca-runner-0935-{DAY.isoformat()}-MSFT")]
    kept = dop.reconcile(orders, history, trading_day=DAY, slot=SLOT, scope="slot")
    assert [o["symbol"] for o in kept] == ["MSFT", "AAPL"]


def test_slot_scope_still_drops_a_ticker_bought_in_this_slots_window_under_any_reference(config):
    orders = _planned("MSFT", "AAPL")
    history = [_at("MSFT", 11, 45, ref="some-other-executor-ref")]
    assert [o["symbol"] for o in dop.reconcile(orders, history, trading_day=DAY, slot=SLOT, scope="slot")] == ["AAPL"]


def test_slot_scope_window_start_is_inclusive_and_end_is_exclusive(config):
    orders = _planned("MSFT", "AAPL")
    at_start = [_at("MSFT", 11, 30)]
    at_end = [_at("AAPL", 12, 30)]
    assert [o["symbol"] for o in dop.reconcile(orders, at_start, trading_day=DAY, slot=SLOT, scope="slot")] == ["AAPL"]
    assert [o["symbol"] for o in dop.reconcile(orders, at_end, trading_day=DAY, slot=SLOT, scope="slot")] == ["MSFT", "AAPL"]


def test_slot_scope_always_honors_an_exact_reference_match(config):
    orders = _planned("MSFT")
    history = [_at("MSFT", 9, 40, ref=orders[0]["ref_id"])]  # this slot's own reference, however it is stamped
    assert dop.reconcile(orders, history, trading_day=DAY, slot=SLOT, scope="slot") == []


@pytest.mark.parametrize("state", ["cancelled", "rejected", "failed"])
def test_slot_scope_counts_a_dead_attempt_inside_the_window(config, state):
    orders = _planned("MSFT")
    assert dop.reconcile(orders, [_at("MSFT", 11, 45, state=state)], trading_day=DAY, slot=SLOT, scope="slot") == []


def test_slot_scope_needs_a_slot_and_scope_must_be_known(config):
    with pytest.raises(ValueError):
        dop.reconcile(_planned("MSFT"), [], trading_day=DAY, scope="slot")
    with pytest.raises(ValueError):
        dop.reconcile(_planned("MSFT"), [], trading_day=DAY, slot=SLOT, scope="week")


def test_revalidation_slot_scope_does_not_call_an_earlier_slot_buy_already_bought(config):
    orders = _planned("MSFT")
    history = [_at("MSFT", 9, 40, ref=f"dca-runner-0935-{DAY.isoformat()}-MSFT")]
    result = dop.revalidate_for_placement(
        orders, broker_orders=history, fresh_quotes={"MSFT": 100.0}, trading_day=DAY, tier=NORMAL_TIER,
        config=config, slot=SLOT, scope="slot",
    )
    assert [o["symbol"] for o in result.orders] == ["MSFT"]
    day_scoped = dop.revalidate_for_placement(
        orders, broker_orders=history, fresh_quotes={"MSFT": 100.0}, trading_day=DAY, tier=NORMAL_TIER, config=config,
    )
    assert day_scoped.orders == ()  # the default stays the conservative day-wide exclusion


def test_revalidation_slot_scope_still_blocks_a_same_window_buy(config):
    result = dop.revalidate_for_placement(
        _planned("MSFT"), broker_orders=[_at("MSFT", 11, 45)], fresh_quotes={"MSFT": 100.0}, trading_day=DAY,
        tier=NORMAL_TIER, config=config, slot=SLOT, scope="slot",
    )
    assert ("MSFT", "already_bought") in result.skipped


# -- buying power -----------------------------------------------------------------------


def test_revalidation_fails_closed_when_the_batch_exceeds_buying_power(config):
    orders = _planned("MSFT", "AAPL")  # $4 + $4
    result = dop.revalidate_for_placement(
        orders, broker_orders=[], fresh_quotes={"MSFT": 100.0, "AAPL": 100.0}, trading_day=DAY, tier=NORMAL_TIER,
        config=config, buying_power_usd=7.99,
    )
    assert result.orders == ()
    assert "insufficient_buying_power" in _codes(result)


def test_revalidation_allows_a_batch_exactly_equal_to_buying_power(config):
    result = dop.revalidate_for_placement(
        _planned("MSFT", "AAPL"), broker_orders=[], fresh_quotes={"MSFT": 100.0, "AAPL": 100.0}, trading_day=DAY,
        tier=NORMAL_TIER, config=config, buying_power_usd=8.0,
    )
    assert result.errors == ()
    assert len(result.orders) == 2


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, True, "100"])
def test_a_malformed_buying_power_fails_closed(config, bad):
    result = dop.revalidate_for_placement(
        _planned("MSFT"), broker_orders=[], fresh_quotes={"MSFT": 100.0}, trading_day=DAY, tier=NORMAL_TIER,
        config=config, buying_power_usd=bad,
    )
    assert result.orders == ()
    assert "invalid_buying_power" in _codes(result)


def test_compute_buys_nothing_when_the_plan_exceeds_buying_power(global_config):
    result = dop.compute_plan(
        [_quote("VT", 1.2), _quote("AAPL", 1.2)], config=global_config, trading_day=DAY, slot=SLOT, generated_at_et=NOW,
        ladder_version=LADDER_VERSION, orders_today=(), tier=NORMAL_TIER, buying_power_usd=3.99,  # $2 + $2
    )
    assert result.plan is None
    assert "insufficient_buying_power" in _codes(result)


# -- Gate 6.7 spread --------------------------------------------------------------------


def _spread_result(config, spreads, symbols=("MSFT", "AAPL")):
    return dop.revalidate_for_placement(
        _planned(*symbols), broker_orders=[], fresh_quotes={s: 100.0 for s in symbols}, trading_day=DAY,
        tier=NORMAL_TIER, config=config, fresh_spreads=spreads,
    )


def test_spread_within_the_configured_threshold_passes(config):
    assert config.max_bid_ask_spread_pct == 1.0
    result = _spread_result(config, {"MSFT": (99.5, 100.5), "AAPL": (99.9, 100.1)})  # exactly 1.0% and 0.2%
    assert [o["symbol"] for o in result.orders] == ["MSFT", "AAPL"]


def test_a_wide_spread_skips_only_that_ticker(config):
    result = _spread_result(config, {"MSFT": (99.0, 101.0), "AAPL": (99.9, 100.1)})  # 2.0% and 0.2%
    assert [o["symbol"] for o in result.orders] == ["AAPL"]
    assert ("MSFT", "spread_not_ok") in result.skipped


@pytest.mark.parametrize(
    "quote",
    [None, (101.0, 99.0), (0.0, 100.0), (-1.0, 100.0), (float("nan"), 100.0), (99.0, float("inf")), (99.0,), "99/101"],
)
def test_a_missing_crossed_or_unusable_spread_skips_that_ticker(config, quote):
    result = _spread_result(config, {"MSFT": quote, "AAPL": (99.9, 100.1)})
    assert [o["symbol"] for o in result.orders] == ["AAPL"]
    assert ("MSFT", "spread_unavailable") in result.skipped


def test_a_ticker_with_no_spread_entry_at_all_is_skipped(config):
    result = _spread_result(config, {"AAPL": (99.9, 100.1)})
    assert ("MSFT", "spread_unavailable") in result.skipped


# -- trailing deployment figure (the kill switch's input, derived from broker history in the cloud) ------------


def _spent(symbol="MSFT", amount=100.0, *, when=NOW, state="filled", side="buy", agent="agentic", executed=None):
    return BrokerOrder(symbol=symbol, side=side, dollar_amount=amount, created_at=when, placed_agent=agent, state=state,
                       executed_dollar_amount=executed)


def test_trailing_deployed_is_zero_with_no_history():
    assert dop.trailing_deployed_usd([], now=NOW) == 0.0


def test_trailing_deployed_sums_committed_buys_in_the_seven_days_to_now():
    from datetime import timedelta

    orders = [_spent(amount=100.0, when=NOW - timedelta(days=6, hours=23)), _spent("AAPL", 50.5, when=NOW - timedelta(minutes=1))]
    assert dop.trailing_deployed_usd(orders, now=NOW) == pytest.approx(150.5)


def test_trailing_deployed_ignores_anything_older_than_seven_days_or_after_now():
    from datetime import timedelta

    orders = [_spent(amount=999.0, when=NOW - timedelta(days=7, seconds=1)), _spent("AAPL", 7.0, when=NOW + timedelta(seconds=1)),
              _spent("VT", 3.0, when=NOW - timedelta(days=7))]
    assert dop.trailing_deployed_usd(orders, now=NOW) == pytest.approx(3.0)  # exactly seven days back is inside


def test_trailing_deployed_counts_every_placed_agent_because_it_is_an_upper_bound_for_the_kill_switch():
    """The primary counts only scheduled afternoon runs; the cloud cannot tell them apart, so it counts all buys.
    Over-counting can only trip the 1x cap earlier, never later."""
    orders = [_spent(agent="agentic", amount=10.0), _spent("AAPL", 20.0, agent="user"), _spent("VT", 30.0, agent=None)]
    assert dop.trailing_deployed_usd(orders, now=NOW) == pytest.approx(60.0)


def test_trailing_deployed_skips_sells_and_buys_that_never_filled():
    orders = [_spent(side="sell", amount=500.0), _spent("AAPL", 40.0, state="cancelled"), _spent("VT", 5.0)]
    assert dop.trailing_deployed_usd(orders, now=NOW) == pytest.approx(5.0)


def test_trailing_deployed_counts_only_the_executed_part_of_a_cancelled_partial_fill():
    assert dop.trailing_deployed_usd([_spent(amount=40.0, state="cancelled", executed=12.5)], now=NOW) == pytest.approx(12.5)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
def test_a_malformed_committed_amount_raises_instead_of_reading_as_zero(bad):
    """An unknown fill is not a zero fill: a kill switch fed too little would stay off on the day it matters."""
    with pytest.raises(dca_runner.BudgetDataError):
        dop.trailing_deployed_usd([_spent(amount=bad)], now=NOW)


# -- plan_from_decisions: prior_low is safety evidence (PR #1644 review) ---------------------------------------


def _decision(symbol="MSFT", **over):
    row = {"symbol": symbol, "notional_usd": 4.0, "multiplier": 2.0, "drop_pct": 1.2, "decision_price": 100.0,
           "prior_close": 101.0, "prior_low": None}
    row.update(over)
    return row


def _from_decisions(config, rows):
    return dop.plan_from_decisions(
        rows, config=config, trading_day=DAY, slot=SLOT, generated_at_et=NOW, ladder_version=LADDER_VERSION,
        tier=NORMAL_TIER,
    )


@pytest.mark.parametrize("low,expected", [(None, None), (99.5, 99.5)])
def test_plan_from_decisions_keeps_an_absent_or_valid_prior_low(global_config, low, expected):
    result = _from_decisions(global_config, [_decision(prior_low=low)])
    assert [o["symbol"] for o in result.orders] == ["MSFT"]
    assert result.orders[0]["prior_low"] == expected


@pytest.mark.parametrize("bad_low", [0, 0.0, -1.0, float("nan"), float("inf"), float("-inf"), True, False, "99.5"])
def test_plan_from_decisions_rejects_corrupt_prior_low_instead_of_reading_it_as_unknown(global_config, bad_low):
    result = _from_decisions(global_config, [_decision(prior_low=bad_low)])
    assert result.orders == ()
    assert ("MSFT", "invalid_decision") in result.skipped


def test_plan_from_decisions_keeps_the_first_valid_row_when_a_duplicate_has_a_corrupt_prior_low(global_config):
    """The corrupt duplicate is reported, but it must not displace or cancel the valid row already chosen."""
    result = _from_decisions(global_config, [_decision(prior_low=99.5), _decision(prior_low=float("nan"))])
    assert [o["symbol"] for o in result.orders] == ["MSFT"]
    assert result.orders[0]["prior_low"] == 99.5
    assert ("MSFT", "invalid_decision") in result.skipped
