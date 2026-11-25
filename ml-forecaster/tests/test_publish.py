"""Tests for publish.py's fail-closed guarded publish gate.

train.py's CLI maps any status other than "published" to a nonzero process
exit code (see train.main()'s own `return 0 if result["status"] ==
"published" else 1`) -- these tests exercise guarded_publish() itself at the
unit level, which is the return-value-level equivalent of that exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import publish
import pytest

ML_FORECASTER_DIR = Path(__file__).resolve().parent.parent


# The default walk-forward test window _eval_summary() below produces, and
# _PREVIOUS_PAYLOAD's own recorded evaluation_window -- kept as ONE constant
# so every regression-check test below that doesn't care about the window
# specifically (most of them) gets a MATCHING window "for free", the same
# way _PREVIOUS_PAYLOAD's "universe" already matches evaluation_universe=
# ["AAPL"] by default. See test_guarded_publish_ignores_an_evaluation_
# window_mismatch_as_no_floor for the test exercising a genuine mismatch.
_DEFAULT_TEST_WINDOW = ("2026-07-25", "2026-07-31")
# A fold's boundary dates alone are no longer a complete fingerprint (Codex
# review: "Fingerprint observations rather than only fold endpoints") --
# _evaluation_window() also requires each fold's own test_row_fingerprint.
# The exact value doesn't matter for these unit tests (nothing here computes
# a real evaluate._row_fingerprint() hash from actual rows); it only needs
# to be a nonempty string that matches between _eval_summary()'s default
# fold and _DEFAULT_TEST_WINDOW_PERSISTED below, the same "matches for free"
# convention the boundary dates already follow.
_DEFAULT_TEST_ROW_FINGERPRINT = "default-fold-row-fingerprint"
# The persisted shape _evaluation_window()/_parse_persisted_evaluation_window()
# use: a list of [test_start, test_end, test_row_fingerprint] triples, one
# per walk-forward fold -- matching _eval_summary()'s single default fold
# above.
_DEFAULT_TEST_WINDOW_PERSISTED = [[*_DEFAULT_TEST_WINDOW, _DEFAULT_TEST_ROW_FINGERPRINT]]


def _eval_summary(
    model_mae: float,
    *,
    model_rmse: float = 1.0,
    model_hit_rate: float = 0.55,
    baseline_zero_mae: float = 2.0,
    baseline_mean_mae: float = 1.8,
    folds: list | None = None,
) -> dict:
    if folds is None:
        folds = [{
            "test_start": _DEFAULT_TEST_WINDOW[0],
            "test_end": _DEFAULT_TEST_WINDOW[1],
            "test_row_fingerprint": _DEFAULT_TEST_ROW_FINGERPRINT,
        }]
    return {
        "overall": {
            "model_mae": model_mae,
            "model_rmse": model_rmse,
            "model_hit_rate": model_hit_rate,
            "baseline_zero_mae": baseline_zero_mae,
            "baseline_mean_mae": baseline_mean_mae,
        },
        "folds": folds,
    }


_PREVIOUS_PAYLOAD = {
    "status": "active",
    "generated_at": "2026-08-01T00:00:00+00:00",
    "model_version": "2026-08-01-ridge-v1",
    "max_staleness_hours": 240,
    # Matches publish.DEFAULT_HORIZON_TRADING_DAYS -- guarded_publish() calls
    # in this file below that don't pass horizon_trading_days explicitly use
    # that same default, so the regression-check tests below (which rely on
    # the previous model's recorded horizon matching the new run's) stay
    # correct after the horizon-aware comparison fix (see
    # test_guarded_publish_does_not_compare_mae_across_different_horizons for
    # the test that exercises a genuine mismatch).
    "horizon_trading_days": 5,
    "bounds": {"min_multiplier": 0.8, "max_multiplier": 1.2},
    "adjustments": {"AAPL": 1.05},
    # "universe" matches the evaluation_universe=["AAPL"] the regression-gate
    # tests below pass to guarded_publish() -- see test_guarded_publish_
    # ignores_a_universe_mismatch_as_no_floor for the test exercising a
    # genuine mismatch. "evaluation_window" matches _eval_summary()'s own
    # default folds -- see test_guarded_publish_ignores_an_evaluation_
    # window_mismatch_as_no_floor for the test exercising a genuine mismatch.
    "eval_metric": {"mae": 0.5, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
}


def test_guarded_publish_refuses_when_worse_than_currently_published_model(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_text(json.dumps(_PREVIOUS_PAYLOAD), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        # Worse than the previous model's 0.5 MAE, but still comfortably
        # beats both naive baselines below -- isolates the "worse than
        # previous" reason specifically.
        eval_summary=_eval_summary(model_mae=0.9, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],  # matches _PREVIOUS_PAYLOAD's recorded universe
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("currently-published" in reason for reason in result["reasons"])
    assert config_path.read_text(encoding="utf-8") == json.dumps(_PREVIOUS_PAYLOAD)  # byte-for-byte untouched


def test_guarded_publish_refuses_when_worse_than_naive_baseline(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file at all

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=5.0, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("naive" in reason for reason in result["reasons"])
    assert not config_path.exists()


def test_guarded_publish_publishes_when_strictly_better(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    # Previous model's recorded MAE (1.5) is worse than the new model's
    # (0.5) -- a genuine, unambiguous improvement.
    previous = dict(_PREVIOUS_PAYLOAD, eval_metric={"mae": 1.5})
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.1, "MSFT": 0.95},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"
    on_disk = json.loads(config_path.read_bytes())
    assert on_disk["status"] == "active"
    assert on_disk["model_version"] == "2026-08-08-ridge-v1"
    assert on_disk["generated_at"].startswith("2026-08-08")
    assert on_disk["adjustments"] == {"AAPL": 1.1, "MSFT": 0.95}
    assert on_disk["eval_metric"]["mae"] == pytest.approx(0.5)


def test_guarded_publish_treats_a_tie_as_not_an_improvement(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={"mae": 0.5, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"


def test_guarded_publish_first_ever_publish_only_needs_to_beat_the_baseline(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"
    assert config_path.exists()


def test_guarded_publish_ignores_an_inactive_previous_config(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    inactive_payload = {"status": "inactive", "eval_metric": {"mae": 0.0001}}  # impossible to beat if honored
    config_path.write_text(json.dumps(inactive_payload), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


@pytest.mark.parametrize("malformed_payload", ["[]", '"just a string"', "42"])
def test_guarded_publish_treats_a_non_object_previous_file_as_no_floor(tmp_path, malformed_payload):
    """Regression test for "Reject non-object current publish files".

    Syntactically valid JSON of the wrong top-level type (a bare array,
    string, or number) must be treated the same as an absent/corrupted
    file -- blocking_reasons() calls current.get(...), which raises
    AttributeError on anything but a dict, crashing the retrain instead of
    publishing normally with no prior-model floor.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_text(malformed_payload, encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_treats_invalid_utf8_in_the_previous_file_as_no_floor(tmp_path):
    """Regression test for "Handle invalid UTF-8 in the current config"
    (Copilot review). json.loads() given raw bytes decodes them itself and
    raises UnicodeDecodeError for invalid UTF-8 -- neither OSError nor
    json.JSONDecodeError, so it previously escaped _read_current_config()
    uncaught, crashing the whole retrain over one damaged prior publish
    instead of the documented "no valid prior model" fallback.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_bytes(b'{"status": "active", "eval_metric": {"mae": \xff\xfe0.0001}}')

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_read_current_config_rejects_a_duplicate_keyed_file(tmp_path):
    """Regression test for "Reject duplicate JSON keys" (Copilot review).

    json.loads() silently keeps the LAST occurrence of a duplicate object
    key by default, unlike dca_config.load_ml_sizing_adjustment() -- the
    loader the LIVE strategy actually reads this same file through, which
    already rejects duplicate keys via its own _reject_duplicate_keys hook.
    A duplicate-keyed file must be treated the same unreadable way here too
    (None, "no valid prior model"), not silently parsed with whichever
    entry happened to come last, which would let this module and the live
    loader disagree about what the "currently-published" model even
    claims. Tested directly against the private helper -- routing this
    through the full guarded_publish() regression comparison would entangle
    the assertion with horizon/universe/evaluation-window matching, none of
    which is what this fix is actually about.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    # Two "mae" keys inside the same eval_metric object -- plain json.loads()
    # would silently keep only the last (0.0001).
    config_path.write_text(
        '{"status": "active", "eval_metric": {"mae": 0.9, "mae": 0.0001}}',
        encoding="utf-8",
    )

    assert publish._read_current_config(config_path) is None


def test_read_current_config_rejects_invalid_utf8(tmp_path):
    """Regression test for "Handle invalid UTF-8 in the current config"
    (Copilot review), tested directly against the private helper -- see
    test_read_current_config_rejects_a_duplicate_keyed_file's own docstring
    for why the full guarded_publish() path is not used here."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_bytes(b'{"status": "active", "eval_metric": {"mae": \xff\xfe0.0001}}')

    assert publish._read_current_config(config_path) is None


def test_guarded_publish_refuses_an_empty_adjustments_mapping(tmp_path):
    """Regression test for "Refuse active publications with no adjustments".

    An empty adjustments mapping trivially passes every per-key validation
    (both loops in _validate_adjustments() are no-ops over nothing), so it
    previously published a fresh "status": "active" config whose
    "adjustments" is {} -- silently REMOVING every previously-active live
    sizing override instead of refusing, even though train.py's own CLI
    separately refuses this same shape after its own snapshot filtering.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD)
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_adjustments"
    assert config_path.read_text(encoding="utf-8") == json.dumps(previous)  # byte-for-byte untouched


