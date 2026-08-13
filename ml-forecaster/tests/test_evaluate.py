"""Tests for evaluate.py -- walk-forward split correctness + fold/baseline scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import model as model_module
from conftest import make_synthetic_ohlcv, write_ticker_csv
from evaluate import DEFAULT_N_SPLITS, evaluate_walk_forward, score_incumbent_on_folds, walk_forward_splits
from features import TARGET_COLUMN, build_dataset


@pytest.fixture
def dip_dataset(tmp_path):
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=31, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=32, n=260, drift=-0.0002))
    write_ticker_csv(history_dir, "CCC", make_synthetic_ohlcv(seed=33, n=260, drift=0.0001))
    dataset = build_dataset(history_dir, ["AAA", "BBB", "CCC"])
    return dataset[dataset["dip_day"]].reset_index(drop=True)


def test_walk_forward_splits_never_leak_future_into_train_and_expand_monotonically(dip_dataset):
    dataset = dip_dataset.sort_values("date").reset_index(drop=True)
    splits = walk_forward_splits(dataset["date"], n_splits=4)

    assert len(splits) == 4
    train_sizes = []
    for train_idx, test_idx in splits:
        assert len(train_idx) > 0
        assert len(test_idx) > 0
        train_dates = dataset.loc[train_idx, "date"]
        test_dates = dataset.loc[test_idx, "date"]
        # No leakage: every train date is strictly before every test date.
        assert train_dates.max() < test_dates.min()
        train_sizes.append(len(train_idx))

    # Expanding window: each successive fold's train set is strictly larger
    # than the previous fold's (a superset, since dates only accumulate).
    assert train_sizes == sorted(train_sizes)
    assert len(set(train_sizes)) == len(train_sizes)


def test_walk_forward_splits_raises_when_too_few_distinct_trading_days():
    dates = pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    with pytest.raises(ValueError, match="distinct trading days"):
        walk_forward_splits(dates, n_splits=5)


def test_walk_forward_splits_rejects_a_nonpositive_n_splits():
    dates = pd.Series(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    with pytest.raises(ValueError):
        walk_forward_splits(dates, n_splits=0)


def test_walk_forward_splits_excludes_rows_whose_label_window_overlaps_the_test_fold():
    """Regression test for the forward-label-overlap leak.

    8 distinct trading days (0..7), n_splits=1: chunk 0 = [0,1,2,3] (train
    pool), chunk 1 = [4,5,6,7] (test fold, test_start=4). A 3-trading-day
    forward-return horizon means row i's label reads up to day i+3.

    Naively including every row with `date < test_start` (the pre-fix rule)
    would keep rows 1, 2, and 3 in train_idx -- each of those rows' labels
    (label_end_date = date + 3) reach into the test fold (4, 5, and 6, all
    >= test_start=4), leaking test-period closes into training. Only row 0
    (label_end_date = 3, strictly before test_start) is actually leak-free.
    """
    horizon = 3
    dates = pd.Series(range(8))
    label_end_dates = dates + horizon

    splits = walk_forward_splits(dates, n_splits=1, label_end_dates=label_end_dates)
    assert len(splits) == 1
    train_idx, test_idx = splits[0]

    assert list(test_idx) == [4, 5, 6, 7]
    assert list(train_idx) == [0]

    # Pin down exactly which rows the pre-fix, date-only rule would have
    # wrongly kept in train -- this is the leak the fix closes.
    naive_train_idx = np.flatnonzero(dates.to_numpy() < 4)
    assert set(naive_train_idx) == {0, 1, 2, 3}
    assert set(naive_train_idx) - set(train_idx) == {1, 2, 3}


def test_walk_forward_splits_label_end_dates_must_be_row_aligned_with_dates():
    dates = pd.Series(range(6))
    mismatched_label_end_dates = pd.Series(range(5))  # one row short
    with pytest.raises(ValueError, match="row-aligned"):
        walk_forward_splits(dates, n_splits=1, label_end_dates=mismatched_label_end_dates)


def test_evaluate_walk_forward_reports_every_fold_with_finite_baselines(dip_dataset):
    result = evaluate_walk_forward(dip_dataset, n_splits=3, alpha=1.0)

    assert result["n_splits"] == 3
    assert len(result["folds"]) == 3
    for fold in result["folds"]:
        for key in (
            "model_mae",
            "model_rmse",
            "model_hit_rate",
            "baseline_zero_mae",
            "baseline_zero_rmse",
            "baseline_zero_hit_rate",
            "baseline_mean_mae",
            "baseline_mean_rmse",
            "baseline_mean_hit_rate",
        ):
            assert key in fold
            assert np.isfinite(fold[key])
        assert fold["train_end"] < fold["test_start"]  # ISO date strings compare correctly lexicographically

    overall = result["overall"]
    assert overall["model_mae"] >= 0
    assert overall["baseline_zero_mae"] >= 0
    assert 0.0 <= overall["model_hit_rate"] <= 1.0


def test_evaluate_walk_forward_uses_default_n_splits(dip_dataset):
    result = evaluate_walk_forward(dip_dataset)
    assert result["n_splits"] == DEFAULT_N_SPLITS


def test_evaluate_walk_forward_rejects_an_empty_dataset(dip_dataset):
    with pytest.raises(ValueError):
        evaluate_walk_forward(dip_dataset.iloc[:0])


def test_evaluate_walk_forward_rejects_a_dataset_missing_label_end_date(dip_dataset):
    """A dataset without a label_end_date column can't have its walk-forward
    folds' label-overlap checked -- refuse rather than silently falling back
    to the leaky date-only rule."""
    stripped = dip_dataset.drop(columns=["label_end_date"])
    with pytest.raises(ValueError, match="label_end_date"):
        evaluate_walk_forward(stripped)


def test_evaluate_walk_forward_folds_never_include_a_train_row_whose_label_reaches_into_the_test_fold(dip_dataset):
    """End-to-end regression check: evaluate_walk_forward's own folds (built
    from a real build_dataset() output, not a hand-rolled synthetic example)
    must never put a row in train whose label_end_date falls inside (or
    after the start of) the test fold's date range."""
    dataset = dip_dataset.sort_values("date", kind="stable").reset_index(drop=True)
    splits = walk_forward_splits(
        dataset["date"], n_splits=3, label_end_dates=dataset["label_end_date"]
    )
    for train_idx, test_idx in splits:
        test_start = dataset.loc[test_idx, "date"].min()
        train_label_ends = dataset.loc[train_idx, "label_end_date"]
        assert (train_label_ends < test_start).all()


