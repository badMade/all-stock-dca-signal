"""A single, pooled, regularized linear model for the ML sizing-adjustment forecaster.

v1 design, deliberately simple: one Ridge-regularized linear regression
trained across the FULL ticker universe found in the archive -- NOT one
model per ticker. Per-ticker histories in this repo are small (a few hundred
to ~1500 rows once rolling-window warmup and the forward-return horizon are
trimmed), far too little to fit a separate model per symbol without
overfitting. Pooling multiplies the effective sample size (every ticker's
rows contribute to one shared fit) and keeps the resulting coefficients
directly interpretable -- which matters here specifically because this
model's output perturbs a REAL per-trade dollar amount once published to
config/ml_sizing_adjustment.json and read by
near_close_dca.graded_buy_multiplier(). A reviewer can look at
`coefficients` in a saved model.json and see exactly which feature is
pushing a given prediction up or down; that auditability is worth more here
than the extra accuracy a fancier model might buy.

v2 note (not built, deliberately): a pooled gradient-boosted-tree model
(e.g. scikit-learn's HistGradientBoostingRegressor) is a reasonable upgrade
once this v1 has a live track record -- it could capture ticker-specific
nonlinearities and feature interactions a linear model can't. v1 stays
linear and auditable by design; see this module's own predict path for why
that choice also has a security/trust-boundary payoff, not just an
interpretability one.

Target: the forward N-trading-day realized return (features.TARGET_COLUMN),
conditional on the row being a dip day (features.py's "dip_day" flag) --
callers (train.py, evaluate.py) are responsible for filtering to dip-day
rows before calling fit(); this module does not filter for them, so it can
also be reused for evaluate.py's non-dip-filtered internal checks if ever
needed.

Serialization is plain JSON, NOT pickle. scikit-learn's Ridge is used only
to FIT (`fit()` below) -- once fit, its `coef_`/`intercept_` are extracted
into a small dataclass whose `to_dict()`/`from_dict()` are ordinary
`json.dumps`/`json.loads`, and PREDICTION (`TrainedModel.predict_return_pct`)
is a plain numpy dot product against those stored numbers. No sklearn
Estimator object, and therefore no pickle bytestream, ever crosses the
boundary between this untrusted-heavy-dependency sibling tree and anything
that later reads a saved model.json -- avoiding the arbitrary-code-execution
trust boundary an `__reduce__`-based unpickle would otherwise open up. (The
trusted stdlib-only core, near_close_dca.py, never imports this module or
model.json directly either way -- it only ever reads the bounded,
schema-validated config/ml_sizing_adjustment.json that publish.py writes --
but this module's own on-disk artifacts stay pickle-free regardless, since
model.json is itself handed between agent sessions/machines as a plain run
artifact.)
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from features import NUMERIC_FEATURE_COLUMNS, TARGET_COLUMN

#: day_of_week dummy columns, Monday=0 .. Sunday=6. The archive should only
#: ever carry weekday rows, but sizing the one-hot space over the full week
#: costs nothing and means an unexpected weekend row degrades to "one more
#: valid dummy slot" instead of silently falling outside every column.
DAY_OF_WEEK_LABELS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULT_ALPHA = 1.0
#: Predicted forward return is clipped to +/- this many percentage points
#: before being rescaled into a multiplier -- a "sane band" guard so one
#: outlier prediction (a thin-history ticker, an extreme feature row) can't
#: swing the published multiplier further than a moderate, plausible
#: forward-return forecast would.
DEFAULT_RETURN_CLIP_PCT = 10.0
#: Matches config/ml_sizing_adjustment.json's own documented example bounds
#: (schemas/ml_sizing_adjustment.schema.json allows up to [0.5, 1.5]).
DEFAULT_MIN_MULTIPLIER = 0.8
DEFAULT_MAX_MULTIPLIER = 1.2


def _feature_names(ticker_vocabulary: list[str]) -> list[str]:
    """The full, ORDERED column list `coefficients` lines up against."""
    names = list(NUMERIC_FEATURE_COLUMNS)
    names += [f"ticker={ticker}" for ticker in ticker_vocabulary]
    names += [f"day_of_week={label}" for label in DAY_OF_WEEK_LABELS]
    return names


def _design_matrix(
    frame: pd.DataFrame,
    ticker_vocabulary: list[str],
    numeric_feature_stats: dict[str, dict[str, float]],
) -> np.ndarray:
    """Build the standardized+one-hot design matrix `fit()` and `predict_return_pct()` share.

    Numeric columns are standardized ((x - mean) / std) using the stats
    captured at TRAINING time (never recomputed from `frame` itself) --
    exactly what a fitted model needs to score new/held-out rows
    consistently with how it was trained.

    A ticker in `frame` that is NOT in `ticker_vocabulary` (unseen at
    training time -- e.g. a brand-new universe addition) gets an all-zero
    one-hot row: its prediction rests entirely on the intercept and numeric
    features, with no ticker-specific offset it never had a chance to learn.
    This is a deliberate graceful degradation, not an error -- model.py must
    stay usable the moment a new ticker is added to config/tickers.json,
    before the next weekly retrain has ever seen it.
    """
    n_rows = len(frame)
    numeric_columns = []
    for column in NUMERIC_FEATURE_COLUMNS:
        values = frame[column].to_numpy(dtype=float)
        stats = numeric_feature_stats.get(column, {"mean": 0.0, "std": 1.0})
        std = stats["std"] if stats["std"] else 1.0
        numeric_columns.append((values - stats["mean"]) / std)
    numeric_matrix = np.column_stack(numeric_columns) if numeric_columns else np.zeros((n_rows, 0))

    ticker_index = {ticker: position for position, ticker in enumerate(ticker_vocabulary)}
    ticker_matrix = np.zeros((n_rows, len(ticker_vocabulary)))
    for row, ticker in enumerate(frame["ticker"].to_numpy()):
        position = ticker_index.get(ticker)
        if position is not None:
            ticker_matrix[row, position] = 1.0

    dow_matrix = np.zeros((n_rows, len(DAY_OF_WEEK_LABELS)))
    for row, day_of_week in enumerate(frame["day_of_week"].to_numpy()):
        day_index = int(day_of_week)
        if 0 <= day_index < len(DAY_OF_WEEK_LABELS):
            dow_matrix[row, day_index] = 1.0

    return np.hstack([numeric_matrix, ticker_matrix, dow_matrix])


@dataclass
class TrainedModel:
    """A fitted pooled Ridge model, fully described by JSON-serializable state.

    See module docstring for why prediction never touches scikit-learn or
    pickle: only `fit()` (a module-level function, not a method here) needs
    scikit-learn installed; scoring an already-trained model.json requires
    only numpy + pandas.
    """

    model_version: str
    alpha: float
    ticker_vocabulary: list[str]
    numeric_feature_stats: dict[str, dict[str, float]]
    feature_names: list[str]
    coefficients: list[float]
    intercept: float
    return_clip_pct: float = DEFAULT_RETURN_CLIP_PCT
    trained_at: str | None = None
    n_training_rows: int = 0
    # Mirrors publish.DEFAULT_HORIZON_TRADING_DAYS / features.FORWARD_RETURN_HORIZON_DAYS --
    # duplicated here (not imported) for the same light-import-footprint reason those two
    # modules already duplicate it between each other. Without this, a model.json trained
    # with a non-default --horizon carries no record of which forward-return horizon its
    # coefficients/metrics describe -- ambiguous once a later publish replaces the live
    # config, or immediately for a --dry-run artifact that never reaches publish.py's own
    # horizon_trading_days field at all (Codex review: "Record the configured horizon in
    # run artifacts").
    horizon_trading_days: int = 5
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrainedModel":
        """Reconstruct from `to_dict()`'s output (or an equivalent JSON blob).

        Unknown keys are ignored (forward-compatible with a future field);
        this is the mirror of to_dict(), not a schema validator -- a
        genuinely malformed model.json still raises via the dataclass's own
        required-field TypeError.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "TrainedModel":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def predict_return_pct(self, frame: pd.DataFrame) -> np.ndarray:
        """Predicted forward-return percentage for every row in `frame`."""
        design = _design_matrix(frame, self.ticker_vocabulary, self.numeric_feature_stats)
        return design @ np.asarray(self.coefficients, dtype=float) + self.intercept

    def predict_multipliers(
        self,
        frame: pd.DataFrame,
        *,
        min_multiplier: float = DEFAULT_MIN_MULTIPLIER,
        max_multiplier: float = DEFAULT_MAX_MULTIPLIER,
    ) -> dict[str, float]:
        """Bounded per-ticker sizing multiplier, keyed by UPPERCASED ticker.

        `frame` should carry (typically) one row per ticker -- the row whose
        features represent "now" for publishing purposes (see train.py's own
        choice of "each ticker's most recent available row"). If `frame`
        carries more than one row for the same ticker, the LAST one wins
        (matching this repo's own general last-write-wins convention).
        """
        predicted_returns = self.predict_return_pct(frame)
        multipliers: dict[str, float] = {}
        for ticker, predicted_return in zip(frame["ticker"].to_numpy(), predicted_returns, strict=True):
            predicted_return = float(predicted_return)
            if not math.isfinite(predicted_return):
                # A non-finite prediction (e.g. an overflowing design-matrix
                # computation from an extreme feature value) must not
                # silently produce an apparently-valid bounded multiplier --
                # rescale_return_to_multiplier's own min/max clipping maps a
                # NaN input to max_multiplier and +-inf to
                # max_multiplier/min_multiplier, output that is completely
                # indistinguishable from a genuine, schema-valid prediction
                # and would pass guarded_publish() unnoticed (Codex review:
                # "Reject non-finite forecasts before clipping"). Skip this
                # ticker's adjustment entirely rather than publish a bogus
                # one -- the caller's existing "ticker has no adjustment"
                # handling already covers the resulting gap.
                continue
            multipliers[str(ticker).upper()] = rescale_return_to_multiplier(
                predicted_return,
                min_multiplier=min_multiplier,
                max_multiplier=max_multiplier,
                return_clip_pct=self.return_clip_pct,
            )
        return multipliers