def test_guarded_publish_refuses_out_of_schema_bounds(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        min_multiplier=0.2,  # outside the schema's allowed [0.5, 1.0]
        max_multiplier=1.2,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_bounds"
    assert not config_path.exists()


@pytest.mark.parametrize("bad_bound", ["0.8", None, [0.8], float("nan"), float("inf")])
def test_guarded_publish_refuses_a_non_numeric_multiplier_bound(tmp_path, bad_bound):
    """Regression test for "Validate multiplier bound types before
    comparing". A caller passing a genuinely non-numeric bound (a string,
    None, a list) isn't a bool either, so it previously survived
    _validate_bounds()'s own bool-only check and reached the chained
    numeric comparisons, raising an uncaught TypeError instead of the
    documented blocked_invalid_bounds result -- reachable by any direct
    guarded_publish() caller that doesn't go through train.py's own
    argparse type=float validation.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        min_multiplier=bad_bound,
        max_multiplier=1.2,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_bounds"
    assert not config_path.exists()


def test_guarded_publish_refuses_an_adjustment_outside_declared_bounds(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.4},  # outside this run's own 0.8/1.2 bounds (though inside schema-hard 0.5/1.5)
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_adjustments"
    assert not config_path.exists()


@pytest.mark.parametrize("bad_value", ["not-a-number", None, float("nan"), float("inf")])
def test_guarded_publish_refuses_a_non_finite_or_non_numeric_adjustment(tmp_path, bad_value):
    """Regression test for "Validate adjustment types before comparing
    their bounds". A direct (non-CLI) guarded_publish() caller isn't
    constrained to pass genuine finite floats -- a string or None must not
    raise an uncaught TypeError from the bound comparisons, and NaN/inf
    must not silently compare as "in bounds" via a broken comparison
    either; all must produce the documented blocked_invalid_adjustments
    result instead.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": bad_value},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_adjustments"
    assert not config_path.exists()


def test_guarded_publish_refuses_a_boolean_adjustment(tmp_path):
    """A bool is an int subclass -- True/False must not silently pass the
    numeric range comparisons as exactly 1.0/0.0 and get published,
    replacing any prior live adjustment for that ticker."""
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": True},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_adjustments"
    assert not config_path.exists()


def test_guarded_publish_refuses_case_folded_duplicate_adjustment_keys(tmp_path):
    """Regression test for "Reject case-folded adjustment keys before
    publishing". Distinct input keys that normalize to the same symbol
    (e.g. "AAPL" and "aapl") must not silently collapse into one dict entry
    at the write path, publishing whichever value iteration happens to
    visit last while still reporting success.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": 0.8, "aapl": 1.2},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_adjustments"
    assert not config_path.exists()


def test_guarded_publish_refuses_a_non_string_adjustment_key(tmp_path):
    """Regression test for "Reject non-string adjustment keys". A
    non-string key can't be normalized by ticker.upper() at all -- the
    write path's own unconditional call would otherwise raise an uncaught
    AttributeError instead of producing the documented
    blocked_invalid_adjustments result.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments=cast(Any, {1: 1.1}),
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_adjustments"
    assert not config_path.exists()


def test_guarded_publish_refuses_when_eval_summary_lacks_a_usable_mae(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary={"overall": {}},
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_missing_eval_metric"
    assert not config_path.exists()


def test_published_output_validates_against_the_repo_schema(tmp_path):
    """The generated config must validate against schemas/ml_sizing_adjustment.schema.json.

    Skips (rather than fails) if jsonschema isn't installed -- it's a dev
    dependency of the CORE repo (requirements-dev.txt), not of ml-forecaster
    itself (this subproject's own requirements.txt deliberately stays
    limited to pandas/numpy/scikit-learn/scipy/pytest), matching how
    test_config_invariants.py's own schema tests already importorskip it.
    """
    jsonschema = pytest.importorskip("jsonschema")
    config_path = tmp_path / "ml_sizing_adjustment.json"

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05, "MSFT": 0.92},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    assert result["status"] == "published"

    schema = json.loads((publish.REPO_ROOT / "schemas" / "ml_sizing_adjustment.schema.json").read_bytes())
    document = json.loads(config_path.read_bytes())
    jsonschema.validate(document, schema)


# --- horizon persistence + horizon-aware regression comparison -------------


def test_guarded_publish_persists_the_horizon_used_to_train_the_model(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        horizon_trading_days=1,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"
    on_disk = json.loads(config_path.read_bytes())
    assert on_disk["horizon_trading_days"] == 1


def test_guarded_publish_does_not_compare_mae_across_different_horizons(tmp_path):
    """A --horizon 1 run must not be regression-checked against a --horizon 5
    model's recorded MAE -- the two error scales aren't comparable (see
    blocking_reasons()'s own comment). The previous model's MAE (0.0001) is
    deliberately near-impossible to beat, so if the horizon mismatch were
    ignored this would incorrectly block."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD, horizon_trading_days=5, eval_metric={"mae": 0.0001})
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        horizon_trading_days=1,  # different horizon than the previously-published model's 5
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"
    on_disk = json.loads(config_path.read_bytes())
    assert on_disk["horizon_trading_days"] == 1


def test_guarded_publish_still_enforces_regression_when_horizon_matches(tmp_path):
    """Same setup as the mismatch test above, but with a MATCHING horizon --
    the regression floor must still apply."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        horizon_trading_days=5,
        eval_metric={"mae": 0.0001, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        horizon_trading_days=5,  # matches the previously-published model
        evaluation_universe=["AAPL"],  # matches the previously-published model
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("currently-published" in reason for reason in result["reasons"])
    assert config_path.read_text(encoding="utf-8") == json.dumps(previous)  # untouched


def test_guarded_publish_ignores_a_universe_mismatch_as_no_floor(tmp_path):
    """Regression test for "Compare models on the same evaluation sample".

    Weekly retrains use an expanding archive and can score a changed
    active-ticker universe, so the incumbent's recorded MAE was measured on
    a different walk-forward sample -- comparing directly could let an
    easier evaluation population make a genuinely worse model look
    numerically better. A changed universe (even with a MATCHING horizon)
    must be treated the same as a horizon mismatch: no floor to beat. The
    previous model's MAE (0.0001) is deliberately near-impossible to beat,
    so if the universe mismatch were ignored this would incorrectly block.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD, eval_metric={"mae": 0.0001, "universe": ["AAPL", "MSFT"]})
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],  # a smaller universe than the previous run's ["AAPL", "MSFT"]
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"
    on_disk = json.loads(config_path.read_bytes())
    assert on_disk["eval_metric"]["universe"] == ["AAPL"]


def test_guarded_publish_treats_a_previous_file_with_no_recorded_universe_as_no_floor(tmp_path):
    """An older published file that predates this fix has no recorded
    universe at all -- must be treated the same as a genuine mismatch (no
    floor to beat), not assumed compatible."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD, eval_metric={"mae": 0.0001})  # no "universe" key
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_treats_a_previous_file_with_no_recorded_horizon_as_no_floor(tmp_path):
    """An older published file that predates this fix has no
    horizon_trading_days at all -- must be treated the same as a horizon
    mismatch (no floor to beat), not crash or silently compare anyway."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD, eval_metric={"mae": 0.0001})
    del previous["horizon_trading_days"]
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        horizon_trading_days=5,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_skill_ratio_fallback_blocks_a_regression_when_window_mismatches(tmp_path):
    """Regression test for "Re-score the incumbent when evaluation dates
    advance". Simply skipping the incumbent comparison on a window mismatch
    (an earlier version of this fix) silently neutered it for the normal
    weekly cadence -- the archive advances every retrain, so the raw-MAE
    comparison would essentially never fire again. Here the candidate's raw
    MAE (0.5) looks BETTER than the incumbent's (1.0), but the candidate's
    newly-added evaluation period is much calmer (baselines 0.6/0.55 vs.
    the incumbent's 2.0/1.8) -- relative to its own contemporaneous naive
    baseline, the candidate is actually WORSE (skill ratio ~0.91 vs. the
    incumbent's ~0.56), a genuine regression the skill-ratio fallback must
    still catch even though raw MAE alone would have missed it entirely.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"], "evaluation_window": [["2026-06-01", "2026-06-07"]],
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        # Default _eval_summary() folds cover _DEFAULT_TEST_WINDOW
        # (2026-07-25..2026-07-31) -- a later, calmer window than the
        # previous run's recorded 2026-06-01..2026-06-07.
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=0.6, baseline_mean_mae=0.55),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("skill ratio" in reason for reason in result["reasons"])
    assert config_path.read_text(encoding="utf-8") == json.dumps(previous)  # byte-for-byte untouched


def test_guarded_publish_detects_a_changed_interior_fold_with_an_unchanged_overall_span(tmp_path):
    """Regression test for "Fingerprint the actual evaluation observations".
    The OLD evaluation-window fingerprint recorded only the overall
    (earliest test_start, latest test_end) SPAN across all folds -- so two
    runs whose ENDPOINT dates matched but whose INTERIOR fold dates
    differed (e.g. an archive correction that changed interior dip-day rows
    or per-ticker availability, shifting which trading days a fold actually
    covers, without moving either endpoint) would still look like an
    identical evaluation window and get compared via raw MAE, even though
    the two runs were scored on genuinely different observations.

    Here the previous run's second fold covers 2026-07-25..2026-07-31; the
    candidate's covers 2026-07-20..2026-07-31 instead -- the OVERALL span
    is identical either way (2026-06-01..2026-07-31), but the actual folds
    differ. Must fall through to the skill-ratio fallback (like a genuine
    date-range mismatch), not the exact-MAE comparison -- mirroring
    test_guarded_publish_skill_ratio_fallback_blocks_a_regression_when_
    window_mismatches's own regression-detection setup.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"],
            "evaluation_window": [["2026-06-01", "2026-06-07"], ["2026-07-25", "2026-07-31"]],
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    candidate_folds = [
        {"test_start": "2026-06-01", "test_end": "2026-06-07"},
        {"test_start": "2026-07-20", "test_end": "2026-07-31"},  # differs from the incumbent's 07-25 start
    ]
    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        # Raw MAE (0.5) looks better than the incumbent's (1.0), but the
        # candidate's own contemporaneous baseline is much calmer
        # (0.6/0.55 vs. 2.0/1.8) -- relative skill is actually WORSE, the
        # same regression shape as the sibling span-mismatch test above.
        eval_summary=_eval_summary(
            model_mae=0.5, baseline_zero_mae=0.6, baseline_mean_mae=0.55, folds=candidate_folds
        ),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("skill ratio" in reason for reason in result["reasons"])
    assert config_path.read_text(encoding="utf-8") == json.dumps(previous)  # byte-for-byte untouched


def test_guarded_publish_detects_a_changed_interior_row_with_unchanged_fold_boundaries(tmp_path):
    """Regression test for "Fingerprint observations rather than only fold
    endpoints". Recording every fold's own (test_start, test_end) boundary
    pair (the prior round's fix, tested by
    test_guarded_publish_detects_a_changed_interior_fold_with_an_unchanged_
    overall_span above) is STILL an incomplete fingerprint: an archive
    correction that adds, removes, or changes a row INSIDE a fold's date
    range -- without moving that fold's own test_start/test_end -- leaves
    the boundary pair looking identical across two runs even though the
    actual scored observations differ.

    Here both runs' single fold covers the IDENTICAL 2026-07-25..2026-07-31
    boundary, but the persisted run's test_row_fingerprint differs from the
    candidate's (see _eval_summary()'s default fold's own
    _DEFAULT_TEST_ROW_FINGERPRINT) -- must fall through to the skill-ratio
    fallback, not the exact-MAE comparison, mirroring the sibling
    span/boundary-mismatch tests' own regression-detection setup.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"],
            "evaluation_window": [[*_DEFAULT_TEST_WINDOW, "a-different-row-fingerprint"]],
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        # Raw MAE (0.5) looks better than the incumbent's (1.0), but the
        # candidate's own contemporaneous baseline is much calmer
        # (0.6/0.55 vs. 2.0/1.8) -- relative skill is actually WORSE, the
        # same regression shape as the sibling window-mismatch tests above.
        # Uses _eval_summary()'s DEFAULT fold -- identical test_start/
        # test_end to `previous` above, but a different test_row_fingerprint.
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=0.6, baseline_mean_mae=0.55),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("skill ratio" in reason for reason in result["reasons"])
    assert config_path.read_text(encoding="utf-8") == json.dumps(previous)  # byte-for-byte untouched


def test_guarded_publish_skill_ratio_fallback_allows_a_genuine_improvement_when_window_mismatches(tmp_path):
    """Same setup as the regression case above, but the candidate's skill
    ratio is genuinely better (not just its raw MAE) -- must still publish."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"], "evaluation_window": [["2026-06-01", "2026-06-07"]],
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        # Same regime as the incumbent's own baseline (2.0/1.8) -- a real
        # skill improvement (0.5/1.8 ~ 0.28) versus the incumbent's ~0.56,
        # not merely a calmer period producing a smaller raw MAE.
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_treats_a_previous_file_with_no_recorded_window_as_skill_ratio_fallback(tmp_path):
    """An older published file that predates the evaluation_window field
    entirely has no recorded window at all -- treated the SAME as a
    genuine mismatch (skill-ratio fallback, not a direct MAE comparison),
    not assumed compatible."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={"mae": 1.0, "universe": ["AAPL"], "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8},
    )  # no "evaluation_window" key at all
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=0.6, baseline_mean_mae=0.55),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    # Same regression as the explicit-mismatch test above -- a missing
    # window is exactly as unsafe to compare directly as a different one.
    assert result["status"] == "blocked_regression"
    assert any("skill ratio" in reason for reason in result["reasons"])


