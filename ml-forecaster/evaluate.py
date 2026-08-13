"""Walk-forward backtest + scoring for the pooled ridge sizing model.

Splits are chronological, EXPANDING-window only -- never k-fold, never
shuffled. This is time-series data: a shuffled/k-fold split would let rows
from AFTER a given test fold leak into that fold's training set, which is
exactly the kind of lookahead features.py's own no-lookahead contract is
designed to prevent at the feature level. walk_forward_splits() enforces the
same discipline at the split level: every fold's train set is built only
from rows whose forward-return LABEL (not merely the row's own date) is
strictly earlier than that fold's test window, and each successive fold's
train set is a strict superset of the previous one's (the "expanding
window"). A row dated before the test window can still have a target
computed from closes inside it -- see walk_forward_splits()'s own docstring
on why eligibility is decided by label_end_date, not date.

Each fold is scored on MAE, RMSE, and directional hit-rate (does the sign of
the prediction match the sign of the actual forward return?), against the
model AND two naive baselines computed from that SAME fold's train/test
split (never leaking test-fold data into a baseline either):

- predict-zero: always predict 0% forward return.
- predict-trailing-mean: always predict the fold's own training-set mean
  forward return (a fold-specific constant, not a single global constant --
  a naive baseline that "knows" the fold's own recent regime is a fair
  comparison for a model that gets the same training data).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

import model as model_module
from features import TARGET_COLUMN

DEFAULT_N_SPLITS = 5

#: FoldResult fields that are NOT score metrics (identifiers/bookkeeping) --
#: excluded when averaging fold scores into an "overall" summary.
_NON_METRIC_FOLD_FIELDS = frozenset(
    {"fold", "train_start", "train_end", "test_start", "test_end", "n_train", "n_test", "test_row_fingerprint"}
)


@dataclass
class FoldResult:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    # A SHA-256 hex digest of every test row's own (ticker, date) identity,
    # sorted so row order never affects it -- publish.py's own
    # _evaluation_window() uses this, alongside test_start/test_end, to
    # decide whether two runs' folds are safe to compare via raw MAE. The
    # boundary dates ALONE are not a complete fingerprint: an archive
    # correction that adds/removes/changes interior rows -- e.g. a ticker's
    # row within [test_start, test_end] gets dropped or a new ticker's row
    # is added -- without moving either boundary date leaves test_start/
    # test_end looking identical across two runs even though the actual
    # scored OBSERVATIONS differ, so a raw-MAE comparison would wrongly
    # treat a genuinely different sample as the same evaluation window
    # (Codex review: "Fingerprint observations rather than only fold
    # endpoints"). Excluded from _aggregate()'s metric averaging via
    # _NON_METRIC_FOLD_FIELDS, same as every other identifier field here.
    test_row_fingerprint: str
    model_mae: float
    model_rmse: float
    model_hit_rate: float
    baseline_zero_mae: float
    baseline_zero_rmse: float
    baseline_zero_hit_rate: float
    baseline_mean_mae: float
    baseline_mean_rmse: float
    baseline_mean_hit_rate: float


def _row_fingerprint(frame: pd.DataFrame) -> str:
    """SHA-256 hex digest of `frame`'s own (ticker, date) row identities,
    order-independent. See FoldResult.test_row_fingerprint's own docstring
    for why boundary dates alone are an incomplete fingerprint."""
    identities = sorted(
        f"{ticker}:{pd.Timestamp(date).date().isoformat()}"
        for ticker, date in zip(frame["ticker"].astype(str), frame["date"], strict=True)
    )
    return hashlib.sha256("|".join(identities).encode("utf-8")).hexdigest()


def walk_forward_splits(
    dates: pd.Series,
    *,
    n_splits: int = DEFAULT_N_SPLITS,
    label_end_dates: pd.Series | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Chronological expanding-window split indices, row-aligned to `dates`.

    The sorted, unique dates in `dates` are cut into `n_splits + 1` roughly
    equal chronological chunks. Fold i's TEST set is chunk i (1-indexed,
    i=1..n_splits); its TRAIN set is every row ELIGIBLE before that chunk's
    first date (`test_start`). Chunk 0 is never used as a test fold -- it has
    no preceding data to train on -- which is why there are `n_splits + 1`
    chunks for `n_splits` folds.

    `label_end_dates`, if given, must be row-aligned to `dates` (same length,
    same row order) and is each row's LABEL END date -- the date its forward-
    return target actually reads up to (see features.compute_label_end_date()).
    When supplied, TRAIN eligibility is decided by `label_end_date <
    test_start`, NOT merely `date < test_start`: a row whose own date is
    safely before the test fold can still have a target computed from closes
    INSIDE that fold, which would leak test-period prices into training if
    the row were left in (see this module's own docstring). Omitting
    `label_end_dates` falls back to the plain `date < test_start` rule --
    only correct when the caller has already independently guaranteed no
    label overlap, which is why evaluate_walk_forward() always supplies it.

    Raises ValueError if there are fewer than `n_splits + 1` distinct
    trading days -- fail closed rather than silently returning fewer folds
    than the caller asked for (or an empty/degenerate fold).
    """
    if n_splits < 1:
        raise ValueError(f"n_splits must be >= 1, got {n_splits}")
    unique_dates = np.sort(pd.Series(dates).unique())
    if len(unique_dates) < n_splits + 1:
        raise ValueError(
            f"need at least {n_splits + 1} distinct trading days for {n_splits} walk-forward "
            f"fold(s) (chunk 0 is reserved purely for initial training); got {len(unique_dates)}"
        )
    chunks = np.array_split(unique_dates, n_splits + 1)
    dates_array = pd.Series(dates).to_numpy()

    if label_end_dates is None:
        eligibility_array = dates_array
    else:
        eligibility_array = pd.Series(label_end_dates).to_numpy()
        if len(eligibility_array) != len(dates_array):
            raise ValueError(
                f"label_end_dates length {len(eligibility_array)} does not match dates length "
                f"{len(dates_array)} -- must be row-aligned"
            )

    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for fold_index in range(1, n_splits + 1):
        test_start = chunks[fold_index][0]
        test_end = chunks[fold_index][-1]
        train_idx = np.flatnonzero(eligibility_array < test_start)
        test_idx = np.flatnonzero((dates_array >= test_start) & (dates_array <= test_end))
        splits.append((train_idx, test_idx))
    return splits


