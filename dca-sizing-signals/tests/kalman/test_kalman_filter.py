"""Filter math: the research's EMA table, prior-vs-posterior innovation, predict-only
steps, beta recovery and tracking, tuning, and calibration on known-Gaussian data."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from kalman_signals.kalman_filter import (
    BetaState,
    FilterParams,
    beta_step,
    fit_params,
    ols,
    predict,
    run_filter,
    steady_state_gain,
)


@pytest.mark.parametrize("q, gain, span", [(0.001, 0.031, 63), (0.01, 0.095, 20), (0.1, 0.270, 6.4), (1.0, 0.618, 2.2)])
def test_steady_state_gain_matches_research_table(q, gain, span):
    k = steady_state_gain(q)
    assert k == pytest.approx(gain, abs=0.0015)
    assert 2.0 / k - 1.0 == pytest.approx(span, rel=0.03)


def test_steady_state_gain_is_the_riccati_fixed_point():
    q = 0.05
    p = 1.0  # P_prior / R, iterated
    for _ in range(500):
        k = p / (p + 1.0)
        p = (1.0 - k) * p + q
    assert p / (p + 1.0) == pytest.approx(steady_state_gain(q), rel=1e-9)


@pytest.mark.parametrize("q", [0.0, -1.0, math.inf, math.nan])
def test_steady_state_gain_rejects_bad_q(q):
    with pytest.raises(ValueError):
        steady_state_gain(q)


PARAMS = FilterParams(q=1e-4, q_alpha=1e-8, q_beta=1e-4, r_obs=2e-4, factor_sd=0.01, fit_through="x", n_train=1)
STATE = BetaState(alpha=0.001, beta=1.2, p11=1e-6, p12=0.0, p22=0.01)


def test_innovation_is_the_prior_one():
    step = beta_step(STATE, 0.03, 0.01, PARAMS)
    assert step.innovation == pytest.approx(0.03 - 0.001 - 1.2 * 0.01)
    # The posterior residual is shrunk by the same observation: never use it as a signal.
    post_resid = 0.03 - step.posterior.alpha - step.posterior.beta * 0.01
    assert abs(post_resid) < abs(step.innovation)


def test_update_shrinks_variance_and_keeps_p_valid():
    step = beta_step(STATE, 0.03, 0.02, PARAMS)
    post, prior = step.posterior, step.prior
    assert post.p22 < prior.p22 and post.p11 < prior.p11
    assert post.p11 > 0 and post.p22 > 0 and post.p11 * post.p22 - post.p12 ** 2 > 0
    assert step.innovation_var > PARAMS.r_obs


def test_predict_only_grows_uncertainty_and_keeps_the_state():
    nxt = predict(STATE, PARAMS.q_alpha, PARAMS.q_beta)
    assert (nxt.alpha, nxt.beta) == (STATE.alpha, STATE.beta)
    assert nxt.p22 == pytest.approx(STATE.p22 + PARAMS.q_beta)


def _pairs(n, beta, seed=1, f_sd=0.01, e_sd=0.015, beta_path=None):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        f = rng.gauss(0, f_sd)
        b = beta_path[i] if beta_path else beta
        out.append((b * f + rng.gauss(0, e_sd), f))
    return out


def test_recovers_a_constant_beta():
    obs = _pairs(500, 1.5)
    params = fit_params(obs, "t")
    run = run_filter(obs, params)
    assert run.state.beta == pytest.approx(1.5, abs=0.25)


def test_tracks_a_beta_change_better_than_full_sample_ols():
    path = [0.5] * 400 + [1.8] * 400
    obs = _pairs(800, None, seed=3, beta_path=path)
    params = fit_params(obs, "t")
    run = run_filter(obs, params)
    assert abs(run.state.beta - 1.8) < abs(ols(obs).beta - 1.8)


def test_missing_sessions_are_predict_only():
    obs = _pairs(300, 1.0)
    params = fit_params(obs, "t")
    holed = list(obs)
    for i in range(150, 160):
        holed[i] = None
    full, gap = run_filter(obs, params), run_filter(holed, params)
    assert gap.steps_since_init == full.steps_since_init - 10


def test_fit_needs_enough_data_and_a_moving_factor():
    assert fit_params(_pairs(50, 1.0), "t") is None
    assert fit_params([(0.01 * i, 0.0) for i in range(300)], "t") is None


def test_fit_records_its_version():
    params = fit_params(_pairs(300, 1.0), "2026-01-30")
    assert params.version.startswith("kalman_beta/v1;q=")
    assert "fit_through=2026-01-30" in params.version


def test_prior_innovations_are_calibrated_on_gaussian_data():
    # Out of sample: fit on the first 400, then score the next 1,000 with frozen params.
    obs = _pairs(1400, 1.1, seed=11)
    params = fit_params(obs[:400], "t")
    state = run_filter(obs[:400], params).state
    zs = []
    for s, f in obs[400:]:
        step = beta_step(state, s, f, params)
        zs.append(step.z)
        state = step.posterior
    assert statistics.pvariance(zs) == pytest.approx(1.0, abs=0.1)
    share = sum(1 for z in zs if abs(z) > 1.96) / len(zs)
    assert 0.03 <= share <= 0.08
