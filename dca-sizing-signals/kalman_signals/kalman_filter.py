"""Pure Kalman filter math for the residual-dip shadow signal (docs/adr-kalman-residual-dip-shadow.md).

Two models, both on plain floats so the package stays stdlib-only (ADR "What the research
assumed": the research's sketch used numpy, which this package does not depend on):

  * the local-level model, kept only for ``steady_state_gain()``. The research (section 2)
    shows a steady-state local-level filter IS an EMA with alpha = K; the function lets a
    test pin that table instead of trusting it. Nothing here offers a "Kalman fair value"
    signal -- the ADR rejects it as an EMA in disguise;
  * the time-varying regression r_stock = alpha + beta * r_factor + noise, state
    (alpha, beta) following a random walk (ADR D4). ``beta_step`` returns the PRIOR
    innovation -- computed from the predicted state before this observation updates it --
    because the posterior residual is mechanically shrunk by the same observation
    (research 5). Every signal downstream uses the prior one.

Tuning (ADR D5) is a small grid over q, scored by one-step predictive log-likelihood on a
training window that the caller guarantees ends before the evaluation session. The
smoother is never run, so no future observation can leak into a filtered state.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

#: First filtered steps after an initialization that are never scored (ADR D5).
DEFAULT_BURN_IN = 60
#: Fewest observed (r_stock, r_factor) pairs a training window needs before a fit.
DEFAULT_MIN_TRAIN = 120
#: q = per-step beta-drift variance as a fraction of R / var(r_factor). Per-step beta
#: drift sd is then sqrt(q * R / var_f): with an idiosyncratic sd 1.5x the factor's, the
#: three values give roughly 0.005, 0.015 and 0.047 per day. Every value tried is one trial.
DEFAULT_Q_GRID = (1e-5, 1e-4, 1e-3)
#: alpha drifts far more slowly than beta: Q_alpha = q * R * ALPHA_DRIFT_SHARE.
ALPHA_DRIFT_SHARE = 0.01
MODEL_NAME = "kalman_beta/v1"


def steady_state_gain(q: float) -> float:
    """Steady-state gain K of the local-level model for q = Q/R (research section 2).

    p = (q + sqrt(q^2 + 4q)) / 2 solves the Riccati equation for P_prior / R, and
    K = p / (p + 1). The equivalent EMA has alpha = K, span N = 2 / K - 1.
    """
    if not (math.isfinite(q) and q > 0.0):
        raise ValueError("q must be finite and positive")
    p = (q + math.sqrt(q * q + 4.0 * q)) / 2.0
    return p / (p + 1.0)


@dataclass(frozen=True)
class BetaState:
    """Filtered (alpha, beta) and its symmetric 2x2 covariance [[p11, p12], [p12, p22]]."""

    alpha: float
    beta: float
    p11: float
    p12: float
    p22: float

    @property
    def beta_sd(self) -> float:
        return math.sqrt(max(self.p22, 0.0))


@dataclass(frozen=True)
class FilterParams:
    """Frozen per-(ticker, factor) parameters. ``fit_through`` is the newest session the
    fit read; every record carries ``version`` so a later reader knows which fit made it."""

    q: float
    q_alpha: float
    q_beta: float
    r_obs: float
    factor_sd: float
    fit_through: str
    n_train: int

    @property
    def version(self) -> str:
        return f"{MODEL_NAME};q={self.q:g};fit_through={self.fit_through};n={self.n_train}"


@dataclass(frozen=True)
class BetaStep:
    """One observation's result. ``innovation`` / ``innovation_var`` are the PRIOR ones."""

    prior: BetaState
    posterior: BetaState
    innovation: float
    innovation_var: float

    @property
    def z(self) -> float:
        return self.innovation / math.sqrt(self.innovation_var)

    @property
    def log_likelihood(self) -> float:
        return -0.5 * (math.log(2.0 * math.pi * self.innovation_var) + self.innovation ** 2 / self.innovation_var)


def predict(state: BetaState, q_alpha: float, q_beta: float) -> BetaState:
    """Random-walk predict step. Also the whole step for a session with no observation
    (a halt, a missing close): P grows, the state does not move (research 2, point 2)."""
    return replace(state, p11=state.p11 + q_alpha, p22=state.p22 + q_beta)


def beta_step(state: BetaState, r_stock: float, r_factor: float, params: FilterParams) -> BetaStep:
    """Predict, then update on one (r_stock, r_factor) observation."""
    prior = predict(state, params.q_alpha, params.q_beta)
    innovation = r_stock - prior.alpha - prior.beta * r_factor
    # H = [1, f]; S = H P H' + R; P H' = [p11 + f p12, p12 + f p22].
    ph0 = prior.p11 + r_factor * prior.p12
    ph1 = prior.p12 + r_factor * prior.p22
    s = ph0 + r_factor * ph1 + params.r_obs
    k0, k1 = ph0 / s, ph1 / s
    posterior = BetaState(
        alpha=prior.alpha + k0 * innovation,
        beta=prior.beta + k1 * innovation,
        # P - K (H P), written out; H P = (P H')' by symmetry.
        p11=prior.p11 - k0 * ph0,
        p12=prior.p12 - k0 * ph1,
        p22=prior.p22 - k1 * ph1,
    )
    return BetaStep(prior=prior, posterior=posterior, innovation=innovation, innovation_var=s)


