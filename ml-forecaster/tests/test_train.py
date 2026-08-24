"""Tests for train.py's CLI wiring -- especially the prediction-frame fix.

Most of train.py's actual feature-engineering logic lives in features.py and
is covered there (test_features.py); these tests exercise train.main() end
to end to pin the WIRING itself -- i.e. that main() actually calls
build_feature_frame() for the row it scores, not just that
build_feature_frame() itself behaves correctly in isolation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_synthetic_ohlcv, write_ticker_csv

import model as model_module
import train as train_module

ML_FORECASTER_DIR = Path(__file__).resolve().parent.parent


def test_main_scores_adjustments_from_build_feature_frames_latest_row_not_the_stale_dataset_tail(tmp_path):
    """Regression test for "scores the target-trimmed tail instead of current features".

    Runs train.main() --dry-run, then independently reconstructs both
    candidate "current snapshot" rows a caller could have used -- the
    target-trimmed dataset's tail (the pre-fix, buggy row) and
    build_feature_frame()'s tail (the fixed row) -- and asserts the
    published/dry-run adjustment matches scoring the LATTER, not the former.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    models_dir = tmp_path / "models"
    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(models_dir),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
            # This test is about WHICH row gets scored, not about staleness
            # filtering (covered separately) -- the synthetic archive's last
            # row predates --now by design, so raise the budget out of the
            # way rather than let the staleness guard drop AAA entirely.
            "--max-staleness-hours", "87600",  # ~10 years; keeps this test's old, unrelated archive in scope
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(models_dir, "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    fitted = model_module.TrainedModel.load(run_dir / "model.json")

    feature_frame = train_module.build_feature_frame(history_dir, ["AAA"])
    latest_row = feature_frame.sort_values("date", kind="stable").groupby("ticker", as_index=False).tail(1)
    expected_multiplier = fitted.predict_multipliers(latest_row)["AAA"]
    assert report["adjustments"]["AAA"] == pytest.approx(expected_multiplier)

    # The row build_dataset()'s target-trimmed tail would have scored is
    # STRICTLY OLDER (by construction -- see
    # test_build_feature_frame_retains_the_unlabeled_tail_that_build_dataset_drops
    # in test_features.py) and, for this seeded synthetic series, scores to
    # a genuinely different multiplier -- pinning that main() is not
    # accidentally scoring that stale row instead.
    dataset = train_module.build_dataset(history_dir, ["AAA"], horizon=5)
    stale_row = dataset.sort_values("date", kind="stable").groupby("ticker", as_index=False).tail(1)
    assert stale_row["date"].iloc[0] < latest_row["date"].iloc[0]
    stale_multiplier = fitted.predict_multipliers(stale_row)["AAA"]
    assert report["adjustments"]["AAA"] != pytest.approx(stale_multiplier)


def test_main_excludes_archive_rows_after_now_for_a_historical_replay(tmp_path):
    """Regression test for "Exclude observations after the effective run
    time". --now is documented to support being earlier than the archive
    tail, for reproducible/test replay runs -- when it is, the published/
    dry-run adjustment must be scored off the row AT/BEFORE --now, never off
    the archive's true (future-relative-to-the-replay) tail, and training/
    evaluation must not see those future rows either.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    frame = make_synthetic_ohlcv(seed=41, n=200, start="2024-01-02", drift=0.0004)
    write_ticker_csv(history_dir, "AAA", frame)

    # A replay timestamp INSIDE the archive, well short of its true tail.
    replay_now = frame["Date"].iloc[150]

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", replay_now.isoformat(),
            "--max-staleness-hours", "87600",  # keep the (unrelated) staleness guard out of the way
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", replay_now.date().isoformat())
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    fitted = model_module.TrainedModel.load(run_dir / "model.json")

    # What SHOULD be scored: the row at/before replay_now, from a properly
    # truncated feature frame.
    truncated_frame = train_module.build_feature_frame(history_dir, ["AAA"], as_of=replay_now)
    expected_row = truncated_frame.sort_values("date", kind="stable").groupby("ticker", as_index=False).tail(1)
    assert expected_row["date"].iloc[0] <= replay_now
    expected_multiplier = fitted.predict_multipliers(expected_row)["AAA"]
    assert report["adjustments"]["AAA"] == pytest.approx(expected_multiplier)

    # The archive genuinely extends past replay_now, and scoring its true
    # (untruncated) tail gives a DIFFERENT multiplier -- proving the
    # truncation actually mattered, not merely that it happened to be a
    # no-op for this seed.
    full_frame = train_module.build_feature_frame(history_dir, ["AAA"])
    full_latest_row = full_frame.sort_values("date", kind="stable").groupby("ticker", as_index=False).tail(1)
    assert full_latest_row["date"].iloc[0] > replay_now
    future_multiplier = fitted.predict_multipliers(full_latest_row)["AAA"]
    assert report["adjustments"]["AAA"] != pytest.approx(future_multiplier)


def test_default_models_dir_honors_dca_cron_tracking_root(tmp_path):
    """Regression test for "Route model artifacts through the shared
    artifact root". DEFAULT_MODELS_DIR must be DERIVED from
    dca_paths.stream_root("ml_forecaster") (honouring $DCA_CRON_TRACKING_ROOT,
    the same convention every other scheduled stream uses), not hardcoded to
    a directory inside ml-forecaster/ itself -- which ignores the override
    entirely and can be read-only under an installed wheel.

    A fresh subprocess is used (not importlib.reload()) so this exercises
    exactly what a real invocation sees: DEFAULT_MODELS_DIR is a module-level
    constant resolved once at import time, matching every other stream's own
    dca_paths.stream_root(...) constants (see dca_paths.py's own docstring
    on why that resolution timing is correct here).
    """
    cron_root = tmp_path / "cron_tracking"
    env = dict(os.environ, DCA_CRON_TRACKING_ROOT=str(cron_root))
    result = subprocess.run(
        [sys.executable, "-c", "import train; print(train.DEFAULT_MODELS_DIR)"],
        cwd=str(ML_FORECASTER_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    printed_path = Path(result.stdout.strip())
    assert printed_path == cron_root / "ml_forecaster" / "models"


def test_default_tickers_config_path_honors_dca_config_dir(tmp_path):
    """Regression test for "Honor the active config directory in forecaster
    defaults". train.DEFAULT_TICKERS_CONFIG_PATH must be DERIVED from
    dca_config.get_config_dir() (honouring $DCA_CONFIG_DIR), not hardcoded
    to a REPO_ROOT-relative literal -- otherwise a default (no --tickers,
    no --tickers-config) run against a non-default config directory would
    silently read a DIFFERENT tickers.json than the live strategy's own
    load_strategy_config() does, training/publishing an adjustment for a
    universe the live strategy never actually trades. See
    test_default_models_dir_honors_dca_cron_tracking_root just above for the
    identical fresh-subprocess pattern and why it's needed here too (a
    module-level constant resolved once at import time).
    """
    alt_config_dir = tmp_path / "alt_config"
    alt_config_dir.mkdir()
    env = dict(os.environ, DCA_CONFIG_DIR=str(alt_config_dir))
    result = subprocess.run(
        [sys.executable, "-c", "import train; print(train.DEFAULT_TICKERS_CONFIG_PATH)"],
        cwd=str(ML_FORECASTER_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    printed_path = Path(result.stdout.strip())
    assert printed_path == alt_config_dir / "tickers.json"


def _find_run_dir(models_dir, date_str):
    """The single run directory train.main() created for ``date_str``.

    run_dir is now ``<date>-<8-char-run-id>``, not a bare date, so a test
    with a fixed --now can't hardcode the exact path (Codex review:
    "Preserve same-day model artifacts across retraining attempts" --
    run_id exists precisely so two same-date runs never collide).
    """
    matches = sorted(models_dir.glob(f"{date_str}-*"))
    assert len(matches) == 1, f"expected exactly one run dir for {date_str}, found {matches}"
    return matches[0]


def _write_tickers_config(path, active):
    path.write_text(json.dumps({"active": active, "excluded": []}), encoding="utf-8")


def test_main_defaults_to_the_active_ticker_config_not_the_full_archive(tmp_path, capsys):
    """Regression test for "trains on every archived CSV instead of the active universe".

    The synthetic archive has FOUR tickers; the synthetic config/tickers.json-
    shaped file lists only two of them as active. With no --tickers and no
    --all-tickers-in-archive, main() must restrict training to exactly the
    active two, silently dropping the other two archived-but-inactive CSVs --
    even though they have perfectly good history and would otherwise be
    picked up by a bare glob.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=42, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "CCC", make_synthetic_ohlcv(seed=43, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "DDD", make_synthetic_ohlcv(seed=44, n=260, drift=0.0004))

    tickers_config = tmp_path / "tickers.json"
    # Deliberately unsorted and includes a symbol ("ZZZ") absent from the
    # archive entirely, to also pin the "log/skip, don't crash" contract for
    # a config entry that has no CSV yet.
    _write_tickers_config(tickers_config, ["BBB", "AAA", "ZZZ"])

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers-config", str(tickers_config),
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert set(report["tickers"]) == {"AAA", "BBB"}

    stderr = capsys.readouterr().err
    assert "ZZZ" in stderr


