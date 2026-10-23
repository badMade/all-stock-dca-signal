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
from typing import Any, cast

import model as model_module
import numpy as np
import pandas as pd
from features import NUMERIC_FEATURE_COLUMNS, TARGET_COLUMN

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
    """SHA-256 hex digest of `frame`'s scored CONTENT, order-independent. See
    FoldResult.test_row_fingerprint's own docstring for why boundary dates alone
    are an incomplete fingerprint.

    Hashes the score-relevant values, not only the (ticker, date) identity. An
    archive correction that rewrites OHLCV for a row that already exists --
    changing its features and target without adding or removing the row --
    leaves the identity set untouched, so an identity-only digest declared the
    folds the same sample. `_evaluation_window()` then let `blocking_reasons()`
    compare raw MAEs across genuinely different data whenever the incumbent
    artifact could not be re-scored, and an easier corrected sample could
    authorize a model that had actually regressed (Codex review: "Include score
    values in the fold fingerprint").

    Values are formatted at repr precision so a float that survives a round trip
    unchanged hashes identically, while any real correction moves the digest.
    Missing values render as the literal "nan", which is itself a meaningful
    change of content when a correction fills or clears a cell.
    """
    ticker_idx = frame.columns.get_loc("ticker")
    date_idx = frame.columns.get_loc("date")
    scored_columns = [
        column
        for column in (*NUMERIC_FEATURE_COLUMNS, TARGET_COLUMN)
        if column in frame.columns
    ]
    col_indices = {c: frame.columns.get_loc(c) for c in scored_columns}
    identities = sorted(
        "|".join(
            (
                f"{row[ticker_idx]}:{pd.Timestamp(row[date_idx]).date().isoformat()}",
                *(f"{column}={row[col_indices[column]]!r}" for column in scored_columns),
            )
        )
        for row in frame.itertuples(index=False, name=None)
    )
    return hashlib.sha256("\u241f".join(identities).encode("utf-8")).hexdigest()


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
        train = cast(pd.DataFrame, dataset.iloc[train_idx])
        test = cast(pd.DataFrame, dataset.iloc[test_idx])

        fitted = model_module.fit(train, alpha=alpha, model_version=f"walkforward-fold-{fold_number}")
        actual = test[TARGET_COLUMN].to_numpy(dtype=float)
        predicted = fitted.predict_return_pct(test)

        trailing_mean = train[TARGET_COLUMN].mean()
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


def select_holdout_tickers(tickers: list[str], *, holdout_frac: float = 0.3, seed: int = 0) -> list[str]:
    """Deterministically pick the held-out tickers: sorted by a seeded SHA-256 of each symbol.

    Hash-ordered rather than ``random``-sampled so the choice depends only on
    the ticker set, ``holdout_frac`` and ``seed`` -- never on discovery order or
    a process-global RNG -- and the same universe always yields the same
    diagnostic. At least one ticker is held out and at least one is left to
    train on; fewer than two distinct tickers cannot be split and raises.
    """
    unique = sorted({str(t).upper() for t in tickers})
    if len(unique) < 2:
        raise ValueError(f"need at least 2 distinct tickers for a ticker-held-out evaluation, got {len(unique)}")
    if not 0.0 < holdout_frac < 1.0:
        raise ValueError(f"holdout_frac must be strictly between 0 and 1, got {holdout_frac}")
    ranked = sorted(unique, key=lambda t: hashlib.sha256(f"{seed}:{t}".encode("utf-8")).hexdigest())
    n_holdout = min(len(unique) - 1, max(1, math.ceil(holdout_frac * len(unique))))
    return sorted(ranked[:n_holdout])


