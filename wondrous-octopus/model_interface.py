"""The model contract every forecaster implements, plus shared post-processing.

Everything the harness knows about a model goes through the
``OHLCForecaster`` protocol below: fit once, predict batches, absorb
revealed days as online updates, optionally health-check its own encoder,
and round-trip through disk. The two model families (baseline_model.py,
octopus_model.py) are deliberately indistinguishable from the harness's side
-- that is what makes their walk-forward comparison honest.

Artifact conventions (the "no pickle, ever" rule)
-------------------------------------------------
Model artifacts are ``metadata.json`` (plain JSON) plus, where there are
learned arrays, ``weights.npz`` written with :func:`save_arrays` and read
with :func:`load_arrays` (``allow_pickle=False`` enforced). ``torch.save``
is BANNED in this subproject: it is a pickle stream, and this repo's stance
on model artifacts (established in ml-forecaster/model.py's "No pickle,
ever" section) is that nothing executable crosses the disk boundary. Torch
state dicts cross disk only as named float arrays inside the ``.npz``.

Shared post-processing
----------------------
Every model's raw output passes through :func:`finalize_forecast`, which
applies two repairs (both counted, never silent):

- quantile-crossing repair: per component, p10 <= p50 <= p90 by sorting;
- bar-consistency repair: per quantile level and for the point row, the
  predicted high is raised to max(high, open, close) and the predicted low
  lowered to min(low, open, close), so every emitted forecast is a
  geometrically possible bar.

Adaptive conformal calibration
------------------------------
:class:`ConformalCalibrator` keeps one width multiplier per (component) and
nudges it daily toward the coverage target (adaptive-conformal-inference
style: miss -> widen a little, cover -> narrow a little). It is applied
identically to every model's bands, and both raw and calibrated bands are
ledgered so the report can show exactly what calibration contributed.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence, cast

import numpy as np
from data import COMPONENTS, TickerSeries, TrainingPanel

QUANTILES = (0.1, 0.5, 0.9)
QUANTILE_KEYS = ("p10", "p50", "p90")

#: Metadata filename every model artifact directory carries.
METADATA_FILENAME = "metadata.json"
WEIGHTS_FILENAME = "weights.npz"


@dataclass(frozen=True)
class ForecastRequest:
    """One (ticker, origin day t) asking for day t+1.

    ``features_window`` is the RAW (un-normalized) trailing feature matrix
    ending at day t, padded at the front for short histories with
    ``window_mask`` marking real rows. Models that only want "today" read
    the last real row; sequence models consume the whole window.
    """

    ticker: str
    ticker_id: int
    origin_date: str  # ISO date of day t
    features_window: np.ndarray  # (window_len, n_features) float32
    window_mask: np.ndarray  # (window_len,) bool
    day_of_week: np.ndarray  # (window_len,) int8
    window_dates: np.ndarray  # (window_len,) datetime64[ns]; NaT on padded rows
    ref_close: float  # adjusted close at day t
    adj_ratio: float  # day t's Adj Close / Close ratio (for dollar conversion)


@dataclass(frozen=True)
class OHLCForecast:
    ticker: str
    origin_date: str
    target_date: str | None  # None in live use until the next trading day is known
    point: dict[str, float]  # component -> predicted log-return vs ref_close
    quantiles: dict[str, dict[str, float]]  # component -> {"p10": ..., "p50": ..., "p90": ...}
    ref_close: float
    adj_ratio: float


@dataclass(frozen=True)
class RevealedDay:
    """Ground truth for one matured forecast, handed back for scoring and updates."""

    ticker: str
    ticker_id: int
    origin_date: str
    target_date: str
    actual: dict[str, float]  # component -> realized log-return vs origin day's close
    features_window: np.ndarray  # the window as of origin_date (for update batches)
    window_mask: np.ndarray
    day_of_week: np.ndarray
    window_dates: np.ndarray
    ref_close: float


class OHLCForecaster(Protocol):
    """What the harness requires of a model. See the module docstring."""

    name: str

    def fit(self, panel: TrainingPanel, *, seed: int, step_index: int = 0) -> dict[str, Any]:
        """``step_index`` is the harness's absolute step counter at the time
        of this fit -- 0 for the initial fit, the current cursor for a
        recovery retrain. A model with its own internal refit cadence
        (gbm_quantile) anchors its "steps since last refit" marker to this
        value so the configured cadence resumes cleanly after a retrain
        instead of immediately forcing another full refit on the very next
        online update."""
        ...

    def predict(self, requests: Sequence[ForecastRequest]) -> list[OHLCForecast]: ...

    def update(self, revealed: Sequence[RevealedDay], *, step_index: int) -> dict[str, Any]: ...

    def reconstruction_diagnostic(self, requests: Sequence[ForecastRequest]) -> dict[str, np.ndarray] | None:
        """Encoder health check: reconstruct each request's LAST real day.

        Returns ``{mode_name: (n_requests, 4) array}`` of the masked day's
        own reconstructed bar features (ret_open/high/low/close vs the prior
        close), one entry per masking mode the model supports, or ``None``
        for models with no encoder to health-check (the baselines). The
        harness compares against the actual bar and ledgers the error --
        telemetry only, never an input to anything.
        """
        ...

    def save(self, directory: Path) -> None: ...

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        panel_provider: Callable[[], TrainingPanel] | None = None,
        strict: bool = True,
        device: str = "auto",
    ) -> "OHLCForecaster":
        """``strict=False`` (an explicit recovery retrain) tells a model that
        refits on load (gbm_quantile) to accept a training set that no longer
        matches the checkpoint, instead of refusing -- the caller is about to
        refit every model again anyway. Models with no refit-on-load step
        ignore it. ``device`` is the invocation's requested compute device
        (octopus only -- CPU-only models ignore it)."""
        ...


# --------------------------------------------------------------------------
# Shared post-processing
# --------------------------------------------------------------------------


@dataclass
class RepairCounts:
    quantile_crossings: int = 0
    bar_repairs: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"quantile_crossings": self.quantile_crossings, "bar_repairs": self.bar_repairs}


def _repair_bar(values: dict[str, float]) -> tuple[dict[str, float], bool]:
    """Force high >= max(open, close) and low <= min(open, close)."""
    high = max(values["high"], values["open"], values["close"])
    low = min(values["low"], values["open"], values["close"])
    changed = high != values["high"] or low != values["low"]
    if changed:
        values = dict(values, high=high, low=low)
    return values, changed


def finalize_forecast(
    *,
    ticker: str,
    origin_date: str,
    target_date: str | None,
    point: dict[str, float],
    quantiles: dict[str, dict[str, float]],
    ref_close: float,
    adj_ratio: float,
    repairs: RepairCounts,
) -> OHLCForecast:
    """Apply the shared repairs to one model's raw output. See module docstring."""
    repaired_quantiles: dict[str, dict[str, float]] = {}
    for component in COMPONENTS:
        p10, p50, p90 = (quantiles[component][key] for key in QUANTILE_KEYS)
        ordered = sorted((p10, p50, p90))
        if ordered != [p10, p50, p90]:
            repairs.quantile_crossings += 1
        repaired_quantiles[component] = dict(zip(QUANTILE_KEYS, ordered, strict=True))
    point_repaired, changed = _repair_bar(dict(point))
    if changed:
        repairs.bar_repairs += 1
    # Bar-repair each quantile LEVEL as a bar of its own (the p10 bar, the
    # p50 bar, the p90 bar): each is a "possible day" the band sketches.
    for key in QUANTILE_KEYS:
        level = {component: repaired_quantiles[component][key] for component in COMPONENTS}
        level_repaired, changed = _repair_bar(level)
        if changed:
            repairs.bar_repairs += 1
            for component in COMPONENTS:
                repaired_quantiles[component][key] = level_repaired[component]
    return OHLCForecast(
        ticker=ticker,
        origin_date=origin_date,
        target_date=target_date,
        point=point_repaired,
        quantiles=repaired_quantiles,
        ref_close=ref_close,
        adj_ratio=adj_ratio,
    )