def rescale_return_to_multiplier(
    predicted_return_pct: float,
    *,
    min_multiplier: float = DEFAULT_MIN_MULTIPLIER,
    max_multiplier: float = DEFAULT_MAX_MULTIPLIER,
    return_clip_pct: float = DEFAULT_RETURN_CLIP_PCT,
) -> float:
    """Clip a predicted forward return to a sane band, then rescale linearly into a bounded multiplier.

    A predicted return of 0% (no expected move) maps to the midpoint of
    [min_multiplier, max_multiplier] -- 1.0 for the schema's own documented
    default 0.8/1.2 bounds, i.e. "no adjustment". A predicted return at or
    beyond +/-return_clip_pct maps to max_multiplier / min_multiplier
    respectively; nothing this function returns can ever fall outside
    [min_multiplier, max_multiplier], which is itself the bounded-multiplier
    contract config/ml_sizing_adjustment.json's schema enforces on publish.
    """
    if return_clip_pct <= 0:
        raise ValueError("return_clip_pct must be positive")
    if not math.isfinite(predicted_return_pct):
        # NaN/+-inf must be rejected explicitly, not left to the min/max
        # clipping below: Python's nested min/max silently maps a NaN input
        # to max_multiplier and an infinite one to max_multiplier/
        # min_multiplier -- output indistinguishable from a genuine
        # prediction, so a caller that doesn't separately check finiteness
        # would publish it as one (Codex review: "Reject non-finite
        # forecasts before clipping"). Every caller in this tree already
        # filters non-finite predictions before reaching here (see
        # predict_multipliers() and train.py's own scoring loop); this is
        # the fail-closed backstop for any other caller.
        raise ValueError(f"predicted_return_pct must be finite, got {predicted_return_pct!r}")
    clipped = max(-return_clip_pct, min(return_clip_pct, predicted_return_pct))
    fraction = (clipped + return_clip_pct) / (2.0 * return_clip_pct)  # 0..1
    return round(min_multiplier + fraction * (max_multiplier - min_multiplier), 6)


