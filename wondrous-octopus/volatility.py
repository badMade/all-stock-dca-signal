"""A small GARCH(1,1) volatility component for wondrous-octopus."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from data import COMPONENTS, TrainingPanel
from model_interface import repair_band_geometry

MAX_PERSISTENCE = 0.995
_MIN_VARIANCE = 1e-12
_CLOSE_INDEX = COMPONENTS.index("close")


@dataclass(frozen=True)
class GarchParams:
    omega: float
    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.omega <= 0.0:
            raise ValueError("omega must be strictly positive")
        if self.alpha < 0.0 or self.beta < 0.0:
            raise ValueError("alpha and beta must be non-negative")
        if self.persistence >= 1.0:
            raise ValueError("alpha + beta must be strictly below one")

    @property
    def persistence(self) -> float:
        return float(self.alpha + self.beta)

    @property
    def unconditional(self) -> float:
        return float(self.omega / (1.0 - self.persistence))


def conditional_variances(returns: np.ndarray, params: GarchParams) -> np.ndarray:
    series = np.asarray(returns, dtype=np.float64)
    if not len(series):
        return np.zeros(0, dtype=np.float64)
    sigma2 = np.empty(len(series), dtype=np.float64)
    sigma2[0] = params.unconditional
    for index in range(1, len(series)):
        sigma2[index] = params.omega + params.alpha * series[index - 1] ** 2 + params.beta * sigma2[index - 1]
    return sigma2


def forecast_variance(returns: np.ndarray, params: GarchParams) -> float:
    series = np.asarray(returns, dtype=np.float64)
    sigma2 = params.unconditional
    for value in series:
        sigma2 = params.omega + params.alpha * float(value) ** 2 + params.beta * sigma2
    return float(sigma2)


def negative_log_likelihood(returns: np.ndarray, params: GarchParams) -> float:
    series = np.asarray(returns, dtype=np.float64)
    sigma2 = params.unconditional
    total = 0.0
    for value in series:
        value = float(value)
        total += math.log(sigma2) + (value * value) / sigma2
        sigma2 = params.omega + params.alpha * value * value + params.beta * sigma2
    return 0.5 * total


def _candidate_params(sample_variance: float, alpha: float, beta: float) -> GarchParams | None:
    if alpha < 0.0 or beta < 0.0:
        return None
    persistence = alpha + beta
    if persistence > MAX_PERSISTENCE:
        return None
    omega = sample_variance * (1.0 - persistence)
    if omega <= 0.0:
        return None
    return GarchParams(omega=omega, alpha=alpha, beta=beta)


def fit_garch(returns: np.ndarray) -> GarchParams:
    series = np.asarray(returns, dtype=np.float64)
    if len(series) < 2:
        raise ValueError("fit_garch requires at least two returns")
    sample_variance = max(float(np.mean(series**2)), _MIN_VARIANCE)
    best_params: GarchParams | None = None
    best_loss = math.inf

    def consider(alpha: float, beta: float) -> None:
        nonlocal best_params, best_loss
        params = _candidate_params(sample_variance, alpha, beta)
        if params is None:
            return
        loss = negative_log_likelihood(series, params)
        if loss < best_loss:
            best_loss = loss
            best_params = params

    for alpha in np.linspace(0.0, 0.20, 11):
        for beta in np.linspace(0.0, MAX_PERSISTENCE, 25):
            consider(float(alpha), float(beta))

    assert best_params is not None
    center_alpha = best_params.alpha
    center_beta = best_params.beta
    step_alpha = 0.04
    step_beta = 0.08
    for _ in range(6):
        for d_alpha in (-2, -1, 0, 1, 2):
            for d_beta in (-2, -1, 0, 1, 2):
                consider(center_alpha + d_alpha * step_alpha, center_beta + d_beta * step_beta)
        assert best_params is not None
        center_alpha = best_params.alpha
        center_beta = best_params.beta
        step_alpha *= 0.5
        step_beta *= 0.5
    return best_params


class GarchVolatility:
    """Fit one close-to-close GARCH path per ticker and rescale emitted bands."""

    def __init__(self, *, min_observations: int = 60, min_scale: float = 0.5, max_scale: float = 2.5) -> None:
        self.min_observations = int(min_observations)
        self.min_scale = float(min_scale)
        self.max_scale = float(max_scale)
        self._params: dict[str, GarchParams] = {}
        self._sigma2: dict[str, float] = {}

    def fit(self, panel: TrainingPanel, seed: int) -> dict[str, Any]:
        del seed
        self._params = {}
        self._sigma2 = {}
        cutoff = panel.normalization.get("window_end")
        cutoff_value = np.datetime64(str(cutoff)) if cutoff else None
        observations: dict[str, int] = {}
        fitted = 0
        skipped_short_history = 0
        for ticker, series in panel.series.items():
            usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
            if cutoff_value is not None:
                usable &= series.label_end_dates <= cutoff_value
            rows = np.flatnonzero(usable)
            observations[ticker] = int(len(rows))
            if len(rows) < self.min_observations:
                skipped_short_history += 1
                continue
            returns = np.asarray(series.targets[rows, _CLOSE_INDEX], dtype=np.float64)
            params = fit_garch(returns)
            self._params[ticker] = params
            self._sigma2[ticker] = forecast_variance(returns, params)
            fitted += 1
        return {"fitted": fitted, "skipped_short_history": skipped_short_history, "observations": observations}

    def scale(self, ticker: str) -> float:
        params = self._params.get(ticker)
        sigma2 = self._sigma2.get(ticker)
        if params is None or sigma2 is None:
            return 1.0
        raw = math.sqrt(max(sigma2, _MIN_VARIANCE) / params.unconditional)
        return float(min(self.max_scale, max(self.min_scale, raw)))

    def observe(self, ticker: str, value: float) -> None:
        params = self._params.get(ticker)
        sigma2 = self._sigma2.get(ticker)
        if params is None or sigma2 is None:
            return
        self._sigma2[ticker] = params.omega + params.alpha * float(value) ** 2 + params.beta * sigma2

    def apply(self, band: dict[str, dict[str, float]], *, ticker: str) -> dict[str, dict[str, float]]:
        scale = self.scale(ticker)
        if scale == 1.0:
            return band
        scaled = {}
        for component, quantiles in band.items():
            median = float(quantiles["p50"])
            scaled[component] = {
                "p10": median + (float(quantiles["p10"]) - median) * scale,
                "p50": median,
                "p90": median + (float(quantiles["p90"]) - median) * scale,
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
            min_observations=int(state["min_observations"]),
            min_scale=float(state["min_scale"]),
            max_scale=float(state["max_scale"]),
        )
        component._params = {
            ticker: GarchParams(
                omega=float(values["omega"]),
                alpha=float(values["alpha"]),
                beta=float(values["beta"]),
            )
            for ticker, values in state["params"].items()
        }
        component._sigma2 = {ticker: float(value) for ticker, value in state["sigma2"].items()}
        return component