def test_load_active_tickers_excludes_a_ticker_present_in_both_lists(tmp_path):
    """Regression test for "Exclude non-tradable tickers from the default
    training universe". config/tickers.json's own schema does not forbid a
    ticker appearing in both "active" and "excluded" at once (a stale entry
    left behind after being excluded, or vice versa) -- _load_active_tickers()
    must subtract "excluded" from "active", mirroring near_close_dca.py's own
    `tradable_tickers = [t for t in config.tickers if t not in
    config.excluded_tickers]` computation for the live strategy, so a ticker
    the live strategy never actually trades can't still influence the pooled
    Ridge fit or receive a published sizing adjustment.
    """
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(
        json.dumps({"active": ["AAA", "BBB", "CCC"], "excluded": ["BBB"]}), encoding="utf-8"
    )

    assert train_module._load_active_tickers(tickers_config) == ["AAA", "CCC"]


def test_load_active_tickers_defaults_excluded_to_empty_when_absent(tmp_path):
    """"excluded" is optional per schemas/tickers.schema.json (only "active"
    is required) -- its absence must not raise, and must behave exactly like
    an empty list."""
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(json.dumps({"active": ["AAA", "BBB"]}), encoding="utf-8")  # no "excluded" key

    assert train_module._load_active_tickers(tickers_config) == ["AAA", "BBB"]


@pytest.mark.parametrize(
    "excluded_payload",
    [
        [{}],  # unhashable dict member -- set(excluded) would raise TypeError, not ValueError
        [[]],  # unhashable list member -- same hazard
        [1],  # not a string at all
        ["aapl"],  # a string, but fails the symbol pattern
    ],
)
def test_load_active_tickers_raises_value_error_not_type_error_on_a_malformed_excluded_member(
    tmp_path, excluded_payload
):
    """Regression test for "Validate excluded ticker members before
    constructing the set". A schema-invalid but JSON-valid "excluded" member
    (an unhashable dict/list, in particular) previously reached
    `set(excluded)` unvalidated and raised an uncaught TypeError -- not one
    of main()'s caught exception types (OSError/ValueError/KeyError) --
    crashing a scheduled retrain with a traceback instead of the documented
    REFUSED result. Every "excluded" member must now be validated the same
    way "active" members already are, before the set is ever constructed.
    """
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(
        json.dumps({"active": ["AAPL"], "excluded": excluded_payload}), encoding="utf-8"
    )

    with pytest.raises(ValueError):
        train_module._load_active_tickers(tickers_config)


def test_load_active_tickers_includes_whole_share_tickers(tmp_path):
    """Codex review ("Train and publish ML sizing for whole-share
    symbols"): a whole-share ticker (e.g. BYDDY, not fractional-tradable)
    lives in its own "whole_share" category, not "active" -- omitting it
    here silently starved that symbol of a published sizing adjustment.
    _load_active_tickers() must include it, still honoring "excluded" and
    avoiding a duplicate for a symbol already present in "active".
    """
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(
        json.dumps(
            {
                "active": ["AAA", "BBB"],
                "excluded": ["CCC"],
                "whole_share": ["DDD", "BBB", "CCC"],
            }
        ),
        encoding="utf-8",
    )

    assert train_module._load_active_tickers(tickers_config) == ["AAA", "BBB", "DDD"]


def test_load_active_tickers_defaults_whole_share_to_empty_when_absent(tmp_path):
    """"whole_share" is optional -- its absence must not raise, and must
    behave exactly like an empty list."""
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(json.dumps({"active": ["AAA", "BBB"]}), encoding="utf-8")  # no "whole_share" key

    assert train_module._load_active_tickers(tickers_config) == ["AAA", "BBB"]


def test_load_active_tickers_raises_value_error_on_a_malformed_whole_share_member(tmp_path):
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(
        json.dumps({"active": ["AAPL"], "whole_share": ["byddy"]}), encoding="utf-8"
    )

    with pytest.raises(ValueError):
        train_module._load_active_tickers(tickers_config)


