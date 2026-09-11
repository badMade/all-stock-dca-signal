"""Kalman forecaster used by the wondrous-octopus walk-forward harness."""

from __future__ import annotations

import math
from dataclasses import dataclass
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
_Z90 = NormalDist().inv_cdf(0.9)
_COMPONENT_TO_INDEX = {name: idx for idx, name in enumerate(COMPONENTS)}


def steady_state_variance(q: float, r: float) -> float:
    if q < 0.0 or r <= 0.0:
        raise ValueError("q must be >= 0 and r must be > 0")
    return float((q + math.sqrt(q * q + 4.0 * q * r)) / 2.0)


def steady_state_gain(q: float, r: float) -> float:
    variance = steady_state_variance(q, r)
    return float(variance / (variance + r))


@dataclass
class ScalarFilter:
    r: float
    q_ratio: float = DEFAULT_Q_RATIO
    trend: bool = False
    prior_variance: float = 1.0
    prior_level: float = 0.0
    prior_slope: float = 0.0

    def __post_init__(self) -> None:
        if self.r <= 0.0:
            raise ValueError("r must be > 0")
        if self.q_ratio < 0.0:
            raise ValueError("q_ratio must be >= 0")
        if self.prior_variance <= 0.0:
            raise ValueError("prior_variance must be > 0")
        self.level = float(self.prior_level)
        self.slope = float(self.prior_slope)
        self.observations = 0
        if self.trend:
            self.covariance = np.eye(2, dtype=np.float64) * float(self.prior_variance)
        else:
            self.covariance = np.array([[float(self.prior_variance)]], dtype=np.float64)

    def project(self) -> tuple[float, float]:
        predicted_state, predicted_covariance = self._predict()
        mean = float(predicted_state[0])
        variance = float(predicted_covariance[0, 0] + self.r)
        return mean, variance

    def absorb(self, observation: float) -> float:
        predicted_state, predicted_covariance = self._predict()
        innovation = float(observation - predicted_state[0])
        innovation_variance = float(predicted_covariance[0, 0] + self.r)
        kalman_gain = predicted_covariance[:, 0] / innovation_variance
        updated_state = predicted_state + kalman_gain * innovation
        updated_covariance = predicted_covariance - np.outer(kalman_gain, predicted_covariance[0, :])
        self.level = float(updated_state[0])
        self.slope = float(updated_state[1]) if self.trend else 0.0
        self.covariance = updated_covariance
        self.observations += 1
        return innovation

    def _predict(self) -> tuple[np.ndarray, np.ndarray]:
        process = self.q_ratio * self.r
        if self.trend:
            state = np.array([self.level, self.slope], dtype=np.float64)
            transition = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float64)
            noise = process * np.array([[0.25, 0.5], [0.5, 1.0]], dtype=np.float64)
            predicted_state = transition @ state
            predicted_covariance = transition @ self.covariance @ transition.T + noise
            return predicted_state, predicted_covariance
        predicted_covariance = self.covariance + np.array([[process]], dtype=np.float64)
        return np.array([self.level], dtype=np.float64), predicted_covariance