def test_guarded_publish_treats_the_old_flat_span_shape_as_unparseable(tmp_path):
    """A published file written by the PRE-fix version of this module
    recorded evaluation_window as a flat two-string span
    (`["2026-06-01", "2026-06-07"]`), not the current list-of-fold-pairs
    shape (`[["2026-06-01", "2026-06-07"]]`). _parse_persisted_evaluation_
    window() must recognize this old shape as unparseable (each element is
    a bare string, not a `[start, end]` pair list) and return None --
    treated the SAME as a missing window, not crash, and not silently
    misinterpreted as a match against a same-valued new-shape window.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"],
            "evaluation_window": ["2026-06-01", "2026-06-07"],  # old flat shape, pre-fix
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=0.6, baseline_mean_mae=0.55),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("skill ratio" in reason for reason in result["reasons"])


def test_guarded_publish_treats_a_previous_file_with_no_recorded_baseline_as_no_floor(tmp_path):
    """When the window mismatches AND the incumbent's own recorded
    baseline MAEs are unavailable (a legacy file, or one hand-edited to
    drop them), the skill ratio can't be computed at all -- unlike a
    genuine mismatch with a usable baseline, this must impose no floor
    rather than block or crash."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={"mae": 0.0001, "universe": ["AAPL"], "evaluation_window": [["2026-06-01", "2026-06-07"]]},
        # no baseline_zero_mae/baseline_mean_mae
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_skill_ratio_fallback_does_not_crash_on_a_zero_previous_baseline(tmp_path):
    """Regression test for "Reject zero baselines before computing skill
    ratios". The incumbent's recorded naive baselines are both exactly 0.0
    (finite, so _incumbent_baseline_mae() previously returned 0.0 rather
    than None) -- computing `previous_mae / previous_baseline_mae` in the
    skill-ratio fallback would raise an uncaught ZeroDivisionError, aborting
    the whole guarded_publish() call instead of failing closed. A zero
    baseline is degenerate but not itself corrupted data (finite, so
    _is_finite_number() alone can't catch it), so this must fall back to "no
    incumbent floor" rather than crash -- the candidate here genuinely beats
    its own contemporaneous naive baseline (0.5 < 1.8), so it still
    publishes once the division is skipped instead of raising.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 0.0001, "universe": ["AAPL"], "evaluation_window": [["2026-06-01", "2026-06-07"]],
            "baseline_zero_mae": 0.0, "baseline_mean_mae": 0.0,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_skill_ratio_fallback_does_not_crash_on_a_zero_new_baseline(tmp_path):
    """Same ZeroDivisionError hazard as the previous-baseline case above,
    but on THIS run's own baseline_mae (the skill ratio's denominator on the
    candidate side) instead of the incumbent's. Since a naive baseline MAE
    of exactly 0.0 can never be beaten (a real MAE is never negative), the
    unconditional `new_mae >= baseline_mae` check at the bottom of
    blocking_reasons() correctly blocks publish here -- the point of this
    test is that it blocks via that ordinary comparison rather than crashing
    inside the skipped skill-ratio division.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"], "evaluation_window": [["2026-06-01", "2026-06-07"]],
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=0.0, baseline_mean_mae=0.0),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert not any("skill ratio" in reason for reason in result["reasons"])
    assert any("naive walk-forward baseline" in reason for reason in result["reasons"])
    assert config_path.read_text(encoding="utf-8") == json.dumps(previous)  # byte-for-byte untouched