def test_main_excludes_a_ticker_listed_in_both_active_and_excluded(tmp_path):
    """End-to-end counterpart to test_load_active_tickers_excludes_a_ticker_
    present_in_both_lists: a default (no --tickers, no
    --all-tickers-in-archive) run must not train on, evaluate, or publish an
    adjustment for a ticker present in both "active" and "excluded", even
    though it has a perfectly good archived history."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=42, n=260, drift=0.0004))

    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(
        json.dumps({"active": ["AAA", "BBB"], "excluded": ["BBB"]}), encoding="utf-8"
    )

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers-config", str(tickers_config),
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert set(report["tickers"]) == {"AAA"}


@pytest.mark.parametrize(
    "raw_payload",
    [
        "[]",  # valid JSON, wrong top-level type (list, not object)
        '"AAPL"',  # valid JSON, a bare string
        "42",  # valid JSON, a bare number
        '{"active": "AAPL"}',  # object, but "active" is a string, not a list
        '{"active": 42}',  # object, but "active" is a number
        '{"excluded": []}',  # object, but no "active" key at all
        '{"active": [1]}',  # well-formed list, but a member is not a string at all
        '{"active": ["aapl"]}',  # a string member, but lowercase -- fails the symbol pattern
        '{"active": [""]}',  # a string member, but empty -- fails the symbol pattern
        '{"active": ["TOO-LONG"]}',  # a string member, but too long / has a hyphen
        '{"active": ["AAPL"], "excluded": "AAPL"}',  # "excluded" present but not a list
        '{"active": ["AAPL"], "excluded": [{}]}',  # excluded member is an unhashable dict
        '{"active": ["AAPL"], "excluded": [[]]}',  # excluded member is an unhashable list
        '{"active": ["AAPL"], "excluded": [1]}',  # excluded member is not a string at all
        '{"active": ["AAPL"], "excluded": ["aapl"]}',  # excluded member fails the symbol pattern
    ],
)
def test_main_refuses_a_malformed_tickers_config_instead_of_crashing(tmp_path, capsys, raw_payload):
    """Regression test for "Reject non-object ticker-universe configs" and
    "Validate every configured active ticker".

    A syntactically valid JSON file with the wrong top-level shape (a bare
    list/string/number) previously reached `payload["active"]`, which raises
    TypeError -- not one of main()'s caught exception types
    (OSError/ValueError/KeyError) -- crashing a scheduled retrain with a
    traceback instead of the documented REFUSED result. A non-list "active"
    value (e.g. a bare string) is a separate, quieter failure mode:
    list("AAPL") would previously succeed by iterating characters
    ('A','A','P','L') instead of raising at all. A well-formed list
    containing an invalid member (a non-string, or a string that doesn't
    match a real ticker symbol) is a THIRD failure mode: it used to pass
    this function unchanged and only crashed later in main(), when
    `', '.join(missing)` hit a non-string element from the "missing" set
    difference.
    """
    tickers_config = tmp_path / "tickers.json"
    tickers_config.write_text(raw_payload, encoding="utf-8")
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers-config", str(tickers_config),
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert not (tmp_path / "models").exists()


@pytest.mark.parametrize("bad_now", ["not-a-date", "2024-13-45", "", "yesterday"])
def test_main_refuses_a_malformed_now_instead_of_crashing(tmp_path, capsys, bad_now):
    """Regression test for "Validate --now before starting the retrain".
    datetime.fromisoformat() previously raised an uncaught ValueError for a
    malformed --now, outside any refusal boundary -- an operator or
    scheduled wrapper supplying one crashed the whole retrain with a
    traceback instead of the documented REFUSED result and evaluation
    report. history_dir is deliberately left empty of any CSV to also prove
    the CLI refuses BEFORE ever reaching build_dataset()/any archive read.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", bad_now,
        ]
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert not (tmp_path / "models").exists()


def test_main_refuses_a_malformed_archive_csv_instead_of_crashing(tmp_path, capsys):
    """Regression test for "Refuse malformed archive files cleanly".
    load_history_csv() (reached inside build_dataset(), per discovered
    ticker) raises ValueError for a file-level archive problem -- here, a
    CSV missing a required OHLCV column entirely -- and this build_dataset()
    call previously sat outside every refusal boundary, unlike the
    walk-forward evaluation call just below it (already wrapped in an
    identical try/except). A single malformed archived CSV crashed the
    whole retrain with a traceback before it ever created a run directory
    or evaluation report, instead of the documented REFUSED result.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    # No "Close" column at all -- load_history_csv() requires it.
    (history_dir / "AAA.csv").write_text("Date,Open,High,Low\n2024-01-02,10.0,11.0,9.0\n", encoding="utf-8")

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert not (tmp_path / "models").exists()


def test_main_all_tickers_in_archive_flag_restores_full_archive_discovery(tmp_path):
    """--all-tickers-in-archive is the explicit opt-in back to the pre-fix behavior."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=42, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "CCC", make_synthetic_ohlcv(seed=43, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "DDD", make_synthetic_ohlcv(seed=44, n=260, drift=0.0004))

    tickers_config = tmp_path / "tickers.json"
    _write_tickers_config(tickers_config, ["AAA"])

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers-config", str(tickers_config),
            "--all-tickers-in-archive",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert set(report["tickers"]) == {"AAA", "BBB", "CCC", "DDD"}


def test_main_dry_run_never_touches_the_output_config(tmp_path):
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=42, n=260, drift=0.0004))
    output_path = tmp_path / "ml_sizing_adjustment.json"

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--output", str(output_path),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )

    assert exit_code == 0
    assert not output_path.exists()


