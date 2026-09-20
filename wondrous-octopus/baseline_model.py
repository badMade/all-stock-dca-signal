"""Baseline forecasters: persistence and gradient-boosted quantile models.

Both implement the model_interface.OHLCForecaster protocol, so the harness
cannot tell them apart from the octopus model -- that indistinguishability
is what makes "does the deep model actually beat something simple?" an
honest, like-for-like question.

PersistenceModel is the yardstick: tomorrow = today. Its bands come from
each ticker's own trailing realized residuals, so persistence gets HONEST
quantiles and the coverage comparison means something (a strawman baseline
with no bands would make every other model look well-calibrated for free).

GbmQuantileModel is the "lightweight stack" contender: sklearn
HistGradientBoostingRegressor (quantile loss -- the fast histogram variant
has supported it since sklearn 1.1, and at millions of rows it is the only
tractable choice of the two GBM classes) x (4 components x 3 quantiles),
plus 4 absolute-error point fits = 16 small models over the LAST row of each
request's window (a GBM is not a sequence model; the trailing-return/vol
features already summarize the window). Its "online update" is a scheduled
full refit -- the honest sklearn analogue of online learning, documented as
such rather than pretended otherwise.

The no-pickle rule shapes GBM checkpointing: tree ensembles cannot be dumped
to JSON tractably, so save() writes ONLY metadata (hyperparameters +
last-refit cutoff + seed) and load() deterministically REFITS from the
panel truncated to that cutoff -- fit is a pure function of
(data-through-date, seed), and tests assert the reload reproduces the
original's predictions exactly.
"""

from __future__ import annotations

import hashlib
from collections import deque
from pathlib import Path
from typing import Any, Callable, Sequence

import features as _features
import numpy as np
from data import COMPONENTS, TrainingPanel
from model_interface import (
    ForecastRequest,
    OHLCForecast,
    RepairCounts,
    RevealedDay,
    batch_last_rows,
    finalize_forecast,
    load_arrays,
    read_metadata,
    save_arrays,
    write_metadata,
)

_features_module: Any = _features
FEATURE_NAMES: Any = _features_module.FEATURE_NAMES

#: Feature-column indices used to express "tomorrow = today" in target space:
#: ln(O_t / C_t) = ret_open - ret_close, etc. (both returns share the
#: C_{t-1} denominator, which cancels).
_RET_INDICES = {
    "open": FEATURE_NAMES.index("ret_open"),
    "high": FEATURE_NAMES.index("ret_high"),
    "low": FEATURE_NAMES.index("ret_low"),
    "close": FEATURE_NAMES.index("ret_close"),
}

#: Below this many of a ticker's own residuals, its bands borrow the pooled
#: global buffer instead -- a young ticker gets market-typical bands rather
#: than degenerate ones from a handful of samples.
_MIN_OWN_RESIDUALS = 30


class TrainingDataChanged(RuntimeError):
    """Raised by GbmQuantileModel.load() when refit-on-load reproduces a
    training set that differs from the one the checkpoint recorded."""


def _persistence_point(feature_row: np.ndarray) -> dict[str, float]:
    ret_close = float(feature_row[_RET_INDICES["close"]])
    return {
        "open": float(feature_row[_RET_INDICES["open"]]) - ret_close,
        "high": float(feature_row[_RET_INDICES["high"]]) - ret_close,
        "low": float(feature_row[_RET_INDICES["low"]]) - ret_close,
        "close": 0.0,
    }


def _fit_cutoff(panel: TrainingPanel) -> np.datetime64 | None:
    """The initial-fit window end recorded at panel build (None = all rows)."""
    window_end = panel.normalization.get("window_end")
    return np.datetime64(str(window_end)) if window_end else None