def repair_band_geometry(quantiles: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Re-impose quantile ordering and bar validity on a band dict.

    Conformal calibration scales each component independently, so once the
    per-component scales diverge it can re-break the geometry
    finalize_forecast established (e.g. widen `high` less than `open` and
    push high_p10 below open_p10). Every band that leaves the harness passes
    through here AFTER calibration (Codex review: "Repair calibrated bands
    before emitting them").
    """
    repaired: dict[str, dict[str, float]] = {}
    for component in COMPONENTS:
        ordered = sorted(quantiles[component][key] for key in QUANTILE_KEYS)
        repaired[component] = dict(zip(QUANTILE_KEYS, ordered, strict=True))
    for key in QUANTILE_KEYS:
        level = {component: repaired[component][key] for component in COMPONENTS}
        level_repaired, changed = _repair_bar(level)
        if changed:
            for component in COMPONENTS:
                repaired[component][key] = level_repaired[component]
    return repaired


# --------------------------------------------------------------------------
# Adaptive conformal band calibration
# --------------------------------------------------------------------------


@dataclass
class ConformalCalibrator:
    """One width multiplier per component, nudged daily toward target coverage.

    Multiplicative adaptive-conformal update: on each revealed day, per
    component, ``scale *= exp(lr * (miss - (1 - target)))`` -- at exactly
    target coverage the expected drift is zero; sustained misses widen the
    band, sustained cover narrows it. Scales are clipped so a wild regime
    cannot run the band to zero or infinity. State is plain floats, so it
    checkpoints as JSON.
    """

    target: float = 0.8
    lr: float = 0.02
    min_scale: float = 0.25
    max_scale: float = 4.0
    scales: dict[str, float] = field(default_factory=lambda: {component: 1.0 for component in COMPONENTS})
    #: The most recent target_date this calibrator has already applied a
    #: batch nudge for. observe_batch() uses this to refuse a second batch
    #: for the same (or an earlier) day -- see its docstring.
    last_calibrated_target: str | None = None

    def calibrate(self, forecast: OHLCForecast) -> dict[str, dict[str, float]]:
        """Calibrated bands: p10/p90 scaled around p50 (p50 itself untouched)."""
        calibrated: dict[str, dict[str, float]] = {}
        for component in COMPONENTS:
            band = forecast.quantiles[component]
            scale = self.scales[component]
            p50 = band["p50"]
            calibrated[component] = {
                "p10": p50 + (band["p10"] - p50) * scale,
                "p50": p50,
                "p90": p50 + (band["p90"] - p50) * scale,
            }
        return calibrated

    def observe(self, calibrated: dict[str, dict[str, float]], actual: dict[str, float]) -> None:
        for component in COMPONENTS:
            band = calibrated[component]
            miss = 0.0 if band["p10"] <= actual[component] <= band["p90"] else 1.0
            scale = self.scales[component] * math.exp(self.lr * (miss - (1.0 - self.target)))
            self.scales[component] = min(self.max_scale, max(self.min_scale, scale))

    def observe_batch(
        self, observations: list[tuple[dict[str, dict[str, float]], dict[str, float]]], *, target_date: str
    ) -> None:
        """One daily nudge per component, from the AGGREGATE miss rate
        across every observation in the batch.

        ``observe()`` is documented as ONE update per revealed day; calling
        it once per TICKER instead (a full-universe day matures thousands
        of tickers at once) applies the same exponential nudge thousands of
        times within a single calendar day, driving the scale to its clip
        bound almost immediately and making the final value sensitive to
        ticker ordering rather than actual coverage (Codex review:
        "Aggregate calibration updates once per market day"). This computes
        one miss RATE per component across the whole batch and applies the
        same multiplicative update exactly once.

        ``target_date`` -- the one calendar day every observation in this
        batch targets. A no-op if this calibrator already applied a batch
        for this (or a later) target_date: a lagging ticker's forecast can
        mature independently, via rebind_unresolved(), after the REST of
        the market's forecasts for that same day already went through the
        normal walk-forward batch -- re-nudging with only that smaller
        late cohort would double-weight the day and make future bands
        depend on archive-arrival order rather than one aggregate update
        per market day (Codex review: "Avoid recalibrating an already-
        processed target day").
        """
        if not observations:
            return
        if self.last_calibrated_target is not None and target_date <= self.last_calibrated_target:
            return
        for component in COMPONENTS:
            misses = sum(
                0.0 if calibrated[component]["p10"] <= actual[component] <= calibrated[component]["p90"] else 1.0
                for calibrated, actual in observations
            )
            miss_rate = misses / len(observations)
            scale = self.scales[component] * math.exp(self.lr * (miss_rate - (1.0 - self.target)))
            self.scales[component] = min(self.max_scale, max(self.min_scale, scale))
        self.last_calibrated_target = target_date

    def to_state(self) -> dict[str, Any]:
        # The clip bounds round-trip too: a calibrator constructed with
        # non-default bounds must clip identically after resume (Copilot
        # review: "to_state/from_state do not round-trip min/max_scale").
        return {
            "target": self.target,
            "lr": self.lr,
            "min_scale": self.min_scale,
            "max_scale": self.max_scale,
            "scales": dict(self.scales),
            "last_calibrated_target": self.last_calibrated_target,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "ConformalCalibrator":
        calibrator = cls(
            target=float(state["target"]),
            lr=float(state["lr"]),
            # .get(): checkpoints written before the bounds were persisted
            # restore with the construction-time defaults they actually ran with.
            min_scale=float(state.get("min_scale", 0.25)),
            max_scale=float(state.get("max_scale", 4.0)),
            # .get(): checkpoints written before this watermark existed
            # restore with no watermark, same as a fresh calibrator --
            # the very next batch simply sets it, same as always.
            last_calibrated_target=state.get("last_calibrated_target"),
        )
        calibrator.scales = {component: float(value) for component, value in state["scales"].items()}
        return calibrator


# --------------------------------------------------------------------------
# No-pickle artifact helpers
# --------------------------------------------------------------------------


def write_metadata(directory: Path, metadata: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / METADATA_FILENAME).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def read_metadata(directory: Path) -> dict[str, Any]:
    return json.loads((directory / METADATA_FILENAME).read_text())


def save_arrays(directory: Path, arrays: dict[str, np.ndarray]) -> None:
    """Write named numeric arrays as the model's weights.npz (never pickle)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, array in arrays.items():
        if array.dtype == object:  # pragma: no cover - defensive: object arrays pickle
            raise TypeError(f"array {name!r} has object dtype; only numeric arrays may be saved")
    np.savez(directory / WEIGHTS_FILENAME, **cast(Any, arrays))


def load_arrays(directory: Path) -> dict[str, np.ndarray]:
    with np.load(directory / WEIGHTS_FILENAME, allow_pickle=False) as archive:
        return {name: archive[name] for name in archive.files}


def last_real_row(request: ForecastRequest) -> np.ndarray:
    """The day-t feature vector of a request (the last unpadded window row)."""
    real_positions = np.flatnonzero(request.window_mask)
    return request.features_window[real_positions[-1]]


def batch_last_rows(requests: Iterable[ForecastRequest]) -> np.ndarray:
    return np.stack([last_real_row(request) for request in requests])


def _window_slices(
    series: TickerSeries, row_index: int, window_len: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Front-padded (features, mask, day_of_week, dates) window ending at row_index."""
    start = max(0, row_index + 1 - window_len)
    rows = series.features[start : row_index + 1]
    day_of_week = series.day_of_week[start : row_index + 1]
    dates = series.dates[start : row_index + 1]
    pad = window_len - len(rows)
    mask = np.ones(len(rows), dtype=bool)
    if pad:
        rows = np.concatenate([np.zeros((pad, rows.shape[1]), dtype=rows.dtype), rows])
        day_of_week = np.concatenate([np.zeros(pad, dtype=day_of_week.dtype), day_of_week])
        # Explicit unit on the NaT fill value: numpy deprecates the generic-
        # unit-to-`dates.dtype` conversion `np.datetime64("NaT")` would
        # otherwise need here, and the padded window (common at the archive
        # start / a ticker's warm-up) would break on a numpy upgrade that
        # turns the deprecation into an error (Codex review: "Construct
        # padded NaT values with an explicit unit").
        unit: Any = np.datetime_data(dates.dtype)[0]
        nat = np.datetime64("NaT", unit)
        dates = np.concatenate([np.full(pad, nat, dtype=dates.dtype), dates])
        mask = np.concatenate([np.zeros(pad, dtype=bool), mask])
    return rows, mask, day_of_week, dates


def make_request(series: TickerSeries, row_index: int, window_len: int) -> ForecastRequest:
    """A ForecastRequest for predicting the row AFTER ``row_index``."""
    rows, mask, day_of_week, dates = _window_slices(series, row_index, window_len)
    return ForecastRequest(
        ticker=series.ticker,
        ticker_id=series.ticker_id,
        origin_date=np.datetime_as_string(series.dates[row_index], unit="D"),
        features_window=rows,
        window_mask=mask,
        day_of_week=day_of_week,
        window_dates=dates,
        ref_close=float(series.ref_close[row_index]),
        adj_ratio=float(series.adj_ratio[row_index]),
    )


def make_revealed(series: TickerSeries, row_index: int, window_len: int) -> RevealedDay:
    """The RevealedDay for row ``row_index``'s (now knowable) target."""
    rows, mask, day_of_week, dates = _window_slices(series, row_index, window_len)
    return RevealedDay(
        ticker=series.ticker,
        ticker_id=series.ticker_id,
        origin_date=np.datetime_as_string(series.dates[row_index], unit="D"),
        target_date=np.datetime_as_string(series.label_end_dates[row_index], unit="D"),
        actual={component: float(series.targets[row_index][position]) for position, component in enumerate(COMPONENTS)},
        features_window=rows,
        window_mask=mask,
        day_of_week=day_of_week,
        window_dates=dates,
        ref_close=float(series.ref_close[row_index]),
    )