def fit(
    dataset: pd.DataFrame,
    *,
    alpha: float = DEFAULT_ALPHA,
    model_version: str,
    return_clip_pct: float = DEFAULT_RETURN_CLIP_PCT,
    trained_at: str | None = None,
    horizon_trading_days: int = 5,
) -> TrainedModel:
    """Fit a pooled Ridge model on `dataset` (see features.build_dataset()).

    `dataset` must carry NUMERIC_FEATURE_COLUMNS, "ticker", "day_of_week",
    and TARGET_COLUMN -- typically already filtered to dip-day rows by the
    caller (see this module's own docstring). scikit-learn is imported
    lazily, here, so that scoring an already-trained model.json (via
    TrainedModel.load()/predict_return_pct()) never requires scikit-learn to
    be installed -- only training does.
    """
    from sklearn.linear_model import Ridge

    if dataset.empty:
        raise ValueError("cannot fit a model on an empty dataset")

    ticker_vocabulary = sorted(dataset["ticker"].astype(str).unique().tolist())
    numeric_feature_stats: dict[str, dict[str, float]] = {}
    for column in NUMERIC_FEATURE_COLUMNS:
        values = dataset[column].to_numpy(dtype=float)
        numeric_feature_stats[column] = {"mean": float(np.mean(values)), "std": float(np.std(values, ddof=0))}

    design = _design_matrix(dataset, ticker_vocabulary, numeric_feature_stats)
    target = dataset[TARGET_COLUMN].to_numpy(dtype=float)

    regressor = Ridge(alpha=alpha)
    regressor.fit(design, target)

    return TrainedModel(
        model_version=model_version,
        alpha=alpha,
        ticker_vocabulary=ticker_vocabulary,
        numeric_feature_stats=numeric_feature_stats,
        feature_names=_feature_names(ticker_vocabulary),
        coefficients=[float(coefficient) for coefficient in regressor.coef_],
        intercept=float(regressor.intercept_),
        return_clip_pct=return_clip_pct,
        trained_at=trained_at,
        n_training_rows=len(dataset),
        horizon_trading_days=horizon_trading_days,
    )


__all__ = [
    "DAY_OF_WEEK_LABELS",
    "DEFAULT_ALPHA",
    "DEFAULT_MAX_MULTIPLIER",
    "DEFAULT_MIN_MULTIPLIER",
    "DEFAULT_RETURN_CLIP_PCT",
    "TrainedModel",
    "fit",
    "rescale_return_to_multiplier",
]