def test_two_same_date_runs_do_not_overwrite_each_others_artifacts(tmp_path):
    """Two full train.py invocations sharing the same UTC date (a retry, a
    later manual re-run, or a second --dry-run) must land in two DIFFERENT
    run directories, so neither overwrites the other's model.json/
    eval_report.json -- including a run that was successfully published
    earlier that same day (Codex review: "Preserve same-day model artifacts
    across retraining attempts")."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))
    models_dir = tmp_path / "models"
    same_now = "2024-06-01T00:00:00+00:00"

    exit_code_1 = train_module.main(
        ["--history-dir", str(history_dir), "--tickers", "AAA",
         "--models-dir", str(models_dir), "--dry-run", "--now", same_now]
    )
    assert exit_code_1 == 0
    run_dir_1 = _find_run_dir(models_dir, "2024-06-01")
    report_1 = json.loads((run_dir_1 / "eval_report.json").read_text(encoding="utf-8"))
    model_json_1_before = (run_dir_1 / "model.json").read_text(encoding="utf-8")

    exit_code_2 = train_module.main(
        ["--history-dir", str(history_dir), "--tickers", "AAA",
         "--models-dir", str(models_dir), "--dry-run", "--now", same_now]
    )
    assert exit_code_2 == 0

    # Two distinct run directories now exist for the same date.
    run_dirs = sorted(models_dir.glob("2024-06-01-*"))
    assert len(run_dirs) == 2
    run_dir_2 = next(d for d in run_dirs if d != run_dir_1)

    # The FIRST run's artifacts are untouched by the second run.
    assert (run_dir_1 / "model.json").read_text(encoding="utf-8") == model_json_1_before
    assert json.loads((run_dir_1 / "eval_report.json").read_text(encoding="utf-8")) == report_1
    # Each run's model_version is distinct, tying it to its own run directory.
    report_2 = json.loads((run_dir_2 / "eval_report.json").read_text(encoding="utf-8"))
    assert report_1["model_version"] != report_2["model_version"]


def test_main_excludes_a_stale_ticker_snapshot_from_adjustments(tmp_path, capsys):
    """Regression test for "Reject stale feature snapshots before publishing".

    FRESH's archive ends 8 days before --now (inside the default 240h/10-day
    staleness budget); STALE's archive ends years earlier. Only FRESH may be
    scored -- publishing an adjustment off STALE's ancient snapshot would let
    a stalled archive refresh silently keep nudging real order sizing for up
    to 240 more hours, since guarded_publish() would stamp a fresh
    generated_at over it and the live loader only checks that timestamp, not
    the feature date underneath it.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(
        history_dir, "FRESH",
        make_synthetic_ohlcv(seed=41, n=40, start="2024-04-01", drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "STALE",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "FRESH", "STALE",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert "FRESH" in report["adjustments"]
    assert "STALE" not in report["adjustments"]

    stderr = capsys.readouterr().err
    assert "STALE" in stderr
    assert "stale" in stderr.lower()


def test_main_dry_run_refuses_when_every_snapshot_is_stale(tmp_path, capsys):
    """Regression test for "Refuse publishing when every feature snapshot is
    stale". When EVERY ticker's latest row is excluded by the staleness
    filter, adjustments ends up empty -- this must be reported as an
    explicit refusal, not silently "evaluated" with an empty mapping.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(
        history_dir, "STALE",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "STALE",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0  # --dry-run itself never fails

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked_no_fresh_snapshot"
    assert report["adjustments"] == {}

    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr


def test_main_live_refuses_and_does_not_publish_when_every_snapshot_is_stale(tmp_path):
    """The live (non-dry-run) path must refuse outright -- never call
    guarded_publish() with an empty adjustments mapping, which would
    otherwise happily publish a fresh, active config with no per-ticker
    overrides, silently reverting every live sizing adjustment."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(
        history_dir, "STALE",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )
    output_path = tmp_path / "ml_sizing_adjustment.json"
    previous_payload = '{"status": "active", "adjustments": {"STALE": 1.1}}'
    output_path.write_text(previous_payload, encoding="utf-8")

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "STALE",
            "--models-dir", str(tmp_path / "models"),
            "--output", str(output_path),
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 1

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked_no_fresh_snapshot"
    assert "publish_result" not in report  # guarded_publish() was never even called

    # The previously-published live config is left completely untouched.
    assert output_path.read_text(encoding="utf-8") == previous_payload


def test_main_publishes_with_the_real_wall_clock_generated_at_for_a_near_replay(tmp_path, monkeypatch):
    """Regression test for "Prevent replay timestamps from replacing the
    live publish". generated_at passed to guarded_publish() must be the
    REAL wall clock at publish time, not a --now replay timestamp -- even
    for a "near" replay (--now a couple hours before the real wall clock,
    still close enough to be allowed to publish live at all -- see
    test_main_live_refuses_to_publish_a_replay_that_drifts_from_the_real_wall_clock
    for the "far" replay case, which must never reach guarded_publish() in
    the first place). Otherwise a successful non-dry-run replay could
    overwrite a working live publication with one the core loader's own
    staleness check immediately rejects (as stale, or as a negative-age
    "future" publish), silently breaking live sizing until the next real
    retrain.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    replay_now_dt = datetime.now(timezone.utc) - timedelta(hours=2)
    # AAA's archive ends comfortably within the default 240h staleness
    # budget of replay_now_dt -- otherwise the "refuse when every snapshot
    # is stale" guard (a separate, earlier fix) would block the run before
    # it ever reaches guarded_publish() at all. Needs to START well before
    # replay_now_dt too (realized_vol_20d's rolling window has no
    # min_periods override, so it needs 20+ prior rows before it produces
    # anything but NaN -- too short a lookback truncates every row's
    # features away entirely). BBB is a large, unrelated archive purely to
    # clear the minimum-dip-day-rows floor for walk-forward evaluation to
    # run.
    archive_start = (replay_now_dt - timedelta(days=60)).date().isoformat()
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=45, start=archive_start, drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )

    captured_generated_at = []
    real_guarded_publish = train_module.publish_module.guarded_publish

    def spying_guarded_publish(**kwargs):
        captured_generated_at.append(kwargs.get("generated_at"))
        return real_guarded_publish(**kwargs)

    monkeypatch.setattr(train_module.publish_module, "guarded_publish", spying_guarded_publish)

    # --models-dir must stay under the configured ML artifact root: publishing
    # now refuses a model artifact outside it (Codex review: "Keep external
    # model references portable") -- irrelevant to what this test actually
    # checks (generated_at), so simply keep it in-root here.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    models_dir = Path(train_module.stream_root("ml_forecaster")) / "models"
    train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(models_dir),
            "--output", str(tmp_path / "ml_sizing_adjustment.json"),
            "--now", replay_now_dt.isoformat(),
        ]
    )

    assert len(captured_generated_at) == 1  # guarded_publish() really was reached and called
    generated_at = captured_generated_at[0]
    assert generated_at is not None
    real_now = datetime.now(timezone.utc)
    assert abs((real_now - generated_at).total_seconds()) < 60  # close to the real wall clock
    # Meaningfully different from the replay timestamp itself (proving this
    # isn't just accidentally passing because the two happen to be close).
    assert abs((generated_at - replay_now_dt).total_seconds()) > 3600


def test_main_shortens_published_staleness_budget_by_the_snapshots_own_age(tmp_path, monkeypatch):
    """Regression test for "Age published adjustments from their feature
    snapshots". The live loader (dca_config.load_ml_sizing_adjustment)
    measures staleness ONLY from the published file's own generated_at --
    a feature snapshot that is already meaningfully old at publish time
    (but still under the admission filter) would otherwise get ANOTHER
    full max_staleness_hours budget once published, so the market data
    actually driving the adjustment could influence live orders for close
    to DOUBLE the configured budget. The max_staleness_hours actually
    WRITTEN into the payload must be shrunk by however stale the snapshot
    already was, not passed through as the raw CLI value.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    now_dt = datetime.now(timezone.utc) - timedelta(hours=1)  # near-real, passes the replay-drift check trivially
    # AAA's archive deliberately stops ~100h before now_dt -- comfortably
    # inside the default 240h admission budget, but old enough that the
    # remaining budget must be meaningfully less than 240h.
    target_last_business_day = pd.bdate_range(end=(now_dt - timedelta(hours=100)).date(), periods=1)[0]
    start_date = pd.bdate_range(end=target_last_business_day, periods=45)[0]
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=45, start=start_date.date().isoformat(), drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )

    captured_kwargs = {}
    real_guarded_publish = train_module.publish_module.guarded_publish

    def spying_guarded_publish(**kwargs):
        captured_kwargs.update(kwargs)
        return real_guarded_publish(**kwargs)

    monkeypatch.setattr(train_module.publish_module, "guarded_publish", spying_guarded_publish)

    # --models-dir must stay under the configured ML artifact root: publishing
    # now refuses a model artifact outside it (Codex review: "Keep external
    # model references portable") -- irrelevant to what this test actually
    # checks (the staleness budget), so simply keep it in-root here.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    models_dir = Path(train_module.stream_root("ml_forecaster")) / "models"
    train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(models_dir),
            "--output", str(tmp_path / "ml_sizing_adjustment.json"),
            "--now", now_dt.isoformat(),
            "--max-staleness-hours", "240",
        ]
    )

    assert "max_staleness_hours" in captured_kwargs  # guarded_publish() really was reached
    effective = captured_kwargs["max_staleness_hours"]
    assert 0 < effective < 240 - 90  # meaningfully shrunk from the raw 240h CLI value