def test_guarded_publish_treats_missing_or_malformed_candidate_folds_as_no_floor(tmp_path):
    """If THIS run's own eval_summary carries no usable folds (missing,
    empty, or malformed), the candidate's own evaluation window can't be
    determined at all -- must be treated as unsafe to compare, not crash or
    silently skip the window check."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_text(json.dumps(_PREVIOUS_PAYLOAD), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8, folds=[]),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


# --- max_staleness_hours validation -----------------------------------------


def test_guarded_publish_refuses_zero_max_staleness_hours(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_text(json.dumps(_PREVIOUS_PAYLOAD), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        max_staleness_hours=0,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_staleness"
    assert config_path.read_text(encoding="utf-8") == json.dumps(_PREVIOUS_PAYLOAD)  # byte-for-byte untouched


def test_guarded_publish_refuses_negative_max_staleness_hours(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_text(json.dumps(_PREVIOUS_PAYLOAD), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        max_staleness_hours=-5.0,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_staleness"
    assert config_path.read_text(encoding="utf-8") == json.dumps(_PREVIOUS_PAYLOAD)


def test_guarded_publish_refuses_non_finite_max_staleness_hours(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file at all

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        max_staleness_hours=float("nan"),
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_invalid_staleness"
    assert not config_path.exists()


@pytest.mark.parametrize("bad_mae", [float("nan"), float("inf"), float("-inf")])
def test_guarded_publish_refuses_non_finite_model_mae(tmp_path, bad_mae):
    """Regression test for "Reject non-finite evaluation metrics before publishing".

    NaN/inf pass `isinstance(value, (int, float))`, and every ">="/"<="
    regression comparison against a NaN silently evaluates false -- without
    an explicit finiteness check, a broken evaluation would sail through the
    gate rather than being blocked by it.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file at all

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=bad_mae),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_missing_eval_metric"
    assert not config_path.exists()


