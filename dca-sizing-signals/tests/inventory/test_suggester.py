"""The SizingSuggester wrapper: shrink-only output and fail-closed on every bad input."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from dca_shared_interfaces.forecasting import SizingSuggester
from inventory_signals import (
    UNSCORED_MAE,
    InventoryReservationParams,
    InventoryReservationSuggester,
    InventorySuggestion,
    InventoryUnavailable,
)

from ._inputs import AS_OF, NOW_0935, NOW_1530, suggester, toy_inputs


def _by_ticker(outcome):
    return {s.ticker: s for s in outcome.suggestions}


def test_satisfies_sizing_suggester_protocol():
    assert isinstance(suggester(), SizingSuggester)


def test_unknown_slot_rejected_at_construction():
    with pytest.raises(ValueError):
        InventoryReservationSuggester("16:00", lambda a: None)


def test_overweight_name_shrinks_and_others_are_neutral():
    out = suggester().evaluate(AS_OF)
    assert out.ok, out
    rows = _by_ticker(out)
    assert set(rows) == {"HEAVY", "ONTGT", "LIGHT"}
    assert rows["HEAVY"].q == pytest.approx(1.0)
    assert rows["HEAVY"].multiplier_adjustment_pct == pytest.approx(-50.0)
    for t in ("ONTGT", "LIGHT"):
        assert rows[t].q == 0.0
        assert rows[t].multiplier_adjustment_pct == 0.0
    assert all(r.multiplier_adjustment_pct <= 0.0 for r in rows.values())


def test_rows_are_unscored_and_carry_slot():
    for s in suggester().evaluate(AS_OF).suggestions:
        assert isinstance(s, InventorySuggestion)
        assert s.scored is False and s.eval_metric_mae == UNSCORED_MAE and s.slot == "09:35"


def test_threshold_deepens_for_overweight_and_is_none_without_reference():
    rows = _by_ticker(suggester().evaluate(AS_OF))
    # HEAVY: q=1, full day ahead, median vol -> delta = 0.03 (z = 1, no widening).
    assert rows["HEAVY"].discount == pytest.approx(0.03)
    assert rows["HEAVY"].max_buy_price == pytest.approx(10.0 * math.exp(-0.03))
    assert rows["ONTGT"].max_buy_price == pytest.approx(20.0)  # q=0, no widening: the plain rule
    assert rows["LIGHT"].max_buy_price is None


def test_inventory_term_fades_toward_the_close():
    morning = _by_ticker(suggester().evaluate(AS_OF))["HEAVY"].discount
    close = _by_ticker(suggester("15:30", toy_inputs("15:30", NOW_1530), NOW_1530).evaluate(AS_OF))["HEAVY"]
    assert close.discount < morning
    assert close.discount == pytest.approx(0.03 * 0.15)
    # The size multiplier does not depend on time left.
    assert close.multiplier_adjustment_pct == pytest.approx(-50.0)


def test_cost_basis_not_market_value_drives_q():
    # The input is cost basis by contract; a price crash changes nothing here.
    inputs = toy_inputs(reference_price_by_ticker={"HEAVY": 1.0})
    assert _by_ticker(suggester(inputs=inputs).evaluate(AS_OF))["HEAVY"].q == pytest.approx(1.0)


def test_supplied_target_weights():
    weights = {"HEAVY": 0.5, "ONTGT": 0.2, "LIGHT": 0.2, "NONE": 0.1}
    rows = _by_ticker(suggester(inputs=toy_inputs(target_weight_by_ticker=weights)).evaluate(AS_OF))
    assert rows["HEAVY"].q == 0.0  # $200 of $400 is exactly its 50% target
    assert rows["ONTGT"].q == pytest.approx(0.25)


def test_suggest_returns_one_row_and_refuses_non_candidates():
    s = suggester()
    assert s.suggest("HEAVY", AS_OF).ticker == "HEAVY"
    with pytest.raises(InventoryUnavailable) as exc:
        s.suggest("NONE", AS_OF)
    assert exc.value.reason == "ticker_not_candidate"


def _boom(_):
    raise RuntimeError("broker down")


@pytest.mark.parametrize("reason,make", [
    ("input_load_failed", lambda: InventoryReservationSuggester("09:35", _boom, clock=lambda: NOW_0935)),
    ("invalid_input", lambda: InventoryReservationSuggester("09:35", lambda a: {"not": "inputs"},
                                                             clock=lambda: NOW_0935)),
    ("stale_inputs", lambda: suggester(inputs=replace(toy_inputs(), as_of=AS_OF - timedelta(days=1)))),
    ("slot_mismatch", lambda: suggester(inputs=toy_inputs(slot="10:30"))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(known_at=datetime(2026, 11, 23, 14, 40)))),
    ("future_inputs", lambda: suggester(inputs=toy_inputs(known_at=NOW_0935 + timedelta(minutes=1)))),
    ("stale_inputs", lambda: suggester(now=NOW_0935 + timedelta(hours=2))),
    # an earlier slot's snapshot replayed into the 15:30 slot
    ("stale_inputs", lambda: suggester("15:30", toy_inputs("15:30", known_at=NOW_1530 - timedelta(minutes=40)),
                                       NOW_1530)),
    ("no_committed_capital", lambda: suggester(inputs=toy_inputs(
        committed_usd_by_ticker={"HEAVY": 0.0, "ONTGT": 0.0, "LIGHT": 0.0}))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(
        committed_usd_by_ticker={"HEAVY": -1.0, "ONTGT": 100.0, "LIGHT": 50.0}))),
    ("missing_candidates", lambda: suggester(inputs=toy_inputs(candidate_tickers=[]))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(candidate_tickers="HEAVY"))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(candidate_tickers=["HEAVY", "HEAVY"]))),
    ("missing_cost_basis", lambda: suggester(inputs=toy_inputs(candidate_tickers=["NEW"]))),
    ("missing_volatility", lambda: suggester(inputs=toy_inputs(daily_vol_by_ticker={"ONTGT": 0.03}))),
    ("missing_volatility", lambda: suggester(inputs=toy_inputs(slot_realized_vol_by_ticker={}))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(
        daily_vol_by_ticker={"HEAVY": 0.0, "ONTGT": 0.03, "LIGHT": 0.03}))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(
        daily_vol_by_ticker={"HEAVY": math.nan, "ONTGT": 0.03, "LIGHT": 0.03}))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(
        slot_realized_vol_by_ticker={"HEAVY": -0.1, "ONTGT": 0.0, "LIGHT": 0.0}))),
    ("invalid_seasonal_profile", lambda: suggester(inputs=toy_inputs(seasonal_profile={"09:35": 1.0}))),
    ("invalid_target_weights", lambda: suggester(inputs=toy_inputs(target_weight_by_ticker={"HEAVY": 1.0}))),
    ("invalid_target_weights", lambda: suggester(inputs=toy_inputs(
        target_weight_by_ticker={"HEAVY": 0.5, "ONTGT": 0.5, "LIGHT": 0.5, "NONE": 0.5}))),
    ("invalid_input", lambda: suggester(inputs=toy_inputs(
        reference_price_by_ticker={"HEAVY": 0.0}))),
    ("invalid_input", lambda: suggester(params=InventoryReservationParams(m_min=0.0))),
])
def test_fails_closed(reason, make):
    out = make().evaluate(AS_OF)
    assert not out.ok and out.reason == reason and out.suggestions == (), out
    with pytest.raises(InventoryUnavailable):
        make().suggest("HEAVY", AS_OF)


def test_known_at_in_another_timezone_is_converted_to_et():
    # 09:40 ET expressed in UTC is still the same ET trading day.
    known = NOW_0935.astimezone(timezone(timedelta(hours=9)))  # next calendar day in Tokyo
    assert suggester(inputs=toy_inputs(known_at=known)).evaluate(AS_OF).ok