def test_main_live_refuses_to_publish_a_replay_that_drifts_from_the_real_wall_clock(tmp_path, capsys):
    """Regression test for "Block historical replays from publishing live
    adjustments". A --now far from the real wall clock must never reach
    guarded_publish() at all for a live (non-dry-run) run -- generated_at is
    now correctly the real wall clock (a separate, earlier fix), which
    alone would make the live loader treat adjustments derived from
    arbitrarily old market data as freshly generated and replace a working
    current model. Refusing outright, before guarded_publish() is even
    called, closes that regardless of what the row-level (as_of-relative)
    staleness filter concluded.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=40, start="2024-04-01", drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )
    output_path = tmp_path / "ml_sizing_adjustment.json"
    previous_payload = '{"status": "active", "adjustments": {"AAA": 1.1}}'
    output_path.write_text(previous_payload, encoding="utf-8")

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(tmp_path / "models"),
            "--output", str(output_path),
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 1

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked_replay_not_current"

    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr

    # The previously-published live config is left completely untouched.
    assert output_path.read_text(encoding="utf-8") == previous_payload


def test_main_live_refuses_a_forward_dated_now_even_within_the_staleness_budget(tmp_path, capsys):
    """Regression test for "Refuse forward-dated live replays". The prior
    drift check took `abs(real_now - now)`, so a --now set AHEAD of the
    real wall clock by less than max_staleness_hours was admitted the same
    as an equally-distant PAST replay. But `as_of` (derived from a forward-
    dated `now`) gates which archived rows are treated as "available" for
    feature/target computation -- a future `now` can therefore admit
    archived observations dated after the real publication moment, while
    the resulting config is stamped with the real generated_at and consumed
    as a genuinely fresh live adjustment. A forward-dated `now` must be
    refused outright for a live run, regardless of how small the drift is.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    # Only 1 hour ahead of the real wall clock -- comfortably inside the
    # default 240h staleness budget, so the OLD abs()-based check would
    # have let this through. AAA's archive ends near the REAL current date
    # (not future_now) so it survives the earlier as_of-relative snapshot
    # filter and this test actually exercises the forward-dating check
    # itself, not an unrelated staleness refusal.
    future_now = datetime.now(timezone.utc) + timedelta(hours=1)
    target_last_business_day = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=1)[0]
    start_date = pd.bdate_range(end=target_last_business_day, periods=45)[0]
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=45, start=start_date.date().isoformat(), drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )
    output_path = tmp_path / "ml_sizing_adjustment.json"
    previous_payload = '{"status": "active", "adjustments": {"AAA": 1.1}}'
    output_path.write_text(previous_payload, encoding="utf-8")

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(tmp_path / "models"),
            "--output", str(output_path),
            "--now", future_now.isoformat(),
        ]
    )
    assert exit_code == 1

    run_dir = _find_run_dir(tmp_path / "models", future_now.date().isoformat())
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked_replay_not_current"

    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert "ahead of the real wall clock" in stderr

    # The previously-published live config is left completely untouched.
    assert output_path.read_text(encoding="utf-8") == previous_payload


def test_main_dry_run_is_unaffected_by_a_forward_dated_now(tmp_path):
    """--dry-run must remain usable for a forward-dated --now -- only the
    LIVE publish path refuses it; dry-run never touches the live config."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    future_now = datetime.now(timezone.utc) + timedelta(hours=1)
    target_last_business_day = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=1)[0]
    start_date = pd.bdate_range(end=target_last_business_day, periods=45)[0]
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=45, start=start_date.date().isoformat(), drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(tmp_path / "models"),
            "--output", str(tmp_path / "ml_sizing_adjustment.json"),
            "--now", future_now.isoformat(),
            "--dry-run",
        ]
    )
    assert exit_code == 0
    assert not (tmp_path / "ml_sizing_adjustment.json").exists()


def test_main_live_refuses_a_replay_snapshot_stale_relative_to_the_real_publish_clock(tmp_path, capsys):
    """Regression test for "Check replay snapshots against the publication
    clock". A --now that individually passes BOTH the row-vs-as_of staleness
    filter AND the replay-drift check can still carry a feature snapshot
    that is stale relative to the REAL wall clock this publish is actually
    happening at -- e.g. an 8-day-old --now (within the 10-day/240h drift
    budget) scoring a snapshot that is ALSO ~8 days stale as of that --now
    (within the same 10-day as_of budget) is, relative to real_now, ~16
    days old: well past the same 10-day budget from the live loader's own
    perspective. The live (non-dry-run) path must exclude that snapshot --
    and refuse outright when nothing survives -- rather than publish it
    with a fresh generated_at.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    real_now_at_setup = datetime.now(timezone.utc)
    replay_now_dt = real_now_at_setup - timedelta(days=8)
    # AAA's archive ends ~8 days before replay_now_dt (comfortably inside
    # replay_now_dt's own 10-day as_of budget), but ~16 days before the
    # REAL wall clock (comfortably outside the live 10-day budget). BBB is
    # a large, unrelated, and already ancient archive purely to clear the
    # minimum-dip-day-rows floor -- it fails the EARLIER as_of-relative
    # filter on its own, same as every other test using this pattern.
    archive_end_target = replay_now_dt - timedelta(days=7)
    archive_start = (archive_end_target - timedelta(days=60)).date().isoformat()
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=45, start=archive_start, drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )
    output_path = tmp_path / "ml_sizing_adjustment.json"

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(tmp_path / "models"),
            "--output", str(output_path),
            "--now", replay_now_dt.isoformat(),
        ]
    )
    assert exit_code == 1

    run_dir = _find_run_dir(tmp_path / "models", replay_now_dt.date().isoformat())
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked_no_fresh_snapshot"
    assert "AAA" not in report["adjustments"]

    stderr = capsys.readouterr().err
    assert "publish clock" in stderr
    assert "AAA" in stderr
    assert not output_path.exists()


