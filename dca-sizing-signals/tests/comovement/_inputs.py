"""Shared toy ComovementInputs for the suggester and publishing tests (see _toy.py: none
of this is market data)."""

from __future__ import annotations

from datetime import datetime, timezone

from comovement_signals import ComovementConfig, ComovementInputs

from ._toy import day, three_block_rows

AS_OF = day(8)
# The toy history is 8 returns long, so the 120-session production minimum is lowered to 8
# here. Every other threshold is the ADR default.
TOY_CONFIG = ComovementConfig(min_sessions=8, min_overlap_sessions=8)
FIXED_NOW = datetime(2026, 1, 9, 20, 0, tzinfo=timezone.utc)


def clock():
    return FIXED_NOW


def toy_inputs(**overrides) -> ComovementInputs:
    """Block A (A1,A2,A3) proposes 40 + 30 + 30 = $100 against a $200 budget (cap $70), so
    signal 1 scales it by 0.7. B1 proposes $20, C1 $10: untouched.

    Day returns: all of block A is down 2% to 3% (3 of 3 = 100%), so signal 2 marks it.
    Escalations inside A: $10, $20, $30 (total $60, largest $30, scale 0.5, caps $5, $10,
    $15). Per-ticker multiplier = (proposed - escalation + cap) / proposed:
    A1 (40 - 10 + 5) / 40 = 0.875; A2 (30 - 20 + 10) / 30 = 2/3; A3 (30 - 30 + 15) / 30 = 0.5.
    """
    values = dict(
        close_rows=three_block_rows(),
        proposed_buys_usd_by_ticker={"A1": 40.0, "A2": 30.0, "A3": 30.0, "B1": 20.0, "C1": 10.0},
        day_budget_usd=200.0,
        day_returns_by_ticker={"A1": -0.03, "A2": -0.025, "A3": -0.02, "B1": 0.01, "B2": 0.01,
                               "B3": 0.0, "C1": 0.01, "C2": 0.0, "C3": 0.0},
        day_returns_session=AS_OF,
        escalation_usd_by_ticker={"A1": 10.0, "A2": 20.0, "A3": 30.0},
    )
    values.update(overrides)
    return ComovementInputs(**values)
