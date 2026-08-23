"""Tests for model.py -- Ridge fit/predict round-trip + JSON (de)serialization."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from conftest import make_synthetic_ohlcv, write_ticker_csv
from features import TARGET_COLUMN, build_dataset

import model


@pytest.fixture
def dip_dataset(tmp_path):
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=21, n=220, drift=0.0006))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=22, n=220, drift=-0.0003))
    dataset = build_dataset(history_dir, ["AAA", "BBB"])
    return dataset[dataset["dip_day"]].reset_index(drop=True)


def test_fit_predict_round_trip_produces_finite_in_sample_improvement(dip_dataset):
    fitted = model.fit(dip_dataset, alpha=1.0, model_version="test-v1")
    predictions = fitted.predict_return_pct(dip_dataset)

    assert predictions.shape == (len(dip_dataset),)
    assert np.isfinite(predictions).all()

    # In-sample sanity check only (not a claim about out-of-sample skill --
    # that is evaluate.py's job): a regularized fit on this exact data
    # should do at least as well as a constant-mean predictor on those same
    # rows.
    actual = dip_dataset[TARGET_COLUMN].to_numpy()
    model_mae = np.mean(np.abs(actual - predictions))
    mean_mae = np.mean(np.abs(actual - actual.mean()))
    assert model_mae <= mean_mae + 1e-9


def test_fit_rejects_an_empty_dataset(dip_dataset):
    with pytest.raises(ValueError):
        model.fit(dip_dataset.iloc[:0], alpha=1.0, model_version="test-v1")


def test_predict_handles_an_unseen_ticker_without_raising(dip_dataset):
    fitted = model.fit(dip_dataset, alpha=1.0, model_version="test-v1")
    unseen = dip_dataset.iloc[[0]].copy()
    unseen["ticker"] = "ZZZZ_NEVER_TRAINED"

    prediction = fitted.predict_return_pct(unseen)

    assert np.isfinite(prediction).all()


def test_to_dict_from_dict_round_trip_preserves_predictions(dip_dataset):
    fitted = model.fit(dip_dataset, alpha=1.0, model_version="test-v1")
    restored = model.TrainedModel.from_dict(json.loads(json.dumps(fitted.to_dict())))

    original_predictions = fitted.predict_return_pct(dip_dataset)
    restored_predictions = restored.predict_return_pct(dip_dataset)

    np.testing.assert_allclose(original_predictions, restored_predictions)
    assert restored.model_version == fitted.model_version
    assert restored.coefficients == fitted.coefficients
    assert restored.ticker_vocabulary == fitted.ticker_vocabulary


def test_save_load_round_trip_writes_plain_json_not_pickle(tmp_path, dip_dataset):
    fitted = model.fit(dip_dataset, alpha=1.0, model_version="test-v1")
    path = tmp_path / "model.json"

    fitted.save(path)
    loaded = model.TrainedModel.load(path)

    np.testing.assert_allclose(fitted.predict_return_pct(dip_dataset), loaded.predict_return_pct(dip_dataset))

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)  # must be plain, parseable JSON -- not a pickle bytestream
    assert parsed["model_version"] == "test-v1"
    assert "\x80" not in raw  # crude smell test for a pickle protocol-marker byte


def test_rescale_return_to_multiplier_clips_and_hits_the_midpoint_at_zero():
    midpoint = model.rescale_return_to_multiplier(
        0.0, min_multiplier=0.8, max_multiplier=1.2, return_clip_pct=10.0
    )
    high = model.rescale_return_to_multiplier(
        100.0, min_multiplier=0.8, max_multiplier=1.2, return_clip_pct=10.0
    )
    low = model.rescale_return_to_multiplier(
        -100.0, min_multiplier=0.8, max_multiplier=1.2, return_clip_pct=10.0
    )

    assert midpoint == pytest.approx(1.0)
    assert high == pytest.approx(1.2)
    assert low == pytest.approx(0.8)


def test_rescale_return_to_multiplier_never_exceeds_bounds():
    for predicted in (-500.0, -10.0, -1.0, 0.0, 1.0, 10.0, 500.0):
        multiplier = model.rescale_return_to_multiplier(
            predicted, min_multiplier=0.8, max_multiplier=1.2, return_clip_pct=10.0
        )
        assert 0.8 <= multiplier <= 1.2


def test_rescale_return_to_multiplier_rejects_nonpositive_clip():
    with pytest.raises(ValueError):
        model.rescale_return_to_multiplier(0.0, return_clip_pct=0.0)


@pytest.mark.parametrize("bad_prediction", [float("nan"), float("inf"), float("-inf")])
def test_rescale_return_to_multiplier_rejects_a_non_finite_prediction(bad_prediction):
    """Regression test for "Reject non-finite forecasts before clipping".
    Without this, the min/max clipping below silently maps a NaN prediction
    to max_multiplier and +-inf to max_multiplier/min_multiplier -- output
    indistinguishable from a genuine, schema-valid prediction, which would
    pass guarded_publish()'s bounds check unnoticed."""
    with pytest.raises(ValueError):
        model.rescale_return_to_multiplier(bad_prediction, min_multiplier=0.8, max_multiplier=1.2, return_clip_pct=10.0)


def test_predict_multipliers_skips_a_ticker_with_a_non_finite_prediction(dip_dataset):
    """A non-finite prediction (e.g. an overflowing design-matrix
    computation) must be excluded from the returned mapping entirely, not
    silently rescaled into an apparently-valid bounded multiplier."""
    fitted = model.fit(dip_dataset, alpha=1.0, model_version="test-v1")
    # intercept=NaN forces predict_return_pct to NaN for every row
    # regardless of the design matrix (NaN + anything is NaN) -- a simple,
    # deterministic stand-in for a real overflowing computation.
    broken = dataclasses.replace(fitted, intercept=float("nan"))

    multipliers = broken.predict_multipliers(dip_dataset.iloc[[0]])

    assert multipliers == {}