def test_main_dry_run_is_unaffected_by_replay_drift(tmp_path):
    """--dry-run must remain usable for a far replay -- only the LIVE
    publish path is refused for replay drift; dry-run legitimately wants
    to simulate "what would this historical point in time have produced"
    without ever touching the live config."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(
        history_dir, "AAA",
        make_synthetic_ohlcv(seed=41, n=40, start="2024-04-01", drift=0.0004),
    )
    write_ticker_csv(
        history_dir, "BBB",
        make_synthetic_ohlcv(seed=42, n=260, start="2021-01-04", drift=0.0004),
    )

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA", "BBB",
            "--models-dir", str(tmp_path / "models"),
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "dry_run_not_published"


@pytest.mark.parametrize("bad_horizon", ["0", "-1"])
def test_main_rejects_a_nonpositive_horizon_before_building_any_dataset(tmp_path, capsys, bad_horizon):
    """Regression test for the "negative --horizon leaks test rows into training" finding.

    A nonpositive --horizon makes features.compute_forward_return_target()/
    compute_label_end_date() shift BACKWARD instead of forward, which can
    leak test-window rows into the training set and let contaminated metrics
    pass the publish gate. The fix validates --horizon at arg-parse time
    (train._positive_int), so argparse must reject it -- via SystemExit(2),
    argparse's own error-exit convention -- before build_dataset() (and
    therefore any archive read) ever runs. history_dir is deliberately left
    empty of any CSV to also prove that: if the CLI got as far as discovering
    tickers/building a dataset, it would refuse for an unrelated reason
    ("no ticker history CSVs found") rather than the horizon-specific one
    this test pins.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        train_module.main(
            [
                "--history-dir", str(history_dir),
                "--tickers", "AAA",
                "--models-dir", str(tmp_path / "models"),
                "--horizon", bad_horizon,
                "--dry-run",
                "--now", "2024-06-01T00:00:00+00:00",
            ]
        )
    assert exc_info.value.code == 2

    stderr = capsys.readouterr().err
    assert "--horizon" in stderr
    assert "positive" in stderr.lower()
    # No dataset construction (and therefore no models-dir artifact) ever
    # happened -- the rejection is at arg-parse time, before main() even
    # reaches build_dataset().
    assert not (tmp_path / "models").exists()


def test_main_records_a_non_default_horizon_in_model_and_report(tmp_path):
    """Regression test for "Record the configured horizon in run artifacts".

    Neither model.json nor eval_report.json used to record --horizon --
    once a later publish replaces the live config (which DOES record it,
    via publish.guarded_publish()'s own horizon_trading_days field), or
    immediately for a --dry-run artifact that never reaches publish.py at
    all, there was no way to tell whether a given model.json's coefficients
    described a 1-day, 5-day, or 20-day forecast.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--horizon", "10",  # non-default
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0

    run_dir = _find_run_dir(tmp_path / "models", "2024-06-01")
    report = json.loads((run_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert report["horizon_trading_days"] == 10

    saved_model = model_module.TrainedModel.load(run_dir / "model.json")
    assert saved_model.horizon_trading_days == 10


@pytest.mark.parametrize("bad_staleness", ["nan", "inf", "-inf", "0", "-5", "1e308"])
def test_main_rejects_a_non_finite_or_nonpositive_max_staleness_hours(tmp_path, capsys, bad_staleness):
    """Regression test for "Validate staleness before computing the feature
    cutoff" and "Bound staleness before accepting the CLI value". A NaN/inf
    --max-staleness-hours previously passed argparse's bare type=float
    unvalidated, then crashed constructing timedelta(hours=...) for the
    feature-staleness cutoff -- before guarded_publish() (which validates
    the same value again) ever got a chance to produce its documented
    blocked_invalid_staleness outcome instead of a traceback. "1e308" is a
    SEPARATE hazard from NaN/inf: it is entirely finite and positive by
    math.isfinite's own definition, so it passed the original finite-
    positive check, but timedelta's internal representation is bounded
    (~2.7e11 days), so timedelta(hours=1e308) still raised an uncaught
    OverflowError at that same feature-staleness cutoff computation --
    after training and evaluation had already run.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        train_module.main(
            [
                "--history-dir", str(history_dir),
                "--tickers", "AAA",
                "--models-dir", str(tmp_path / "models"),
                "--max-staleness-hours", bad_staleness,
                "--dry-run",
                "--now", "2024-06-01T00:00:00+00:00",
            ]
        )
    assert exc_info.value.code == 2

    stderr = capsys.readouterr().err
    assert "--max-staleness-hours" in stderr
    assert not (tmp_path / "models").exists()


