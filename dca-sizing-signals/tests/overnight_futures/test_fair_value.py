"""FairValueCalculator (docs/adr-overnight-futures-tracker.md, D4 and D9).

Derived from the CME published formula, not from any implementation:

    Fair Value = Cash x [1 + r x (days / 360)] - expected dividend points to expiry

D9 fail-closed rule: any missing or impossible input yields None, never a default
and never an exception, so the tracker can mark the row ``fv_quality = 'missing'``.
"""

from __future__ import annotations

import math

import pytest

from overnight_futures.fair_value import fair_value


def test_cme_worked_example() -> None:
    """CME's own example: cash 1146.00, r 5.7%, 78 days, dividends 3.47 -> 1156.68."""
    assert fair_value(cash=1146.00, rate=0.057, days_to_expiry=78, dividend_points=3.47) == pytest.approx(
        1156.68, abs=0.005
    )


def test_uses_actual_360_day_count() -> None:
    """360, not 365: one full 'year' of 360 days at 10% adds exactly 10% of cash."""
    assert fair_value(cash=1000.0, rate=0.10, days_to_expiry=360, dividend_points=0.0) == pytest.approx(1100.0)


def test_expiry_day_has_no_carry() -> None:
    """At zero days the premium is gone: fair value is cash minus any dividends still owed."""
    assert fair_value(cash=5000.0, rate=0.05, days_to_expiry=0, dividend_points=0.0) == pytest.approx(5000.0)


def test_dividends_lower_fair_value_point_for_point() -> None:
    base = fair_value(cash=5000.0, rate=0.04, days_to_expiry=30, dividend_points=0.0)
    with_div = fair_value(cash=5000.0, rate=0.04, days_to_expiry=30, dividend_points=12.5)
    assert base is not None and with_div is not None
    assert base - with_div == pytest.approx(12.5)


def test_negative_rate_is_allowed() -> None:
    """Rates have been negative in some markets; that is a value, not a missing input."""
    result = fair_value(cash=1000.0, rate=-0.005, days_to_expiry=90, dividend_points=0.0)
    assert result is not None and result < 1000.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cash": None, "rate": 0.05, "days_to_expiry": 30, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": None, "days_to_expiry": 30, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": 0.05, "days_to_expiry": None, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": 0.05, "days_to_expiry": 30, "dividend_points": None},
        {"cash": math.nan, "rate": 0.05, "days_to_expiry": 30, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": math.inf, "days_to_expiry": 30, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": 0.05, "days_to_expiry": 30, "dividend_points": math.nan},
        {"cash": 0.0, "rate": 0.05, "days_to_expiry": 30, "dividend_points": 1.0},
        {"cash": -1.0, "rate": 0.05, "days_to_expiry": 30, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": 0.05, "days_to_expiry": -1, "dividend_points": 1.0},
        {"cash": 1000.0, "rate": 0.05, "days_to_expiry": 30, "dividend_points": -0.01},
    ],
    ids=[
        "cash-none", "rate-none", "days-none", "div-none", "cash-nan", "rate-inf", "div-nan",
        "cash-zero", "cash-negative", "days-negative", "div-negative",
    ],
)
def test_missing_or_impossible_input_returns_none(kwargs: dict) -> None:
    assert fair_value(**kwargs) is None


def test_dividends_exceeding_carried_cash_return_none() -> None:
    """A non-positive fair value cannot anchor a log return; refuse rather than emit it."""
    assert fair_value(cash=10.0, rate=0.0, days_to_expiry=10, dividend_points=10.0) is None
