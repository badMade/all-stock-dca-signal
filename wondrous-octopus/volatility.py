"""Optional GARCH(1,1) volatility scaling for forecast bands."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from data import COMPONENTS, TrainingPanel
from model_interface import repair_band_geometry
from scipy.optimize import minimize

MAX_PERSISTENCE = 0.999
_CLOSE_INDEX = COMPONENTS.index("close")


@dataclass(frozen=True)
class GarchParams:
    omega: float
    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.omega <= 0.0:
            raise ValueError("omega must be > 0")
        if self.alpha < 0.0 or self.beta < 0.0:
            raise ValueError("alpha/beta must be >= 0")
        if self.alpha + self.beta >= 1.0:
            raise ValueError("alpha + beta must be < 1")

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    @property
    def unconditional(self) -> float:
        return self.omega / (1.0 - self.persistence)


def conditional_variances(returns: np.ndarray, params: GarchParams) -> np.ndarray:
    returns = np.asarray(returns, dtype=np.float64)
    if returns.ndim != 1:
        raise ValueError("returns must be 1D")
    variances = np.empty_like(returns)
    sigma2 = params.unconditional
    for idx, value in enumerate(returns):
        variances[idx] = sigma2
        sigma2 = params.omega + params.alpha * float(value) ** 2 + params.beta * sigma2
    return variances


def forecast_variance(returns: np.ndarray, params: GarchParams) -> float:
    returns = np.asarray(returns, dtype=np.float64)
    if not len(returns):
        return params.unconditional
    path = conditional_variances(returns, params)
    return float(params.omega + params.alpha * float(returns[-1]) ** 2 + params.beta * float(path[-1]))


def negative_log_likelihood(returns: np.ndarray, params: GarchParams) -> float:
    returns = np.asarray(returns, dtype=np.float64)
    if len(returns) == 0:
        raise ValueError("returns must be non-empty")
    sigma2 = conditional_variances(returns, params)
    sigma2 = np.maximum(sigma2, 1e-12)
    return float(0.5 * np.sum(np.log(2.0 * math.pi * sigma2) + (returns**2) / sigma2))


def fit_garch(returns: np.ndarray) -> GarchParams:
    returns = np.asarray(returns, dtype=np.float64)
    if returns.ndim != 1 or len(returns) < 3:
        raise ValueError("returns must be 1D with at least 3 observations")
    sample_variance = float(np.mean(returns**2))
    sample_variance = max(sample_variance, 1e-12)

    def objective(x: np.ndarray) -> float:
        alpha = float(x[0])
        beta = float(x[1])
        persistence = alpha + beta
        if alpha < 0.0 or beta < 0.0 or persistence >= MAX_PERSISTENCE:
            return 1e30
        omega = sample_variance * (1.0 - persistence)
        params = GarchParams(omega=omega, alpha=alpha, beta=beta)
        return negative_log_likelihood(returns, params)

    starts = (
        np.array([0.08, 0.90], dtype=np.float64),
        np.array([0.05, 0.92], dtype=np.float64),
        np.array([0.12, 0.80], dtype=np.float64),
    )
    best_x = starts[0]
    best_value = float("inf")
    bounds = [(1e-10, MAX_PERSISTENCE - 1e-10), (1e-10, MAX_PERSISTENCE - 1e-10)]
    constraints = ({"type": "ineq", "fun": lambda x: MAX_PERSISTENCE - float(x[0] + x[1])},)
    for start in starts:
        result = minimize(objective, start, method="SLSQP", bounds=bounds, constraints=constraints)
        candidate_x = result.x if result.success else start
        candidate_value = objective(np.asarray(candidate_x, dtype=np.float64))
        if candidate_value < best_value:
            best_value = candidate_value
            best_x = np.asarray(candidate_x, dtype=np.float64)

    alpha = float(np.clip(best_x[0], 0.0, MAX_PERSISTENCE))
    beta = float(np.clip(best_x[1], 0.0, MAX_PERSISTENCE))
    if alpha + beta >= MAX_PERSISTENCE:
        total = alpha + beta
        alpha *= (MAX_PERSISTENCE - 1e-9) / total
        beta *= (MAX_PERSISTENCE - 1e-9) / total
    omega = sample_variance * (1.0 - alpha - beta)
    return GarchParams(omega=omega, alpha=alpha, beta=beta)


class GarchVolatility:
    def __init__(self, *, min_observations: int = 120, min_scale: float = 0.5, max_scale: float = 2.5) -> None:
        self.min_observations = min_observations
        self.min_scale = min_scale
        self.max_scale = max_scale
        self._params: dict[str, GarchParams] = {}
        self._sigma2: dict[str, float] = {}

    def fit(self, panel: TrainingPanel, *, seed: int) -> dict[str, Any]:
        del seed
        self._params = {}
        self._sigma2 = {}
        cutoff_raw = panel.normalization.get("window_end")
        cutoff = np.datetime64(str(cutoff_raw)) if cutoff_raw else None
        fitted = 0
        skipped_short_history = 0
        observations: dict[str, int] = {}
        for ticker, series in panel.series.items():
            usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
            if cutoff is not None:
                usable &= series.label_end_dates <= cutoff
            rows = np.flatnonzero(usable)
            observations[ticker] = int(len(rows))
            if len(rows) < self.min_observations:
                skipped_short_history += 1
                continue
            returns = series.targets[rows, _CLOSE_INDEX].astype(np.float64)
            params = fit_garch(returns)
            self._params[ticker] = params
            self._sigma2[ticker] = forecast_variance(returns, params)
            fitted += 1
        return {
            "fitted": fitted,
            "skipped_short_history": skipped_short_history,
            "observations": observations,
        }

    def scale(self, ticker: str) -> float:
        params = self._params.get(ticker)
        sigma2 = self._sigma2.get(ticker)
        if params is None or sigma2 is None:
            return 1.0
        raw = math.sqrt(max(sigma2, 1e-12) / params.unconditional)
        return float(min(self.max_scale, max(self.min_scale, raw)))

    def observe(self, ticker: str, close_return: float) -> None:
        params = self._params.get(ticker)
        sigma2 = self._sigma2.get(ticker)
        if params is None or sigma2 is None:
            return
        self._sigma2[ticker] = float(params.omega + params.alpha * close_return**2 + params.beta * sigma2)

    def apply(self, band: dict[str, dict[str, float]], *, ticker: str) -> dict[str, dict[str, float]]:
        scale = self.scale(ticker)
        scaled: dict[str, dict[str, float]] = {}
        for component in COMPONENTS:
            component_band = band[component]
            median = component_band["p50"]
            scaled[component] = {
                "p10": median + (component_band["p10"] - median) * scale,
                "p50": median,
                "p90": median + (component_band["p90"] - median) * scale,
            }
        return repair_band_geometry(scaled)

    def to_state(self) -> dict[str, Any]:
        return {
            "min_observations": self.min_observations,
            "min_scale": self.min_scale,
            "max_scale": self.max_scale,
            "params": {
                ticker: {"omega": params.omega, "alpha": params.alpha, "beta": params.beta}
                for ticker, params in self._params.items()
            },
            "sigma2": dict(self._sigma2),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "GarchVolatility":
        component = cls(
            min_observations=int(state.get("min_observations", 120)),
            min_scale=float(state.get("min_scale", 0.5)),
            max_scale=float(state.get("max_scale", 2.5)),
        )
        component._params = {
            ticker: GarchParams(
                omega=float(values["omega"]),
                alpha=float(values["alpha"]),
                beta=float(values["beta"]),
            )
            for ticker, values in state.get("params", {}).items()
        }
        component._sigma2 = {ticker: float(value) for ticker, value in state.get("sigma2", {}).items()}
        return component
