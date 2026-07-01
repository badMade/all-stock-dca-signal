"""Self-test for the graded DCA sizing ladder.

Runnable with either ``python test_near_close_dca.py`` or ``pytest``. Boundary
percentages are converted to fractions before calling graded_buy_multiplier, which
expects a fraction of the previous close (2.0% down == 0.02).
"""

from near_close_dca import ACTIVE_TICKERS, EXCLUDED_TICKERS, StrategyConfig, graded_buy_multiplier


# (drop percent from prior close, expected multiplier).
LADDER_BOUNDARY_CASES = [
    (0.09, 0.0),   # below the 0.1% minimum -> no buy
    (0.1, 1.0),    # 1x tier begins
    (0.999, 1.0),  # still 1x just under 1.0%
    (1.0, 1.5),    # 1.5x tier begins
    (1.999, 1.5),  # still 1.5x just under 2.0%
    (2.0, 2.0),    # 2x tier begins
    (2.999, 2.0),  # still 2x just under 3.0%
    (3.0, 2.5),    # 2.5x tier begins
    (3.999, 2.5),  # still 2.5x just under 4.0%
    (4.0, 3.0),    # 3x tier begins
    (4.999, 3.0),  # still 3x just under 5.0%
    (5.0, 3.5),    # 3.5x tier begins
    (5.999, 3.5),  # still 3.5x just under 6.0%
    (6.0, 4.0),    # 4x tier begins
    (6.999, 4.0),  # still 4x just under 7.0%
    (7.0, 4.5),    # 4.5x tier begins
    (7.999, 4.5),  # still 4.5x just under 8.0%
    (8.0, 5.0),    # 5x tier begins
    (12.0, 5.0),   # no upper cutoff, still 5x
]


def test_ladder_boundaries():
    config = StrategyConfig()
    for drop_pct, expected in LADDER_BOUNDARY_CASES:
        drop_fraction = drop_pct / 100.0
        actual = graded_buy_multiplier(drop_fraction, config)
        assert actual == expected, f"drop {drop_pct}% -> {actual}, expected {expected}"


def test_base_amount_is_one_dollar():
    assert StrategyConfig().base_buy_amount_usd == 1.0


def test_budget_caps():
    config = StrategyConfig()
    assert config.daily_base_cap_usd == 60.0
    assert config.reserve_cap_usd == 420.0
    assert config.hard_daily_max_usd == 120.0
    assert config.weekly_notional_cap_usd == 600.0


def test_ticker_universe():
    assert "IBM" in ACTIVE_TICKERS
    assert "IRDM" in ACTIVE_TICKERS
    assert "GLW" in ACTIVE_TICKERS
    assert "BYDDY" in EXCLUDED_TICKERS
    assert "BYDDY" not in ACTIVE_TICKERS


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("All self-tests passed.")


if __name__ == "__main__":
    _run_all()
