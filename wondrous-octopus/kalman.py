"""A lightweight Kalman forecaster for the wondrous-octopus test suite.

This module keeps the same small, no-pickle surface as the other forecasters:
plain JSON metadata plus numeric ``weights.npz`` arrays, causal fit/update
semantics, and one independent filter per (ticker, OHLC component).
"""

from __future__ import annotations

import math
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Sequence

import numpy as np

from data import COMPONENTS, TrainingPanel
from model_interface import (
    ForecastRequest,
    OHLCForecast,
    RepairCounts,
    RevealedDay,
    finalize_forecast,
    load_arrays,
    read_metadata,
    save_arrays,
    write_metadata,
)

DEFAULT_Q_RATIO = 0.01
_MIN_VARIANCE = 1e-8
_Z90 = NormalDist().inv_cdf(0.9)


def steady_state_variance(q: float, r: float) -> float:
    """The positive scalar Riccati root for process variance ``q`` and noise ``r``."""
    if q < 0.0:
        raise ValueError("q must be non-negative")
    if r <= 0.0:
        raise ValueError("r must be strictly positive")
    return float((q + math.sqrt(q * q + 4.0 * q * r)) / 2.0)


def steady_state_gain(q: float, r: float) -> float:
    variance = steady_state_variance(q, r)
    return float(variance / (variance + r))