@pytest.mark.parametrize("bad_return_clip_pct", ["nan", "inf", "-inf", "0", "-5"])
def test_main_rejects_a_non_finite_or_nonpositive_return_clip_pct(tmp_path, capsys, bad_return_clip_pct):
    """Regression test for "Validate the return clipping argument before
    training". A NaN/inf/nonpositive --return-clip-pct previously passed
    argparse's bare type=float unvalidated, then let training and
    walk-forward evaluation run to completion before
    rescale_return_to_multiplier()'s own "if return_clip_pct <= 0: raise
    ValueError" finally crashed the run with an uncaught traceback --
    instead of refusing before any of that work even started.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        train_module.main(
            [
                "--history-dir", str(history_dir),
                "--tickers", "AAA",
                "--models-dir", str(tmp_path / "models"),
                "--return-clip-pct", bad_return_clip_pct,
                "--dry-run",
                "--now", "2024-06-01T00:00:00+00:00",
            ]
        )
    assert exc_info.value.code == 2

    stderr = capsys.readouterr().err
    assert "--return-clip-pct" in stderr
    assert not (tmp_path / "models").exists()


@pytest.mark.parametrize(
    "min_multiplier,max_multiplier",
    [
        ("2.0", "1.0"),  # inverted: min > max
        ("nan", "1.2"),
        ("0.8", "nan"),
        ("inf", "1.2"),
        ("0.1", "1.2"),  # below the schema's min_multiplier floor (0.5)
        ("0.8", "3.0"),  # above the schema's max_multiplier ceiling (1.5)
    ],
)
@pytest.mark.parametrize("dry_run", [True, False])
def test_main_rejects_invalid_multiplier_bounds_before_scoring(
    tmp_path, capsys, min_multiplier, max_multiplier, dry_run
):
    """Regression test for "Validate multiplier bounds before dry-run
    scoring". Unlike --return-clip-pct/--max-staleness-hours (argparse
    `type=` validators, checked before main() even starts), an inverted or
    non-finite --min-multiplier/--max-multiplier pair previously reached
    rescale_return_to_multiplier() completely unchecked and produced
    inverted or non-finite proposed adjustments -- the only bounds
    validation was inside guarded_publish(), which --dry-run never calls,
    so a dry run could "succeed" (exit 0) with schema-violating artifacts on
    disk. Now checked up front, before any dataset/training work, for BOTH
    --dry-run and the live path.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    args = [
        "--history-dir", str(history_dir),
        "--tickers", "AAA",
        "--models-dir", str(tmp_path / "models"),
        "--min-multiplier", min_multiplier,
        "--max-multiplier", max_multiplier,
        "--now", "2024-06-01T00:00:00+00:00",
    ]
    if dry_run:
        args.append("--dry-run")

    exit_code = train_module.main(args)
    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert "REFUSED" in stderr
    assert not (tmp_path / "models").exists()


def test_main_still_accepts_a_positive_return_clip_pct(tmp_path):
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--return-clip-pct", "2.5",
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0