@dataclass(frozen=True)
class Ols:
    alpha: float
    beta: float
    resid_var: float
    factor_var: float
    n: int


def ols(pairs: Sequence[tuple[float, float]]) -> Ols | None:
    """Plain OLS of r_stock on r_factor. None when the factor has no variance or n < 3."""
    n = len(pairs)
    if n < 3:
        return None
    mean_s = sum(s for s, _ in pairs) / n
    mean_f = sum(f for _, f in pairs) / n
    sxx = sum((f - mean_f) ** 2 for _, f in pairs)
    if not sxx > 0.0:
        return None
    sxy = sum((f - mean_f) * (s - mean_s) for s, f in pairs)
    beta = sxy / sxx
    alpha = mean_s - beta * mean_f
    resid = sum((s - alpha - beta * f) ** 2 for s, f in pairs) / (n - 2)
    return Ols(alpha=alpha, beta=beta, resid_var=resid, factor_var=sxx / (n - 1), n=n)


def initial_state(pairs: Sequence[tuple[float, float]]) -> BetaState | None:
    """Initialize from OLS on ``pairs`` (the first burn-in observations), with the OLS
    coefficient covariance as P0. Those observations fall inside the burn-in, so they are
    never scored by the state they produced."""
    fit = ols(pairs)
    if fit is None or not fit.resid_var > 0.0:
        return None
    n = fit.n
    mean_f = sum(f for _, f in pairs) / n
    sxx = fit.factor_var * (n - 1)
    var_beta = fit.resid_var / sxx
    return BetaState(
        alpha=fit.alpha,
        beta=fit.beta,
        p11=fit.resid_var / n + mean_f * mean_f * var_beta,
        p12=-mean_f * var_beta,
        p22=var_beta,
    )


@dataclass(frozen=True)
class FilterRun:
    """The state after filtering a window, and how many observed steps followed init."""

    state: BetaState
    steps_since_init: int
    log_likelihood: float  # summed over scored (post-burn-in) steps only


def run_filter(
    observations: Sequence[tuple[float, float] | None],
    params: FilterParams,
    burn_in: int = DEFAULT_BURN_IN,
) -> FilterRun | None:
    """Initialize on the first ``burn_in`` observed pairs, then filter the rest.

    ``None`` entries are sessions with no observation: predict only. Returns None when
    there are not enough observed pairs to initialize.
    """
    observed = [i for i, ob in enumerate(observations) if ob is not None]
    if len(observed) < burn_in:
        return None
    init_end = observed[burn_in - 1]
    state = initial_state([observations[i] for i in observed[:burn_in]])  # type: ignore[misc]
    if state is None:
        return None
    steps = 0
    loglik = 0.0
    for ob in observations[init_end + 1:]:
        if ob is None:
            state = predict(state, params.q_alpha, params.q_beta)
            continue
        step = beta_step(state, ob[0], ob[1], params)
        steps += 1
        if steps > burn_in:
            loglik += step.log_likelihood
        state = step.posterior
    return FilterRun(state=state, steps_since_init=steps, log_likelihood=loglik)


def fit_params(
    observations: Sequence[tuple[float, float] | None],
    fit_through: str,
    q_grid: Sequence[float] = DEFAULT_Q_GRID,
    burn_in: int = DEFAULT_BURN_IN,
    min_train: int = DEFAULT_MIN_TRAIN,
) -> FilterParams | None:
    """Pick q from ``q_grid`` by predictive log-likelihood on ``observations``.

    R is the OLS residual variance of the whole window (an upper bound on the true
    observation noise when beta really varies; kept simple and recorded in the version).
    Ties go to the smaller q -- the smoother beta, which absorbs less of a real dip
    (research 4, "beta chasing"). Returns None with too little data or a flat factor.
    """
    pairs = [ob for ob in observations if ob is not None]
    if len(pairs) < max(min_train, 2 * burn_in):
        return None
    fit = ols(pairs)
    if fit is None or not (fit.resid_var > 0.0 and fit.factor_var > 0.0):
        return None
    best: FilterParams | None = None
    best_ll = -math.inf
    for q in sorted(q_grid):
        params = FilterParams(
            q=q,
            q_alpha=q * fit.resid_var * ALPHA_DRIFT_SHARE,
            q_beta=q * fit.resid_var / fit.factor_var,
            r_obs=fit.resid_var,
            factor_sd=math.sqrt(fit.factor_var),
            fit_through=fit_through,
            n_train=len(pairs),
        )
        run = run_filter(observations, params, burn_in=burn_in)
        if run is None or not math.isfinite(run.log_likelihood):
            continue
        if run.log_likelihood > best_ll:
            best, best_ll = params, run.log_likelihood
    return best