def evaluate_ticker_holdout(
    dataset: pd.DataFrame,
    *,
    holdout_frac: float = 0.3,
    seed: int = 0,
    n_splits: int = DEFAULT_N_SPLITS,
    alpha: float = model_module.DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Diagnostic: does the pooled model generalize to tickers it never trained on?

    ``evaluate_walk_forward`` pools every ticker into each fold, so a good score
    can come from memorizing per-ticker behavior (the model's ticker one-hot
    included) rather than from anything that transfers. This runs the SAME
    chronological folds and the SAME train-eligibility rule (``label_end_date`` <
    ``test_start``), but fits only on tickers A and scores on held-out tickers B
    over the fold's test dates -- B's rows never reach any fit, and an unseen
    ticker maps to an all-zero one-hot row (model.py), so no model change is
    needed. Each fold also scores the same fitted model on A's own test-window
    rows (``seen_ticker_model_mae``); the held-out minus seen gap is the
    generalization cost.

    DIAGNOSTIC ONLY: the result is reported under ``report["diagnostics"]`` and is
    deliberately never merged into ``evaluate_walk_forward``'s output, its
    ``overall`` aggregate or ``FoldResult`` -- ``publish.guarded_publish`` reads
    only those, and ``eval_metric.evaluation_window`` (the fold-comparability
    fingerprint) must not move because a diagnostic was added.

    Folds where either side is empty after the ticker split are skipped and
    counted in ``skipped_folds`` rather than scored on nothing.
    """
    if dataset.empty:
        raise ValueError("cannot evaluate an empty dataset")
    if "label_end_date" not in dataset.columns:
        raise ValueError("dataset must carry a 'label_end_date' column (see features.build_dataset())")
    holdout = select_holdout_tickers(list(dataset["ticker"].astype(str)), holdout_frac=holdout_frac, seed=seed)
    holdout_set = set(holdout)
    train_tickers = sorted({str(t).upper() for t in dataset["ticker"]} - holdout_set)

    dataset = dataset.sort_values("date", kind="stable").reset_index(drop=True)
    is_holdout = dataset["ticker"].astype(str).str.upper().isin(holdout_set).to_numpy()
    splits = walk_forward_splits(dataset["date"], n_splits=n_splits, label_end_dates=dataset["label_end_date"])

    folds: list[FoldResult] = []
    seen_maes: list[float] = []
    skipped = 0
    for fold_number, (train_idx, test_idx) in enumerate(splits, start=1):
        train_idx = train_idx[~is_holdout[train_idx]]
        held_idx = test_idx[is_holdout[test_idx]]
        seen_idx = test_idx[~is_holdout[test_idx]]
        if len(train_idx) == 0 or len(held_idx) == 0:
            skipped += 1
            continue
        train = cast(pd.DataFrame, dataset.iloc[train_idx])
        test = cast(pd.DataFrame, dataset.iloc[held_idx])

        fitted = model_module.fit(train, alpha=alpha, model_version=f"ticker-holdout-fold-{fold_number}")
        actual = test[TARGET_COLUMN].to_numpy(dtype=float)
        predicted = fitted.predict_return_pct(test)
        zero_baseline = np.zeros_like(actual)
        mean_baseline = np.full_like(actual, train[TARGET_COLUMN].mean())
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
        if len(seen_idx):
            seen = cast(pd.DataFrame, dataset.iloc[seen_idx])
            seen_maes.append(
                _mae(seen[TARGET_COLUMN].to_numpy(dtype=float), fitted.predict_return_pct(seen))
            )
        else:
            seen_maes.append(float("nan"))

    overall = _aggregate(folds)
    finite_seen = [m for m in seen_maes if math.isfinite(m)]
    if finite_seen and overall:
        overall["seen_ticker_model_mae"] = float(np.mean(finite_seen))
        overall["generalization_gap_mae"] = overall["model_mae"] - overall["seen_ticker_model_mae"]
    return {
        "n_splits": len(folds),
        "skipped_folds": skipped,
        "alpha": alpha,
        "holdout_frac": holdout_frac,
        "seed": seed,
        "holdout_tickers": holdout,
        "train_tickers": train_tickers,
        "folds": [asdict(fold) for fold in folds],
        "overall": overall,
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
    """``(candidate_mae, incumbent_mae)`` on a shared, later holdout.

    The comparison candidate uses the prospective final model's
    hyperparameters, but is freshly fit only on rows whose labels are known
    before the holdout starts.  The incumbent is frozen.  Consequently every
    scored label is unseen by both models, while the comparison candidate can
    still learn from rows that arrived after the incumbent's training cutoff
    (``incumbent_model.trained_at``). Returns ``None`` if the cutoff can't be
    determined, or either side of the cutoff split is empty.

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
    4. Comparing the final candidate (fit on the complete current dataset)
       directly on post-cutoff rows made its score in-sample while the
       incumbent's was out-of-sample.  The later holdout below instead fits
       an aligned comparison candidate through the start of that holdout;
       the final publication model may be fit on all rows only after this
       honest gate has been computed.

    Both the training-cutoff/held-out split above AND the target each row's
    target represents are keyed off ``label_end_date``, not ``date`` -- the
    same distinction ``walk_forward_splits()`` itself already draws for
    train/test eligibility (see that function's docstring): a row's label is
    a forward `horizon`-day return, so a row dated only a few sessions
    before `training_cutoff` can still have a label that only became fully
    computable AFTER training happened.

    Returns ``None`` when there is not enough post-cutoff history to provide
    both newer candidate-training observations and a nonempty holdout.
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

    dataset = dataset.sort_values("date", kind="stable").reset_index(drop=True)
    if not (dataset["label_end_date"] <= training_cutoff).any():
        return None
    post_cutoff_dates = np.sort(dataset.loc[dataset["label_end_date"] > training_cutoff, "date"].unique())
    if len(post_cutoff_dates) < 2:
        return None

    test_start = post_cutoff_dates[len(post_cutoff_dates) // 2]
    train = cast(pd.DataFrame, dataset[dataset["label_end_date"] < test_start])
    test = cast(
        pd.DataFrame,
        dataset[(dataset["date"] >= test_start) & (dataset["label_end_date"] > training_cutoff)],
    )
    if train.empty or test.empty or not (train["label_end_date"] > training_cutoff).any():
        return None

    comparison_candidate = model_module.fit(
        train,
        alpha=candidate_model.alpha,
        model_version="incumbent-comparison-candidate",
        return_clip_pct=candidate_model.return_clip_pct,
        horizon_trading_days=candidate_model.horizon_trading_days,
    )

    actual = test[TARGET_COLUMN].to_numpy(dtype=float)
    candidate_predicted = comparison_candidate.predict_return_pct(test)
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
    "evaluate_ticker_holdout",
    "evaluate_walk_forward",
    "score_incumbent_on_folds",
    "select_holdout_tickers",
    "walk_forward_splits",
]
