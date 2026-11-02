"""Signal 4, cheaper-leg tilt (SPECULATIVE, shadow only): docs/adr-comovement-signals.md,
open question 7's proposed default. For a pair with daily-return correlation of at least
0.75, regress log(A) on log(B) by OLS over the trailing formation window only, z-score
the spread on the evaluation day against the formation residuals, and record a shadow
weight for leg A = clip(0.5 - 0.15 * z, 0.2, 0.8) of the pair's combined DCA dollars
(leg B gets 1 - A). The research report rates this 30% likely to add value; nothing reads
the record.

Leg A is the alphabetically first ticker. All numbers are hand-built toys (_toy.py).
"""

from __future__ import annotations

import math

import pytest

from comovement_signals import (
    SIGNAL_CHEAPER_LEG_TILT,
    TiltConfig,
    cheaper_leg_tilt_shadow,
    ols_hedge,
    tilt_weight,
)

from ._toy import day, rows_from_closes

# --- tilt_weight: clip(0.5 - 0.15 z, 0.2, 0.8) -----------------------------------------


@pytest.mark.parametrize("z,expected", [
    (0.0, 0.5),     # no stretch: an even split
    (1.0, 0.35),    # A rich by 1 sd: 0.5 - 0.15
    (-1.0, 0.65),   # A cheap by 1 sd: 0.5 + 0.15
    (2.0, 0.2),     # exactly at the lower bound: 0.5 - 0.30 = 0.2
    (-2.0, 0.8),    # exactly at the upper bound
    (3.0, 0.2),     # past the bound: clipped
    (-10.0, 0.8),
])
def test_tilt_weight_values_and_clip(z, expected):
    assert tilt_weight(z) == pytest.approx(expected)


@pytest.mark.parametrize("z", [0.0, 0.3, 1.0, 1.7, 2.0, 2.5, 50.0])
def test_tilt_weight_is_symmetric(z):
    # Swapping the legs flips the sign of z, so w(z) + w(-z) must be exactly one pair's
    # worth of dollars.
    assert tilt_weight(z) + tilt_weight(-z) == pytest.approx(1.0)


def test_tilt_weight_always_inside_bounds():
    for k in range(-100, 101):
        assert 0.2 <= tilt_weight(k / 10) <= 0.8


@pytest.mark.parametrize("z", [float("nan"), float("inf"), -float("inf")])
def test_tilt_weight_rejects_non_finite(z):
    with pytest.raises(ValueError):
        tilt_weight(z)


# --- ols_hedge ----------------------------------------------------------------------------


def test_ols_hedge_by_hand():
    # x = 0, 1, 2, 3 ; y = 1, 3, 5, 8.  mean x 1.5, mean y 4.25.
    # dx = (-1.5, -0.5, 0.5, 1.5), dy = (-3.25, -1.25, 0.75, 3.75)
    # Sxy = 4.875 + 0.625 + 0.375 + 5.625 = 11.5 ; Sxx = 5 -> beta = 2.3
    # intercept = 4.25 - 2.3 * 1.5 = 0.8
    # residuals 0.2, -0.1, -0.4, 0.3 (sum 0); sample sd = sqrt(0.30 / 3) = sqrt(0.1)
    fit = ols_hedge([0, 1, 2, 3], [1, 3, 5, 8])
    assert fit.beta == pytest.approx(2.3)
    assert fit.intercept == pytest.approx(0.8)
    assert fit.residual_mean == pytest.approx(0.0, abs=1e-12)
    assert fit.residual_sd == pytest.approx(math.sqrt(0.1))


def test_ols_hedge_degenerate_inputs_return_none():
    assert ols_hedge([1, 1, 1], [1, 2, 3]) is None   # no variance in x
    assert ols_hedge([0, 1, 2], [1, 3, 5]) is None   # perfect fit: zero residual sd
    assert ols_hedge([0], [1]) is None


# --- the full shadow evaluation on a toy pair ------------------------------------------

