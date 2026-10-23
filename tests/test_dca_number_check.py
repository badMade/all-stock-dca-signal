"""Tests for ``dca_number_check``: prose number extraction and order-plan grounding."""

from __future__ import annotations

from decimal import Decimal

import dca_number_check as nc
from dca_order_plan import PlanError


def _payload(price=100.0, close=101.0, low=100.5, symbol="MSFT"):
    return {"sources": {"robinhood": {symbol: {"quote": {"price": price},
                                               "previous_close_bar": {"close": close, "low": low}}}}}


def _order(**over):
    order = {"symbol": "MSFT", "notional_usd": 2.0, "multiplier": 1.0, "drop_pct": 0.990099,
             "decision_price": 100.0, "prior_close": 101.0, "prior_low": 100.5,
             "ref_id": "dca-runner-1130-2026-10-07-MSFT"}
    order.update(over)
    return order


def _plan(*orders):
    return {"orders": list(orders)}


def _fields(errors):
    return sorted(e.detail.split(":")[0] for e in errors)


def test_extract_numbers_handles_currency_percent_commas_and_decimals():
    text = "Bought $1,234.50 of MSFT, down 5.5% (was 3), ok."
    assert nc.extract_numbers(text) == [Decimal("1234.50"), Decimal("5.5"), Decimal("3")]


def test_extract_numbers_ignores_non_strings_and_identifiers():
    assert nc.extract_numbers(None) == []
    assert nc.extract_numbers("ticker B2B no figures") == []


def test_unsupported_numbers_compares_by_value_not_spelling():
    assert nc.unsupported_numbers("price was 5.00 USD", "now $5") == []
    assert nc.unsupported_numbers("price was 5.00 USD", "now $6 and 5%") == [Decimal("6")]


def test_grounded_plan_has_no_findings():
    assert nc.plan_grounding_errors(_plan(_order()), _payload()) == []


def test_decision_price_not_in_quote_is_flagged():
    errors = nc.plan_grounding_errors(_plan(_order(decision_price=90.0, drop_pct=10.891089)), _payload())
    assert _fields(errors) == ["MSFT.decision_price"]
    assert all(isinstance(e, PlanError) and e.code == nc.UNSUPPORTED_NUMBER_CODE for e in errors)


def test_price_inside_relative_tolerance_passes():
    # 0.3% off the quote: inside PRICE_REL_TOL, so a live tick between fetch and plan is not noise-flagged.
    assert nc.plan_grounding_errors(_plan(_order(decision_price=100.3, drop_pct=0.693069)), _payload()) == []


def test_prior_close_and_low_are_tied_to_the_bar():
    errors = nc.plan_grounding_errors(_plan(_order(prior_close=110.0, prior_low=50.0, drop_pct=9.090909)), _payload())
    assert _fields(errors) == ["MSFT.prior_close", "MSFT.prior_low"]


def test_prior_low_none_is_not_checked():
    assert nc.plan_grounding_errors(_plan(_order(prior_low=None)), _payload()) == []


def test_inflated_drop_pct_is_flagged_even_with_plausible_prices():
    errors = nc.plan_grounding_errors(_plan(_order(drop_pct=12.0)), _payload())
    assert _fields(errors) == ["MSFT.drop_pct"]


def test_notional_must_be_multiplier_times_a_ladder_base():
    assert _fields(nc.plan_grounding_errors(_plan(_order(notional_usd=7.0)), _payload())) == ["MSFT.notional_usd"]
    # 3x of the $1 base and of the $2 base are both fine without a caller-supplied base.
    assert nc.plan_grounding_errors(_plan(_order(multiplier=3.0, notional_usd=3.0)), _payload()) == []
    assert nc.plan_grounding_errors(_plan(_order(multiplier=3.0, notional_usd=6.0)), _payload()) == []


def test_base_usd_for_narrows_the_accepted_base():
    plan = _plan(_order(multiplier=3.0, notional_usd=6.0))
    assert nc.plan_grounding_errors(plan, _payload(), base_usd_for=lambda o: 2.0) == []
    assert _fields(nc.plan_grounding_errors(plan, _payload(), base_usd_for=lambda o: 1.0)) == ["MSFT.notional_usd"]


def test_symbol_absent_from_payload_is_reported_not_trusted():
    errors = nc.plan_grounding_errors(_plan(_order(symbol="AAPL")), _payload())
    assert _fields(errors) == ["AAPL.decision_price"]


def test_malformed_inputs_become_findings_never_raise():
    assert nc.plan_grounding_errors({}, _payload())
    assert nc.plan_grounding_errors(_plan("junk"), _payload())
    assert nc.plan_grounding_errors(_plan(_order(decision_price=float("nan"))), {"sources": "bad"})
    assert nc.plan_grounding_errors(_plan(_order(decision_price=True)), None)


def test_any_source_may_support_a_price():
    payload = _payload(price=500.0)
    payload["sources"]["finance"] = {"MSFT": {"quote": {"price": 100.0}, "previous_close_bar": {"close": 101.0, "low": 100.5}}}
    assert nc.plan_grounding_errors(_plan(_order()), payload) == []
