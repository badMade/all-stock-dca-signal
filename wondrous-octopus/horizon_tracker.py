"""Matures, scores, calibrates, checkpoints and exports multi-horizon forecasts.

Every model that implements ``predict_horizons`` emits a forecast for EVERY
horizon 1..H each day. Only the configured **evaluation horizons** (default
5/10/15/20; widen toward H as the model earns it) are measured: for those, a
pending entry waits until the ticker's own h-th next row has been revealed,
is scored against that bar, ledgered as a ``horizon_score`` record, and feeds
that horizon's own conformal calibrator. The full 1..H forecast of the most
recent origin day is exported at every checkpoint (``horizons/``), so the
long horizons are usable even before they are measured -- with the stated
caveat that an unmeasured horizon's band widths are interpolated, not earned
(see ``HorizonConformalCalibrator``).

Walk-forward honesty lives in one place: a score for horizon h at cursor
date D reads the realized bar through :func:`horizons.horizon_targets` with
``asof = D``, which cannot return a bar dated after D. A pending entry is
never even looked at before the earliest calendar date it could mature
(``calendar[origin_pos + h]`` -- every ticker row advances at least one
calendar position), so a full-universe step touches only the entries that can
possibly be due, not every forecast still in flight.

Checkpointing is numeric-only (``horizon_pending.npz``, loaded with
``allow_pickle=False``) plus the calibrators' plain JSON state; the file's
SHA-256 rides in the harness state so a swapped or edited file is REFUSED.
"""

from __future__ import annotations

import csv
import hashlib
import math
import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from data import COMPONENTS, TrainingPanel
from horizons import FAR_FUTURE_KEY, horizon_targets, validate_horizons
from metrics import score_forecast
from model_interface import (
    QUANTILE_KEYS,
    HorizonConformalCalibrator,
    HorizonForecast,
    repair_horizon_arrays,
)

PENDING_FILENAME = "horizon_pending.npz"
EXPORT_DIRNAME = "horizons"

_LAGGING = -2  # sentinel "bucket day" for entries that must be re-examined every step
_FAR = -1


class HorizonTrackerError(RuntimeError):
    """A checkpoint's horizon state failed verification (REFUSED, never repaired)."""


def _day_int(iso: str) -> int:
    return int(np.datetime64(iso, "D").astype(np.int64))


def _day_iso(days: int) -> str:
    return str(np.datetime64(int(days), "D"))


def _calibrated(quantiles: np.ndarray, scales: np.ndarray) -> np.ndarray:
    """Scale ``(E, 4, 3)`` raw bands by ``(E, 4)`` and re-impose band geometry."""
    scaled = HorizonConformalCalibrator.apply(quantiles.astype(np.float64), scales.astype(np.float64))
    _, fixed, _ = repair_horizon_arrays(np.zeros(scaled.shape[:-1]), scaled)
    return fixed


def _band_dict(array: np.ndarray) -> dict[str, dict[str, float]]:
    return {
        component: {key: float(array[slot, index]) for index, key in enumerate(QUANTILE_KEYS)}
        for slot, component in enumerate(COMPONENTS)
    }