class PersistenceModel:
    """Tomorrow = today, with empirical residual bands. See module docstring."""

    name = "persistence"

    def __init__(self, *, residual_window: int = 250, pool_window: int = 5000) -> None:
        self.residual_window = residual_window
        self.pool_window = pool_window
        self._residuals: dict[str, deque] = {}
        self._pool: deque = deque(maxlen=pool_window)
        # Running count of residuals ever OFFERED to the pool (not just the
        # ones currently kept) -- update()'s reservoir sampling needs this
        # to compute each new residual's correct replacement probability.
        # Checkpointed so a resume continues the same reservoir, not a
        # freshly-reset one.
        self._pool_seen = 0

    # -- protocol ----------------------------------------------------------
    def fit(self, panel: TrainingPanel, *, seed: int, step_index: int = 0) -> dict[str, Any]:
        del step_index  # no internal refit cadence to anchor
        # fit() REPLACES state, never appends: a recovery retrain on a
        # loaded model would otherwise duplicate every residual it re-reads
        # (Codex review: "persistence duplicates old residuals").
        self._residuals = {}
        self._pool = deque(maxlen=self.pool_window)
        cutoff = _fit_cutoff(panel)
        seeded = 0
        # Reservoir-sample (Vitter Algorithm R) directly into the pool AS
        # residuals are computed, instead of materializing every trainable
        # ticker-day's residual into a Python list first and batch-sampling
        # it afterward: at full-universe scale that transient list can hold
        # millions of small numpy arrays -- several GB -- even though the
        # final pool is bounded to pool_window entries (Codex review:
        # "Reservoir-sample persistence residuals while collecting"). Same
        # algorithm the earlier order-bias fix used (uniform across ALL
        # tickers, not just the last ~20 alphabetical ones appended into a
        # bounded deque), just applied in one streaming pass under the model
        # seed instead of two.
        rng = np.random.default_rng(seed)
        pool_seen = 0
        for ticker, series in panel.series.items():
            usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
            if cutoff is not None:
                # Trainability: a residual exists once its target day has
                # passed -- label_end_date at or before the window end.
                usable &= series.label_end_dates <= cutoff
            rows = np.flatnonzero(usable)
            if not len(rows):
                continue
            buffer = self._buffer(ticker)
            for row in rows[-self.residual_window :]:
                residual = self._residual(series.features[row], series.targets[row])
                buffer.append(residual)
                seeded += 1
                pool_seen += 1
                if len(self._pool) < self.pool_window:
                    self._pool.append(residual)
                else:
                    slot = int(rng.integers(0, pool_seen))
                    if slot < self.pool_window:
                        self._pool[slot] = residual
        # update()'s reservoir sampling continues from here: "seen" is
        # every residual OFFERED, whether or not it survived into the pool.
        self._pool_seen = pool_seen
        return {"seeded_residuals": seeded}

    def predict(self, requests: Sequence[ForecastRequest]) -> list[OHLCForecast]:
        repairs = RepairCounts()
        forecasts = []
        # The pooled buffer is shared by every young ticker in the batch and
        # unchanged within one predict() call: stack + quantile it ONCE, not
        # once per request (Antigravity review: deque conversion churn).
        pool_quantiles = self._quantiles_of(self._pool) if self._pool else None
        for request in requests:
            feature_row = request.features_window[np.flatnonzero(request.window_mask)[-1]]
            point = _persistence_point(feature_row)
            quantiles = self._bands(request.ticker, point, pool_quantiles)
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
        # Reservoir sampling (Algorithm R) instead of ordered append: the
        # harness supplies `revealed` in TICKER-SORTED order (sorted(...)
        # over (ticker, origin_row) pairs), and appending straight into a
        # bounded deque would silently keep only the last pool_window
        # entries -- alphabetically LATE tickers -- once a single day's
        # batch exceeds pool_window at full-universe scale, reintroducing
        # the exact ordering bias fit()'s uniform sampling avoids (Codex
        # review: "Sample online pooled residuals without ticker-order
        # bias"). Seeded from step_index: deterministic and resume-safe,
        # like every other per-step draw in this system.
        rng = np.random.default_rng(step_index)
        for day in revealed:
            feature_row = day.features_window[np.flatnonzero(day.window_mask)[-1]]
            actual = np.array([day.actual[component] for component in COMPONENTS], dtype=np.float32)
            residual = self._residual(feature_row, actual)
            self._buffer(day.ticker).append(residual)
            self._pool_seen += 1
            if len(self._pool) < self.pool_window:
                self._pool.append(residual)
            else:
                replace_at = int(rng.integers(0, self._pool_seen))
                if replace_at < self.pool_window:
                    self._pool[replace_at] = residual
        return {"absorbed": len(revealed)}

    def reconstruction_diagnostic(
        self, requests: Sequence[ForecastRequest]
    ) -> dict[str, np.ndarray] | None:
        return None  # no encoder to health-check

    def save(self, directory: Path) -> None:
        arrays: dict[str, np.ndarray] = {}
        for ticker, buffer in self._residuals.items():
            if buffer:
                arrays[f"res_{ticker}"] = np.stack(list(buffer))
        if self._pool:
            arrays["res__pool"] = np.stack(list(self._pool))
        save_arrays(directory, arrays)
        write_metadata(
            directory,
            {
                "name": self.name,
                "residual_window": self.residual_window,
                "pool_window": self.pool_window,
                "pool_seen": self._pool_seen,
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
    ) -> "PersistenceModel":
        del panel_provider, strict, device  # state is fully in the artifact; nothing to re-derive
        metadata = read_metadata(directory)
        model = cls(residual_window=metadata["residual_window"], pool_window=metadata["pool_window"])
        arrays = load_arrays(directory)
        for name, array in arrays.items():
            if name == "res__pool":
                model._pool.extend(array)
            elif name.startswith("res_"):
                model._buffer(name[len("res_") :]).extend(array)
        # .get(): a checkpoint written before this field existed restores
        # with len(pool) instead -- an undercount only if the pool wasn't
        # yet full when saved, in which case reservoir replacement doesn't
        # kick in again until _pool_seen catches back up to the true count
        # anyway (append-until-full behaves identically either way).
        model._pool_seen = metadata.get("pool_seen", len(model._pool))
        return model

    # -- internals ---------------------------------------------------------
    def _buffer(self, ticker: str) -> deque:
        if ticker not in self._residuals:
            self._residuals[ticker] = deque(maxlen=self.residual_window)
        return self._residuals[ticker]

    @staticmethod
    def _residual(feature_row: np.ndarray, target_row: np.ndarray) -> np.ndarray:
        point = _persistence_point(feature_row)
        return np.array(
            [float(target_row[position]) - point[component] for position, component in enumerate(COMPONENTS)],
            dtype=np.float32,
        )

    @staticmethod
    def _quantiles_of(source) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        stacked = np.stack(list(source))
        return (
            np.quantile(stacked, 0.1, axis=0),
            np.quantile(stacked, 0.5, axis=0),
            np.quantile(stacked, 0.9, axis=0),
        )

    def _bands(
        self,
        ticker: str,
        point: dict[str, float],
        pool_quantiles: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
    ) -> dict[str, dict[str, float]]:
        own = self._residuals.get(ticker)
        if own is not None and len(own) >= _MIN_OWN_RESIDUALS:
            p10, p50, p90 = self._quantiles_of(own)
        elif pool_quantiles is not None:
            p10, p50, p90 = pool_quantiles
        elif self._pool:
            p10, p50, p90 = self._quantiles_of(self._pool)
        else:
            # Nothing observed anywhere yet: a flat +-2% prior band, wide
            # enough to be visibly uninformed rather than falsely confident.
            return {
                component: {"p10": point[component] - 0.02, "p50": point[component], "p90": point[component] + 0.02}
                for component in COMPONENTS
            }
        return {
            component: {
                "p10": point[component] + float(p10[position]),
                "p50": point[component] + float(p50[position]),
                "p90": point[component] + float(p90[position]),
            }
            for position, component in enumerate(COMPONENTS)
        }


class GbmQuantileModel:
    """16 histogram-GBM fits behind the shared protocol. See module docstring."""

    name = "gbm_quantile"

    def __init__(
        self,
        *,
        refit_every: int = 21,
        trailing_years: float = 8.0,
        row_cap: int = 500_000,
        max_iter: int = 100,
    ) -> None:
        self.refit_every = refit_every
        self.trailing_years = trailing_years
        self.row_cap = row_cap
        self.max_iter = max_iter
        # Estimator settings shared by all 16 fits. early_stopping is forced
        # off: its internal validation split is randomized and would break
        # the refit-on-load exactness this model's checkpointing depends on.
        self._hist_params = {"max_iter": max_iter, "learning_rate": 0.1, "early_stopping": False}
        # The design matrix is [features..., day_of_week, cluster_id]; the
        # trailing column is a style-cluster LABEL, and an unmarked integer
        # column gets threshold-split as an ordered quantity, which would
        # invent an ordering between arbitrary cluster ids. Declaring it
        # categorical makes the estimator split on set membership instead.
        # day_of_week is deliberately left as it was -- pre-existing
        # behaviour, and changing it is not this change's business.
        self._categorical_design_columns = (-1,)
        self._estimators: dict[str, dict[str, Any]] = {}
        self._panel: TrainingPanel | None = None
        self._seed = 0
        self._last_refit_step = 0
        self._last_refit_date: str | None = None
        self._latest_revealed_date: str | None = None
        self._last_fingerprint: str | None = None

    # -- protocol ----------------------------------------------------------
    def fit(self, panel: TrainingPanel, *, seed: int, step_index: int = 0) -> dict[str, Any]:
        self._panel = panel
        self._seed = seed
        cutoff = _fit_cutoff(panel)
        cutoff_str = np.datetime_as_string(cutoff, unit="D") if cutoff is not None else None
        rows = self._fit_at(cutoff_str)
        # Anchored to the CALLER's step_index (0 for the initial fit, the
        # harness's current cursor for a recovery retrain) rather than
        # hardcoded 0: retrain() doesn't reset the harness's own absolute
        # step_index, so a bare 0 here would make the very next online
        # update's `step_index - _last_refit_step >= refit_every` check
        # immediately true, forcing another full 16-model refit right
        # after recovery already refit through the cursor (Codex review:
        # "Preserve the GBM refit cadence after recovery retraining").
        self._last_refit_step = step_index
        self._last_refit_date = cutoff_str
        return {"training_rows": rows, "cutoff": cutoff_str}

    def predict(self, requests: Sequence[ForecastRequest]) -> list[OHLCForecast]:
        design = self._design(requests)
        raw: dict[str, dict[str, np.ndarray]] = {}
        for component, fits in self._estimators.items():
            raw[component] = {key: fits[key].predict(design) for key in ("point", "p10", "p50", "p90")}
        repairs = RepairCounts()
        forecasts = []
        for position, request in enumerate(requests):
            point = {component: float(raw[component]["point"][position]) for component in COMPONENTS}
            quantiles = {
                component: {key: float(raw[component][key][position]) for key in ("p10", "p50", "p90")}
                for component in COMPONENTS
            }
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
        for day in revealed:
            if self._latest_revealed_date is None or day.target_date > self._latest_revealed_date:
                self._latest_revealed_date = day.target_date
        if step_index - self._last_refit_step >= self.refit_every and self._latest_revealed_date:
            rows = self._fit_at(self._latest_revealed_date)
            self._last_refit_step = step_index
            self._last_refit_date = self._latest_revealed_date
            return {"refit": True, "training_rows": rows, "cutoff": self._last_refit_date}
        return {"refit": False}

    def reconstruction_diagnostic(
        self, requests: Sequence[ForecastRequest]
    ) -> dict[str, np.ndarray] | None:
        return None  # no encoder to health-check

    def save(self, directory: Path) -> None:
        # Metadata ONLY -- no pickle means no serialized trees; load()
        # refits deterministically instead (see module docstring).
        write_metadata(
            directory,
            {
                "name": self.name,
                "refit_every": self.refit_every,
                "trailing_years": self.trailing_years,
                "row_cap": self.row_cap,
                "max_iter": self.max_iter,
                "seed": self._seed,
                "last_refit_step": self._last_refit_step,
                "last_refit_date": self._last_refit_date,
                "latest_revealed_date": self._latest_revealed_date,
                "training_fingerprint": self._last_fingerprint,
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
    ) -> "GbmQuantileModel":
        del device  # sklearn estimators are CPU-only; nothing to honor here
        if panel_provider is None:
            raise ValueError("GbmQuantileModel.load requires a panel_provider (no-pickle refit-on-load)")
        metadata = read_metadata(directory)
        model = cls(
            refit_every=metadata["refit_every"],
            trailing_years=metadata["trailing_years"],
            row_cap=metadata["row_cap"],
            max_iter=metadata["max_iter"],
        )
        model._panel = panel_provider()
        model._seed = metadata["seed"]
        # Fingerprint the training matrix BEFORE fitting the 16 estimators
        # (cheap: array construction/subsampling, no ML), not after: a
        # strict load against a changed archive would otherwise spend the
        # full refit -- up to row_cap=500k rows across 16 fits -- before
        # ever raising (Antigravity review: "Fail fast on stale checkpoints
        # in GbmQuantileModel.load").
        design, targets = model._training_matrix(metadata["last_refit_date"])
        model._last_fingerprint = model._fingerprint(design, targets)
        recorded_fingerprint = metadata.get("training_fingerprint")
        if strict and recorded_fingerprint is not None and model._last_fingerprint != recorded_fingerprint:
            # Persistence and Octopus restore their EXACT saved state on
            # load; GBM instead refits from the current panel, so a changed
            # panel (an archive correction, or newly-eligible tickers) would
            # otherwise silently become a DIFFERENT model than the one the
            # checkpoint recorded, before any new day is processed (Codex
            # review: "Detect training-data changes before refitting GBM
            # checkpoints"). strict=False (an explicit --retrain) accepts
            # this refit as a stepping stone -- retrain() immediately refits
            # every model again anyway.
            raise TrainingDataChanged(
                f"gbm_quantile refit-on-load at cutoff {metadata['last_refit_date']} reproduced a DIFFERENT "
                "training set than the one the checkpoint recorded (the archive changed between save and "
                "load); pass --retrain for a sanctioned recovery refit, or investigate the archive change"
            )
        model._fit_estimators(design, targets)
        model._last_refit_step = metadata["last_refit_step"]
        model._last_refit_date = metadata["last_refit_date"]
        model._latest_revealed_date = metadata["latest_revealed_date"]
        return model

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _fingerprint(design: np.ndarray, targets: np.ndarray) -> str:
        hasher = hashlib.sha256()
        hasher.update(np.ascontiguousarray(design).tobytes())
        hasher.update(np.ascontiguousarray(targets).tobytes())
        return hasher.hexdigest()

    def _categorical_mask(self, n_columns: int) -> np.ndarray:
        """Boolean mask marking the design matrix's categorical columns."""
        mask = np.zeros(n_columns, dtype=bool)
        for column in self._categorical_design_columns:
            mask[column] = True
        return mask

    @staticmethod
    def _design(requests: Sequence[ForecastRequest]) -> np.ndarray:
        last_rows = batch_last_rows(requests)
        last_real = [np.flatnonzero(request.window_mask)[-1] for request in requests]
        day_of_week = np.array(
            [request.day_of_week[position] for request, position in zip(requests, last_real, strict=True)],
            dtype=np.float32,
        )
        # The style cluster rides along as its own column; without it the
        # label would be computed, checkpointed and then never seen by the
        # model meant to use it. It is declared categorical at fit time (see
        # _CATEGORICAL_DESIGN_COLUMNS) -- appending the raw integer alone
        # would let the estimator threshold-split it as an ordered float,
        # inventing exactly the ordering between arbitrary cluster ids that
        # keeping the label out of the float feature vector was meant to
        # avoid (Copilot review). Column order must match _training_matrix.
        cluster_id = np.array(
            [request.cluster_id[position] for request, position in zip(requests, last_real, strict=True)],
            dtype=np.float32,
        )
        return np.column_stack([last_rows, day_of_week, cluster_id])

    def _usable_mask(self, series, cutoff64: np.datetime64 | None) -> np.ndarray:
        usable = np.isfinite(series.features).all(axis=1) & np.isfinite(series.targets).all(axis=1)
        if cutoff64 is not None:
            # The trainability rule: a row enters training only once its
            # label end (the next row's date) is at or before the cutoff.
            usable &= series.label_end_dates <= cutoff64
            if self.trailing_years:
                horizon_start = cutoff64 - np.timedelta64(int(self.trailing_years * 365.25), "D")
                usable &= series.dates >= horizon_start
        return usable

    def _training_matrix(self, cutoff: str | None) -> tuple[np.ndarray, np.ndarray]:
        assert self._panel is not None
        cutoff64 = np.datetime64(cutoff) if cutoff else None

        # Pass 1: count usable rows per ticker WITHOUT retaining any feature
        # data. An 8-year trailing window at full-universe scale can hold
        # ~12M rows; materializing that full design matrix before the
        # row_cap subsample even applies is exactly the multi-gigabyte peak
        # this pass avoids (Codex review: "Apply the GBM row cap before
        # concatenating all rows").
        counts: list[tuple[str, int]] = []
        total_n = 0
        for ticker, series in self._panel.series.items():
            n = int(self._usable_mask(series, cutoff64).sum())
            if n:
                counts.append((ticker, n))
                total_n += n
        if not total_n:
            raise ValueError("REFUSED: no trainable rows at or before cutoff for gbm_quantile fit")

        if total_n <= self.row_cap:
            # Common/small-scale case: below the cap, so there is nothing to
            # subsample -- materialize normally.
            blocks_x: list[np.ndarray] = []
            blocks_y: list[np.ndarray] = []
            for ticker, _n in counts:
                series = self._panel.series[ticker]
                rows = np.flatnonzero(self._usable_mask(series, cutoff64))
                blocks_x.append(
                    np.column_stack(
                        [
                            series.features[rows],
                            series.day_of_week[rows].astype(np.float32),
                            series.cluster_id[rows].astype(np.float32),
                        ]
                    )
                )
                blocks_y.append(series.targets[rows])
            return np.concatenate(blocks_x), np.concatenate(blocks_y)

        # Deterministic subsample under the model seed, over the population
        # count computed above -- identical selection to a single-shot
        # rng.choice(total_n, row_cap) on the full concatenated matrix
        # (needed so refit-on-load draws the identical rows), but computed
        # by extracting only the chosen rows per ticker instead of ever
        # building the full matrix.
        rng = np.random.default_rng(self._seed)
        chosen = np.sort(rng.choice(total_n, size=self.row_cap, replace=False))
        sample_series = self._panel.series[counts[0][0]]
        # +2: day_of_week and cluster_id, the two categoricals _design()
        # appends at predict time. A mismatch here trains the model on a
        # different column layout than it scores with.
        design = np.empty((self.row_cap, sample_series.features.shape[1] + 2), dtype=np.float32)
        targets = np.empty((self.row_cap, sample_series.targets.shape[1]), dtype=np.float32)
        write_at = 0
        offset = 0
        for ticker, n in counts:
            block_end = offset + n
            lo = int(np.searchsorted(chosen, offset, side="left"))
            hi = int(np.searchsorted(chosen, block_end, side="left"))
            if hi > lo:
                local_rows = chosen[lo:hi] - offset
                series = self._panel.series[ticker]
                rows = np.flatnonzero(self._usable_mask(series, cutoff64))[local_rows]
                k = len(rows)
                design[write_at : write_at + k] = np.column_stack(
                    [
                        series.features[rows],
                        series.day_of_week[rows].astype(np.float32),
                        series.cluster_id[rows].astype(np.float32),
                    ]
                )
                targets[write_at : write_at + k] = series.targets[rows]
                write_at += k
            offset = block_end
        return design, targets

    def _fit_at(self, cutoff: str | None) -> int:
        design, targets = self._training_matrix(cutoff)
        self._last_fingerprint = self._fingerprint(design, targets)
        return self._fit_estimators(design, targets)

    def _fit_estimators(self, design: np.ndarray, targets: np.ndarray) -> int:
        # Lazy import inside fit: the house pattern for heavy deps (see
        # ml-forecaster/model.py's identically-motivated lazy sklearn import).
        import sys

        from sklearn.ensemble import HistGradientBoostingRegressor

        total_estimators = len(COMPONENTS) * 4
        completed = 0
        is_tty = sys.stdout.isatty()

        def _update_progress(component: str, target_name: str) -> None:
            if not is_tty:
                return
            pct = (completed / total_estimators) * 100
            bar_len = 24
            filled = bar_len * completed // total_estimators
            bar = (
                "=" * filled
                + (">" if filled < bar_len else "")
                + " " * (bar_len - filled - (1 if filled < bar_len else 0))
            )
            line = (
                f"\r[fit] gbm_quantile: [{bar}] {completed:2d}/{total_estimators} "
                f"({pct:3.0f}%) {component}:{target_name:<5}"
            )
            sys.stdout.write(line)
            sys.stdout.flush()

        self._estimators = {}
        for position, component in enumerate(COMPONENTS):
            y = targets[:, position].astype(np.float64)
            fits: dict[str, Any] = {}
            categorical: Any = self._categorical_mask(design.shape[1])
            _update_progress(component, "point")
            fits["point"] = HistGradientBoostingRegressor(
                loss="absolute_error",
                random_state=self._seed,
                categorical_features=categorical,
                **self._hist_params,
            ).fit(design, y)
            completed += 1
            _update_progress(component, "point")
            for key, quantile in (("p10", 0.1), ("p50", 0.5), ("p90", 0.9)):
                _update_progress(component, key)
                fits[key] = HistGradientBoostingRegressor(
                    loss="quantile",
                    quantile=quantile,
                    random_state=self._seed,
                    categorical_features=categorical,
                    **self._hist_params,
                ).fit(design, y)
                completed += 1
                _update_progress(component, key)
            self._estimators[component] = fits

        if is_tty:
            bar = "=" * 24
            sys.stdout.write(f"\r[fit] gbm_quantile: [{bar}] 16/16 (100%) done!                        \n")
            sys.stdout.flush()
        return len(design)


def attach_target_dates(forecasts: list[OHLCForecast], panel: TrainingPanel) -> list[OHLCForecast]:
    """Fill target_date from the panel (replay mode: the next row is known)."""
    filled = []
    for forecast in forecasts:
        series = panel.series.get(forecast.ticker)
        target_date = None
        if series is not None:
            # Calendar-day comparison, matching the same fix applied
            # elsewhere in the codebase (Codex review: "Match forecast
            # origins by calendar day").
            positions = np.flatnonzero(series.dates.astype("datetime64[D]") == np.datetime64(forecast.origin_date))
            if len(positions):
                label_end = series.label_end_dates[positions[0]]
                if not np.isnat(label_end):
                    target_date = str(np.datetime_as_string(label_end, unit="D"))
        filled.append(
            OHLCForecast(
                ticker=forecast.ticker,
                origin_date=forecast.origin_date,
                target_date=target_date,
                point=forecast.point,
                quantiles=forecast.quantiles,
                ref_close=forecast.ref_close,
                adj_ratio=forecast.adj_ratio,
            )
        )
    return filled