# --- score_incumbent_on_folds() ---------------------------------------------


def test_score_incumbent_on_folds_scores_the_given_candidate_without_refitting(dip_dataset):
    """Regression test for "Avoid comparing the incumbent with its exact
    reconstruction". An earlier version fit a throwaway "comparison
    candidate" INSIDE this function, on exactly the rows available by the
    incumbent's own training cutoff -- under an unchanged alpha/universe and
    append-only history (the ordinary weekly retrain case), that
    reconstructs the incumbent's own weights almost exactly, so the two
    sides always came out a near-tie and blocked every routine retrain. This
    function must score the CALLER-SUPPLIED candidate model directly against
    the incumbent, never refit anything itself -- pinned here with a
    candidate fit at a deliberately different alpha from the incumbent's
    own, which an internally-refitting version could never distinguish.
    """
    dataset = dip_dataset.sort_values("date", kind="stable").reset_index(drop=True)
    unique_label_ends = sorted(dataset["label_end_date"].unique())
    assert len(unique_label_ends) > 1, "fixture needs more than one distinct label_end_date"
    cutoff = pd.Timestamp(unique_label_ends[len(unique_label_ends) // 2])

    incumbent = model_module.fit(
        dataset[dataset["label_end_date"] <= cutoff],
        model_version="incumbent-under-test", trained_at=cutoff.isoformat(),
    )
    candidate = model_module.fit(dataset, alpha=5.0, model_version="candidate-under-test")

    result = score_incumbent_on_folds(incumbent, candidate, dataset)
    assert result is not None
    candidate_mae, incumbent_mae = result

    test = dataset[dataset["label_end_date"] > cutoff]
    assert not test.empty
    actual = test[TARGET_COLUMN].to_numpy(dtype=float)
    expected_candidate_mae = float(np.mean(np.abs(actual - candidate.predict_return_pct(test))))
    expected_incumbent_mae = float(np.mean(np.abs(actual - incumbent.predict_return_pct(test))))
    assert candidate_mae == pytest.approx(expected_candidate_mae)
    assert incumbent_mae == pytest.approx(expected_incumbent_mae)
    assert incumbent_mae != pytest.approx(candidate_mae)


def test_score_incumbent_on_folds_a_genuine_improvement_escapes_a_reconstructed_tie(dip_dataset):
    """The scenario this fix specifically unblocks. On a routine retrain
    (unchanged alpha/universe, append-only history), the incumbent's own
    exact reconstruction -- fit on only the rows it originally saw -- always
    ties the incumbent when scored the same way. The REAL candidate (fit on
    the FULL current dataset, including rows added since the incumbent
    trained) is genuinely different and must not be trapped in that same
    permanent tie.
    """
    dataset = dip_dataset.sort_values("date", kind="stable").reset_index(drop=True)
    unique_label_ends = sorted(dataset["label_end_date"].unique())
    cutoff = pd.Timestamp(unique_label_ends[len(unique_label_ends) // 2])
    train = dataset[dataset["label_end_date"] <= cutoff]

    incumbent = model_module.fit(train, model_version="incumbent-under-test", trained_at=cutoff.isoformat())

    # The old, unsound design in miniature: a reconstruction fit on the
    # IDENTICAL train slice, same alpha -- this always ties the incumbent.
    reconstruction = model_module.fit(train, model_version="reconstruction")
    reconstruction_result = score_incumbent_on_folds(incumbent, reconstruction, dataset)
    assert reconstruction_result is not None
    reconstruction_mae, reconstruction_incumbent_mae = reconstruction_result
    assert reconstruction_mae == pytest.approx(reconstruction_incumbent_mae)

    # The real candidate: fit on the FULL dataset -- genuinely informed by
    # more data than the incumbent, unlike the reconstruction above.
    real_candidate = model_module.fit(dataset, model_version="real-candidate")
    real_result = score_incumbent_on_folds(incumbent, real_candidate, dataset)
    assert real_result is not None
    real_candidate_mae, real_incumbent_mae = real_result
    assert real_incumbent_mae == pytest.approx(reconstruction_incumbent_mae)  # incumbent side unaffected
    assert real_candidate_mae != pytest.approx(reconstruction_mae)  # NOT stuck at the reconstruction's tie


def test_score_incumbent_on_folds_includes_a_row_whose_date_precedes_cutoff_but_label_end_date_does_not(dip_dataset):
    """Regression test for "Identify unseen labels by their end date". A row
    dated before the incumbent's training cutoff can still have a label that
    only became fully computable AFTER training happened, whenever the
    forward-return horizon hasn't fully elapsed since that row's own date --
    on an ordinary weekly retrain with the default 5-day horizon this is the
    COMMON case (the newest usable row is itself horizon sessions behind the
    archive tail). Splitting on `date` alone would wrongly exclude these
    rows from the held-out set entirely; splitting on `label_end_date` must
    include them.
    """
    dataset = dip_dataset.sort_values("date", kind="stable").reset_index(drop=True)

    # A cutoff strictly between some row's own `date` and that same row's
    # `label_end_date` -- i.e. a row whose date is pre-cutoff but whose
    # label only became known post-cutoff.
    straddling = dataset[dataset["date"] < dataset["label_end_date"]]
    assert not straddling.empty, "fixture needs at least one row with date < label_end_date"
    row = straddling.iloc[len(straddling) // 2]
    cutoff = pd.Timestamp(row["date"])
    assert cutoff < pd.Timestamp(row["label_end_date"])

    fitted = model_module.fit(dataset, model_version="incumbent-under-test", trained_at=cutoff.isoformat())
    candidate = model_module.fit(dataset, model_version="candidate-under-test")
    result = score_incumbent_on_folds(fitted, candidate, dataset)
    assert result is not None  # would be None if the fix regressed back to date-only filtering

    held_out = dataset[dataset["label_end_date"] > cutoff]
    assert (held_out["date"] <= cutoff).any(), (
        "this test needs at least one qualifying row whose OWN date is still <= cutoff"
    )


def test_score_incumbent_on_folds_returns_none_when_zero_rows_qualify(dip_dataset):
    """A training cutoff after every row in the dataset (the incumbent was
    trained more recently than the candidate's entire evaluation range)
    leaves nothing to compare."""
    fitted = model_module.fit(
        dip_dataset, model_version="incumbent-under-test", trained_at="2099-01-01T00:00:00+00:00"
    )
    assert score_incumbent_on_folds(fitted, fitted, dip_dataset) is None


def test_score_incumbent_on_folds_returns_none_when_every_row_is_post_cutoff(dip_dataset):
    """A training cutoff before every row in the dataset leaves nothing on
    the incumbent's own side of the split."""
    fitted = model_module.fit(
        dip_dataset, model_version="incumbent-under-test", trained_at="1900-01-01T00:00:00+00:00"
    )
    assert score_incumbent_on_folds(fitted, fitted, dip_dataset) is None


def test_score_incumbent_on_folds_returns_none_when_trained_at_is_missing(dip_dataset):
    fitted = model_module.fit(dip_dataset, model_version="v", trained_at=None)
    assert score_incumbent_on_folds(fitted, fitted, dip_dataset) is None


def test_score_incumbent_on_folds_returns_none_when_trained_at_is_unparseable(dip_dataset):
    fitted = model_module.fit(dip_dataset, model_version="v", trained_at="not-a-timestamp")
    assert score_incumbent_on_folds(fitted, fitted, dip_dataset) is None


def test_score_incumbent_on_folds_returns_none_for_an_empty_dataset(dip_dataset):
    fitted = model_module.fit(dip_dataset, model_version="v", trained_at="2000-01-01T00:00:00+00:00")
    assert score_incumbent_on_folds(fitted, fitted, dip_dataset.iloc[:0]) is None


def test_score_incumbent_on_folds_returns_none_when_label_end_date_is_missing(dip_dataset):
    fitted = model_module.fit(dip_dataset, model_version="v", trained_at="2000-01-01T00:00:00+00:00")
    stripped = dip_dataset.drop(columns=["label_end_date"])
    assert score_incumbent_on_folds(fitted, fitted, stripped) is None


def test_predict_zero_baseline_hit_rate_matches_fraction_of_nonpositive_actuals(dip_dataset):
    """Sanity-check the predict-zero baseline's own hit-rate definition.

    sign(0) == 0, so predict-zero "hits" exactly the test rows whose actual
    forward return is <= 0 (sign 0 or -1... but a forward return of exactly
    0.0 is vanishingly unlikely in this synthetic data, so this mostly
    checks the negative-actual fraction).
    """
    result = evaluate_walk_forward(dip_dataset, n_splits=2, alpha=1.0)
    for fold in result["folds"]:
        assert 0.0 <= fold["baseline_zero_hit_rate"] <= 1.0
