"""Shared toy inputs for the inventory-reservation tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

from inventory_signals import InventoryInputs, InventoryReservationParams, InventoryReservationSuggester

AS_OF = date(2026, 11, 23)  # a Monday; 09:35 ET = 14:35 UTC (EST)
NOW_0935 = datetime(2026, 11, 23, 14, 40, tzinfo=timezone.utc)
NOW_1530 = datetime(2026, 11, 23, 20, 35, tzinfo=timezone.utc)
# U-shaped toy profile: heavy open and close, quiet midday.
PROFILE = {"09:35": 0.30, "10:30": 0.15, "11:30": 0.10, "12:30": 0.08, "13:30": 0.09, "14:30": 0.13,
           "15:30": 0.15}


def toy_inputs(slot: str = "09:35", known_at: datetime = NOW_0935, **overrides) -> InventoryInputs:
    # Four names, $400 committed -> equal-weight target $100 each.
    base = InventoryInputs(
        as_of=AS_OF,
        slot=slot,
        known_at=known_at,
        committed_usd_by_ticker={"HEAVY": 200.0, "ONTGT": 100.0, "LIGHT": 50.0, "NONE": 50.0},
        candidate_tickers=["HEAVY", "ONTGT", "LIGHT"],
        daily_vol_by_ticker={"HEAVY": 0.03, "ONTGT": 0.03, "LIGHT": 0.03, "NONE": 0.03},
        slot_realized_vol_by_ticker={"HEAVY": 0.0, "ONTGT": 0.0, "LIGHT": 0.0},
        seasonal_profile=dict(PROFILE),
        reference_price_by_ticker={"HEAVY": 10.0, "ONTGT": 20.0},
    )
    return replace(base, **overrides)


def suggester(slot: str = "09:35", inputs: InventoryInputs | None = None, now: datetime = NOW_0935,
              params: InventoryReservationParams | None = None) -> InventoryReservationSuggester:
    data = inputs if inputs is not None else toy_inputs(slot)
    return InventoryReservationSuggester(slot, lambda a: data, params=params, clock=lambda: now)
