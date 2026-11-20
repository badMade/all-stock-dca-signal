"""Tests for ``dca_order_plan.validate_plan`` accepting a plan that CARRIES the day's universe.

Piece 3(d) of docs/adr-backup-planner-daily-universe.md (accepted 2026-11-20). Written BEFORE the implementation (TDD).

Mirrors what ``scripts/backup_planner._validate_plan_first`` already does for plan-first documents. A Mac plan may carry
an optional ``universe`` key (the day's sorted list). When it does:

* the plan's ``universe_hash`` must be the hash of the CARRIED list, not of the reader's ``active_tickers``;
* every order symbol must be in the carried list, which may hold names the reader's own list lacks;
* the validator keeps bounds of its own, since no reader list vouches for the carried one: at most 100 symbols,
  canonical, sorted and unique, and no symbol in the reader's ``excluded_tickers``;
* every dollar cap is unchanged.

A plan without the key is validated exactly as before. Expected values come from the ADR and the hash rule, not from the
validator's output.
"""

from __future__ import annotations

import pytest
from test_dca_order_plan import DAY, LADDER_VERSION, SLOT, _codes, _expected_universe_hash, _order, _plan

import dca_config
import dca_order_plan as dop
import dca_runner


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


@pytest.fixture
def ctx(config):
    """The reader's own view, as in test_dca_order_plan.py."""
    return {
        "trading_day": DAY,
        "slot": SLOT,
        "ladder_version": LADDER_VERSION,
        "active_tickers": tuple(config.tickers),
        "excluded_tickers": frozenset(config.excluded_tickers),
        "max_per_order_usd": dca_runner.MAX_PER_ORDER_USD,
    }


CARRIED = ["AAPL", "MSFT", "ZZZZ"]  # ZZZZ is not in the reader's own list


def _carried_plan(config, universe=CARRIED, orders=None, **over):
    """A plan carrying ``universe`` with a hash that matches it unless ``over`` says otherwise."""
    plan = _plan(config, orders=orders, universe_hash=_expected_universe_hash(universe), **over)
    plan["universe"] = list(universe)
    return plan


def test_a_plan_without_a_carried_list_is_validated_as_before(config, ctx):
    result = dop.validate_plan(_plan(config), **ctx)
    assert result.errors == ()


def test_a_carried_list_is_accepted_and_replaces_the_readers_list(config, ctx):
    result = dop.validate_plan(_carried_plan(config), **ctx)
    assert result.errors == ()
    assert [o["symbol"] for o in result.orders] == ["MSFT", "AAPL"]


def test_an_order_for_a_name_only_the_carried_list_has_is_accepted(config, ctx):
    plan = _carried_plan(config, orders=[_order("ZZZZ")])
    assert dop.validate_plan(plan, **ctx).errors == ()


def test_an_order_outside_the_carried_list_is_rejected_even_if_the_reader_lists_it(config, ctx):
    # NVDA is in the reader's own list but not in the carried one: the carried list governs.
    assert "NVDA" in ctx["active_tickers"]
    plan = _carried_plan(config, orders=[_order("NVDA")])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "unknown_symbol" in _codes(result)


def test_the_hash_must_be_the_carried_lists_not_the_readers(config, ctx):
    plan = _carried_plan(config)
    plan["universe_hash"] = _expected_universe_hash(config.tickers)
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "universe_hash_mismatch" in _codes(result)


def test_a_tampered_carried_list_fails_its_own_hash(config, ctx):
    plan = _carried_plan(config)
    plan["universe"] = [*CARRIED, "YYYY"]
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "universe_hash_mismatch" in _codes(result)


def test_a_carried_list_of_101_names_is_too_large(config, ctx):
    big = sorted(f"T{i:03d}" for i in range(101))
    plan = _carried_plan(config, universe=big, orders=[_order("T000")])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "universe_too_large" in _codes(result)


def test_a_carried_list_of_exactly_100_names_is_allowed(config, ctx):
    edge = sorted(f"T{i:03d}" for i in range(100))
    plan = _carried_plan(config, universe=edge, orders=[_order("T000")])
    assert dop.validate_plan(plan, **ctx).errors == ()


@pytest.mark.parametrize(
    "bad",
    [
        ["MSFT", "AAPL"],  # not sorted
        ["AAPL", "AAPL", "MSFT"],  # duplicate
        ["AAPL", "msft"],  # not canonical
        [],  # empty
        "AAPL",  # not a list
        ["AAPL", 7],  # non-string member
    ],
)
def test_a_non_canonical_carried_list_is_a_schema_violation(config, ctx, bad):
    plan = _carried_plan(config, orders=[_order("AAPL")])
    plan["universe"] = bad
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "schema_violation" in _codes(result)


def test_an_excluded_name_in_the_carried_list_is_rejected_even_with_no_order_for_it(config, ctx):
    universe = ["AAPL", "MSFT", "QBUT"]
    plan = _carried_plan(config, universe=universe, orders=[_order("MSFT")])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert "excluded_in_universe" in _codes(result)


def test_the_per_order_dollar_cap_still_applies_with_a_carried_list(config, ctx):
    plan = _carried_plan(config, orders=[_order("MSFT", notional=ctx["max_per_order_usd"] + 1)])
    result = dop.validate_plan(plan, **ctx)
    assert result.orders == ()
    assert result.errors