def test_guarded_publish_refuses_when_both_naive_baselines_are_non_finite(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file at all

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1, baseline_zero_mae=float("nan"), baseline_mean_mae=float("inf")),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("no usable baseline" in reason for reason in result["reasons"])
    assert not config_path.exists()


@pytest.mark.parametrize(
    "baseline_zero_mae,baseline_mean_mae",
    [
        (float("nan"), 1.8),  # only baseline_zero_mae invalid
        (2.0, float("inf")),  # only baseline_mean_mae invalid
    ],
)
def test_guarded_publish_refuses_when_exactly_one_naive_baseline_is_non_finite(
    tmp_path, baseline_zero_mae, baseline_mean_mae
):
    """Regression test for "Require both baseline metrics before publishing".

    Filtering out just the one invalid baseline and proceeding with the
    remaining one (an earlier, incomplete version of this fix) could publish
    a model that beats the surviving baseline but would have FAILED the
    omitted one had it been valid -- the module's own documented contract
    requires beating the BETTER of both, which is only knowable when both
    are actually usable. model_mae=0.1 comfortably beats whichever single
    baseline survives (1.8 or 2.0), proving this blocks specifically because
    ONE baseline is unusable, not because 0.1 is a genuinely bad MAE.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file at all

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(
            model_mae=0.1, baseline_zero_mae=baseline_zero_mae, baseline_mean_mae=baseline_mean_mae
        ),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "blocked_regression"
    assert any("no usable baseline" in reason for reason in result["reasons"])
    assert not config_path.exists()


def test_guarded_publish_ignores_a_non_finite_previous_mae_as_no_floor(tmp_path):
    """A corrupted/tampered previous file carrying a non-finite recorded MAE
    must impose no floor -- comparing a real MAE against NaN would otherwise
    silently pass through blocking_reasons() rather than being treated as
    "no valid prior model to regress against" (Codex review: "Reject
    non-finite evaluation metrics before publishing")."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD, eval_metric={"mae": float("nan")})
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert result["status"] == "published"


def test_guarded_publish_serializes_concurrent_attempts_through_the_config_lock(tmp_path):
    """Regression test for "Make the regression check and publish atomic".

    Holds the same sidecar lock guarded_publish() itself must acquire
    (`<config>.lock`) from a background thread for a short, measurable
    window before releasing it. If guarded_publish() didn't actually wait
    for that lock, the call below would return almost immediately; instead
    it must block for at least the hold duration, proving the read-compare-
    write critical section is genuinely serialized rather than racing a
    concurrent retrain's read of the same currently-published MAE.
    """
    import threading
    import time

    from scripts.file_lock import cross_process_lock

    config_path = tmp_path / "ml_sizing_adjustment.json"
    lock_path = config_path.parent / f"{config_path.name}.lock"
    hold_seconds = 0.3
    holder_acquired = threading.Event()

    def hold_lock():
        with cross_process_lock(lock_path):
            holder_acquired.set()
            time.sleep(hold_seconds)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert holder_acquired.wait(timeout=5.0)

    start = time.monotonic()
    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.1),
        model_version="v",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    elapsed = time.monotonic() - start
    holder.join()

    assert elapsed >= hold_seconds * 0.8  # generous margin against scheduling jitter
    assert result["status"] == "published"


