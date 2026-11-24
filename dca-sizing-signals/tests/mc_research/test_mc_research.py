"""The TDD checks listed in docs/mc_variance_reduction.md ("TDD checks worth writing first").

Every test uses a fixed root seed, so the numeric bands below are deterministic, not flaky: they
were set from a pilot run (2026-11-23) and leave room only for platform float differences.
"""

from __future__ import annotations

import ast
import math
import sys
from pathlib import Path

import pytest
from mc_research import (
    InsufficientEffectiveSampleSize,
    LadderVariant,
    RandomStreams,
    antithetic,
    antithetic_pair_correlation,
    black_scholes_call,
    black_scholes_put,
    compare,
    control_variate,
    derive_seed,
    effective_sample_size,
    flat_dca,
    flat_dca_gbm_mean,
    gbm_paths,
    graded_dca,
    importance_sampling,
    paired_difference,
    plain,
    run_strategies,
    stepped_multiplier,
)

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "mc_research"
TIERS = ((0.0, 1.0), (0.01, 2.0), (0.02, 3.0))  # illustrative 1x/2x/3x, not the live config


def _call_study(seed: int, antithetic_paths: bool = False):
    paths = gbm_paths(s0=100.0, mu=0.0, sigma=0.2, n_steps=1, n_paths=10_000, dt=1.0, root_seed=seed,
                      antithetic=antithetic_paths)
    terminal = paths.terminal_prices()
    return paths, terminal, [max(s - 100.0, 0.0) for s in terminal]


# --- video reproduction -----------------------------------------------------------------------


def test_black_scholes_reference_value():
    assert black_scholes_call(100, 100, 0.0, 0.2, 1.0) == pytest.approx(7.9656, abs=1e-4)
    # put-call parity: C - P = S - K e^{-rT}
    c, p = black_scholes_call(100, 95, 0.03, 0.25, 0.5), black_scholes_put(100, 95, 0.03, 0.25, 0.5)
    assert c - p == pytest.approx(100 - 95 * math.exp(-0.03 * 0.5), abs=1e-9)


def test_video_reproduction_plain_antithetic_and_control_variate():
    """S0=K=100, vol 20%, 10,000 sims: plain SE ~0.13, antithetic ~0.106, CV ~0.06, all CIs hold BS."""
    bs = black_scholes_call(100, 100, 0.0, 0.2, 1.0)
    paths, terminal, payoff = _call_study(seed=3)
    base = plain(payoff)
    cv = control_variate(payoff, terminal, paths.terminal_mean)
    _, _, anti_payoff = _call_study(seed=3, antithetic_paths=True)
    anti = antithetic(anti_payoff)

    assert 0.12 < base.se < 0.14
    assert 0.095 < anti.se < 0.115
    assert 0.055 < cv.se < 0.065
    for est in (base, cv, anti):
        assert est.ci_low <= bs <= est.ci_high, est
    assert 4.0 < cv.variance_reduction_vs(base) < 5.5  # the video's ~4.7x
    assert anti.n == 5_000  # SE from pair means, not 2n draws


def test_estimators_are_unbiased_across_seeds():
    bs = black_scholes_call(100, 100, 0.0, 0.2, 1.0)
    for seed in range(10, 16):
        paths, terminal, payoff = _call_study(seed)
        for est in (plain(payoff), control_variate(payoff, terminal, paths.terminal_mean),
                    control_variate(payoff, terminal, paths.terminal_mean, pilot_fraction=0.2)):
            assert abs(est.mean - bs) < 4 * est.se, (seed, est)
        _, _, anti_payoff = _call_study(seed, antithetic_paths=True)
        anti = antithetic(anti_payoff)
        assert abs(anti.mean - bs) < 4 * anti.se, (seed, anti)


def test_antithetic_pairs_are_negatively_correlated_for_a_monotone_payoff():
    _, _, payoff = _call_study(seed=3, antithetic_paths=True)
    assert antithetic_pair_correlation(payoff) < -0.3


# --- seed determinism and stream independence ---------------------------------------------------


def test_same_seed_is_bit_identical_and_different_seeds_differ():
    kwargs = dict(s0=50.0, mu=0.05, sigma=0.3, n_steps=20, n_paths=8, dt=1 / 252)
    assert gbm_paths(root_seed=42, **kwargs) == gbm_paths(root_seed=42, **kwargs)
    assert gbm_paths(root_seed=42, **kwargs).prices != gbm_paths(root_seed=43, **kwargs).prices


