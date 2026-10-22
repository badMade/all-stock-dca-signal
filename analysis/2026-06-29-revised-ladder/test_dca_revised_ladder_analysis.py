from __future__ import annotations

import sys
from pathlib import Path

# Ensure parent analysis directory is on sys.path so module can be imported
analysis_dir = Path(__file__).resolve().parent
if str(analysis_dir) not in sys.path:
    sys.path.insert(0, str(analysis_dir))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from dca_revised_ladder_analysis import compute_tier_and_multiplier, tier_for_drop  # noqa: E402


def test_scalar_thresholds():
    cases = [
        (float("nan"), 0.0, "0x"),
        (-1.0, 0.0, "0x"),
        (0.0, 0.0, "0x"),
        (0.099, 0.0, "0x"),
        (0.1, 1.0, "1x"),
        (1.0, 1.0, "1x"),
        (1.99, 1.0, "1x"),
        (2.0, 1.5, "1.5x"),
        (4.99, 1.5, "1.5x"),
        (5.0, 2.0, "2x"),
        (7.99, 2.0, "2x"),
        (8.0, 3.0, "3x"),
        (12.5, 3.0, "3x"),
    ]
    for drop, exp_mult, exp_tier in cases:
        mult, tier = compute_tier_and_multiplier(drop)
        assert mult == exp_mult
        assert tier == exp_tier

        # Test adapted scalar helper tier_for_drop alias
        mult_alias, tier_alias = tier_for_drop(drop)
        assert mult_alias == exp_mult
        assert tier_alias == exp_tier


def test_vectorized_series():
    drops = pd.Series([np.nan, -1.0, 0.0, 0.099, 0.1, 1.0, 1.99, 2.0, 4.99, 5.0, 7.99, 8.0, 12.5])
    expected_mults = [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.5, 1.5, 2.0, 2.0, 3.0, 3.0]
    expected_tiers = ["0x", "0x", "0x", "0x", "1x", "1x", "1x", "1.5x", "1.5x", "2x", "2x", "3x", "3x"]

    mults, tiers = compute_tier_and_multiplier(drops)
    assert isinstance(mults, pd.Series)
    assert isinstance(tiers, pd.Series)

    pd.testing.assert_series_equal(mults, pd.Series(expected_mults, index=drops.index, dtype=float))
    pd.testing.assert_series_equal(tiers, pd.Series(expected_tiers, index=drops.index))


def test_signal_status_vectorized():
    mults = pd.Series([0.0, 1.0, 1.5, 0.0, 3.0])
    status = np.where(mults > 0, "BUY_PREVIEW", "NO_BUY")
    expected = np.array(["NO_BUY", "BUY_PREVIEW", "BUY_PREVIEW", "NO_BUY", "BUY_PREVIEW"])
    np.testing.assert_array_equal(status, expected)


def test_apply_budget_accepts_empty_decisions():
    from dca_revised_ladder_analysis import apply_budget

    decisions = pd.DataFrame(columns=["date", "multiplier", "drop_pct", "planned_spend", "close"])

    assert apply_budget(decisions).empty