def test_guarded_publish_rechecks_incumbent_after_a_better_concurrent_publish(tmp_path, monkeypatch):
    """A queued, worse candidate must compare against the newly live model.

    Both candidates improve on the original MAE of 2.0, but the 1.5 candidate
    is a regression after the 1.0 candidate publishes.  Pause the first call
    immediately after its incumbent read (while it owns the publish lock), then
    start the second call.  The second call must remain outside the critical
    section until the first atomic replacement completes and must consequently
    reject itself against MAE 1.0 rather than the stale MAE 2.0.

    The second attempt's own lock-acquisition attempt (not merely
    `worse.start()` returning) is the release signal for the first attempt --
    `Thread.start()` only schedules the thread, it doesn't prove the thread has
    reached `guarded_publish`'s critical section yet. Without that second
    synchronization point a regressed `guarded_publish` that reads the
    incumbent outside the lock could still pass this test purely by scheduling
    luck: if the "worse" thread doesn't get CPU time until *after* "better"
    has already finished its whole read-compare-write and released the lock,
    "worse" would read the already-published MAE 1.0 and correctly block even
    under the broken implementation, so the regression would go undetected.

    The lock-attempt signal is tied to the "worse" thread's OWN identity
    (`threading.current_thread().name`), not to a raw ordinal count of every
    `cross_process_lock` call. A count alone silently assumes exactly one
    `cross_process_lock` call per `guarded_publish` attempt; a regression that
    splits the critical section into two separate locked sections (one around
    the read, a second later around the write -- the exact "lock covers only
    the read" shape this test exists to catch) would make "better" itself
    place the 2nd global call (its own write-lock attempt), firing the signal
    early and letting "better" finish before "worse" ever reads. Naming the
    threads and checking identity instead makes the signal fire only on
    "worse"'s own first attempt, however many times "better" acquires the
    lock.
    """
    import threading
    from contextlib import contextmanager

    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        eval_metric={
            **_PREVIOUS_PAYLOAD["eval_metric"],
            "mae": 2.0,
            "baseline_zero_mae": 3.0,
            "baseline_mean_mae": 2.5,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    real_read = publish._read_current_config
    real_lock = publish.cross_process_lock
    first_read = threading.Event()
    release_first = threading.Event()
    worse_attempted_lock = threading.Event()
    better_read_paused = threading.Event()

    def pause_first_read(path):
        current = real_read(path)
        # Only the "better" thread's read is ever paused -- "worse" always
        # reads for real, whenever its own execution reaches this call.
        if threading.current_thread().name == "better" and not better_read_paused.is_set():
            better_read_paused.set()
            first_read.set()
            assert release_first.wait(timeout=5.0)
        return current

    @contextmanager
    def signal_worse_lock_attempt(path):
        if threading.current_thread().name == "worse":
            # "worse" has reached the point of trying to acquire the lock
            # "better" holds (or, under a regressed implementation, is about
            # to run unsynchronized) -- only now is it safe to let "better"
            # proceed and complete its critical section.
            worse_attempted_lock.set()
        with real_lock(path):
            yield

    monkeypatch.setattr(publish, "_read_current_config", pause_first_read)
    monkeypatch.setattr(publish, "cross_process_lock", signal_worse_lock_attempt)
    results = {}

    def attempt(name, mae):
        results[name] = publish.guarded_publish(
            adjustments={"AAPL": 1.05},
            eval_summary=_eval_summary(mae, baseline_zero_mae=3.0, baseline_mean_mae=2.5),
            model_version=name,
            config_path=config_path,
            evaluation_universe=["AAPL"],
            generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )

    better = threading.Thread(target=attempt, args=("better", 1.0), name="better")
    worse = threading.Thread(target=attempt, args=("worse", 1.5), name="worse")
    better.start()
    assert first_read.wait(timeout=5.0)
    worse.start()
    assert worse_attempted_lock.wait(timeout=5.0)
    release_first.set()
    better.join(timeout=5.0)
    worse.join(timeout=5.0)

    assert not better.is_alive()
    assert not worse.is_alive()
    assert results["better"]["status"] == "published"
    assert results["worse"]["status"] == "blocked_regression"
    published = json.loads(config_path.read_bytes())
    assert published["model_version"] == "better"
    assert published["eval_metric"]["mae"] == pytest.approx(1.0)


# --- incumbent re-scoring (score_incumbent_model / model_artifact_path) ----


def test_guarded_publish_persists_the_model_artifact_path(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"  # no prior file

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        model_artifact_path="/artifacts/models/2026-08-08/model.json",
    )

    assert result["status"] == "published"
    on_disk = json.loads(config_path.read_bytes())
    assert on_disk["model_artifact_path"] == "/artifacts/models/2026-08-08/model.json"


def test_guarded_publish_prefers_rescoring_over_a_matching_window(tmp_path):
    """Regression test for "Compare the incumbent on the current evaluation
    folds". When an incumbent artifact is re-scorable, that comparison must
    win even when the recorded evaluation window ALSO happens to match --
    the candidate's raw MAE (0.6) would pass the old exact-window comparison
    against the incumbent's recorded MAE (1.0), but the re-scored pair says
    otherwise (restricted candidate MAE 0.6 >= restricted incumbent MAE
    0.5), so the re-score comparison must block instead.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        model_artifact_path="/artifacts/models/incumbent/model.json",
        eval_metric={"mae": 1.0, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    def _score_incumbent(model_artifact_path: str) -> tuple[float, float]:
        assert model_artifact_path == "/artifacts/models/incumbent/model.json"
        return (0.6, 0.5)  # incumbent's restricted MAE (0.5) beats the candidate's (0.6)

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.6, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        score_incumbent_model=_score_incumbent,
    )

    assert result["status"] == "blocked_regression"
    assert any("re-scored MAE" in reason for reason in result["reasons"])


def test_guarded_publish_rescoring_publishes_a_genuine_improvement(tmp_path):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(_PREVIOUS_PAYLOAD, model_artifact_path="/artifacts/models/incumbent/model.json")
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.3, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        # Restricted candidate MAE (0.3) beats the restricted incumbent MAE (1.0).
        score_incumbent_model=lambda _path: (0.3, 1.0),
    )

    assert result["status"] == "published"


def test_guarded_publish_rescoring_wins_over_the_skill_ratio_fallback_verdict(tmp_path):
    """Same window-mismatch setup as
    test_guarded_publish_skill_ratio_fallback_blocks_a_regression_when_
    window_mismatches (where the skill-ratio fallback alone would block),
    but with a re-scorable incumbent artifact whose restricted pair the
    candidate genuinely beats -- the re-score comparison must take priority
    and publish, not fall through to the skill-ratio verdict.
    """
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        model_artifact_path="/artifacts/models/incumbent/model.json",
        eval_metric={
            "mae": 1.0, "universe": ["AAPL"], "evaluation_window": [["2026-06-01", "2026-06-07"]],
            "baseline_zero_mae": 2.0, "baseline_mean_mae": 1.8,
        },
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=0.6, baseline_mean_mae=0.55),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        # The skill-ratio fallback (see the sibling test) would BLOCK this
        # exact eval_summary/previous pairing -- proving this test's publish
        # outcome comes from the re-score path taking priority, not from a
        # weaker candidate slipping past the fallback. Restricted candidate
        # MAE (0.5) beats the restricted incumbent MAE (0.9).
        score_incumbent_model=lambda _path: (0.5, 0.9),
    )

    assert result["status"] == "published"


def test_guarded_publish_falls_back_when_no_previous_artifact_path_is_recorded(tmp_path):
    """A legacy published file with no model_artifact_path at all (predates
    this feature) must fall back to the existing window/skill-ratio logic
    exactly as before, even when a score_incumbent_model callback IS
    supplied -- there is nothing for it to be called with."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    config_path.write_text(json.dumps(_PREVIOUS_PAYLOAD), encoding="utf-8")  # no model_artifact_path key

    calls = []

    def _score_incumbent(path):
        calls.append(path)
        return (0.01, 0.01)

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.9, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        score_incumbent_model=_score_incumbent,
    )

    assert calls == []  # never invoked -- no artifact path to score
    assert result["status"] == "blocked_regression"
    assert any("currently-published" in reason for reason in result["reasons"])