class HorizonTracker:
    def __init__(
        self,
        max_horizon: int,
        eval_horizons: Sequence[int],
        model_names: Sequence[str],
        *,
        calibration: bool = True,
        seed: int = 0,
        schema_version: int = 1,
    ) -> None:
        self.max_horizon, self.eval_horizons = validate_horizons(max_horizon, eval_horizons)
        if self.max_horizon < 2:
            raise ValueError("HorizonTracker requires max_horizon >= 2")
        self.model_names = tuple(model_names)
        self.calibration = calibration
        self.seed = seed
        self.schema_version = schema_version
        self.calibrators: dict[str, HorizonConformalCalibrator] = {
            name: HorizonConformalCalibrator(self.eval_horizons) for name in self.model_names
        }
        #: due-day key (ISO date or FAR_FUTURE_KEY) -> pending entries
        self.pending: dict[str, list[dict[str, Any]]] = {}
        #: entries that could not be advanced this step (ticker absent from a
        #: rebuilt panel, or lagging behind the market) and are re-examined next step
        self.lagging: list[dict[str, Any]] = []
        #: model -> the most recent origin day's full 1..H forecast (for export)
        self.latest: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Issuing forecasts
    # ------------------------------------------------------------------
    @staticmethod
    def _calendar_days(panel: TrainingPanel) -> np.ndarray:
        return panel.calendar.astype("datetime64[D]")

    def _due_key(self, calendar_days: np.ndarray, origin_date: str, horizon: int) -> str:
        origin_pos = int(np.searchsorted(calendar_days, np.datetime64(origin_date, "D")))
        due_pos = origin_pos + horizon
        if due_pos >= len(calendar_days):
            return FAR_FUTURE_KEY
        return str(calendar_days[due_pos])

    def record(
        self,
        model: str,
        forecasts: Sequence[HorizonForecast],
        rows: Sequence[tuple[str, int]],
        panel: TrainingPanel,
        date_iso: str,
    ) -> None:
        """Queue one model's day-``date_iso`` forecasts for maturation."""
        if not forecasts:
            return
        point = np.stack([forecast.point for forecast in forecasts])  # (N, H, 4)
        quantiles = np.stack([forecast.quantiles for forecast in forecasts])  # (N, H, 4, 3)
        if point.shape[1] != self.max_horizon:
            raise ValueError(f"model {model!r} emitted {point.shape[1]} horizons, expected {self.max_horizon}")
        calibrator = self.calibrators[model]
        scales = (
            calibrator.scales_matrix(self.max_horizon)
            if self.calibration
            else np.ones((self.max_horizon, len(COMPONENTS)))
        )
        calibrated = _calibrated(quantiles, scales)
        eval_index = [h - 1 for h in self.eval_horizons]
        calendar_days = self._calendar_days(panel)
        key = self._due_key(calendar_days, date_iso, self.eval_horizons[0])
        bucket = self.pending.setdefault(key, [])
        scales_eval = scales[eval_index].astype(np.float32)
        for position, (forecast, (ticker, row)) in enumerate(zip(forecasts, rows, strict=True)):
            bucket.append(
                {
                    "model": model,
                    "ticker": ticker,
                    "origin_row": int(row),
                    "origin_date": forecast.origin_date,
                    "ref_close": float(forecast.ref_close),
                    "adj_ratio": float(forecast.adj_ratio),
                    "next": 0,
                    "point": point[position, eval_index].astype(np.float32),
                    "q_raw": quantiles[position, eval_index].astype(np.float32),
                    "scales": scales_eval,
                }
            )
        self.latest[model] = {
            "origin_date": date_iso,
            "tickers": [ticker for ticker, _row in rows],
            "ref_close": np.array([f.ref_close for f in forecasts], dtype=np.float64),
            "point": point.astype(np.float32),
            "q_cal": calibrated.astype(np.float32),
        }

    # ------------------------------------------------------------------
    # Maturing / scoring
    # ------------------------------------------------------------------
    def mature(self, panel: TrainingPanel, date_iso: str) -> list[dict[str, Any]]:
        """Score every horizon whose target is revealed as of ``date_iso``.

        Returns the ``horizon_score`` ledger records (the caller appends
        them) and applies one calibration nudge per (model, horizon,
        target_date) afterwards, in chronological order.
        """
        date64 = np.datetime64(date_iso, "D")
        calendar_days = self._calendar_days(panel)
        batch = self.lagging
        self.lagging = []
        for key in sorted(key for key in self.pending if key <= date_iso):
            batch.extend(self.pending.pop(key))
        # Live mode grows the calendar by a day at a time: entries parked
        # beyond its old end may now have a real due date.
        for entry in self.pending.pop(FAR_FUTURE_KEY, []):
            self._file(entry, calendar_days, batch, date_iso)
        records: list[dict[str, Any]] = []
        observations: dict[tuple[str, int, str], list[tuple[dict, dict]]] = {}
        for entry in batch:
            self._advance(entry, panel, date64, calendar_days, date_iso, records, observations)
        for model, horizon, target in sorted(observations, key=lambda k: (k[2], k[1], k[0])):
            self.calibrators[model].observe_batch(horizon, observations[(model, horizon, target)], target_date=target)
        return records

    def _file(self, entry: dict[str, Any], calendar_days: np.ndarray, due_now: list, date_iso: str) -> None:
        key = self._due_key(calendar_days, entry["origin_date"], self.eval_horizons[entry["next"]])
        if key <= date_iso:
            due_now.append(entry)
        else:
            self.pending.setdefault(key, []).append(entry)

    def _advance(
        self,
        entry: dict[str, Any],
        panel: TrainingPanel,
        date64: np.datetime64,
        calendar_days: np.ndarray,
        date_iso: str,
        records: list[dict[str, Any]],
        observations: dict[tuple[str, int, str], list[tuple[dict, dict]]],
    ) -> None:
        series = panel.series.get(entry["ticker"])
        if series is None:
            # Temporarily absent (CSV gap in a rebuilt panel): keep it, it may return.
            self.lagging.append(entry)
            return
        day_dates = series.dates.astype("datetime64[D]")
        origin64 = np.datetime64(entry["origin_date"], "D")
        origin_row = int(entry["origin_row"])
        if not (0 <= origin_row < len(day_dates)) or day_dates[origin_row] != origin64:
            positions = np.flatnonzero(day_dates == origin64)
            if not len(positions):
                return  # the origin day no longer exists in a revised archive
            origin_row = int(positions[0])
            entry["origin_row"] = origin_row
        horizons = self.eval_horizons
        realized_all: np.ndarray | None = None
        while entry["next"] < len(horizons):
            slot = entry["next"]
            horizon = horizons[slot]
            target_row = origin_row + horizon
            if target_row >= len(day_dates) or day_dates[target_row] > date64:
                break
            if realized_all is None:
                # asof = the cursor date: the embargo is enforced by construction.
                realized_all = horizon_targets(series, origin_row, horizons[-1], date64)
            realized = realized_all[horizon - 1]
            if not np.isfinite(realized).all():
                break
            raw_origin_old = entry["ref_close"] / entry["adj_ratio"]
            raw_origin_new = float(series.ref_close[origin_row]) / float(series.adj_ratio[origin_row])
            rebase = (
                0.0
                if np.isclose(raw_origin_old, raw_origin_new, rtol=1e-9)
                else float(np.log(float(series.ref_close[origin_row]) / entry["ref_close"]))
            )
            actual = {component: float(realized[position]) + rebase for position, component in enumerate(COMPONENTS)}
            point = {component: float(entry["point"][slot][position]) for position, component in enumerate(COMPONENTS)}
            raw_bands = _band_dict(entry["q_raw"][slot].astype(np.float64))
            calibrated_bands = _band_dict(
                _calibrated(entry["q_raw"][slot : slot + 1], entry["scales"][slot : slot + 1])[0]
            )
            score = score_forecast(
                point=point,
                quantiles=calibrated_bands,
                quantiles_raw=raw_bands,
                actual=actual,
                ref_close=entry["ref_close"],
            )
            target_iso = str(day_dates[target_row])
            records.append(
                {
                    "schema_version": self.schema_version,
                    "record_type": "horizon_score",
                    "model": entry["model"],
                    "ticker": entry["ticker"],
                    "origin_date": entry["origin_date"],
                    "target_date": target_iso,
                    "horizon": horizon,
                    "seed": self.seed,
                    "point": point,
                    "quantiles": calibrated_bands,
                    "quantiles_raw": raw_bands,
                    "actual": actual,
                    "ref_close": entry["ref_close"],
                    "adj_ratio": entry["adj_ratio"],
                    "score": score,
                }
            )
            observations.setdefault((entry["model"], horizon, target_iso), []).append((calibrated_bands, actual))
            entry["next"] = slot + 1
        if entry["next"] >= len(horizons):
            return
        key = self._due_key(calendar_days, entry["origin_date"], horizons[entry["next"]])
        if key <= date_iso:
            # Its calendar lower bound has passed but this ticker's own row
            # has not (a lagging archive): re-examine next step.
            self.lagging.append(entry)
        else:
            self.pending.setdefault(key, []).append(entry)

    # ------------------------------------------------------------------
    # Export of the full 1..H forecast
    # ------------------------------------------------------------------
    def write_latest(self, run_dir: Path) -> list[Path]:
        """Write each model's newest full 1..H forecast as ``horizons/<model>.csv``.

        Calibrated bands (interpolated width scales off the evaluation
        checkpoints -- see ``HorizonConformalCalibrator``), log-returns vs the
        origin day's close, one row per (ticker, horizon). Atomic replace.
        """
        written: list[Path] = []
        directory = Path(run_dir) / EXPORT_DIRNAME
        for model, latest in self.latest.items():
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{model}.csv"
            temporary = directory / f".{model}.csv.tmp"
            with temporary.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                header = ["ticker", "origin_date", "horizon", "ref_close"]
                for component in COMPONENTS:
                    header += [f"{component}_point", f"{component}_p10", f"{component}_p50", f"{component}_p90"]
                writer.writerow(header)
                for index, ticker in enumerate(latest["tickers"]):
                    for horizon in range(1, self.max_horizon + 1):
                        row: list[Any] = [ticker, latest["origin_date"], horizon, f"{latest['ref_close'][index]:.6f}"]
                        for slot in range(len(COMPONENTS)):
                            row.append(f"{float(latest['point'][index, horizon - 1, slot]):.6g}")
                            row.extend(f"{float(v):.6g}" for v in latest["q_cal"][index, horizon - 1, slot])
                        writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            written.append(target)
        return written

    # ------------------------------------------------------------------
    # Checkpointing (numeric arrays + plain JSON; never pickle)
    # ------------------------------------------------------------------
    def save(self, directory: Path) -> dict[str, Any]:
        """Write ``horizon_pending.npz`` into ``directory``; return the JSON state."""
        directory = Path(directory)
        ordered: list[tuple[int, dict[str, Any]]] = [(_LAGGING, entry) for entry in self.lagging]
        for key in sorted(self.pending):
            bucket_day = _FAR if key == FAR_FUTURE_KEY else _day_int(key)
            ordered.extend((bucket_day, entry) for entry in self.pending[key])
        tickers = sorted({entry["ticker"] for _bucket, entry in ordered})
        ticker_index = {ticker: index for index, ticker in enumerate(tickers)}
        model_index = {name: index for index, name in enumerate(self.model_names)}
        count, width = len(ordered), len(self.eval_horizons)
        arrays = {
            "bucket": np.array([bucket for bucket, _ in ordered], dtype=np.int64),
            "model": np.array([model_index[e["model"]] for _, e in ordered], dtype=np.int32),
            "ticker": np.array([ticker_index[e["ticker"]] for _, e in ordered], dtype=np.int32),
            "origin_row": np.array([e["origin_row"] for _, e in ordered], dtype=np.int64),
            "origin_day": np.array([_day_int(e["origin_date"]) for _, e in ordered], dtype=np.int64),
            "next": np.array([e["next"] for _, e in ordered], dtype=np.int32),
            "ref_close": np.array([e["ref_close"] for _, e in ordered], dtype=np.float64),
            "adj_ratio": np.array([e["adj_ratio"] for _, e in ordered], dtype=np.float64),
            "point": np.stack([e["point"] for _, e in ordered]) if count else np.zeros((0, width, 4), np.float32),
            "q_raw": np.stack([e["q_raw"] for _, e in ordered]) if count else np.zeros((0, width, 4, 3), np.float32),
            "scales": np.stack([e["scales"] for _, e in ordered]) if count else np.zeros((0, width, 4), np.float32),
        }
        path = directory / PENDING_FILENAME
        np.savez(path, **arrays)  # type: ignore[arg-type]
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        return {
            "max_horizon": self.max_horizon,
            "eval_horizons": list(self.eval_horizons),
            "models": list(self.model_names),
            "tickers": tickers,
            "n_pending": count,
            "pending_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "calibrators": {name: calibrator.to_state() for name, calibrator in self.calibrators.items()},
        }

    def load(self, directory: Path, state: dict[str, Any]) -> None:
        """Restore from :meth:`save`'s output; REFUSE on any mismatch."""
        if int(state["max_horizon"]) != self.max_horizon or tuple(state["eval_horizons"]) != self.eval_horizons:
            raise HorizonTrackerError("REFUSED: checkpointed horizon config differs from the requested one")
        if tuple(state["models"]) != self.model_names:
            raise HorizonTrackerError("REFUSED: checkpointed horizon model list differs from the requested one")
        path = Path(directory) / PENDING_FILENAME
        if not path.is_file():
            raise HorizonTrackerError(f"REFUSED: {PENDING_FILENAME} is missing from the checkpoint")
        if hashlib.sha256(path.read_bytes()).hexdigest() != state["pending_sha256"]:
            raise HorizonTrackerError(f"REFUSED: {PENDING_FILENAME} does not match its recorded SHA-256")
        with np.load(path, allow_pickle=False) as archive:
            arrays = {name: archive[name] for name in archive.files}
        tickers = state["tickers"]
        self.pending, self.lagging = {}, []
        for index in range(len(arrays["bucket"])):
            entry = {
                "model": self.model_names[int(arrays["model"][index])],
                "ticker": tickers[int(arrays["ticker"][index])],
                "origin_row": int(arrays["origin_row"][index]),
                "origin_date": _day_iso(arrays["origin_day"][index]),
                "ref_close": float(arrays["ref_close"][index]),
                "adj_ratio": float(arrays["adj_ratio"][index]),
                "next": int(arrays["next"][index]),
                "point": arrays["point"][index],
                "q_raw": arrays["q_raw"][index],
                "scales": arrays["scales"][index],
            }
            bucket = int(arrays["bucket"][index])
            if bucket == _LAGGING:
                self.lagging.append(entry)
            else:
                key = FAR_FUTURE_KEY if bucket == _FAR else _day_iso(bucket)
                self.pending.setdefault(key, []).append(entry)
        self.calibrators = {
            name: HorizonConformalCalibrator.from_state(payload) for name, payload in state["calibrators"].items()
        }
        for value in (entry for bucket in self.pending.values() for entry in bucket):
            if not math.isfinite(value["ref_close"]):
                raise HorizonTrackerError("REFUSED: non-finite reference close in the horizon checkpoint")