class KalmanForecaster:
    name = "kalman"

    def __init__(
        self,
        *,
        trend: bool = False,
        q_ratio: float = DEFAULT_Q_RATIO,
        default_r: float = 1e-4,
        prior_variance: float = 1.0,
    ) -> None:
        self.trend = trend
        self.q_ratio = q_ratio
        self.default_r = default_r
        self.prior_variance = prior_variance
        self._filters: dict[str, dict[str, ScalarFilter]] = {}
        self._pooled_prior = {component: (0.0, default_r) for component in COMPONENTS}
        self.last_repairs = RepairCounts()

    def fit(self, panel: TrainingPanel, *, seed: int, step_index: int = 0) -> dict[str, Any]:
        del seed, step_index
        self._filters = {}
        self._pooled_prior = {component: (0.0, self.default_r) for component in COMPONENTS}
        absorbed = 0
        pooled_values: dict[str, list[float]] = {component: [] for component in COMPONENTS}
        cutoff_raw = panel.normalization.get("window_end")
        cutoff = np.datetime64(str(cutoff_raw)) if cutoff_raw else None
        for ticker, series in panel.series.items():
            usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
            if cutoff is not None:
                usable &= series.label_end_dates <= cutoff
            rows = np.flatnonzero(usable)
            if not len(rows):
                continue
            ticker_filters: dict[str, ScalarFilter] = {}
            for component in COMPONENTS:
                values = series.targets[rows, _COMPONENT_TO_INDEX[component]].astype(np.float64)
                pooled_values[component].extend(float(value) for value in values)
                variance = float(np.mean(values**2)) if len(values) else self.default_r
                variance = max(self.default_r, variance)
                filt = ScalarFilter(
                    r=variance,
                    q_ratio=self.q_ratio,
                    trend=self.trend,
                    prior_variance=self.prior_variance,
                )
                for value in values:
                    filt.absorb(float(value))
                ticker_filters[component] = filt
            self._filters[ticker] = ticker_filters
            absorbed += int(len(rows))
        for component in COMPONENTS:
            values = pooled_values[component]
            if values:
                arr = np.asarray(values, dtype=np.float64)
                mean = float(np.mean(arr))
                variance = float(np.mean((arr - mean) ** 2))
                self._pooled_prior[component] = (mean, max(self.default_r, variance))
        return {"absorbed": absorbed}

    def predict(self, requests: Sequence[ForecastRequest]) -> list[OHLCForecast]:
        repairs = RepairCounts()
        forecasts: list[OHLCForecast] = []
        for request in requests:
            point: dict[str, float] = {}
            quantiles: dict[str, dict[str, float]] = {}
            for component in COMPONENTS:
                filt = self._filters.get(request.ticker, {}).get(component)
                if filt is None:
                    mean, variance = self._pooled_prior[component]
                    filt = ScalarFilter(
                        r=variance,
                        q_ratio=self.q_ratio,
                        trend=self.trend,
                        prior_variance=self.prior_variance,
                        prior_level=mean,
                    )
                mean, variance = filt.project()
                half = _Z90 * math.sqrt(variance)
                point[component] = mean
                quantiles[component] = {"p10": mean - half, "p50": mean, "p90": mean + half}
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
            ticker_filters = self._filters.setdefault(day.ticker, {})
            for component in COMPONENTS:
                filt = ticker_filters.get(component)
                if filt is None:
                    mean, variance = self._pooled_prior[component]
                    filt = ScalarFilter(
                        r=variance,
                        q_ratio=self.q_ratio,
                        trend=self.trend,
                        prior_variance=self.prior_variance,
                        prior_level=mean,
                    )
                    ticker_filters[component] = filt
                filt.absorb(float(day.actual[component]))
        return {"absorbed": len(revealed)}

    def reconstruction_diagnostic(self, requests: Sequence[ForecastRequest]) -> None:
        del requests
        return None

    def save(self, directory: Path) -> None:
        filter_rows: list[tuple[str, str]] = []
        levels: list[float] = []
        slopes: list[float] = []
        observations: list[int] = []
        rs: list[float] = []
        covariance: list[np.ndarray] = []
        for ticker in sorted(self._filters):
            for component in COMPONENTS:
                filt = self._filters[ticker][component]
                filter_rows.append((ticker, component))
                levels.append(filt.level)
                slopes.append(filt.slope)
                observations.append(filt.observations)
                rs.append(filt.r)
                cov = np.zeros((2, 2), dtype=np.float64)
                if filt.trend:
                    cov[:, :] = filt.covariance
                else:
                    cov[0, 0] = float(filt.covariance[0, 0])
                covariance.append(cov)
        arrays = {
            "levels": np.asarray(levels, dtype=np.float64),
            "slopes": np.asarray(slopes, dtype=np.float64),
            "observations": np.asarray(observations, dtype=np.int64),
            "r": np.asarray(rs, dtype=np.float64),
            "covariance": np.asarray(covariance, dtype=np.float64),
            "pooled_mean": np.asarray([self._pooled_prior[component][0] for component in COMPONENTS], dtype=np.float64),
            "pooled_r": np.asarray([self._pooled_prior[component][1] for component in COMPONENTS], dtype=np.float64),
        }
        save_arrays(directory, arrays)
        write_metadata(
            directory,
            {
                "name": self.name,
                "trend": self.trend,
                "q_ratio": self.q_ratio,
                "default_r": self.default_r,
                "prior_variance": self.prior_variance,
                "filters": [{"ticker": ticker, "component": component} for ticker, component in filter_rows],
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
        arrays = load_arrays(directory)
        model = cls(
            trend=bool(metadata.get("trend", False)),
            q_ratio=float(metadata.get("q_ratio", DEFAULT_Q_RATIO)),
            default_r=float(metadata.get("default_r", 1e-4)),
            prior_variance=float(metadata.get("prior_variance", 1.0)),
        )
        pooled_mean = arrays.get("pooled_mean")
        pooled_r = arrays.get("pooled_r")
        if pooled_mean is not None and pooled_r is not None and len(pooled_mean) == len(COMPONENTS):
            model._pooled_prior = {
                component: (float(pooled_mean[idx]), float(pooled_r[idx])) for idx, component in enumerate(COMPONENTS)
            }
        for idx, row in enumerate(metadata.get("filters", [])):
            ticker = str(row["ticker"])
            component = str(row["component"])
            filt = ScalarFilter(
                r=float(arrays["r"][idx]),
                q_ratio=model.q_ratio,
                trend=model.trend,
                prior_variance=model.prior_variance,
                prior_level=float(arrays["levels"][idx]),
                prior_slope=float(arrays["slopes"][idx]),
            )
            filt.observations = int(arrays["observations"][idx])
            saved_covariance = arrays["covariance"][idx]
            if model.trend:
                filt.covariance = saved_covariance.astype(np.float64, copy=True)
            else:
                filt.covariance = np.array([[float(saved_covariance[0, 0])]], dtype=np.float64)
            model._filters.setdefault(ticker, {})[component] = filt
        return model