def test_spawned_streams_differ_and_key_types_do_not_alias():
    streams = RandomStreams(7)
    assert streams.stream("market", 1).random() != streams.stream("market", 2).random()
    assert derive_seed(7, "1") != derive_seed(7, 1)
    assert streams.child("market").stream(1).random() == streams.stream("market", 1).random()
    with pytest.raises(TypeError):
        derive_seed(7, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RandomStreams(-1)


def test_path_i_does_not_depend_on_how_many_paths_were_requested():
    kwargs = dict(s0=50.0, mu=0.05, sigma=0.3, n_steps=10, dt=1 / 252, root_seed=5)
    assert gbm_paths(n_paths=10, **kwargs).prices[:4] == gbm_paths(n_paths=4, **kwargs).prices
    anti = dict(kwargs, antithetic=True)
    assert gbm_paths(n_paths=10, **anti).prices[:4] == gbm_paths(n_paths=4, **anti).prices


def test_antithetic_paths_mirror_their_shocks():
    paths = gbm_paths(s0=100.0, mu=0.0, sigma=0.2, n_steps=5, n_paths=2, dt=1.0, root_seed=1, antithetic=True)
    up, down = paths.prices
    drift = -0.5 * 0.2 * 0.2
    for t in range(1, 6):
        # log returns are drift + v z and drift - v z, so they sum to twice the drift
        step = math.log(up[t] / up[t - 1]) + math.log(down[t] / down[t - 1])
        assert step == pytest.approx(2 * drift, abs=1e-12)


# --- common random numbers ------------------------------------------------------------------


def _ladder_study(n_paths: int = 400):
    paths = gbm_paths(s0=100.0, mu=0.07, sigma=0.25, n_steps=63, n_paths=n_paths, dt=1 / 252, root_seed=7)
    variant = LadderVariant("graded_1x2x3x", TIERS)
    outcomes = run_strategies(paths, {
        "flat": lambda p: flat_dca(p, daily_budget=10.0),
        variant.name: variant.runner(daily_budget=10.0, cash_rate=0.04, dt=1 / 252),
    })
    mean = flat_dca_gbm_mean(s0=100.0, mu=0.07, n_steps=63, dt=1 / 252, daily_budget=10.0)
    return paths, outcomes, mean


def test_crn_identity_strategy_vs_itself_is_exactly_zero():
    paths, outcomes, _ = _ladder_study(n_paths=20)
    same = run_strategies(paths, {"a": lambda p: flat_dca(p, daily_budget=10.0),
                                  "b": lambda p: flat_dca(p, daily_budget=10.0)})
    est = paired_difference(same["a"], same["b"])
    assert est.mean == 0.0 and est.se == 0.0
    assert est.variance_reduction_vs(plain(outcomes["flat"])) == math.inf


def test_crn_and_flat_dca_control_shrink_the_difference_variance():
    _, outcomes, flat_mean = _ladder_study()
    report = compare(outcomes, variant="graded_1x2x3x", baseline="flat", baseline_mean=flat_mean)
    assert report.crn.mean == pytest.approx(report.independent.mean)
    assert report.crn_variance_reduction() > 10.0  # pilot: ~25x
    assert report.crn_cv_variance_reduction() > report.crn_variance_reduction()  # pilot: ~104x
    assert report.crn_cv.method == "crn_paired+control_variate"
    assert compare(outcomes, variant="graded_1x2x3x", baseline="flat").crn_cv is None


def test_flat_dca_gbm_mean_is_the_true_mean():
    _, outcomes, flat_mean = _ladder_study()
    est = plain(outcomes["flat"])
    assert abs(est.mean - flat_mean) < 4 * est.se


def test_adding_a_branchy_variant_does_not_move_another_variants_outcomes():
    """Synchronization: strategies are pure functions of a pre-generated PathSet."""
    paths, _, _ = _ladder_study(n_paths=30)
    a = LadderVariant("a", TIERS).runner(daily_budget=10.0, cash_rate=0.0, dt=1 / 252)

    def branchy(p):  # buys only on alternate deep dips: a very different branch pattern
        return graded_dca(p[::2], daily_budget=20.0, tiers=((0.03, 5.0),))

    alone = run_strategies(paths, {"a": a})
    together = run_strategies(paths, {"b": branchy, "a": a})
    assert alone["a"] == together["a"]


# --- strategy rules ---------------------------------------------------------------------------


def test_stepped_multiplier_matches_the_live_ladder_convention():
    assert stepped_multiplier(-0.01, TIERS) == 0.0  # price rose: below the first threshold
    assert stepped_multiplier(0.0, TIERS) == 1.0
    assert stepped_multiplier(0.015, TIERS) == 2.0
    assert stepped_multiplier(0.5, TIERS) == 3.0
    assert stepped_multiplier(math.nan, TIERS) == 0.0  # fail closed


def test_equal_contributions_and_cash_accounting():
    path = (100.0, 100.0, 90.0, 99.0)  # flat, -10%, +10%
    assert flat_dca(path, daily_budget=10.0) == pytest.approx((10 / 100 + 10 / 90 + 10 / 99) * 99)
    # Graded: step1 0% -> 1x spends all 10; step2 -10% -> 3x wants 30 but cash is only the 10 just
    # accrued; step3 is up -> 0x, so step3's 10 stays as cash.
    expected = (10 / 100 + 10 / 90) * 99 + 10.0
    assert graded_dca(path, daily_budget=10.0, tiers=TIERS) == pytest.approx(expected)
    # A ladder that never buys still returns every contributed dollar (plus interest).
    assert graded_dca(path, daily_budget=10.0, tiers=((0.5, 1.0),)) == pytest.approx(30.0)
    assert graded_dca(path, daily_budget=10.0, tiers=((0.5, 1.0),), cash_rate=0.05, dt=1.0) > 30.0


def test_bad_paths_fail_closed():
    with pytest.raises(ValueError):
        flat_dca((100.0,), daily_budget=1.0)
    with pytest.raises(ValueError):
        graded_dca((100.0, math.nan), daily_budget=1.0, tiers=TIERS)
    with pytest.raises(ValueError):
        gbm_paths(s0=100.0, mu=0.0, sigma=0.2, n_steps=1, n_paths=3, dt=1.0, root_seed=1, antithetic=True)
    with pytest.raises(ValueError):
        run_strategies(gbm_paths(s0=1.0, mu=0.0, sigma=0.1, n_steps=1, n_paths=1, dt=1.0, root_seed=1), {})


# --- estimator contract -----------------------------------------------------------------------


def test_estimators_fail_closed_on_bad_input():
    with pytest.raises(ValueError):
        plain([1.0])
    with pytest.raises(ValueError):
        plain([1.0, math.inf])
    with pytest.raises(ValueError):
        antithetic([1.0, 2.0, 3.0, 4.0, 5.0])
    with pytest.raises(ValueError):
        paired_difference([1.0, 2.0], [1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        control_variate([1.0, 2.0], [1.0, 2.0], math.nan)
    with pytest.raises(ValueError):
        control_variate([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], 2.0, pilot_fraction=0.5)


def test_estimate_carries_ci_and_serializes():
    est = plain([1.0, 2.0, 3.0, 4.0])
    assert est.ci_low < est.mean < est.ci_high
    assert est.n == 4 and est.n_eff == 4.0
    assert set(est.as_dict()) == {"mean", "se", "ci_low", "ci_high", "n", "n_eff", "method"}


# --- importance sampling ESS guard ------------------------------------------------------------


def test_effective_sample_size():
    assert effective_sample_size([1.0] * 50) == pytest.approx(50.0)
    assert effective_sample_size([1000.0] + [1e-6] * 99) == pytest.approx(1.0, abs=1e-3)
    with pytest.raises(ValueError):
        effective_sample_size([1.0, -1.0])


def test_importance_sampling_tail_probability_and_ess_guard():
    """P(Z > 3) by shifting the draw mean to 3: the IS estimate brackets the truth and reports ESS."""
    rng = RandomStreams(11).stream("is")
    shift = 3.0
    draws = [rng.gauss(shift, 1.0) for _ in range(4_000)]
    weights = [math.exp(-shift * x + 0.5 * shift * shift) for x in draws]  # phi(x) / phi(x - shift)
    indicator = [1.0 if x > 3.0 else 0.0 for x in draws]
    truth = 0.5 * math.erfc(3.0 / math.sqrt(2.0))
    est = importance_sampling(indicator, weights, ess_floor=10.0)
    assert est.ci_low <= truth <= est.ci_high
    assert est.n_eff < est.n
    with pytest.raises(InsufficientEffectiveSampleSize):
        importance_sampling([1.0, 1.0, 1.0], [1000.0, 1e-9, 1e-9], ess_floor=2.0)


# --- no live coupling -------------------------------------------------------------------------


def test_mc_research_imports_only_the_stdlib_and_itself():
    allowed = set(sys.stdlib_module_names) | {"mc_research", "__future__"}
    for py_file in PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            outside = set(names) - allowed
            assert not outside, f"{py_file.name} imports {sorted(outside)}"