def test_guarded_publish_does_not_rescore_across_a_horizon_mismatch(tmp_path):
    """Mirrors test_guarded_publish_does_not_compare_mae_across_different_
    horizons -- a horizon mismatch must also suppress the re-score attempt,
    not just the recorded-MAE/skill-ratio comparisons, since the
    incumbent's predictions are calibrated to its OWN horizon."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD, horizon_trading_days=5, model_artifact_path="/artifacts/models/incumbent/model.json"
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    calls = []

    def _score_incumbent(path):
        calls.append(path)
        return (1.0, 0.0001)  # would block if it were ever consulted

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        horizon_trading_days=1,  # different horizon than the previously-published model's 5
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        score_incumbent_model=_score_incumbent,
    )

    assert calls == []
    assert result["status"] == "published"


def test_guarded_publish_fails_closed_when_the_scorer_raises(tmp_path):
    """A caller-supplied score_incumbent_model performs real I/O (loading a
    model.json) on the other side of the callback. Once the shared-holdout
    gate applies, any failure must retain the incumbent rather than silently
    authorizing publication through a weaker comparison."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        model_artifact_path="/artifacts/models/incumbent/model.json",
        eval_metric={"mae": 1.5, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    def _score_incumbent(_path):
        raise OSError("model.json vanished")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        # This would beat the incumbent via the weaker exact-window fallback,
        # proving that the unavailable shared-holdout score is what blocks it.
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        score_incumbent_model=_score_incumbent,
    )

    assert result["status"] == "blocked_regression"
    assert any("shared out-of-sample holdout" in reason for reason in result["reasons"])


def test_guarded_publish_fails_closed_when_the_scorer_returns_none(tmp_path):
    """The scorer's own documented "can't score it" signal (None -- e.g. the
    there are only five newly available sessions for a five-session target)
    must retain the incumbent, the non-exceptional counterpart to the previous
    test."""
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        model_artifact_path="/artifacts/models/incumbent/model.json",
        eval_metric={"mae": 1.5, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        score_incumbent_model=lambda _path: None,
    )

    assert result["status"] == "blocked_regression"
    assert any("shared out-of-sample holdout" in reason for reason in result["reasons"])


@pytest.mark.parametrize(
    "bad_result",
    [
        (float("nan"), 0.5),
        (0.5, float("inf")),
        (0.5,),  # wrong shape -- not a 2-tuple
        0.5,  # not a tuple at all (the old, pre-fix single-float contract)
        "not-a-tuple",
        (True, 0.5),  # bool is technically an int subclass -- must still be rejected
    ],
)
def test_guarded_publish_fails_closed_when_the_scorer_returns_a_malformed_result(tmp_path, bad_result):
    config_path = tmp_path / "ml_sizing_adjustment.json"
    previous = dict(
        _PREVIOUS_PAYLOAD,
        model_artifact_path="/artifacts/models/incumbent/model.json",
        eval_metric={"mae": 1.5, "universe": ["AAPL"], "evaluation_window": _DEFAULT_TEST_WINDOW_PERSISTED},
    )
    config_path.write_text(json.dumps(previous), encoding="utf-8")

    result = publish.guarded_publish(
        adjustments={"AAPL": 1.05},
        eval_summary=_eval_summary(model_mae=0.5, baseline_zero_mae=2.0, baseline_mean_mae=1.8),
        model_version="2026-08-08-ridge-v1",
        config_path=config_path,
        evaluation_universe=["AAPL"],
        generated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        score_incumbent_model=lambda _path: bad_result,
    )

    assert result["status"] == "blocked_regression"
    assert any("shared out-of-sample holdout" in reason for reason in result["reasons"])


def test_default_config_path_is_shadow_under_cron_tracking_root():
    """publish.DEFAULT_CONFIG_PATH must NOT be the live config/ml_sizing_adjustment.json:
    sizing is still being determined by comparing independent suggesters, so
    ml-forecaster's default publish target is its own shadow incumbent file under
    the suggestion-history root, derived from $DCA_CRON_TRACKING_ROOT (never a
    REPO_ROOT-relative or absolute literal), and never anything under
    $DCA_CONFIG_DIR.

    A fresh subprocess is used (not importlib.reload()) so this exercises
    exactly what a real invocation sees: DEFAULT_CONFIG_PATH is a
    module-level constant resolved once at import time, matching
    train.DEFAULT_MODELS_DIR's own dca_paths.stream_root(...) precedent (see
    test_train.py's test_default_models_dir_honors_dca_cron_tracking_root).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as alt_config_dir:
        env = dict(os.environ, DCA_CONFIG_DIR=alt_config_dir, DCA_CRON_TRACKING_ROOT=alt_config_dir)
        result = subprocess.run(
            [sys.executable, "-c", "import publish; print(publish.DEFAULT_CONFIG_PATH)"],
            cwd=str(ML_FORECASTER_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        printed_path = Path(result.stdout.strip())
        assert printed_path == Path(alt_config_dir) / "sizing_suggestions" / "ml_forecaster" / "incumbent.json"
        assert printed_path.name != "ml_sizing_adjustment.json"


def test_guarded_publish_records_published_and_blocked_runs_in_history(tmp_path):
    """Every run past input validation is appended to the shared suggestion history --
    blocked runs included -- and the live config file is never created by default paths."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sizing_suggestion_history import read_history

    history = tmp_path / "history"
    config_path = tmp_path / "incumbent.json"
    good = {"overall": {"model_mae": 0.01, "baseline_zero_mae": 0.02, "baseline_mean_mae": 0.02}}
    bad = {"overall": {"model_mae": 0.05, "baseline_zero_mae": 0.02, "baseline_mean_mae": 0.02}}
    first = publish.guarded_publish(
        adjustments={"aapl": 1.1}, eval_summary=good, model_version="v1", config_path=config_path, history_dir=history
    )
    second = publish.guarded_publish(
        adjustments={"aapl": 1.15}, eval_summary=bad, model_version="v2", config_path=config_path, history_dir=history
    )
    assert (first["status"], second["status"]) == ("published", "blocked_regression")
    rows = read_history("ml_forecaster", root=history)
    assert [(r["model_version"], r["status"]) for r in rows] == [("v1", "published"), ("v2", "blocked_regression")]
    assert rows[1]["adjustments"] == {"AAPL": 1.15}  # the rejected model's suggestion is still on record