def _mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(actual - predicted)))


def _rmse(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def _hit_rate(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Fraction of rows where sign(predicted) == sign(actual) (0 counts as its own sign)."""
    return float(np.mean(np.sign(actual) == np.sign(predicted)))


def evaluate_walk_forward(
    dataset: pd.DataFrame, *, n_splits: int = DEFAULT_N_SPLITS, alpha: float = model_module.DEFAULT_ALPHA
) -> dict[str, Any]:
    """Run a full walk-forward backtest over `dataset` and return fold + overall scores.

    `dataset` should already be filtered to whatever population the caller
    wants evaluated (train.py passes dip-day rows only, matching the
    target's own conditioning -- see model.py's docstring). A FRESH model is
    model_module.fit() on each fold's train slice -- never a model fit on
    data that includes any of that fold's own test rows.

    `dataset` must also carry a "label_end_date" column -- features.
    build_dataset() always adds one -- so walk_forward_splits() can exclude
    forward-label overlap with each test fold (see that function's own
    docstring on why train/test membership can't be decided from each row's
    own `date` alone).
    """
    if dataset.empty:
        raise ValueError("cannot evaluate an empty dataset")
    if "label_end_date" not in dataset.columns:
        raise ValueError(
            "dataset must carry a 'label_end_date' column (see features.build_dataset()) so "
            "walk_forward_splits() can exclude forward-label overlap with each test fold"
        )
    dataset = dataset.sort_values("date", kind="stable").reset_index(drop=True)
    splits = walk_forward_splits(dataset["date"], n_splits=n_splits, label_end_dates=dataset["label_end_date"])

    folds: list[FoldResult] = []
    for fold_number, (train_idx, test_idx) in enumerate(splits, start=1):
        train = dataset.iloc[train_idx]
        test = dataset.iloc[test_idx]

        fitted = model_module.fit(train, alpha=alpha, model_version=f"walkforward-fold-{fold_number}")
        actual = test[TARGET_COLUMN].to_numpy(dtype=float)
        predicted = fitted.predict_return_pct(test)

        trailing_mean = float(train[TARGET_COLUMN].mean())
        zero_baseline = np.zeros_like(actual)
        mean_baseline = np.full_like(actual, trailing_mean)

        folds.append(
            FoldResult(
                fold=fold_number,
                train_start=str(pd.Timestamp(train["date"].min()).date()),
                train_end=str(pd.Timestamp(train["date"].max()).date()),
                test_start=str(pd.Timestamp(test["date"].min()).date()),
                test_end=str(pd.Timestamp(test["date"].max()).date()),
                n_train=len(train),
                n_test=len(test),
                test_row_fingerprint=_row_fingerprint(test),
                model_mae=_mae(actual, predicted),
                model_rmse=_rmse(actual, predicted),
                model_hit_rate=_hit_rate(actual, predicted),
                baseline_zero_mae=_mae(actual, zero_baseline),
                baseline_zero_rmse=_rmse(actual, zero_baseline),
                baseline_zero_hit_rate=_hit_rate(actual, zero_baseline),
                baseline_mean_mae=_mae(actual, mean_baseline),
                baseline_mean_rmse=_rmse(actual, mean_baseline),
                baseline_mean_hit_rate=_hit_rate(actual, mean_baseline),
            )
        )

    return {
        "n_splits": len(folds),
        "alpha": alpha,
        "folds": [asdict(fold) for fold in folds],
        "overall": _aggregate(folds),
    }


def _aggregate(folds: list[FoldResult]) -> dict[str, float]:
    """Mean of each score metric across folds -- empty dict for zero folds."""
    if not folds:
        return {}
    metric_names = [f.name for f in fields(FoldResult) if f.name not in _NON_METRIC_FOLD_FIELDS]
    return {name: float(np.mean([getattr(fold, name) for fold in folds])) for name in metric_names}


def score_incumbent_on_folds(
    incumbent_model: model_module.TrainedModel,
    candidate_model: model_module.TrainedModel,
    dataset: pd.DataFrame,
) -> tuple[float, float] | None:
    """``(candidate_mae, incumbent_mae)``: both ``candidate_model`` and
    ``incumbent_model`` scored on exactly the rows whose label only became
    available AFTER the incumbent's own training cutoff
    (``incumbent_model.trained_at``) -- information the incumbent could not
    have seen at its own training time. Returns ``None`` if the cutoff can't
    be determined, or either side of the cutoff split is empty.

    Four earlier, all unsound, versions of this function preceded this one:

    1. Scoring the incumbent's frozen weights against EVERY one of the
       candidate's walk-forward folds, reusing the candidate's own overall
       (all-folds) MAE. Most of those "test" rows were already in the
       incumbent's OWN training set -- producing an artificially low,
       largely in-sample incumbent MAE (Codex review: "Avoid scoring the
       incumbent on its training rows").
    2. Requiring a walk-forward fold's ENTIRE test window to fall after the
       cutoff before it could qualify at all, which on an ordinary weekly
       retrain left zero folds qualifying almost always (Codex review:
       "Score the post-cutoff rows instead of requiring a whole fold"), then
       filtering individual ROWS within each fold by their own ``date``
       instead of ``label_end_date`` -- discarding rows whose date looked
       pre-cutoff even though their forward-return label only became
       computable afterward (Codex review: "Identify unseen labels by their
       end date").
    3. Refitting a separate "candidate" model PER walk-forward fold, on that
       fold's own ``train_idx``. That ``train_idx`` is bounded by the fold's
       own arbitrary chunk boundary from ``walk_forward_splits()``'s
       ``n_splits + 1`` partition, not by ``training_cutoff`` -- for
       anything but (at best) the very last fold, this is a far smaller,
       staler slice of history than what the incumbent itself was actually
       trained on (Codex review: "Train the comparison candidate through the
       incumbent cutoff").
    4. Even after fixing (3) to fit ONE comparison candidate on every row
       available by the cutoff (rather than a fold-bounded subset), this
       function still built that candidate ITSELF, internally, on
       ``dataset[dataset["label_end_date"] <= training_cutoff]`` -- under an
       UNCHANGED alpha/universe and append-only history (the ordinary weekly
       retrain case, not an exception), that row set is EXACTLY what the
       incumbent itself was originally trained on, so a deterministic Ridge
       fit on it reconstructs the incumbent's own weights almost exactly.
       The comparison then always came out a near-tie -- not because the
       real new model (the one actually about to be published, trained on
       the FULL current dataset including rows added since the incumbent
       trained) was no better, but because it never participated in this
       comparison at all; a self-reconstruction was compared against
       itself. This blocked every routine retrain, indefinitely, the moment
       the published model's staleness window elapsed (Codex review: "Avoid
       comparing the incumbent with its exact reconstruction"). The caller
       now supplies the REAL candidate model directly -- already fit on
       whatever data it was actually fit on -- and this function only
       scores it, never refits anything.

    Both the training-cutoff/held-out split above AND the target each row's
    target represents are keyed off ``label_end_date``, not ``date`` -- the
    same distinction ``walk_forward_splits()`` itself already draws for
    train/test eligibility (see that function's docstring): a row's label is
    a forward `horizon`-day return, so a row dated only a few sessions
    before `training_cutoff` can still have a label that only became fully
    computable AFTER training happened.

    Scoring ``candidate_model`` on rows it may itself have been (partly)
    trained on is a deliberate, accepted trade-off, not an oversight: there
    is no way to hold out rows that are simultaneously unseen by the
    incumbent (which trained on everything through the cutoff) AND unseen
    by a genuinely newer candidate (which, to represent real progress, must
    be allowed to train on what's accumulated since) without holding out
    nothing at all. The routine walk-forward evaluation elsewhere in this
    module (``evaluate_walk_forward``, feeding ``new_mae``) remains the
    honest out-of-sample estimate this repo publishes and gates on; this
    comparison answers a narrower, complementary question -- did the
    information gained since the incumbent trained actually help predict
    what happened in that period -- not "does the candidate generalize to
    literally unseen future data."
    """
    if not isinstance(incumbent_model.trained_at, str):
        return None
    try:
        parsed_cutoff = datetime.fromisoformat(incumbent_model.trained_at)
    except ValueError:
        return None
    if parsed_cutoff.tzinfo is not None:
        parsed_cutoff = parsed_cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    training_cutoff = pd.Timestamp(parsed_cutoff)

    if dataset.empty or "label_end_date" not in dataset.columns:
        return None

    train = dataset[dataset["label_end_date"] <= training_cutoff]
    test = dataset[dataset["label_end_date"] > training_cutoff]
    if train.empty or test.empty:
        return None

    actual = test[TARGET_COLUMN].to_numpy(dtype=float)
    candidate_predicted = candidate_model.predict_return_pct(test)
    candidate_mae = _mae(actual, candidate_predicted)
    if not math.isfinite(candidate_mae):
        return None
    incumbent_predicted = incumbent_model.predict_return_pct(test)
    incumbent_mae = _mae(actual, incumbent_predicted)
    if not math.isfinite(incumbent_mae):
        return None
    return candidate_mae, incumbent_mae


__all__ = [
    "DEFAULT_N_SPLITS",
    "FoldResult",
    "evaluate_walk_forward",
    "score_incumbent_on_folds",
    "walk_forward_splits",
]