def test_main_still_accepts_a_positive_horizon(tmp_path):
    """--horizon 5 (an ordinary positive value) must keep working post-fix."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(tmp_path / "models"),
            "--horizon", "5",
            "--dry-run",
            "--now", "2024-06-01T00:00:00+00:00",
        ]
    )
    assert exit_code == 0


# --- incumbent re-scoring wiring (Codex review: "Compare the incumbent on
# the current evaluation folds") ---------------------------------------------


def test_main_passes_the_model_artifact_path_and_a_working_scorer_to_guarded_publish(tmp_path, monkeypatch):
    """train.main() must hand guarded_publish() BOTH the real model.json path
    it just saved AND a score_incumbent_model callback that can actually
    load and re-score it -- spies on the real guarded_publish() the same way
    test_generated_at_is_the_real_wall_clock_not_the_replay_now does, so this
    doesn't depend on any particular walk-forward pass/fail outcome.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    captured_kwargs = {}
    real_guarded_publish = train_module.publish_module.guarded_publish

    def spying_guarded_publish(**kwargs):
        captured_kwargs.update(kwargs)
        return real_guarded_publish(**kwargs)

    monkeypatch.setattr(train_module.publish_module, "guarded_publish", spying_guarded_publish)

    # --models-dir must stay under the configured ML artifact root: publishing
    # now refuses a model artifact outside it (Codex review: "Keep external
    # model references portable") -- irrelevant to what this test actually
    # checks (the guarded_publish() kwargs), so simply keep it in-root here.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    models_dir = Path(train_module.stream_root("ml_forecaster")) / "models"
    exit_code = train_module.main(
        [
            "--history-dir", str(history_dir),
            "--tickers", "AAA",
            "--models-dir", str(models_dir),
            "--output", str(tmp_path / "ml_sizing_adjustment.json"),
            "--now", "2024-06-01T00:00:00+00:00",
            # This test is about the KWARGS passed to guarded_publish(), not
            # about staleness filtering (covered separately) -- the synthetic
            # archive's last row predates --now by design, so raise the
            # budget out of the way (mirrors test_main_scores_adjustments_
            # from_build_feature_frames_latest_row_not_the_stale_dataset_tail).
            "--max-staleness-hours", "87600",
        ]
    )
    del exit_code  # this test is about the guarded_publish() kwargs, not the pass/fail verdict
    assert captured_kwargs, "guarded_publish() was never reached"

    run_dir = _find_run_dir(models_dir, "2024-06-01")
    model_json_path = run_dir / "model.json"
    # Whatever form it is stored in, it must resolve back to the real artifact
    # (Codex review: "Store a portable incumbent model reference"). --models-dir
    # is under the configured ML artifact root here, so it is stored root-
    # relative; see test_an_external_relative_models_dir_is_refused_rather_
    # than_published for the out-of-root case, which publishing now refuses.
    stored = captured_kwargs["model_artifact_path"]
    assert train_module._resolve_artifact_reference(stored) == model_json_path

    scorer = captured_kwargs["score_incumbent_model"]
    # Scoring this run's OWN just-saved artifact against itself must return
    # None: its trained_at is this same run's `now`, so no row's own
    # label_end_date falls strictly after that cutoff -- the held-out split
    # is empty -- confirms the out-of-sample restriction (Codex review:
    # "Avoid scoring the incumbent on its training rows") is actually wired
    # through, not bypassed.
    assert scorer(str(model_json_path)) is None
    # A path that doesn't exist must fail closed to None too, not raise --
    # publish.blocking_reasons() relies on this contract to fall back
    # gracefully when an incumbent's artifact has gone missing.
    assert scorer(str(tmp_path / "does-not-exist.json")) is None

    # An incumbent trained partway THROUGH this run's own dataset -- not
    # before all of it, and not after all of it -- must score successfully:
    # some rows are available to train the comparison candidate, and some
    # remain held out to score both models on. Proves the callback is wired
    # to the real dip_dataset/alpha this run used, not a stub that always
    # returns None regardless of the artifact.
    dataset = train_module.build_dataset(history_dir, ["AAA"], horizon=5)
    dip_dataset = dataset[dataset["dip_day"]].reset_index(drop=True)
    unique_label_ends = sorted(dip_dataset["label_end_date"].unique())
    assert len(unique_label_ends) > 1, "fixture needs more than one distinct label_end_date"
    midpoint_cutoff = pd.Timestamp(unique_label_ends[len(unique_label_ends) // 2])
    old_incumbent = model_module.fit(
        dip_dataset, model_version="old-incumbent", trained_at=midpoint_cutoff.isoformat()
    )
    old_incumbent_path = tmp_path / "old_incumbent" / "model.json"
    old_incumbent.save(old_incumbent_path)
    old_score = scorer(str(old_incumbent_path))
    assert old_score is not None
    candidate_mae, incumbent_mae = old_score
    assert candidate_mae >= 0.0
    assert incumbent_mae >= 0.0


def test_build_incumbent_scorer_matches_evaluate_score_incumbent_on_folds(tmp_path):
    """train._build_incumbent_scorer()'s own wiring, isolated from main():
    given a real saved model.json and the SAME dip_dataset/candidate model,
    it must produce exactly evaluate.score_incumbent_on_folds()'s own
    result -- not merely "a" finite pair."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=41, n=260, drift=0.0004))

    dataset = train_module.build_dataset(history_dir, ["AAA"], horizon=5)
    dip_dataset = dataset[dataset["dip_day"]].reset_index(drop=True)
    fitted = model_module.fit(
        dip_dataset, model_version="incumbent-under-test", trained_at="2000-01-01T00:00:00+00:00"
    )
    model_json_path = tmp_path / "incumbent" / "model.json"
    fitted.save(model_json_path)

    # The candidate is this run's final model. Its alpha configures the
    # leakage-free comparison model that evaluate fits before the holdout;
    # the final model's own in-sample predictions are not used.
    candidate_model = model_module.fit(dip_dataset, alpha=model_module.DEFAULT_ALPHA * 2, model_version="candidate")

    scorer = train_module._build_incumbent_scorer(dip_dataset, candidate_model)
    result = scorer(str(model_json_path))

    expected = train_module.evaluate_module.score_incumbent_on_folds(fitted, candidate_model, dip_dataset)
    assert result == pytest.approx(expected)


def test_model_artifact_path_under_the_root_is_stored_relative(tmp_path, monkeypatch):
    """Regression test for "Store a portable incumbent model reference".

    `str(model_json_path)` recorded an absolute path tied to the publishing
    checkout's artifact root (or a relative one tied to its working directory),
    and that value is persisted into the version-controlled published config. A
    retrain from another checkout, host, working directory or
    $DCA_CRON_TRACKING_ROOT could then not load the incumbent, so
    _build_incumbent_scorer silently fell back to the weaker skill-ratio
    comparison and a model failing the intended direct comparison could publish.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    import importlib

    import dca_paths

    importlib.reload(dca_paths)
    importlib.reload(train_module)

    root = Path(train_module.stream_root("ml_forecaster"))
    inside = root / "models" / "2026-08-17-abc" / "model.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}", encoding="utf-8")

    stored = train_module._portable_artifact_reference(inside)
    assert not Path(stored).is_absolute()
    assert stored == "models/2026-08-17-abc/model.json"
    assert train_module._resolve_artifact_reference(stored) == inside.resolve()

    # A path OUTSIDE the root has no portable reference at all -- an absolute
    # path is exactly as host-tied as the original bug this function fixes,
    # so publishing is refused rather than silently degraded (Codex review:
    # "Keep external model references portable").
    outside = tmp_path / "elsewhere" / "model.json"
    with pytest.raises(ValueError, match="outside the ML artifact root"):
        train_module._portable_artifact_reference(outside)


def test_a_root_relative_reference_is_stored_with_forward_slashes(tmp_path, monkeypatch):
    """Regression test for "Serialize relative artifact references portably".

    On Windows `str(Path)` yields backslash separators, and a POSIX Path treats
    those as literal filename characters rather than separators -- so a config
    published on Windows and consumed on Linux could not locate the incumbent
    and silently fell back to the weaker skill-ratio comparison, defeating the
    cross-host portability this pair of functions exists for.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    import importlib

    import dca_paths

    importlib.reload(dca_paths)
    importlib.reload(train_module)

    root = Path(train_module.stream_root("ml_forecaster"))
    inside = root / "models" / "2026-08-17-abc" / "model.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}", encoding="utf-8")

    stored = train_module._portable_artifact_reference(inside)
    assert "\\" not in stored
    assert stored == "models/2026-08-17-abc/model.json"

    # A legacy Windows-written reference still resolves on a POSIX host rather
    # than being read as one long filename.
    windows_style = "models\\2026-08-17-abc\\model.json"
    assert train_module._resolve_artifact_reference(windows_style) == inside


def test_a_legacy_working_directory_relative_reference_still_loads(tmp_path, monkeypatch):
    """Regression test for "Preserve pre-change relative artifact references".

    A config published by the EARLIER version recorded `str(model_json_path)`
    unchanged, so an explicit relative --models-dir stored a path relative to
    that retrain's WORKING DIRECTORY. Rebasing every relative reference onto the
    ML artifact root looks in the wrong place, fails to load an incumbent that
    is really still there, and drops _build_incumbent_scorer to the weaker
    skill-ratio comparison -- the silent downgrade this pair of functions exists
    to prevent.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    import importlib

    import dca_paths

    importlib.reload(dca_paths)
    importlib.reload(train_module)

    # A legacy reference: relative, and meaningful only against the working
    # directory it was published from.
    legacy_dir = tmp_path / "legacy_models" / "2026-08-10-abc"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "model.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    legacy_reference = "legacy_models/2026-08-10-abc/model.json"

    resolved = train_module._resolve_artifact_reference(legacy_reference)
    assert resolved == (legacy_dir / "model.json").resolve()

    # The NEW contract still wins wherever both readings could apply: an
    # identically-named artifact under the ML root takes precedence, so a
    # portable reference is never diverted to a stale working-directory copy.
    root = Path(train_module.stream_root("ml_forecaster"))
    rooted = root / legacy_reference
    rooted.parent.mkdir(parents=True, exist_ok=True)
    rooted.write_text("{}", encoding="utf-8")
    assert train_module._resolve_artifact_reference(legacy_reference) == rooted

    # When neither exists the failure points at the intended location, not at
    # a working-directory guess.
    missing = train_module._resolve_artifact_reference("nowhere/model.json")
    assert missing == root / "nowhere/model.json"


def test_an_external_relative_models_dir_is_refused_rather_than_published(tmp_path, monkeypatch):
    """Regression test for "Keep external model references portable".

    `--models-dir ../models` puts the artifact outside the configured ML
    root. An earlier version stored that as an absolute path so it would at
    least round-trip on THIS host -- but that value is persisted into the
    version-controlled published config, so the next retrain on any OTHER
    checkout/host could not load the incumbent, and since the shared
    holdout gate now treats scorer failure as blocking, no retrain could
    publish again until an operator repaired the path by hand. Refusing to
    publish at all, with an actionable message, is the fail-closed fix.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))
    import importlib

    import dca_paths

    importlib.reload(dca_paths)
    importlib.reload(train_module)

    external = tmp_path / "elsewhere" / "2026-08-17-abc"
    external.mkdir(parents=True, exist_ok=True)
    (external / "model.json").write_text("{}", encoding="utf-8")

    # Name it RELATIVELY, exactly as an operator's --models-dir would.
    monkeypatch.chdir(tmp_path)
    relative = Path("elsewhere") / "2026-08-17-abc" / "model.json"
    assert not relative.is_absolute()

    with pytest.raises(ValueError, match="outside the ML artifact root"):
        train_module._portable_artifact_reference(relative)