class ScalarFilter:
    """A scalar local-level or local-trend Kalman filter."""

    def __init__(
        self,
        *,
        r: float,
        q_ratio: float = DEFAULT_Q_RATIO,
        trend: bool = False,
        prior_variance: float | None = None,
        prior_level: float = 0.0,
        prior_slope: float = 0.0,
        observations: int = 0,
        covariance: np.ndarray | None = None,
    ) -> None:
        if r <= 0.0:
            raise ValueError("r must be strictly positive")
        if q_ratio < 0.0:
            raise ValueError("q_ratio must be non-negative")
        self.r = float(r)
        self.q_ratio = float(q_ratio)
        self.trend = bool(trend)
        self.observations = int(observations)
        variance = float(prior_variance if prior_variance is not None else r)
        if variance <= 0.0:
            raise ValueError("prior_variance must be strictly positive")
        if self.trend:
            self._state = np.array([prior_level, prior_slope], dtype=np.float64)
            self.covariance = (
                np.array(covariance, dtype=np.float64, copy=True)
                if covariance is not None
                else np.diag([variance, variance]).astype(np.float64)
            )
        else:
            self._state = np.array([prior_level], dtype=np.float64)
            self.covariance = (
                np.array(covariance, dtype=np.float64, copy=True)
                if covariance is not None
                else np.array([[variance]], dtype=np.float64)
            )

    @property
    def level(self) -> float:
        return float(self._state[0])

    @property
    def slope(self) -> float:
        return float(self._state[1]) if self.trend else 0.0

    def _predict(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        if self.trend:
            transition = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float64)
            process = self.q_ratio * self.r * np.eye(2, dtype=np.float64)
            state = transition @ self._state
            covariance = transition @ self.covariance @ transition.T + process
            observation = np.array([1.0, 0.0], dtype=np.float64)
        else:
            transition = np.array([[1.0]], dtype=np.float64)
            process = np.array([[self.q_ratio * self.r]], dtype=np.float64)
            state = transition @ self._state
            covariance = transition @ self.covariance @ transition.T + process
            observation = np.array([1.0], dtype=np.float64)
        variance = float(observation @ covariance @ observation + self.r)
        return state, covariance, observation, variance

    def project(self) -> tuple[float, float]:
        state, _, observation, variance = self._predict()
        return float(observation @ state), variance

    def absorb(self, value: float) -> float:
        state, covariance, observation, variance = self._predict()
        mean = float(observation @ state)
        innovation = float(value) - mean
        gain = (covariance @ observation) / variance
        self._state = state + gain * innovation
        identity = np.eye(covariance.shape[0], dtype=np.float64)
        update = identity - np.outer(gain, observation)
        self.covariance = update @ covariance
        self.observations += 1
        return innovation

    def to_state(self) -> dict[str, Any]:
        return {
            "r": self.r,
            "q_ratio": self.q_ratio,
            "trend": self.trend,
            "observations": self.observations,
            "state": self._state.tolist(),
            "covariance": self.covariance.tolist(),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "ScalarFilter":
        values = [float(value) for value in state["state"]]
        return cls(
            r=float(state["r"]),
            q_ratio=float(state["q_ratio"]),
            trend=bool(state["trend"]),
            prior_variance=1.0,
            prior_level=values[0],
            prior_slope=values[1] if len(values) > 1 else 0.0,
            observations=int(state["observations"]),
            covariance=np.array(state["covariance"], dtype=np.float64),
        )


class KalmanForecaster:
    """One independent Kalman filter per ticker and OHLC component."""

    name = "kalman"

    def __init__(self, *, q_ratio: float = DEFAULT_Q_RATIO, trend: bool = False) -> None:
        self.q_ratio = float(q_ratio)
        self.trend = bool(trend)
        self._filters: dict[str, dict[str, ScalarFilter]] = {}
        self._pooled_level = {component: 0.0 for component in COMPONENTS}
        self._pooled_r = {component: 0.01 for component in COMPONENTS}
        self.last_repairs = RepairCounts()

    def fit(self, panel: TrainingPanel, *, seed: int, step_index: int = 0) -> dict[str, Any]:
        del seed, step_index
        self._filters = {}
        cutoff = panel.normalization.get("window_end")
        cutoff_value = np.datetime64(str(cutoff)) if cutoff else None
        pooled: dict[str, list[float]] = {component: [] for component in COMPONENTS}
        trainable: dict[str, np.ndarray] = {}
        for ticker, series in panel.series.items():
            usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
            if cutoff_value is not None:
                usable &= series.label_end_dates <= cutoff_value
            rows = np.flatnonzero(usable)
            trainable[ticker] = rows
            if not len(rows):
                continue
            for index, component in enumerate(COMPONENTS):
                pooled[component].extend(float(value) for value in series.targets[rows, index])
        for component in COMPONENTS:
            values = np.asarray(pooled[component], dtype=np.float64)
            if values.size:
                self._pooled_level[component] = float(np.mean(values))
                self._pooled_r[component] = max(float(np.var(values)), _MIN_VARIANCE)
            else:
                self._pooled_level[component] = 0.0
                self._pooled_r[component] = 0.01
        absorbed = 0
        for ticker, series in panel.series.items():
            rows = trainable[ticker]
            if not len(rows):
                continue
            filters = {}
            for index, component in enumerate(COMPONENTS):
                own = np.asarray(series.targets[rows, index], dtype=np.float64)
                r = max(float(np.var(own)), self._pooled_r[component], _MIN_VARIANCE)
                filt = ScalarFilter(
                    r=r,
                    q_ratio=self.q_ratio,
                    trend=self.trend,
                    prior_variance=r,
                    prior_level=self._pooled_level[component],
                )
                for value in own:
                    filt.absorb(float(value))
                filters[component] = filt
            self._filters[ticker] = filters
            absorbed += int(len(rows))
        return {"absorbed": absorbed}

    def predict(self, requests: Sequence[ForecastRequest]) -> list[OHLCForecast]:
        repairs = RepairCounts()
        forecasts = []
        for request in requests:
            filters = self._filters.get(request.ticker)
            point: dict[str, float] = {}
            quantiles: dict[str, dict[str, float]] = {}
            for component in COMPONENTS:
                filt = filters[component] if filters is not None else self._pooled_filter(component)
                mean, variance = filt.project()
                half_width = _Z90 * math.sqrt(max(variance, _MIN_VARIANCE))
                point[component] = mean
                quantiles[component] = {"p10": mean - half_width, "p50": mean, "p90": mean + half_width}
            forecasts.append(
                finalize_forecast(
                    ticker=request.ticker,
                    origin_date=request.origin_date,
                    target_date=None,
                    point=point,
                    quantiles=quantiles,
                    ref_close=request.ref_close,
                    adj_ratio=request.adj_ratio,
                    repairs=repairs,
                )
            )
        self.last_repairs = repairs
        return forecasts

    def update(self, revealed: Sequence[RevealedDay], *, step_index: int) -> dict[str, Any]:
        del step_index
        for day in revealed:
            filters = self._filters.setdefault(
                day.ticker,
                {component: self._pooled_filter(component) for component in COMPONENTS},
            )
            for component in COMPONENTS:
                filters[component].absorb(day.actual[component])
        return {"absorbed": len(revealed)}

    def reconstruction_diagnostic(self, requests: Sequence[ForecastRequest]) -> None:
        del requests
        return None

    def save(self, directory: Path) -> None:
        arrays: dict[str, np.ndarray] = {}
        for ticker, filters in self._filters.items():
            arrays[f"state_{ticker}"] = np.stack([filters[component]._state for component in COMPONENTS]).astype(
                np.float64
            )
            arrays[f"r_{ticker}"] = np.array([filters[component].r for component in COMPONENTS], dtype=np.float64)
            arrays[f"cov_{ticker}"] = np.stack([filters[component].covariance for component in COMPONENTS]).astype(
                np.float64
            )
            arrays[f"obs_{ticker}"] = np.array(
                [filters[component].observations for component in COMPONENTS], dtype=np.int64
            )
        save_arrays(directory, arrays)
        write_metadata(
            directory,
            {
                "name": self.name,
                "q_ratio": self.q_ratio,
                "trend": self.trend,
                "pooled_level": {component: float(self._pooled_level[component]) for component in COMPONENTS},
                "pooled_r": {component: float(self._pooled_r[component]) for component in COMPONENTS},
            },
        )

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        panel_provider: Callable[[], TrainingPanel] | None = None,
        strict: bool = True,
        device: str = "auto",
    ) -> "KalmanForecaster":
        del panel_provider, strict, device
        metadata = read_metadata(directory)
        model = cls(q_ratio=float(metadata["q_ratio"]), trend=bool(metadata["trend"]))
        model._pooled_level = {component: float(value) for component, value in metadata["pooled_level"].items()}
        model._pooled_r = {component: float(value) for component, value in metadata["pooled_r"].items()}
        arrays = load_arrays(directory)
        for name, state in arrays.items():
            if not name.startswith("state_"):
                continue
            ticker = name[len("state_") :]
            measurement = arrays[f"r_{ticker}"]
            covariance = arrays[f"cov_{ticker}"]
            observations = arrays[f"obs_{ticker}"]
            filters = {}
            for index, component in enumerate(COMPONENTS):
                values = np.asarray(state[index], dtype=np.float64)
                filters[component] = ScalarFilter(
                    r=float(measurement[index]),
                    q_ratio=model.q_ratio,
                    trend=model.trend,
                    prior_variance=1.0,
                    prior_level=float(values[0]),
                    prior_slope=float(values[1]) if model.trend else 0.0,
                    observations=int(observations[index]),
                    covariance=np.asarray(covariance[index], dtype=np.float64),
                )
            model._filters[ticker] = filters
        return model

    def _pooled_filter(self, component: str) -> ScalarFilter:
        r = self._pooled_r[component]
        return ScalarFilter(
            r=r,
            q_ratio=self.q_ratio,
            trend=self.trend,
            prior_variance=r,
            prior_level=self._pooled_level[component],
        )