# Log prices relative to ln(100), on day(0)..day(3) (the formation window, 4 sessions) and
# day(4) (the evaluation day):
#   B1 (x): 0, 0.1, 0.3, 0.4, then 0.5
#   A1 (y): 0, 0.2, 0.7, 0.9, then EVAL
# Formation OLS of y on x: mean x 0.2, mean y 0.45; dx = (-0.2, -0.1, 0.1, 0.2),
# dy = (-0.45, -0.25, 0.25, 0.45); Sxy = 0.09 + 0.025 + 0.025 + 0.09 = 0.23, Sxx = 0.1,
# so beta = 2.3. (Adding ln(100) to both logs changes only the intercept.) Residuals
# 0.01, -0.02, 0.02, -0.01, mean 0, sample sd = sqrt(0.001 / 3).
# Fitted A1 on day(4): relative log 0.45 + 2.3 * (0.5 - 0.2) = 1.14.
# Formation returns: B1 0.1, 0.2, 0.1; A1 0.2, 0.5, 0.2 = 3 * B1 - 0.1, so r = 1 >= 0.75.
SD = math.sqrt(0.001 / 3)
TOY = TiltConfig(formation_sessions=4, min_overlap_sessions=3)


def _rows(eval_y: float, extra_history: bool = False) -> list[dict]:
    x = [0.0, 0.1, 0.3, 0.4, 0.5]
    y = [0.0, 0.2, 0.7, 0.9, eval_y]
    rows = (rows_from_closes("B1", [100.0 * math.exp(v) for v in x])
            + rows_from_closes("A1", [100.0 * math.exp(v) for v in y]))
    if extra_history:
        # Five wild earlier sessions, day(-5)..day(-1), before the formation window.
        rows += rows_from_closes("B1", [10.0, 900.0, 3.0, 400.0, 50.0], start=-5)
        rows += rows_from_closes("A1", [700.0, 2.0, 600.0, 5.0, 80.0], start=-5)
    return rows


def test_cheap_leg_a_gets_more_dollars():
    # A1 one formation sd below its fitted value: z = -1, weight A = 0.65.
    result = cheaper_leg_tilt_shadow(_rows(1.14 - SD), as_of=day(4), config=TOY,
                                     pair_dollars={("A1", "B1"): 100.0})
    assert result.ok and result.signal == SIGNAL_CHEAPER_LEG_TILT and result.skipped == {}
    (record,) = result.records
    p = record.payload
    assert p["pair"] == ["A1", "B1"] and p["leg_a"] == "A1" and p["leg_b"] == "B1"
    assert p["hedge_ratio"] == pytest.approx(2.3)
    assert p["formation_start"] == day(0).isoformat() and p["formation_end"] == day(3).isoformat()
    assert p["evaluation_session"] == day(4).isoformat()
    assert p["correlation"] == pytest.approx(1.0)
    assert p["z"] == pytest.approx(-1.0, abs=1e-9)
    assert p["weight_a"] == pytest.approx(0.65) and p["weight_b"] == pytest.approx(0.35)
    assert p["shadow_usd_a"] == pytest.approx(65.0) and p["shadow_usd_b"] == pytest.approx(35.0)
    assert p["label"] == "speculative_shadow_only"
    assert p["live_effect"] == "none"
    assert record.inputs_known_at == day(4)


def test_rich_leg_a_is_clipped_at_the_lower_bound():
    # z = +3: 0.5 - 0.45 = 0.05, clipped up to 0.2; B gets 0.8.
    (record,) = cheaper_leg_tilt_shadow(_rows(1.14 + 3 * SD), as_of=day(4), config=TOY).records
    assert record.payload["z"] == pytest.approx(3.0, abs=1e-9)
    assert record.payload["weight_a"] == pytest.approx(0.2)
    assert record.payload["weight_b"] == pytest.approx(0.8)
    assert "shadow_usd_a" not in record.payload  # no dollars given, none invented


def test_hedge_ratio_uses_the_formation_window_only():
    base = cheaper_leg_tilt_shadow(_rows(1.14 - SD), as_of=day(4), config=TOY).records[0].payload
    # 1. A different evaluation-day close moves z but not the hedge ratio.
    moved = cheaper_leg_tilt_shadow(_rows(1.14 + SD), as_of=day(4), config=TOY).records[0].payload
    assert moved["hedge_ratio"] == pytest.approx(base["hedge_ratio"])
    assert moved["z"] == pytest.approx(1.0, abs=1e-9)
    # 2. Wild history before the formation window changes nothing at all.
    older = cheaper_leg_tilt_shadow(_rows(1.14 - SD, extra_history=True), as_of=day(4), config=TOY)
    assert older.records[0].payload == base
    # 3. Had the evaluation day been inside the fit, beta would differ from 2.3:
    #    OLS over all five points (x 0..0.5, y with EVAL = 1.14 - SD) is not 2.3.
    fit5 = ols_hedge([0.0, 0.1, 0.3, 0.4, 0.5], [0.0, 0.2, 0.7, 0.9, 1.14 - SD])
    assert abs(fit5.beta - 2.3) > 1e-3


