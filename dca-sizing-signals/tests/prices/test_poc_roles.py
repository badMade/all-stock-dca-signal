"""Offline check of the PoC's role discovery (the live PoC cannot run without keys).

Expected behaviour: one ticker per role, chosen deterministically from the window's own
data; common-stock symbols only; core names excluded; a missing role raises rather than
quietly shrinking the universe.
"""

from __future__ import annotations

from datetime import date

import pytest
from market_data_ingest.prices.errors import ProviderError
from market_data_ingest.prices.models import CorporateAction
from market_data_ingest.prices.poc import discover_roles

from .conftest import weekdays

W = weekdays(date(2026, 7, 1), date(2026, 9, 30))[:60]


def presence():
    return {
        "NVDA": W,
        "RVS": W,  # reverse split mid-window
        "HIDV": W,  # high yield
        "LOWD": W,  # low yield
        "GONE": W[:30],  # delisted after session 30
        "NEWCO": W[20:],  # listed at session 20
        "ABC.WS": W[:10],  # a warrant: never a candidate
        "BLIP": W[:10] + W[15:],  # a gap, not a delisting or an IPO
    }


ACTIONS = [
    CorporateAction(
        source="massive", action_type="reverse_split", symbol="RVS", ex_date=W[25], split_from=10, split_to=1
    ),
    CorporateAction(source="massive", action_type="cash_dividend", symbol="HIDV", ex_date=W[30], cash_amount=1.5),
    CorporateAction(source="massive", action_type="cash_dividend", symbol="LOWD", ex_date=W[30], cash_amount=0.1),
    CorporateAction(source="massive", action_type="cash_dividend", symbol="NVDA", ex_date=W[30], cash_amount=9.0),
]
CLOSES = {"NVDA": 100.0, "RVS": 5.0, "HIDV": 20.0, "LOWD": 20.0, "NEWCO": 30.0}


def test_each_role_is_found_from_the_data():
    roles = discover_roles(
        sessions=W, presence=presence(), actions=ACTIONS, last_close=CLOSES, has_yahoo=lambda s: True, exclude=["NVDA"]
    )

    assert roles == {"reverse_split": "RVS", "high_dividend": "HIDV", "delisted": "GONE", "ipo": "NEWCO"}


def test_a_missing_role_raises():
    p = presence()
    del p["GONE"]
    with pytest.raises(ProviderError):
        discover_roles(
            sessions=W, presence=p, actions=ACTIONS, last_close=CLOSES, has_yahoo=lambda s: True, exclude=["NVDA"]
        )