def test_guarded_publish_invalid_input_records_nothing(tmp_path):
    history = tmp_path / "history"
    result = publish.guarded_publish(
        adjustments={"AAPL": 9.9},
        eval_summary={"overall": {"model_mae": 0.01}},
        model_version="v1",
        config_path=tmp_path / "incumbent.json",
        history_dir=history,
    )
    assert result["status"] == "blocked_invalid_adjustments"
    assert not history.exists()


def _ic_folds(*ics):
    return [{"model_rank_ic": ic} for ic in ics]


def _blocked_by_baseline(summary):
    reasons = publish.blocking_reasons(
        new_mae=summary["overall"]["model_mae"], eval_summary=summary, current=None, new_horizon_trading_days=5
    )
    return any("naive walk-forward baseline" in r for r in reasons)


def test_rank_ic_pass_path_lets_a_positive_ic_model_past_the_mae_baseline():
    # MAE 3.9 does not beat baseline 1.8, but all five folds order dip days correctly.
    summary = _eval_summary(3.9, folds=_ic_folds(0.03, 0.04, 0.05, 0.02, 0.04))
    assert not _blocked_by_baseline(summary)


@pytest.mark.parametrize(
    "ics",
    [
        (0.05, -0.01, 0.06, 0.05, 0.04),  # one negative fold
        (0.01, 0.01, 0.01, 0.01, 0.01),  # mean below MIN_MEAN_RANK_IC
        (0.05, 0.05),  # too few folds
        (0.05, float("nan"), 0.05, 0.05),  # non-finite fold
    ],
)
def test_rank_ic_pass_path_still_blocks_weak_or_unreliable_ic(ics):
    assert _blocked_by_baseline(_eval_summary(3.9, folds=_ic_folds(*ics)))


def test_no_rank_ic_at_all_keeps_the_old_mae_gate():
    assert _blocked_by_baseline(_eval_summary(3.9))


# --- rank-IC incumbent comparison (2026-11-24) -----------------------------------------------------------------
# A candidate admitted only through the rank-IC pass path is compared with the incumbent on rank IC, not on the
# incumbent's MAE skill ratio -- but only against an incumbent that itself failed the MAE baseline.

_IC_PASSING = (0.03, 0.04, 0.05, 0.02, 0.04)  # mean 0.036, every fold > 0


def _window_ic_folds(*ics):
    return [
        {
            "model_rank_ic": ic,
            "test_start": _DEFAULT_TEST_WINDOW[0],
            "test_end": _DEFAULT_TEST_WINDOW[1],
            "test_row_fingerprint": _DEFAULT_TEST_ROW_FINGERPRINT,
        }
        for ic in ics
    ]


def _incumbent(mae, baseline, **extra):
    return {
        "status": "active",
        "horizon_trading_days": 5,
        "eval_metric": {
            "mae": mae,
            "baseline_zero_mae": baseline,
            "baseline_mean_mae": baseline,
            "universe": ["AAPL"],
            **extra,
        },
    }


def _reasons_vs(current, ics=_IC_PASSING, mae=3.9):
    summary = _eval_summary(mae, folds=_window_ic_folds(*ics))
    return publish.blocking_reasons(
        new_mae=mae,
        eval_summary=summary,
        current=current,
        new_horizon_trading_days=5,
        new_evaluation_universe=["AAPL"],
    )


def test_rank_ic_candidate_replaces_an_incumbent_that_never_beat_the_mae_baseline():
    # Incumbent MAE 3.9 vs baseline 3.7 (skill ratio > 1) and no recorded rank IC: nothing to beat on MAE.
    assert _reasons_vs(_incumbent(3.9, 3.7)) == []


def test_rank_ic_candidate_must_beat_the_incumbents_recorded_rank_ic():
    blocked = _reasons_vs(_incumbent(3.9, 3.7, model_rank_ic=0.05))
    assert any("mean rank IC" in r for r in blocked)
    assert _reasons_vs(_incumbent(3.9, 3.7, model_rank_ic=0.03)) == []


def test_rank_ic_candidate_never_displaces_an_incumbent_that_beat_the_mae_baseline():
    # Incumbent MAE 3.0 beats its baseline 3.7: the full MAE comparison still applies and blocks the candidate.
    assert _reasons_vs(_incumbent(3.0, 3.7)) != []


def test_a_weak_rank_ic_candidate_still_gets_no_incumbent_exemption():
    weak = _reasons_vs(_incumbent(3.9, 3.7), ics=(0.05, -0.01, 0.06, 0.05, 0.04))
    assert any("naive walk-forward baseline" in r for r in weak)


def test_guarded_publish_persists_the_mean_rank_ic_for_the_next_retrain(tmp_path):
    config_path = tmp_path / "incumbent.json"
    result = publish.guarded_publish(
        adjustments={"AAPL": 1.1},
        eval_summary=_eval_summary(3.9, folds=_window_ic_folds(*_IC_PASSING)),
        model_version="2026-11-24-ridge-v1",
        config_path=config_path,
        history_dir=tmp_path / "history",
        generated_at=datetime(2026, 11, 24, tzinfo=timezone.utc),
        evaluation_universe=["AAPL"],
    )
    assert result["status"] == "published", result
    assert json.loads(config_path.read_bytes())["eval_metric"]["model_rank_ic"] == pytest.approx(0.036)