def test_no_lookahead_rows_after_as_of_change_nothing():
    base = cheaper_leg_tilt_shadow(_rows(1.14 - SD), as_of=day(4), config=TOY)
    future = rows_from_closes("A1", [1.0, 5000.0], start=5) + rows_from_closes("B1", [9000.0, 1.0], start=5)
    later = cheaper_leg_tilt_shadow(_rows(1.14 - SD) + future, as_of=day(4), config=TOY)
    assert later.records == base.records


def test_low_correlation_pair_is_not_evaluated():
    # C1 formation returns -0.1, -0.2, -0.1: r = -1 with B1 and A1, below 0.75.
    rows = _rows(1.14 - SD) + rows_from_closes("C1", [100.0 * math.exp(v) for v in (0.0, -0.1, -0.3, -0.4, -0.5)])
    result = cheaper_leg_tilt_shadow(rows, as_of=day(4), config=TOY)
    assert [r.payload["pair"] for r in result.records] == [["A1", "B1"]]


def test_explicit_pairs_restrict_the_evaluation():
    rows = _rows(1.14 - SD) + rows_from_closes("C1", [100.0 * math.exp(v) for v in (0.0, 0.1, 0.3, 0.4, 0.5)])
    result = cheaper_leg_tilt_shadow(rows, as_of=day(4), config=TOY, pairs=[("B1", "C1")])
    # Pair order is normalised (leg A first alphabetically). B1 and C1 are identical, so
    # the formation fit is perfect: zero residual sd, no z-score, no record.
    assert result.records == ()
    assert result.skipped == {"B1|C1": "degenerate_spread"}


def test_negative_hedge_ratio_is_skipped():
    # With the correlation floor lowered to -1 (config, toy only), a pair that moves
    # opposite gets through to the fit and its negative beta is refused: a buy-only tilt
    # between legs that move apart is not a substitute pair.
    rows = _rows(1.14 - SD) + rows_from_closes("C1", [100.0 * math.exp(v) for v in (0.0, -0.1, -0.35, -0.4, -0.5)])
    cfg = TiltConfig(formation_sessions=4, min_overlap_sessions=3, min_pair_correlation=-1.0)
    result = cheaper_leg_tilt_shadow(rows, as_of=day(4), config=cfg, pairs=[("B1", "C1")])
    assert result.records == ()
    assert result.skipped == {"B1|C1": "non_positive_hedge_ratio"}


def test_insufficient_formation_history_gives_no_record():
    cfg = TiltConfig(formation_sessions=10, min_overlap_sessions=3)
    result = cheaper_leg_tilt_shadow(_rows(1.14 - SD), as_of=day(4), config=cfg)
    assert result.ok and result.records == ()
    assert result.skipped == {"A1|B1": "insufficient_history"}


def test_stale_prices_fail_closed():
    result = cheaper_leg_tilt_shadow(_rows(1.14 - SD), as_of=day(14), config=TOY)
    assert not result.ok and result.reason == "stale_prices" and result.records == ()


def test_stale_leg_is_skipped():
    rows = [r for r in _rows(1.14 - SD) if not (r["ticker"] == "B1" and r["date"] == day(4))]
    result = cheaper_leg_tilt_shadow(rows, as_of=day(4), config=TOY)
    assert result.records == ()
    assert result.skipped == {"A1|B1": "stale_pair"}


def test_production_defaults_match_the_adr():
    cfg = TiltConfig()
    assert (cfg.min_pair_correlation, cfg.center, cfg.slope, cfg.lower, cfg.upper) == (0.75, 0.5, 0.15, 0.2, 0.8)
    assert cfg.formation_sessions == 252
