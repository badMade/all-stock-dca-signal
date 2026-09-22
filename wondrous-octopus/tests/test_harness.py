"""Tests for harness.py -- resume-equals-uninterrupted is the flagship."""

from __future__ import annotations

import dataclasses
import json
import os
from collections import Counter
from typing import Any, Sequence

import baseline_model
import features as _features
import harness as harness_module
import numpy as np
import pandas as pd
import pytest
from harness import HarnessConfig, HarnessError, WalkForwardHarness
from ledger import Ledger
from model_interface import ForecastRequest, RepairCounts

features: Any = _features


def _quiet(_message: str) -> None:
    pass


@pytest.fixture
def panel(synthetic_history_dir):
    probe = features.build_panel(synthetic_history_dir, min_history=60)
    cutoff = pd.Timestamp(probe.calendar[150])
    return features.build_panel(synthetic_history_dir, min_history=60, initial_train_end=cutoff)


def _config(panel, *, n_steps=None, models=("persistence",), **overrides):
    start = np.datetime_as_string(panel.calendar[151], unit="D")
    end = None
    if n_steps is not None:
        end = np.datetime_as_string(panel.calendar[151 + n_steps - 1], unit="D")
    defaults: dict[str, Any] = dict(
        start=start,
        end=end,
        initial_train_end=np.datetime_as_string(panel.calendar[150], unit="D"),
        models=tuple(models),
        window_len=16,
        min_history=60,
        checkpoint_every=6,
        diagnostic_sample=10,
        seed=0,
    )
    defaults.update(overrides)
    return HarnessConfig(**defaults)


def _models(config):
    registry = {"persistence": baseline_model.PersistenceModel, "gbm_quantile": baseline_model.GbmQuantileModel}
    built: dict[str, Any] = {}
    for name in config.models:
        built[name] = registry[name]() if name != "gbm_quantile" else baseline_model.GbmQuantileModel(max_iter=10)
    return built


def _all_records(run_dir, model="persistence"):
    ledger = Ledger(run_dir)
    output = {}
    for kind in ("forecast", "score"):
        output[kind] = list(ledger.iter_records(model=model, kind=kind))
    return output


def _all_seed_triples(harness):
    """fresh()'s seeded reveals, flattened across every target-date bucket
    of harness._pending_seed_reveals -- these are held there (not queued
    directly into pending_revealed) until step()'s cursor actually
    reaches each entry's own target date."""
    return [triple for bucket in harness._pending_seed_reveals.values() for triple in bucket]


def _expected_seed_tickers(panel, target64):
    """Tickers WalkForwardHarness.fresh() should seed for the row whose
    label ends on ``target64``: finite target AND finite features AND past
    the ticker's own warm-up -- the same gate normal reveals get via
    eligible_on() + the features-isfinite filter."""
    expected = set()
    for ticker, series in panel.series.items():
        positions = np.flatnonzero(series.label_end_dates == target64)
        if not len(positions):
            continue
        origin_row = int(positions[0])
        if origin_row < panel.min_history:
            continue
        if not np.isfinite(series.targets[origin_row]).all():
            continue
        if not np.isfinite(series.features[origin_row]).all():
            continue
        expected.add(ticker)
    return expected


class TestBasicProtocol:
    def test_first_cursor_date_label_is_seeded_into_pending_revealed(self, panel, tmp_path):
        # The row whose target is exactly config.start (the day just
        # before start, per ticker) is excluded from BOTH the initial
        # fit's trainability cutoff (target is AFTER the cutoff) AND the
        # walk-forward loop's own first predict (which only predicts FROM
        # config.start onward, never backward) -- without seeding, every
        # ticker's newest causally-available training example would be
        # silently skipped forever (Codex review regression: "Ingest
        # labels revealed on the first simulation date").
        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        start64 = np.datetime64(config.start)
        expected_tickers = _expected_seed_tickers(panel, start64)
        assert expected_tickers  # sanity: at least one ticker has this row
        seeded_tickers = {triple[0] for triple in _all_seed_triples(harness)}
        assert seeded_tickers == expected_tickers

    def test_seeding_anchors_to_the_actual_first_replay_date_not_config_start(self, panel, tmp_path):
        # config.start need not be a trading date -- the documented default
        # (2000-01-01) is a Saturday. harness.cursor (searchsorted against
        # panel.calendar) already resolves to the actual first calendar day
        # step() will process; seeding must match THAT day's labels, not a
        # literal string comparison against config.start, which may never
        # equal any ticker's label_end_date at all (Codex review regression:
        # "Seed reveals from the actual first replay date").
        gap_index = None
        for index in range(150, len(panel.calendar) - 1):  # after the fixture's initial_train_end (calendar[150])
            if panel.calendar[index + 1] - panel.calendar[index] > np.timedelta64(1, "D"):
                gap_index = index
                break
        assert gap_index is not None, "sanity: the synthetic business-day calendar has a weekend gap"
        non_trading_start = panel.calendar[gap_index] + np.timedelta64(1, "D")
        actual_first_replay_date = panel.calendar[gap_index + 1]
        assert non_trading_start not in panel.calendar  # sanity: genuinely not a trading day
        config = dataclasses.replace(
            _config(panel, n_steps=1),
            start=str(np.datetime_as_string(non_trading_start, unit="D")),
            end=None,  # n_steps=1's end was computed relative to the OLD start; open-ended here instead
        )
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert harness.cursor == gap_index + 1  # sanity: cursor landed on the next real trading day
        expected_tickers = _expected_seed_tickers(panel, actual_first_replay_date)
        assert expected_tickers  # sanity: at least one ticker has this row
        seeded_tickers = {triple[0] for triple in _all_seed_triples(harness)}
        assert seeded_tickers == expected_tickers

    def test_seeds_a_ragged_tickers_boundary_row_even_when_its_target_lands_past_the_first_replay_date(
        self, panel, tmp_path
    ):
        # A ragged ticker (a gap, a later join, or simply not trading on
        # the exact global first-replay date) can have its own boundary
        # row's target land on a LATER date than the first replay date --
        # the old exact-match search (label_end_dates == start64) missed
        # it entirely, permanently omitting that ticker's newest
        # pre-replay training example from every model's online training
        # (Codex review: "Reveal pre-start labels on each ticker's first
        # replay day").
        import dataclasses

        config = _config(panel, n_steps=1)
        start64 = np.datetime64(config.start)
        ticker = "AAA"
        series = panel.series[ticker]
        boundary_row = int(np.flatnonzero(series.label_end_dates == start64)[0])
        later_target = start64 + np.timedelta64(3, "D")
        mutated_labels = series.label_end_dates.copy()
        mutated_labels[boundary_row] = later_target
        panel.series[ticker] = dataclasses.replace(series, label_end_dates=mutated_labels)
        try:
            harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
            # Held under its OWN target date -- not yet in pending_revealed,
            # which would let the very first step() flush it before that
            # date is actually reached (Codex review: "Delay ragged
            # boundary reveals until their label date").
            assert not harness.pending_revealed
            later_target_iso = str(np.datetime_as_string(later_target, unit="D"))
            seeded = {(triple[0], triple[1]) for triple in harness._pending_seed_reveals.get(later_target_iso, [])}
            assert (ticker, boundary_row) in seeded
        finally:
            panel.series[ticker] = series  # restore for other tests sharing this fixture

    def test_first_cursor_date_label_flows_into_the_first_update_call(self, panel, tmp_path):
        seen_dates: list[list[str]] = []

        class SpyPersistence(baseline_model.PersistenceModel):
            def update(self, revealed, *, step_index):
                seen_dates.append(sorted(day.origin_date for day in revealed))
                return super().update(revealed, step_index=step_index)

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": SpyPersistence()}, tmp_path / "run", config, log=_quiet
        )
        expected_origin = str(np.datetime_as_string(panel.calendar[150], unit="D"))
        harness.step(panel.calendar[151])
        assert seen_dates
        assert expected_origin in seen_dates[0]

    def test_pending_revealed_flush_matches_despite_an_intraday_stamped_origin_date(self, panel, tmp_path):
        # The queued origin_date_iso is always day-precision, but
        # series.dates could carry an intraday timestamp on a hand-built
        # panel (load_history_csv() now normalizes at ingestion, but this
        # is defense in depth) -- exact equality used to silently drop the
        # queued reveal (never calling update() with it), disabling online
        # training for that entry with no error (Codex review regression:
        # "Match queued reveals by calendar day").
        import dataclasses

        seen_dates: list[list[str]] = []

        class SpyPersistence(baseline_model.PersistenceModel):
            def update(self, revealed, *, step_index):
                seen_dates.append(sorted(day.origin_date for day in revealed))
                return super().update(revealed, step_index=step_index)

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": SpyPersistence()}, tmp_path / "run", config, log=_quiet
        )
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))
        harness.pending_revealed = [[ticker, origin_row, origin_date_iso]]

        intraday_dates = series.dates.copy()
        intraday_dates[origin_row] = intraday_dates[origin_row] + np.timedelta64(16, "h")
        panel.series[ticker] = dataclasses.replace(series, dates=intraday_dates)

        harness.step(panel.calendar[151])
        assert seen_dates  # update() was actually called with the revealed day
        assert origin_date_iso in seen_dates[0]

    def test_forecasts_mature_into_scores(self, panel, tmp_path):
        config = _config(panel, n_steps=8)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert harness.run() == "completed"
        records = _all_records(tmp_path / "run")
        assert len(records["forecast"]) > 0
        assert len(records["score"]) > 0
        # Every score's origin strictly precedes its target.
        for record in records["score"]:
            assert record["origin_date"] < record["target_date"]
        # Scores carry both calibrated and raw bands plus the scoring payload.
        sample = records["score"][0]
        assert "quantiles" in sample and "quantiles_raw" in sample and "score" in sample

    def test_predict_repairs_are_ledgered_when_nonzero(self, panel, tmp_path):
        # predict()'s repair counts land on the transient model.last_repairs
        # attribute, overwritten on the very next predict() call; the
        # harness never read it, so a model could require pervasive output
        # repairs with zero trace in the ledger or report (Codex review
        # regression: "Ledger forecast repairs instead of discarding their
        # counts").
        class RepairSpy(baseline_model.PersistenceModel):
            def predict(self, requests):
                forecasts = super().predict(requests)
                self.last_repairs = RepairCounts(quantile_crossings=3, bar_repairs=2)
                return forecasts

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": RepairSpy()}, tmp_path / "run", config, log=_quiet
        )
        harness.step(panel.calendar[151])
        repairs = list(Ledger(tmp_path / "run").iter_records(model="persistence", kind="repair"))
        assert len(repairs) == 1
        assert repairs[0]["quantile_crossings"] == 3
        assert repairs[0]["bar_repairs"] == 2
        assert repairs[0]["n_predicted"] > 0

    def test_predict_repairs_are_not_ledgered_when_zero(self, panel, tmp_path):
        # No ledger bloat from an all-zero repair record every single step.
        class NoRepairSpy(baseline_model.PersistenceModel):
            def predict(self, requests):
                forecasts = super().predict(requests)
                self.last_repairs = RepairCounts(quantile_crossings=0, bar_repairs=0)
                return forecasts

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": NoRepairSpy()}, tmp_path / "run", config, log=_quiet
        )
        harness.step(panel.calendar[151])
        repairs = list(Ledger(tmp_path / "run").iter_records(model="persistence", kind="repair"))
        assert repairs == []

    def test_step_calls_calibrator_observe_batch_once_not_once_per_ticker(self, panel, tmp_path):
        # Regression test for the harness's CALL SITE, complementing
        # ConformalCalibrator.observe_batch()'s own unit tests: step() must
        # call observe_batch() exactly ONCE per model per day, with every
        # matured ticker's observation bundled into that one call -- not
        # once per ticker, which would apply the same exponential nudge
        # once per ticker instead of once per day (Codex review regression:
        # "Aggregate calibration updates once per market day").
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[151])  # predictions only, nothing matures yet
        calls: list[int] = []
        original = harness.calibrators["persistence"].observe_batch

        def spy(observations, *, target_date):
            calls.append(len(observations))
            return original(observations, target_date=target_date)

        harness.calibrators["persistence"].observe_batch = spy
        result = harness.step(panel.calendar[152])
        assert result["scored"] > 1  # sanity: more than one ticker matured this step
        assert len(calls) == 1
        assert calls[0] > 1

    def test_step_calibrates_each_overdue_target_date_separately(self, panel, tmp_path):
        # A late-rebound backlog can leave several overdue buckets maturing
        # in ONE step (matured_keys pops every key <= date_iso, not just
        # today's). Keying calibration batches only by model would lump
        # multiple distinct target dates into one nudge, making catching
        # up two days behave like one instead of one update per date in
        # chronological order (Codex review regression: "Calibrate each
        # overdue target date separately").
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}

        def _fake_entry(origin_row):
            return {
                "model": "persistence",
                "ticker": ticker,
                "origin_row": origin_row,
                "origin_date": str(np.datetime_as_string(series.dates[origin_row], unit="D")),
                "point": {c: 0.0 for c in ("open", "high", "low", "close")},
                "quantiles": band,
                "quantiles_raw": band,
                "ref_close": 100.0,
                "adj_ratio": 1.0,
            }

        origin_a = int(np.flatnonzero(series.dates == panel.calendar[150])[0])
        origin_b = int(np.flatnonzero(series.dates == panel.calendar[151])[0])
        target_a = str(np.datetime_as_string(panel.calendar[151], unit="D"))
        target_b = str(np.datetime_as_string(panel.calendar[152], unit="D"))
        assert np.datetime_as_string(series.label_end_dates[origin_a], unit="D") == target_a
        assert np.datetime_as_string(series.label_end_dates[origin_b], unit="D") == target_b
        harness.outstanding[target_a] = [_fake_entry(origin_a)]
        harness.outstanding[target_b] = [_fake_entry(origin_b)]

        calls: list[tuple[int, ...]] = []
        original = harness.calibrators["persistence"].observe_batch

        def spy(observations, *, target_date):
            calls.append(tuple(sorted(actual["close"] for _quantiles, actual in observations)))
            return original(observations, target_date=target_date)

        harness.calibrators["persistence"].observe_batch = spy
        harness.step(panel.calendar[152])  # both buckets are <= this date: both mature THIS step
        assert len(calls) == 2  # one call per overdue target date, not one lumped call

    def test_trainability_guard_no_future_rows_in_update_batches(self, panel, tmp_path):
        seen_batches = []

        class SpyPersistence(baseline_model.PersistenceModel):
            def update(self, revealed, *, step_index):
                seen_batches.append((step_index, [(day.ticker, day.target_date) for day in revealed]))
                return super().update(revealed, step_index=step_index)

        config = _config(panel, n_steps=8)
        models = {"persistence": SpyPersistence()}
        harness = WalkForwardHarness.fresh(panel, models, tmp_path / "run", config, log=_quiet)
        step_dates = [np.datetime_as_string(panel.calendar[151 + offset], unit="D") for offset in range(8)]
        harness.run()
        assert seen_batches, "updates should have run"
        for step_index, batch in seen_batches:
            # The step that consumed this batch processed step_dates[...] --
            # every revealed target must be at or before the date of the step
            # AFTER which it was consumed (i.e. never a future row).
            consuming_date = step_dates[min(step_index, len(step_dates) - 1)]
            for _ticker, target_date in batch:
                assert target_date <= consuming_date

    def test_last_row_forecasts_are_unresolved_and_ledgered_with_null_target(self, panel, tmp_path):
        total = len(panel.calendar)
        config = _config(panel, n_steps=None, models=("persistence",))  # run to the very end of the archive
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert harness.run() == "completed"
        assert harness.cursor == total
        records = _all_records(tmp_path / "run")
        unresolved = [record for record in records["forecast"] if record["target_date"] is None]
        # The final calendar day's forecasts have no next row: that IS the
        # "tomorrow" table, parked under the unresolved key.
        assert unresolved
        assert harness_module.UNRESOLVED_KEY in harness.outstanding

    def test_progress_log_line_emitted_per_step(self, panel, tmp_path):
        lines = []
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=lines.append)
        harness.run()
        step_lines = [line for line in lines if "coverage-to-date" in line]
        assert len(step_lines) == 4


class TestDiagnostics:
    def test_non_finite_reconstruction_error_is_clamped_not_propagated(self, panel, tmp_path):
        # A divergent encoder can emit NaN/inf for a sampled ticker's
        # reconstruction; diagnostic_health()'s "current > degrade_factor *
        # baseline" comparison always evaluates False against a NaN average,
        # so an unsanitized non-finite abs_err would silently report OK --
        # precisely when the recovery-retrain warning is needed (Codex
        # review regression: "Reject non-finite encoder diagnostics").
        class DivergentPersistence(baseline_model.PersistenceModel):
            def reconstruction_diagnostic(
                self, requests: Sequence[ForecastRequest]
            ) -> dict[str, np.ndarray] | None:
                matrix = np.zeros((len(requests), 4), dtype=np.float32)
                matrix[0, :] = np.nan  # poison one sampled ticker's reconstruction
                if len(requests) > 1:
                    matrix[1, 0] = np.inf
                return {"time": matrix}

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": DivergentPersistence()}, tmp_path / "run", config, log=_quiet
        )
        harness.step(panel.calendar[151])
        records = list(harness.ledger.iter_records(model="persistence", kind="diagnostic"))
        assert records  # sanity: the diagnostic step actually ran and ledgered something
        for record in records:
            for component, value in record["abs_err"].items():
                assert np.isfinite(value), f"{component} abs_err was not clamped to finite: {value}"


class TestFreshCleansUpOnFailure:
    def test_a_failed_initial_fit_leaves_no_run_directory_behind(self, panel, tmp_path):
        # Ledger.create() writes <run_dir>/ledger to disk BEFORE any
        # model's fit() runs. A model that refuses (gbm_quantile's
        # documented refusal on an untrainable cutoff, or any other
        # failure) used to leave that run-shaped directory behind despite
        # the CLI's documented "REFUSED -- nothing was written" contract
        # (Codex review regression: "Create run artifacts only after
        # initial fits succeed").
        class _RefusingModel:
            name = "refusing"

            def fit(self, panel, *, seed, step_index=0):
                raise RuntimeError("simulated fit refusal")

        config = _config(panel, n_steps=1, models=("persistence",))
        run_dir = tmp_path / "fresh_run"
        assert not run_dir.exists()
        with pytest.raises(RuntimeError, match="simulated fit refusal"):
            WalkForwardHarness.fresh(panel, {"refusing": _RefusingModel()}, run_dir, config, log=_quiet)
        assert not run_dir.exists()

    def test_cleanup_never_removes_a_preexisting_run_directory(self, panel, tmp_path):
        # If the operator pointed --run-dir at a directory that already
        # existed (for whatever reason) before this call, a failed fit
        # must still discard the ledger THIS call created but must never
        # delete the directory itself out from under the operator.
        class _RefusingModel:
            name = "refusing"

            def fit(self, panel, *, seed, step_index=0):
                raise RuntimeError("simulated fit refusal")

        config = _config(panel, n_steps=1, models=("persistence",))
        run_dir = tmp_path / "preexisting_run"
        run_dir.mkdir()
        (run_dir / "operator_file.txt").write_text("do not delete me")
        with pytest.raises(RuntimeError, match="simulated fit refusal"):
            WalkForwardHarness.fresh(panel, {"refusing": _RefusingModel()}, run_dir, config, log=_quiet)
        assert run_dir.exists()
        assert (run_dir / "operator_file.txt").exists()
        assert not (run_dir / "ledger").exists()

    def test_cleanup_never_removes_a_preexisting_ledger_directory(self, panel, tmp_path):
        # Ledger.create() only refuses on an existing *.jsonl.gz shard,
        # not on an existing ledger/ directory holding OTHER operator
        # files -- so run_dir pre-existing isn't the only case the
        # cleanup must respect. A failed fit's unconditional rmtree of
        # run_dir/ledger used to destroy such a pre-existing ledger/
        # directory's contents even though nothing was ever appended to
        # it by this failed attempt (Codex review regression: "Preserve
        # pre-existing files when fresh fitting fails").
        class _RefusingModel:
            name = "refusing"

            def fit(self, panel, *, seed, step_index=0):
                raise RuntimeError("simulated fit refusal")

        config = _config(panel, n_steps=1, models=("persistence",))
        run_dir = tmp_path / "preexisting_ledger_run"
        run_dir.mkdir()
        (run_dir / "ledger").mkdir()
        (run_dir / "ledger" / "operator_file.txt").write_text("do not delete me")
        with pytest.raises(RuntimeError, match="simulated fit refusal"):
            WalkForwardHarness.fresh(panel, {"refusing": _RefusingModel()}, run_dir, config, log=_quiet)
        assert (run_dir / "ledger" / "operator_file.txt").exists()


class TestPanelFingerprint:
    def test_a_date_shift_with_identical_feature_bytes_changes_the_fingerprint(self, panel):
        # Octopus consumes dates/day_of_week directly (day-of-week
        # embeddings, and the replay buffer resolves samples by date), but
        # the fingerprint only hashed series.features -- an archive
        # correction that shifts historical dates or weekday assignments
        # WITHOUT changing any numeric feature byte (e.g. moving a block
        # of bars between two gaps that both remain clipped at the same
        # length) left the hash unchanged, silently resuming Octopus onto
        # training inputs that no longer match what the checkpoint
        # recorded (Codex review regression: "Fingerprint all persisted
        # model inputs").
        import dataclasses

        ticker = "AAA"
        series = panel.series[ticker]
        as_of_date = str(np.datetime_as_string(panel.calendar[150], unit="D"))
        baseline = harness_module._fingerprint_panel_history(panel, as_of_date)

        shifted_day_of_week = series.day_of_week.copy()
        shifted_day_of_week[0] = (int(shifted_day_of_week[0]) + 1) % 7
        panel.series[ticker] = dataclasses.replace(series, day_of_week=shifted_day_of_week)
        shifted = harness_module._fingerprint_panel_history(panel, as_of_date)
        assert shifted != baseline

        panel.series[ticker] = series  # restore
        shifted_dates = series.dates.copy()
        shifted_dates[0] = shifted_dates[0] - np.timedelta64(1, "D")
        panel.series[ticker] = dataclasses.replace(series, dates=shifted_dates)
        shifted = harness_module._fingerprint_panel_history(panel, as_of_date)
        assert shifted != baseline


class TestCheckpointDurability:
    def test_checkpoint_fsyncs_model_artifacts_not_just_the_state_file(self, panel, tmp_path, monkeypatch):
        # model.save()'s own close()/flush() only reaches the OS page
        # cache, not stable storage -- unlike harness_state.json, model
        # artifact files were never fsynced before checkpoint.tmp is
        # renamed into place and the previous checkpoint deleted. A crash
        # between those two points could then leave harness_state.json
        # (which IS fsynced) durably recording a model-artifact
        # fingerprint whose bytes never reached disk, and the next
        # resume() refuses with no earlier checkpoint left to fall back
        # to (Codex review: "Sync model artifacts before publishing the
        # checkpoint").
        synced_fds = []
        original_fsync = os.fsync

        def _spy_fsync(fd):
            synced_fds.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", _spy_fsync)
        config = _config(panel, n_steps=2)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        synced_fds.clear()  # fresh() itself already checkpoints once
        harness.checkpoint()
        model_dir = tmp_path / "run" / "checkpoint" / "models" / "persistence"
        artifact_files = [path for path in model_dir.iterdir() if path.is_file()]
        assert artifact_files  # sanity: persistence actually wrote something
        # At least one fsync per artifact file, plus the model directory
        # and its parent, plus harness_state.json itself.
        assert len(synced_fds) >= len(artifact_files) + 3

    def test_fsync_dir_syncs_a_directorys_own_entries(self, tmp_path):
        directory = tmp_path / "some_dir"
        directory.mkdir()
        (directory / "a_file.txt").write_text("hello")
        harness_module._fsync_dir(directory)  # must not raise

    def test_fsync_dir_is_a_noop_on_windows(self, tmp_path, monkeypatch):
        # os.open(directory, os.O_RDONLY) is a POSIX-only operation -- on
        # Windows it raises OSError, so every checkpoint used to fail
        # immediately after model fitting on that platform the moment
        # checkpoint() started calling this helper unconditionally for each
        # model directory (Codex review: "Make checkpoint directory syncing
        # portable"). fcntl is None is this module's own established
        # Windows detection (see _acquire_run_lock()/_release_run_lock());
        # simulate it directly rather than actually running on Windows.
        directory = tmp_path / "some_dir"
        directory.mkdir()
        (directory / "a_file.txt").write_text("hello")

        def _os_open_should_not_be_called(*args, **kwargs):
            raise AssertionError("os.open() must not be called for a directory on Windows")

        monkeypatch.setattr(harness_module, "fcntl", None)
        monkeypatch.setattr(harness_module.os, "open", _os_open_should_not_be_called)
        harness_module._fsync_dir(directory)  # must not raise

    def test_checkpoint_fsyncs_the_swap_before_removing_the_fallback(self, panel, tmp_path, monkeypatch):
        # The publish-rename (checkpoint.tmp -> checkpoint/, demoting the
        # old checkpoint/ -> checkpoint.prev/) only mutates run_dir's own
        # directory entries in the OS page cache -- without fsyncing
        # run_dir itself before the now-redundant checkpoint.prev/ fallback
        # is removed, a crash could commit that removal before the renames
        # are durable, leaving recovery with NEITHER a complete new
        # checkpoint/ NOR the checkpoint.prev/ fallback it was supposed to
        # still have. Likewise checkpoint.tmp/'s own entries (harness_
        # state.json, models/) must be synced before it is ever renamed
        # into place (Codex review: "Fsync the checkpoint swap before
        # removing the fallback"). Assert the ORDER directly.
        import shutil
        from pathlib import Path

        config = _config(panel, n_steps=2)
        run_dir = tmp_path / "run"
        harness = WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)

        events = []
        original_fsync_dir = harness_module._fsync_dir
        original_rmtree: Any = shutil.rmtree

        def _spy_fsync_dir(directory):
            directory = Path(directory)
            if directory == run_dir / "checkpoint.tmp":
                events.append("fsync:tmp_dir")
            elif directory == run_dir:
                events.append("fsync:run_dir")
            return original_fsync_dir(directory)

        def _spy_rmtree(path, ignore_errors=False, onexc=None):
            if Path(path) == run_dir / "checkpoint.prev":
                events.append("rmtree:previous_dir")
            # harness.py's real call never passes onexc, so this spy must
            # not forward it unconditionally either: Python 3.11's
            # shutil.rmtree has no onexc parameter at all (added in 3.12,
            # replacing the deprecated onerror) -- passing onexc=None still
            # raises TypeError there, failing this test on every 3.11 CI
            # job regardless of what harness.py itself does.
            if onexc is not None:
                return original_rmtree(path, ignore_errors=ignore_errors, onexc=onexc)
            return original_rmtree(path, ignore_errors=ignore_errors)

        monkeypatch.setattr(harness_module, "_fsync_dir", _spy_fsync_dir)
        monkeypatch.setattr(harness_module.shutil, "rmtree", _spy_rmtree)
        # A SECOND checkpoint: fresh() already published one (no
        # checkpoint.prev/ existed yet to demote), so this call is the one
        # that actually renames the existing checkpoint/ into
        # checkpoint.prev/ and then removes that fallback -- the exact
        # sequence the finding is about.
        harness.checkpoint()

        assert events.count("fsync:tmp_dir") == 1
        assert events.count("fsync:run_dir") == 1
        assert events.count("rmtree:previous_dir") == 1
        assert events.index("fsync:tmp_dir") < events.index("fsync:run_dir") < events.index("rmtree:previous_dir")


class TestRunDirectoryLock:
    def test_resume_refuses_while_another_harness_holds_the_lock(self, panel, tmp_path):
        # Two replay resumes (or a replay resume and a daily-update
        # process) both targeting the same run directory used to be able
        # to open the same checkpoint boundary concurrently -- one
        # process's Ledger.resume() truncating or deleting shards the
        # other just appended, or renaming away the other's
        # checkpoint.tmp mid-swap, corrupting the run (Codex review:
        # "Serialize mutations of each run directory"). A second
        # resume()/fresh() on a run_dir whose first harness is still
        # alive (run() never called, so the lock is still held) must
        # REFUSE immediately instead of racing it.
        config = _config(panel, n_steps=2)
        run_dir = tmp_path / "run"
        first = WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        registry = {"persistence": baseline_model.PersistenceModel}
        with pytest.raises(HarnessError, match="already holds the lock"):
            WalkForwardHarness.resume(panel, registry, run_dir, config, log=_quiet)
        del first  # releases the lock (GC closes its file handle)

    def test_lock_is_released_once_run_completes_so_a_later_resume_succeeds(self, panel, tmp_path):
        # The lock must not outlive the harness's own invocation: once
        # run() returns (any exit reason), a LATER process must be able
        # to resume the same run_dir without being blocked by a lock the
        # first process no longer needs.
        config = _config(panel, n_steps=2)
        run_dir = tmp_path / "run"
        first = WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        assert first.run() == "completed"
        registry = {"persistence": baseline_model.PersistenceModel}
        second = WalkForwardHarness.resume(panel, registry, run_dir, config, log=_quiet)
        assert second.run() == "completed"

    def test_run_with_release_lock_false_keeps_the_lock_held_until_released(self, panel, tmp_path):
        # A caller that still has to publish this run's artifacts (write_report(),
        # a daily CSV/summary) must be able to keep serializing a second
        # process out of the run directory past run()'s own return -- the
        # opposite of the default, where run()'s finally always released
        # immediately (Codex review: "Hold the run lock through artifact
        # publication"). release_lock=False defers that release to an
        # explicit release_lock() call.
        config = _config(panel, n_steps=2)
        run_dir = tmp_path / "run"
        first = WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        assert first.run(release_lock=False) == "completed"
        registry = {"persistence": baseline_model.PersistenceModel}
        with pytest.raises(HarnessError, match="already holds the lock"):
            WalkForwardHarness.resume(panel, registry, run_dir, config, log=_quiet)
        first.release_lock()
        second = WalkForwardHarness.resume(panel, registry, run_dir, config, log=_quiet)
        assert second.run() == "completed"

    def test_fresh_refuses_while_another_process_holds_the_lock_via_checkpoint_prev(self, panel, tmp_path):
        # The lock is acquired even on the REFUSED "already holds a
        # checkpoint" path (checked while the lock is held, closing the
        # window between two racing fresh() calls both observing
        # "doesn't exist yet"); confirm fresh() itself also respects a
        # lock already held by a live harness on the same run_dir.
        config = _config(panel, n_steps=2)
        run_dir = tmp_path / "run"
        first = WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        with pytest.raises(HarnessError, match="already holds the lock"):
            WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        del first


class TestResume:
    def test_resume_equals_uninterrupted(self, panel, tmp_path):
        # Straight-through 12 steps.
        config_full = _config(panel, n_steps=12)
        straight = WalkForwardHarness.fresh(
            panel, _models(config_full), tmp_path / "straight", config_full, log=_quiet
        )
        assert straight.run() == "completed"

        # 6 steps, then a fresh process resumes for 6 more.
        config_half = _config(panel, n_steps=6)
        first = WalkForwardHarness.fresh(panel, _models(config_half), tmp_path / "resumed", config_half, log=_quiet)
        assert first.run() == "completed"
        registry = {"persistence": baseline_model.PersistenceModel}
        second = WalkForwardHarness.resume(panel, registry, tmp_path / "resumed", config_full, log=_quiet)
        assert second.run() == "completed"

        straight_records = _all_records(tmp_path / "straight")
        resumed_records = _all_records(tmp_path / "resumed")
        assert straight_records == resumed_records
        # And the shard bytes themselves are identical (deterministic gzip).
        for kind in ("forecast", "score"):
            straight_shards = sorted((tmp_path / "straight" / "ledger" / "persistence").glob(f"{kind}_*"))
            resumed_shards = sorted((tmp_path / "resumed" / "ledger" / "persistence").glob(f"{kind}_*"))
            assert [path.read_bytes() for path in straight_shards] == [
                path.read_bytes() for path in resumed_shards
            ]

    def test_resume_after_crash_between_append_and_checkpoint(self, panel, tmp_path):
        config_full = _config(panel, n_steps=12)
        config_half = _config(panel, n_steps=6)
        first = WalkForwardHarness.fresh(panel, _models(config_half), tmp_path / "run", config_half, log=_quiet)
        first.run()
        # Crash simulation: two more steps' ledger writes land but the
        # checkpoint never does (bypass run() so no checkpoint fires).
        first.config.end = config_full.end
        first.step(panel.calendar[157])
        first.step(panel.calendar[158])
        registry = {"persistence": baseline_model.PersistenceModel}
        second = WalkForwardHarness.resume(panel, registry, tmp_path / "run", config_full, log=_quiet)
        assert second.cursor == 157  # rolled back to the checkpointed boundary
        assert second.run() == "completed"
        straight = WalkForwardHarness.fresh(
            panel, _models(config_full), tmp_path / "straight", config_full, log=_quiet
        )
        straight.run()
        assert _all_records(tmp_path / "run") == _all_records(tmp_path / "straight")

    def test_resume_with_changed_flags_is_refused(self, panel, tmp_path):
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        changed = dataclasses.replace(config, window_len=32)
        with pytest.raises(HarnessError, match="window_len"):
            WalkForwardHarness.resume(
                panel, {"persistence": baseline_model.PersistenceModel}, tmp_path / "run", changed, log=_quiet
            )

    def test_resume_refuses_a_tampered_model_artifact(self, panel, tmp_path):
        # Model checkpoint files (metadata.json + weights.npz) determine
        # every resumed prediction just as much as the ledger does; only
        # ledger shards were fingerprinted before, so a replaced or
        # bit-flipped model file would resume silently onto different
        # state with no audit signal (Codex review regression:
        # "Fingerprint checkpointed model artifacts").
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        metadata_path = tmp_path / "run" / "checkpoint" / "models" / "persistence" / "metadata.json"
        payload = json.loads(metadata_path.read_bytes())
        payload["residual_window"] = payload["residual_window"] + 1  # tamper: still valid JSON
        metadata_path.write_text(json.dumps(payload))
        registry = {"persistence": baseline_model.PersistenceModel}
        with pytest.raises(HarnessError, match="do not hand-edit or replace checkpointed model files"):
            WalkForwardHarness.resume(panel, registry, tmp_path / "run", config, log=_quiet)

    def test_resume_refuses_when_archive_history_changed_underneath_it(self, panel, tmp_path):
        # Persistence and Octopus restore their EXACT saved state on resume
        # rather than refitting from the panel (only GBM does that, and
        # fingerprints its own training matrix to catch this); an archive
        # correction/backfill to already-processed history -- same ticker
        # set, so _verify_resume_universe()'s ticker-hash misses it -- used
        # to resume them silently onto state that no longer matched what
        # the panel now says those rows are (Codex review regression:
        # "Verify archive contents before restoring model state").
        import dataclasses

        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()

        tampered_features = panel.series["AAA"].features.copy()
        tampered_features[100, 0] += 1.0  # row 100 is well before the checkpoint's cursor (row 154)
        tampered_series = dataclasses.replace(panel.series["AAA"], features=tampered_features)
        tampered_panel = dataclasses.replace(panel, series={**panel.series, "AAA": tampered_series})

        registry = {"persistence": baseline_model.PersistenceModel}
        with pytest.raises(HarnessError, match="panel history"):
            WalkForwardHarness.resume(tampered_panel, registry, tmp_path / "run", config, log=_quiet)

        # A recovery retrain (strict_model_data=False) accepts this as a
        # stepping stone -- it immediately retrains from the current panel
        # anyway, mirroring GBM's training-fingerprint check.
        resumed = WalkForwardHarness.resume(
            tampered_panel, registry, tmp_path / "run", config, log=_quiet, strict_model_data=False
        )
        assert resumed.cursor == harness.cursor

    def test_resume_clamps_cursor_to_configured_start_despite_a_backfilled_pre_start_date(
        self, panel, tmp_path
    ):
        # If the archive is rebuilt with a NEW calendar date inserted
        # between the checkpoint's stored last_processed_date (here, the
        # day just before config.start, from fresh()'s pre-any-step
        # checkpoint) and config.start itself, the naive date-based
        # reanchor would land the resumed cursor on that new date -- BEFORE
        # the frozen prediction horizon (Codex review regression: "Clamp
        # the resumed cursor to the configured start").
        import dataclasses

        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        del harness  # only its pre-any-step checkpoint matters below
        state = json.loads((tmp_path / "run" / "checkpoint" / "harness_state.json").read_bytes())
        assert state["last_processed_date"] == str(np.datetime_as_string(panel.calendar[150], unit="D"))

        start_position = int(np.searchsorted(panel.calendar, np.datetime64(config.start)))
        backfilled_date = panel.calendar[150] + np.timedelta64(12, "h")  # strictly between 150 and start_position
        grown_calendar = np.sort(np.insert(panel.calendar, start_position, backfilled_date))
        grown_panel = dataclasses.replace(panel, calendar=grown_calendar)

        registry = {"persistence": baseline_model.PersistenceModel}
        resumed = WalkForwardHarness.resume(grown_panel, registry, tmp_path / "run", config, log=_quiet)
        assert resumed.cursor >= int(np.searchsorted(grown_calendar, np.datetime64(config.start)))

    def test_resume_without_checkpoint_is_refused(self, panel, tmp_path):
        with pytest.raises(HarnessError, match="no checkpoint"):
            WalkForwardHarness.resume(
                panel, {}, tmp_path / "nothing-here", _config(panel, n_steps=2), log=_quiet
            )

    def test_fresh_refuses_existing_run_dir(self, panel, tmp_path):
        config = _config(panel, n_steps=2)
        WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        with pytest.raises(HarnessError, match="already holds a checkpoint"):
            WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)

    def test_fresh_refuses_when_only_checkpoint_prev_is_recoverable(self, panel, tmp_path):
        # Simulates a crash between checkpoint()'s two renames: checkpoint/
        # renamed away to checkpoint.prev/, but checkpoint.tmp/ never
        # installed as the new checkpoint/. fresh()'s pre-existence check
        # used to look only at checkpoint/, so on a run whose ledger has no
        # shards yet (this fresh() call's own first, zero-step checkpoint)
        # it would proceed -- and its own subsequent checkpoint() call would
        # rmtree the still-recoverable checkpoint.prev/ before writing
        # refit-from-scratch state (Codex review: "Refuse fresh runs when
        # checkpoint.prev is recoverable"). The correct recovery is resume(),
        # which already promotes checkpoint.prev/ back into place.
        run_dir = tmp_path / "run"
        config = _config(panel, n_steps=2)
        WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        assert not any((run_dir / "ledger").rglob("*.jsonl.gz"))  # zero-step: no shards yet
        (run_dir / "checkpoint").rename(run_dir / "checkpoint.prev")
        assert not (run_dir / "checkpoint").exists()
        with pytest.raises(HarnessError, match="already holds a checkpoint"):
            WalkForwardHarness.fresh(panel, _models(config), run_dir, config, log=_quiet)
        assert (run_dir / "checkpoint.prev").exists()  # untouched -- still recoverable via resume()
        registry = {"persistence": baseline_model.PersistenceModel}
        resumed = WalkForwardHarness.resume(panel, registry, run_dir, config, log=_quiet)
        assert (run_dir / "checkpoint").exists()
        assert not (run_dir / "checkpoint.prev").exists()
        assert resumed.run() == "completed"

    def test_interrupt_checkpoints_and_reports(self, panel, tmp_path):
        config = _config(panel, n_steps=12, checkpoint_every=100)

        class InterruptingModel(baseline_model.PersistenceModel):
            def __init__(self, trip):
                super().__init__()
                self._trip = trip
                self._calls = 0

            def predict(self, requests):
                self._calls += 1
                if self._calls == 3:
                    self._trip()
                return super().predict(requests)

        holder = {}
        model = InterruptingModel(lambda: holder["harness"].request_interrupt())
        harness = WalkForwardHarness.fresh(panel, {"persistence": model}, tmp_path / "run", config, log=_quiet)
        holder["harness"] = harness
        assert harness.run() == "interrupted"
        state = json.loads((tmp_path / "run" / "checkpoint" / "harness_state.json").read_bytes())
        assert state["step_index"] == harness.step_index


class TestCircuitBreaker:
    """The production-safe-loop pattern: hard ceilings, pre-flight, loud."""

    def test_step_ceiling_trips_preflight_at_exact_boundary(self, panel, tmp_path):
        lines = []
        config = _config(panel, n_steps=12, max_steps_per_run=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=lines.append)
        assert harness.run() == "budget_tripped"
        # Pre-flight semantics: exactly 4 steps ran; the 5th never started.
        assert harness.step_index == 4
        banner = [line for line in lines if "CIRCUIT BREAKER" in line]
        assert banner, "the trip must be loud even if the caller ignores the status"
        assert any("step_ceiling" in line for line in lines)
        # A checkpoint exists, so the trip is a clean bounded stop.
        state_path = tmp_path / "run" / "checkpoint" / "harness_state.json"
        assert json.loads(state_path.read_bytes())["step_index"] == 4

    def test_wallclock_ceiling_trips_before_the_first_model_fits(self, panel, tmp_path):
        # --max-hours 0 used to be checked from the SECOND model onward
        # only, so a SINGLE-model run (e.g. --model octopus, whose initial
        # fit can run for hours at full-universe scale) still fit that one
        # model unconditionally -- fresh() succeeded and only run()'s own
        # pre-flight check reported the trip, AFTER the entire expensive
        # fit had already run. That contradicted _setup_wallclock_tripped()'s
        # own documented contract ("--max-hours 0 ... should refuse before
        # fitting/loading anything") and the hard-pre-flight-ceiling promise
        # of the whole circuit-breaker design (Codex review: "Check the
        # wall-clock limit before the first model"). fresh() must now REFUSE
        # immediately, before the model's fit() is ever called, and nothing
        # is written.
        config = _config(panel, n_steps=12, max_hours_per_run=0.0)
        with pytest.raises(HarnessError, match="budget exhausted during initial fit setup"):
            WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert not (tmp_path / "run" / "checkpoint").exists()  # REFUSED -- nothing written

    def test_wallclock_budget_includes_setup_time(self, panel, tmp_path):
        config = _config(panel, n_steps=12, max_hours_per_run=1.0)
        with pytest.raises(HarnessError, match="budget exhausted during initial fit setup"):
            WalkForwardHarness.fresh(
                panel, _models(config), tmp_path / "run", config, log=_quiet,
                invocation_started_monotonic=-3601.0,
            )
        assert not (tmp_path / "run" / "checkpoint").exists()

    def test_fresh_refuses_the_second_models_fit_once_setup_budget_is_exhausted(self, panel, tmp_path):
        # A multi-model run used to fit every configured model
        # unconditionally during fresh(), with no budget check at all
        # until run()'s own pre-flight guard -- so an --max-hours ceiling
        # could be blown through entirely during setup before the replay
        # loop ever got a chance to enforce it (Codex review: "Enforce the
        # wall-clock ceiling during model setup"). The check now applies to
        # EVERY model including the first (Codex review: "Check the
        # wall-clock limit before the first model"), so this already-
        # exhausted budget refuses at the very first model here too --
        # still a valid regression for the underlying multi-model exposure,
        # just no longer isolating "the second model specifically."
        config = _config(panel, n_steps=2, models=("persistence", "gbm_quantile"), max_hours_per_run=1.0)
        with pytest.raises(HarnessError, match="budget exhausted during initial fit setup"):
            WalkForwardHarness.fresh(
                panel, _models(config), tmp_path / "run", config, log=_quiet,
                invocation_started_monotonic=-3601.0,
            )
        assert not (tmp_path / "run" / "checkpoint").exists()  # REFUSED -- nothing written

    def test_resume_refuses_the_second_models_load_once_setup_budget_is_exhausted(self, panel, tmp_path):
        config = _config(panel, n_steps=2, models=("persistence", "gbm_quantile"))
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        budgeted = dataclasses.replace(config, max_hours_per_run=1.0)
        registry = {
            "persistence": baseline_model.PersistenceModel,
            "gbm_quantile": baseline_model.GbmQuantileModel,
        }
        with pytest.raises(HarnessError, match="budget exhausted while loading checkpointed models"):
            WalkForwardHarness.resume(
                panel, registry, tmp_path / "run", budgeted, log=_quiet,
                invocation_started_monotonic=-3601.0,
            )

    def test_resume_refuses_the_first_models_load_once_setup_budget_is_exhausted(self, panel, tmp_path):
        # A single-model resume (e.g. a lone GBM refit-on-load) used to
        # exempt index 0 from the wall-clock check for the same reason
        # fresh()'s equivalent carve-out existed -- letting an already-
        # exhausted budget perform the entire load/refit anyway before
        # ever refusing (Codex review: "Check the wall-clock limit before
        # the first model"). Mirrors test_resume_refuses_the_second_
        # models_load_once_setup_budget_is_exhausted above, but with only
        # ONE configured model, so the very first (and only) load attempt
        # must itself refuse.
        config = _config(panel, n_steps=2)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        budgeted = dataclasses.replace(config, max_hours_per_run=1.0)
        registry = {"persistence": baseline_model.PersistenceModel}
        with pytest.raises(HarnessError, match="budget exhausted while loading checkpointed models"):
            WalkForwardHarness.resume(
                panel, registry, tmp_path / "run", budgeted, log=_quiet,
                invocation_started_monotonic=-3601.0,
            )

    def test_tripped_run_resumes_to_identical_ledger(self, panel, tmp_path):
        config_full = _config(panel, n_steps=12)
        straight = WalkForwardHarness.fresh(
            panel, _models(config_full), tmp_path / "straight", config_full, log=_quiet
        )
        assert straight.run() == "completed"
        # Budgeted run: 3 invocations of 4 steps each (budgets are
        # resume-safe, so each invocation may set its own).
        config_budgeted = dataclasses.replace(config_full, max_steps_per_run=4)
        harness = WalkForwardHarness.fresh(
            panel, _models(config_budgeted), tmp_path / "budgeted", config_budgeted, log=_quiet
        )
        assert harness.run() == "budget_tripped"
        registry = {"persistence": baseline_model.PersistenceModel}
        for _invocation in range(2):
            harness = WalkForwardHarness.resume(
                panel, registry, tmp_path / "budgeted", config_budgeted, log=_quiet
            )
            harness.run()
        assert _all_records(tmp_path / "budgeted") == _all_records(tmp_path / "straight")

    def test_unbudgeted_run_never_trips(self, panel, tmp_path):
        config = _config(panel, n_steps=8)  # both budgets default to None
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert harness.run() == "completed"


class TestArchiveRevisionSafety:
    def test_scoring_reanchors_origin_rows_by_date(self, panel, tmp_path):
        # An archive revision shifts row indices between live-mode panel
        # rebuilds; scoring must anchor on the origin DATE, never trust a
        # stale stored index (review regression). Simulate by corrupting the
        # stored index of every outstanding forecast, then stepping.
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[151])
        for entries in harness.outstanding.values():
            for entry in entries:
                entry["origin_row"] += 3  # stale index after a fake revision
        result = harness.step(panel.calendar[152])
        assert result["scored"] > 0
        records = _all_records(tmp_path / "run")["score"]
        assert records
        for record in records:
            # Every score's actuals match its claimed origin date exactly.
            series = panel.series[record["ticker"]]
            position = int(np.flatnonzero(series.dates == np.datetime64(record["origin_date"]))[0])
            expected = {
                component: float(series.targets[position][slot])
                for slot, component in enumerate(("open", "high", "low", "close"))
            }
            assert record["actual"] == expected

    def test_score_rebases_actual_onto_the_forecasts_stored_reference_close(self, panel, tmp_path):
        # An archive correction can change origin_row's CLOSE (e.g. a data
        # fix) between when a forecast was made and when it matures.
        # series.targets is always relative to the CURRENT ref_close, while
        # the forecast's point/quantiles are fixed relative to the STORED
        # entry["ref_close"]. Comparing them directly on different
        # reference bases would report a miss for an exact forecast (Codex
        # review regression: "Score revised bars against the forecast's
        # stored reference").
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = int(np.flatnonzero(series.dates == panel.calendar[151])[0])
        true_target = series.targets[origin_row]  # relative to the panel's CURRENT ref_close
        current_ref_close = float(series.ref_close[origin_row])
        stale_ref_close = current_ref_close * 1.05  # simulate a +5% revision to this row's close since prediction
        # A forecast that PERFECTLY predicts the outcome, expressed relative
        # to the STALE (pre-revision) reference close it was made against.
        rebase = float(np.log(current_ref_close / stale_ref_close))
        point = {
            component: float(true_target[slot]) + rebase
            for slot, component in enumerate(("open", "high", "low", "close"))
        }
        band = {c: {"p10": point[c] - 0.05, "p50": point[c], "p90": point[c] + 0.05} for c in point}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": str(np.datetime_as_string(series.dates[origin_row], unit="D")),
            "point": point,
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": stale_ref_close,
            "adj_ratio": 1.0,
        }
        target_iso = str(np.datetime_as_string(panel.calendar[152], unit="D"))
        harness.outstanding[target_iso] = [fake_entry]
        harness.step(panel.calendar[152])
        records = [
            record
            for record in _all_records(tmp_path / "run")["score"]
            if record["ticker"] == ticker
            and record["origin_date"] == fake_entry["origin_date"]
            and record["point"] == point
        ]
        assert len(records) == 1
        record = records[0]
        # A perfect forecast (once correctly rebased) scores ~zero error.
        for component in ("open", "high", "low", "close"):
            assert record["score"]["abs_err"][component] == pytest.approx(0.0, abs=1e-9)

    def test_score_skips_rebase_when_only_adj_ratio_changed_not_raw_price(self, panel, tmp_path):
        # A newly-discovered dividend/split back-adjusts adj_ratio (and
        # therefore ref_close) uniformly across history WITHOUT touching
        # the raw price. series.targets is already recomputed from the
        # current, correctly-rescaled adjusted prices, so the origin-to-
        # target log-return is invariant to a uniform rescaling of both
        # ends and needs no further correction -- comparing ref_close
        # values alone (ignoring adj_ratio) applied a rebase anyway,
        # double-counting the dividend and scoring an accurate forecast
        # as a miss (Codex review regression: "Rebase against the raw
        # origin price").
        import dataclasses

        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = int(np.flatnonzero(series.dates == panel.calendar[151])[0])
        true_target = series.targets[origin_row]  # relative to the CURRENT (post-dividend) ref_close
        stale_ref_close = float(series.ref_close[origin_row])
        stale_adj_ratio = float(series.adj_ratio[origin_row])

        # Simulate a newly-discovered dividend for this one row: ref_close
        # and adj_ratio both rescale by the SAME factor -- the raw price
        # (ref_close / adj_ratio) is unchanged.
        factor = 0.98
        mutated_ref_close = series.ref_close.copy()
        mutated_adj_ratio = series.adj_ratio.copy()
        mutated_ref_close[origin_row] = stale_ref_close * factor
        mutated_adj_ratio[origin_row] = stale_adj_ratio * factor
        panel.series[ticker] = dataclasses.replace(series, ref_close=mutated_ref_close, adj_ratio=mutated_adj_ratio)

        # A forecast made BEFORE the dividend was discovered, exactly
        # predicting true_target relative to the (pre-rescale) stored
        # reference -- no dividend-driven price move actually happened.
        point = {
            component: float(true_target[slot])
            for slot, component in enumerate(("open", "high", "low", "close"))
        }
        band = {c: {"p10": point[c] - 0.05, "p50": point[c], "p90": point[c] + 0.05} for c in point}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": str(np.datetime_as_string(series.dates[origin_row], unit="D")),
            "point": point,
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": stale_ref_close,
            "adj_ratio": stale_adj_ratio,
        }
        target_iso = str(np.datetime_as_string(panel.calendar[152], unit="D"))
        harness.outstanding[target_iso] = [fake_entry]
        harness.step(panel.calendar[152])
        records = [
            record
            for record in _all_records(tmp_path / "run")["score"]
            if record["ticker"] == ticker
            and record["origin_date"] == fake_entry["origin_date"]
            and record["point"] == point
        ]
        assert len(records) == 1
        record = records[0]
        # A perfect forecast (correctly left un-rebased) scores ~zero error.
        for component in ("open", "high", "low", "close"):
            assert record["score"]["abs_err"][component] == pytest.approx(0.0, abs=1e-9)

    def test_matured_forecast_is_scored_despite_an_intraday_stamped_origin_date(self, panel, tmp_path):
        # entry["origin_date"] is always a DAY-PRECISION string (every
        # writer formats it via np.datetime_as_string(..., unit="D")), but
        # series.dates preserves whatever time-of-day component the
        # archive's own Date column carries -- load_history_csv() does not
        # normalize it. Exact equality between the two used to fail on
        # BOTH the fast-path re-anchor check and its positional fallback
        # search, silently dropping the forecast (no score ledgered, no
        # label reaches online training) instead of scoring it (Codex
        # review regression: "Match forecast origins by calendar day").
        import dataclasses

        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = int(np.flatnonzero(series.dates == panel.calendar[151])[0])
        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))
        point = {c: 0.0 for c in ("open", "high", "low", "close")}
        band = {c: {"p10": -0.05, "p50": 0.0, "p90": 0.05} for c in point}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": origin_date_iso,
            "point": point,
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": float(series.ref_close[origin_row]),
            "adj_ratio": 1.0,
        }
        target_iso = str(np.datetime_as_string(panel.calendar[152], unit="D"))
        harness.outstanding[target_iso] = [fake_entry]

        # Simulate the origin row's own Date column carrying a real
        # time-of-day component, discovered only once the forecast has
        # matured (a live-mode panel rebuild from a freshly-refreshed
        # archive).
        intraday_dates = series.dates.copy()
        intraday_dates[origin_row] = intraday_dates[origin_row] + np.timedelta64(16, "h")
        panel.series[ticker] = dataclasses.replace(series, dates=intraday_dates)

        harness.step(panel.calendar[152])
        records = [
            record
            for record in _all_records(tmp_path / "run")["score"]
            if record["ticker"] == ticker and record["origin_date"] == origin_date_iso
        ]
        assert len(records) == 1  # scored, not silently dropped

    def test_matured_forecast_for_missing_ticker_is_requeued_not_dropped(self, panel, tmp_path):
        # A ticker temporarily missing from a rebuilt daily panel (CSV gap,
        # unreadable, too-short) must not silently lose its matured
        # forecast -- the panel's own non-blocking-missing-ticker contract
        # means it may reappear later (Codex review regression: "Preserve
        # matured forecasts for temporarily missing tickers").
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        target_iso = np.datetime_as_string(panel.calendar[152], unit="D")
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": "GHOST",
            "origin_row": 0,
            "origin_date": str(np.datetime_as_string(panel.calendar[151], unit="D")),
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding[target_iso] = [fake_entry]
        harness.step(panel.calendar[152])
        # GHOST isn't in panel.series at all -- it must be requeued
        # somewhere, not silently dropped, and no score record was written.
        assert any(
            entry["ticker"] == "GHOST" for entries in harness.outstanding.values() for entry in entries
        )
        assert not any(record["ticker"] == "GHOST" for record in _all_records(tmp_path / "run")["score"])

    def test_score_deferred_when_revised_target_is_later_than_the_matured_bucket(self, panel, tmp_path):
        # An archive correction can rebuild a row's label_end_date LATER
        # than the bucket its forecast was originally filed under (e.g. a
        # backfilled historical row shifting the next-trading-day
        # alignment). Scoring immediately from the stale bucket would train
        # every model on a day the walk-forward cursor hasn't reached yet --
        # lookahead across the whole market (Codex review regression:
        # "Defer scores until a revised target date is reached"). Simulate
        # the mismatch directly, without touching the panel: file an entry
        # whose origin row's REAL label_end_date is calendar[154] under the
        # calendar[152] bucket -- an earlier, stale key.
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = int(np.flatnonzero(series.dates == panel.calendar[153])[0])
        real_target = str(np.datetime_as_string(panel.calendar[154], unit="D"))
        assert np.datetime_as_string(series.label_end_dates[origin_row], unit="D") == real_target
        stale_bucket = str(np.datetime_as_string(panel.calendar[152], unit="D"))
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": str(np.datetime_as_string(series.dates[origin_row], unit="D")),
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding[stale_bucket] = [fake_entry]
        harness.step(panel.calendar[152])
        # Not scored yet -- the entry's real target (154) hasn't been reached.
        assert not any(
            record["ticker"] == ticker and record["origin_date"] == fake_entry["origin_date"]
            for record in _all_records(tmp_path / "run")["score"]
        )
        # Requeued under its REAL target date, not left in the stale bucket.
        assert stale_bucket not in harness.outstanding
        assert any(entry["ticker"] == ticker for entry in harness.outstanding.get(real_target, []))
        harness.step(panel.calendar[153])
        harness.step(panel.calendar[154])
        # AAA is a normal always-eligible ticker, so the harness's OWN
        # predict phase also naturally forecasts (and later scores) this
        # exact origin row -- filter on the fake entry's distinctive
        # all-zero point (a real persistence forecast essentially never
        # lands on exactly that) to isolate our injected entry from it.
        scored = [
            record
            for record in _all_records(tmp_path / "run")["score"]
            if record["ticker"] == ticker
            and record["origin_date"] == fake_entry["origin_date"]
            and record["point"] == fake_entry["point"]
        ]
        assert len(scored) == 1
        assert scored[0]["target_date"] == real_target

    def test_score_retains_unresolved_when_revised_target_is_unavailable(self, panel, tmp_path):
        # An archive correction can temporarily remove a matured forecast's
        # next row while leaving the origin row intact: label_end_date
        # becomes NaT and the target non-finite. Dropping the forecast
        # outright would discard it permanently even though a later rebuild
        # can restore the target -- it must instead be retained as
        # unresolved, the same way a temporarily-missing ticker is (Codex
        # review regression: "Retain forecasts while their revised target
        # is unavailable").
        config = _config(panel, n_steps=4)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = int(np.flatnonzero(series.dates == panel.calendar[151])[0])
        bucket = str(np.datetime_as_string(panel.calendar[152], unit="D"))
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": str(np.datetime_as_string(series.dates[origin_row], unit="D")),
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding[bucket] = [fake_entry]
        # Simulate the archive correction: this row's next trading row is
        # temporarily gone, so its target/label are invalidated in place.
        original_target = series.targets[origin_row].copy()
        original_label_end = series.label_end_dates[origin_row].copy()
        series.targets[origin_row] = np.nan
        series.label_end_dates[origin_row] = np.datetime64("NaT")
        harness.step(panel.calendar[152])
        # Not scored...
        assert not any(
            record["ticker"] == ticker and record["origin_date"] == fake_entry["origin_date"]
            for record in _all_records(tmp_path / "run")["score"]
        )
        # ...but not dropped either -- retained under the unresolved key.
        unresolved = harness.outstanding.get(harness_module.UNRESOLVED_KEY, [])
        assert any(
            entry["ticker"] == ticker and entry["origin_date"] == fake_entry["origin_date"] for entry in unresolved
        )
        # Once the correction is reversed, rebind_unresolved() picks it back up.
        series.targets[origin_row] = original_target
        series.label_end_dates[origin_row] = original_label_end
        rebound = harness.rebind_unresolved()
        assert rebound >= 1
        assert not any(
            entry["ticker"] == ticker and entry["origin_date"] == fake_entry["origin_date"]
            for entry in harness.outstanding.get(harness_module.UNRESOLVED_KEY, [])
        )


class TestPendingRevealedSafety:
    def test_queued_updates_reanchor_by_date_after_index_corruption(self, panel, tmp_path):
        # pending_revealed can span multiple daily invocations when
        # update_every > 1; a checkpointed integer row index a later archive
        # revision invalidates must not be trusted blindly, or make_revealed
        # trains on (or crashes on) the wrong row (Codex review regression:
        # "Re-anchor queued updates after rebuilding the panel").
        seen_dates: list[list[str]] = []

        class SpyPersistence(baseline_model.PersistenceModel):
            def update(self, revealed, *, step_index):
                seen_dates.append(sorted(day.origin_date for day in revealed))
                return super().update(revealed, step_index=step_index)

        config = _config(panel, n_steps=6, update_every=2)
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": SpyPersistence()}, tmp_path / "run", config, log=_quiet
        )
        harness.step(panel.calendar[151])  # first predictions only, nothing matures yet
        harness.step(panel.calendar[152])  # reveals queue into pending_revealed, not yet flushed
        assert harness.pending_revealed
        expected_dates = sorted(triple[2] for triple in harness.pending_revealed)
        # Corrupt the stored integer row indices the way an archive revision
        # would -- wildly out of bounds without date re-anchoring.
        for triple in harness.pending_revealed:
            triple[1] = 99_999
        harness.step(panel.calendar[153])  # flushes the queued update this step; step 153's own reveals join too
        assert seen_dates
        assert set(expected_dates).issubset(seen_dates[-1])

    def test_missing_ticker_reveal_stays_queued_not_dropped(self, synthetic_history_dir, panel, tmp_path):
        # A ticker temporarily missing from a rebuilt panel must not
        # silently lose its queued online update at the next flush cadence
        # -- keep it pending for a later attempt instead (Codex review
        # regression: "Retain queued reveals for temporarily missing
        # tickers"). Swaps in a panel that consistently excludes the
        # ticker everywhere (series/date_index/tickers), the way a real
        # daily rebuild would, rather than partially mutating one dict.
        config = _config(panel, n_steps=4, update_every=1)  # flush every step
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(panel.series[ticker].dates[origin_row], unit="D"))
        harness.pending_revealed = [[ticker, origin_row, origin_date_iso]]
        reduced = features.build_panel(
            synthetic_history_dir,
            tickers=["BBB", "CCC", "DDD", "ZZZ"],
            min_history=60,
            initial_train_end=pd.Timestamp(panel.calendar[150]),
        )
        harness.set_panel(reduced)
        harness.step(reduced.calendar[151])
        assert any(triple[0] == ticker for triple in harness.pending_revealed)

    def test_pending_reveal_with_invalidated_target_is_retained_not_corrupted(self, panel, tmp_path):
        # An archive revision between queuing and this flush can remove or
        # invalidate the row that WAS this origin's next trading day (NaN
        # targets, NaT label_end_date) while the origin day itself still
        # exists (so the re-anchor-by-date lookup succeeds). Feeding that
        # straight into make_revealed() would hand the model a NaN residual
        # (Persistence) or a NaT-dated training row (GBM) instead of
        # retaining the entry until its target row returns (Codex review
        # regression: "Validate queued reveals after rebuilding the
        # panel").
        seen_dates: list[list[str]] = []

        class SpyPersistence(baseline_model.PersistenceModel):
            def update(self, revealed, *, step_index):
                seen_dates.append([day.origin_date for day in revealed])
                return super().update(revealed, step_index=step_index)

        config = _config(panel, n_steps=4, update_every=1)  # flush every step
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": SpyPersistence()}, tmp_path / "run", config, log=_quiet
        )
        ticker = "AAA"
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(panel.series[ticker].dates[origin_row], unit="D"))
        harness.pending_revealed = [[ticker, origin_row, origin_date_iso]]
        panel.series[ticker].targets[origin_row] = np.nan
        panel.series[ticker].label_end_dates[origin_row] = np.datetime64("NaT")
        harness.step(panel.calendar[151])
        # Retained for a later attempt, not silently dropped or corrupted...
        assert any(triple[0] == ticker and triple[1] == origin_row for triple in harness.pending_revealed)
        # ...and never handed to the model as a training day.
        assert not any(origin_date_iso in batch for batch in seen_dates)

    def test_update_is_skipped_when_no_queued_reveal_is_usable(self, panel, tmp_path):
        # pending_revealed can be nonempty while EVERY queued entry is
        # temporarily unusable (a missing ticker, a not-yet-finite target);
        # calling update([]) in that case still made Octopus take gradient
        # steps from its existing recent/replay buffers, so training
        # depended on transient archive availability instead of on what was
        # actually revealed (Codex review regression: "Skip model updates
        # when no reveal is usable"). A spy proves update() is not called
        # AT ALL, not just called with an empty list.
        call_count = 0

        class SpyPersistence(baseline_model.PersistenceModel):
            def update(self, revealed, *, step_index):
                nonlocal call_count
                call_count += 1
                return super().update(revealed, step_index=step_index)

        config = _config(panel, n_steps=4, update_every=1)  # flush every step
        harness = WalkForwardHarness.fresh(
            panel, {"persistence": SpyPersistence()}, tmp_path / "run", config, log=_quiet
        )
        ticker = "AAA"
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(panel.series[ticker].dates[origin_row], unit="D"))
        harness.pending_revealed = [[ticker, origin_row, origin_date_iso]]
        # Isolates this test's intent (ONLY the one hand-crafted, invalid
        # entry pending) from fresh()'s own ragged-boundary seeding, which
        # now lives in a separate date-gated bucket and would otherwise
        # flow a second, valid entry into pending_revealed at this same
        # step -- see _pending_seed_reveals's docstring.
        harness._pending_seed_reveals = {}
        panel.series[ticker].targets[origin_row] = np.nan
        panel.series[ticker].label_end_dates[origin_row] = np.datetime64("NaT")
        harness.step(panel.calendar[151])
        assert call_count == 0
        assert any(triple[0] == ticker and triple[1] == origin_row for triple in harness.pending_revealed)

    def test_rebind_scores_immediately_dedupes_across_models(self, panel, tmp_path, monkeypatch):
        # One outstanding entry exists per MODEL for the same (ticker,
        # origin_row); the immediate-scoring path must dedupe before
        # queuing the next online update, or every configured model
        # absorbs the same reveal once PER model (Codex review regression:
        # "Deduplicate reveals scored after the calendar is exhausted").
        config = _config(panel, n_steps=6, models=("persistence", "gbm_quantile"))
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[151])  # genuine, fully-populated outstanding entries for BOTH models
        target_key = next(key for key in harness.outstanding if key)
        entries = harness.outstanding.pop(target_key)
        assert len({entry["model"] for entry in entries}) == 2  # sanity: one entry per model
        harness.cursor = len(panel.calendar)
        harness.outstanding[harness_module.UNRESOLVED_KEY] = entries
        # A ticker not yet eligible at row 151 (e.g. CCC, still warming up)
        # but eligible by the panel's TRUE terminal row would otherwise look
        # "newly admitted" to rebind_unresolved()'s separate terminal-row
        # check (Codex review: "Treat newly admitted terminal rows as
        # pending work") -- irrelevant to the dedup behavior under test here.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])

        # rebind_unresolved() now flushes pending_revealed immediately
        # (Codex review: "Update models before forecasting from recovered
        # rows"), so the dedup must be verified through the actual
        # update() call each model receives, not through pending_revealed
        # (which is empty again by the time this returns).
        update_calls: dict[str, list[list[str]]] = {"persistence": [], "gbm_quantile": []}
        for name in update_calls:
            original = harness.models[name].update

            def _spy_update(revealed, *, step_index, _name=name, _original=original):
                update_calls[_name].append([day.ticker for day in revealed])
                return _original(revealed, step_index=step_index)

            harness.models[name].update = _spy_update

        assert harness.rebind_unresolved() == len(entries)
        ticker = entries[0]["ticker"]
        for name in update_calls:
            # Each recovery WAVE may legitimately reveal this ticker again --
            # the drain loop (Codex review: "Drain every recovered row
            # behind the cursor") keeps walking a lagging ticker forward
            # day by day through its whole backlog, one update() call per
            # wave -- but WITHIN any single wave/update() call the two
            # models' entries for the SAME (ticker, origin_row) must still
            # collapse to one, not two.
            assert all(call.count(ticker) <= 1 for call in update_calls[name])
            assert any(ticker in call for call in update_calls[name])


class TestLiveHelpers:
    def test_rebind_unresolved_after_panel_extension(self, synthetic_history_dir, tmp_path):
        # Build a truncated panel (as of ~80% through), run to its end so the
        # tail forecasts are unresolved, then extend the panel and rebind.
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        as_of = pd.Timestamp(probe.calendar[210])
        truncated = features.build_panel(
            synthetic_history_dir, min_history=60, as_of=as_of, initial_train_end=pd.Timestamp(probe.calendar[150])
        )
        config = _config(truncated, n_steps=None)
        harness = WalkForwardHarness.fresh(
            panel=truncated, models=_models(config), run_dir=tmp_path / "run", config=config, log=_quiet
        )
        harness.run()
        assert harness.outstanding.get(harness_module.UNRESOLVED_KEY)
        full = features.build_panel(
            synthetic_history_dir, min_history=60, initial_train_end=pd.Timestamp(probe.calendar[150])
        )
        harness.set_panel(full)
        rebound = harness.rebind_unresolved()
        assert rebound > 0
        assert not harness.outstanding.get(harness_module.UNRESOLVED_KEY)

    def test_rebind_predicts_a_newly_admitted_terminal_row_ticker(self, synthetic_history_dir, tmp_path):
        # A ticker can join the CURRENT panel's terminal row eligible for
        # its first-ever forecast without any NEW calendar date to carry it
        # there -- a new IPO's backfilled history crossing min_history, a
        # previously-unreadable CSV being restored, or a capped-universe
        # ranking swap. With no future step() left this invocation to ever
        # surface it to eligible_on()'s predict phase, rebind_unresolved()
        # must forecast it directly, or the ticker's terminal-row forecast
        # is silently skipped forever (Codex review: "Treat newly admitted
        # terminal rows as pending work"). Simulated here as a ticker
        # entirely excluded from the panel's ticker selection at first
        # (same effect as an unreadable/missing CSV), then admitted with
        # the SAME as_of cutoff -- so the calendar itself does not grow,
        # unlike the panel-extension test above.
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        as_of = pd.Timestamp(probe.calendar[220])
        initial_train_end = pd.Timestamp(probe.calendar[150])
        truncated = features.build_panel(
            synthetic_history_dir, tickers=["AAA", "BBB"], min_history=60,
            as_of=as_of, initial_train_end=initial_train_end,
        )
        config = _config(truncated, n_steps=None)
        harness = WalkForwardHarness.fresh(
            panel=truncated, models=_models(config), run_dir=tmp_path / "run", config=config, log=_quiet
        )
        harness.run()
        assert harness.cursor >= len(harness.panel.calendar)
        assert not any(
            entry["ticker"] == "CCC" for entry in harness.outstanding.get(harness_module.UNRESOLVED_KEY, [])
        )

        admitted = features.build_panel(
            synthetic_history_dir, tickers=["AAA", "BBB", "CCC"], min_history=60,
            as_of=as_of, initial_train_end=initial_train_end,
        )
        assert len(admitted.calendar) == len(truncated.calendar)  # same cutoff -- no new calendar date
        harness.set_panel(admitted)
        assert harness.cursor >= len(harness.panel.calendar)  # still no calendar step to carry CCC forward

        rebound = harness.rebind_unresolved()
        assert rebound > 0
        ccc_entries = [
            entry for entry in harness.outstanding.get(harness_module.UNRESOLVED_KEY, []) if entry["ticker"] == "CCC"
        ]
        assert ccc_entries  # CCC's first-ever forecast was issued

    def test_rebind_predicts_a_ticker_whose_terminal_row_predates_the_market(self, synthetic_history_dir, tmp_path):
        # The sibling test above admits a ticker whose OWN terminal row sits
        # on the market-wide final date, so eligible_on(terminal_date) finds
        # it. This one covers the case that a "prefer this branch" merge
        # silently reverted and no test caught: a ticker whose restored /
        # newly warmed-up CSV is one or more sessions BEHIND the rest of the
        # archive. eligible_on() keys strictly on the market-wide final
        # date, so such a ticker never appears there even though its own
        # terminal row is eligible and finite -- and with the cursor already
        # exhausted no later replay step revisits it, dropping that first
        # forecast forever (Codex review: "Forecast newly eligible tickers
        # from their own terminal row").
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        as_of = pd.Timestamp(probe.calendar[220])
        initial_train_end = pd.Timestamp(probe.calendar[150])
        truncated = features.build_panel(
            synthetic_history_dir, tickers=["AAA", "BBB"], min_history=60,
            as_of=as_of, initial_train_end=initial_train_end,
        )
        config = _config(truncated, n_steps=None)
        harness = WalkForwardHarness.fresh(
            panel=truncated, models=_models(config), run_dir=tmp_path / "run", config=config, log=_quiet
        )
        harness.run()
        assert harness.cursor >= len(harness.panel.calendar)

        # Shorten CCC's archive by three sessions so its own final row lands
        # strictly before the market-wide one AAA/BBB still define. Done on
        # the CSV (not by splicing the built panel) so the rebuilt panel's
        # calendar/date_index stay internally consistent, exactly as a
        # one-session-behind restored file would be in production.
        # Cut by DATE, not by row count: the as_of cutoff below already trims
        # every ticker's tail, so dropping a few trailing CSV rows would be
        # absorbed by it and leave CCC ending on the market date after all.
        lagging_last_date = pd.Timestamp(probe.calendar[217])
        ccc_csv = synthetic_history_dir / "CCC.csv"
        ccc_frame = pd.read_csv(ccc_csv)
        ccc_frame = ccc_frame[pd.to_datetime(ccc_frame["Date"]) <= lagging_last_date]
        ccc_frame.to_csv(ccc_csv, index=False)
        admitted = features.build_panel(
            synthetic_history_dir, tickers=["AAA", "BBB", "CCC"], min_history=60,
            as_of=as_of, initial_train_end=initial_train_end,
        )
        assert admitted.series["CCC"].dates[-1] < admitted.calendar[-1]  # genuinely lagging
        harness.set_panel(admitted)
        assert harness.cursor >= len(harness.panel.calendar)

        harness.rebind_unresolved()
        ccc_entries = [
            entry for entry in harness.outstanding.get(harness_module.UNRESOLVED_KEY, []) if entry["ticker"] == "CCC"
        ]
        assert ccc_entries  # forecast from CCC's OWN terminal row, not the market's

    def test_rebind_reforecasts_a_ticker_whose_dropped_entry_is_still_eligible(self, panel, tmp_path, monkeypatch):
        # The terminal-row check's `already_forecast` set used to be built
        # from the ORIGINAL list popped from UNRESOLVED_KEY, not the
        # CURRENT state -- so a ticker whose only entry the drop branch
        # (deleted origin row) just discarded still looked "already
        # forecasted" here too and was wrongly skipped, even though it now
        # has NO forecast at all and remains eligible on the panel's
        # (shifted) terminal row (Codex review: "Derive terminal coverage
        # from surviving forecasts").
        import dataclasses

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        deleted_row = 100
        deleted_date_iso = str(np.datetime_as_string(series.dates[deleted_row], unit="D"))
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": deleted_row,
            "origin_date": deleted_date_iso,
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [fake_entry]

        kept = np.ones(len(series.dates), dtype=bool)
        kept[deleted_row] = False
        shrunk = dataclasses.replace(
            series,
            dates=series.dates[kept],
            day_of_week=series.day_of_week[kept],
            features=series.features[kept],
            targets=series.targets[kept],
            label_end_dates=series.label_end_dates[kept],
            ref_close=series.ref_close[kept],
            adj_ratio=series.adj_ratio[kept],
        )
        panel.series[ticker] = shrunk
        harness.cursor = len(harness.panel.calendar)
        new_terminal_row = len(shrunk.dates) - 1
        # panel.date_index is stale after the in-place series replacement
        # above (built once at panel-construction time); bypass it so this
        # test exercises rebind_unresolved()'s own already_forecast logic
        # directly, not date_index's unrelated staleness.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [(ticker, new_terminal_row)])

        rebound = harness.rebind_unresolved()
        assert rebound >= 1
        aaa_entries = [
            entry for entry in harness.outstanding.get(harness_module.UNRESOLVED_KEY, []) if entry["ticker"] == ticker
        ]
        assert len(aaa_entries) == 1  # got a fresh forecast, not silently skipped forever
        assert aaa_entries[0]["origin_row"] == new_terminal_row

    def test_rebind_terminal_check_does_not_duplicate_a_just_matured_forecast(self, panel, tmp_path, monkeypatch):
        # Conversely: a ticker whose forecast matures via the DATED-bucket
        # sweep (not UNRESOLVED_KEY) and gets a FRESH terminal forecast
        # from the recovered-rows wave was never in the list originally
        # popped from UNRESOLVED_KEY -- so the OLD already_forecast set
        # didn't know about it either, and the terminal-row check predicted
        # a SECOND, duplicate forecast for the exact same row the wave just
        # issued one for (Codex review: "Derive terminal coverage from
        # surviving forecasts").
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[-2])  # a genuine, fully-populated outstanding entry
        target_key = next(key for key in harness.outstanding if key)
        entry = harness.outstanding[target_key].pop()
        del harness.outstanding[target_key]
        harness.cursor = len(panel.calendar)
        ticker = entry["ticker"]
        origin_row = entry["origin_row"]
        # Simulate a restored dated bucket: step()'s own missing-ticker
        # branch re-queues a matured forecast under its UNCHANGED dated
        # key when the ticker was temporarily absent that day.
        harness.outstanding[target_key] = [entry]
        monkeypatch.setattr(panel, "eligible_on", lambda date: [(ticker, origin_row + 1)])

        rebound = harness.rebind_unresolved()
        assert rebound >= 1
        matching = [
            queued for queued in harness.outstanding.get(harness_module.UNRESOLVED_KEY, [])
            if queued["ticker"] == ticker
        ]
        assert len(matching) == 1  # exactly one terminal forecast, not a duplicate
        assert matching[0]["origin_row"] == origin_row + 1

    def test_rebind_unresolved_matches_despite_an_intraday_stamped_origin_date(self, panel, tmp_path):
        # entry["origin_date"] is always day-precision, but series.dates
        # could carry an intraday timestamp on a hand-built panel
        # (load_history_csv() now normalizes at ingestion, but this is
        # defense in depth) -- exact equality used to leave the forecast
        # stuck under UNRESOLVED_KEY forever (Codex review regression:
        # "Rebind live forecasts by calendar day").
        import dataclasses

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": origin_date_iso,
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [fake_entry]

        intraday_dates = series.dates.copy()
        intraday_dates[origin_row] = intraday_dates[origin_row] + np.timedelta64(16, "h")
        panel.series[ticker] = dataclasses.replace(series, dates=intraday_dates)

        rebound = harness.rebind_unresolved()
        assert rebound == 1
        assert not harness.outstanding.get(harness_module.UNRESOLVED_KEY)

    def test_rebind_drops_an_unresolved_entry_whose_origin_row_was_deleted(self, panel, tmp_path):
        # An archive correction can delete a specific row (e.g. a bad bar
        # struck from the CSV) while the ticker itself remains in the
        # panel. Before this fix, rebind_unresolved() could not tell that
        # apart from "origin row still exists, target merely unresolved"
        # and retained the entry forever under UNRESOLVED_KEY --
        # _score_one() already treats the identical missing-origin-row case
        # as a drop, and because the TICKER stays present,
        # _current_universe_unresolved() (daily_update.py) would keep
        # exporting the permanently-stale entry alongside every newer
        # forecast issued for that ticker (Codex review: "Drop unresolved
        # forecasts whose origin row was deleted").
        import dataclasses

        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        deleted_row = 100
        deleted_date_iso = str(np.datetime_as_string(series.dates[deleted_row], unit="D"))
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": deleted_row,
            "origin_date": deleted_date_iso,
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [fake_entry]

        # Simulate the archive correction: the ticker stays in the panel,
        # but its row at deleted_row is gone -- no row's date equals
        # deleted_date_iso any more.
        kept = np.ones(len(series.dates), dtype=bool)
        kept[deleted_row] = False
        panel.series[ticker] = dataclasses.replace(
            series,
            dates=series.dates[kept],
            day_of_week=series.day_of_week[kept],
            features=series.features[kept],
            targets=series.targets[kept],
            label_end_dates=series.label_end_dates[kept],
            ref_close=series.ref_close[kept],
            adj_ratio=series.adj_ratio[kept],
        )

        rebound = harness.rebind_unresolved()
        assert rebound == 1  # the drop is a mutation of self.outstanding -- must be checkpointed
        assert not harness.outstanding.get(harness_module.UNRESOLVED_KEY)

    def test_rebind_drains_multiple_recovered_rows_behind_an_exhausted_cursor(self, panel, tmp_path, monkeypatch):
        # A lagging ticker whose archive backfills TWO OR MORE missed rows
        # at once used to have its recovery-predict path stop after
        # exactly one row: the forecast made from origin_row + 1 already
        # has a KNOWN, overdue target (origin_row + 2's date -- the cursor
        # stays exhausted this whole call, so that date is necessarily <=
        # the panel's last date), so it landed in a DATED outstanding
        # bucket that no step() this invocation -- and, if the calendar
        # doesn't grow by the next one either, no daily_update.py NOOP
        # shortcut (which only counts pending UNRESOLVED rebinds) -- would
        # ever pop, stranding the ticker mid-recovery (Codex review:
        # "Drain every recovered row behind the cursor"). rebind_unresolved()
        # must keep predicting and scoring until the ticker actually
        # reaches its currently-unresolved final row.
        config = _config(panel, n_steps=1)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        ticker = "AAA"
        series = panel.series[ticker]
        final_row = len(series.dates) - 1
        origin_row = final_row - 3  # three recovered rows behind the true end
        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        fake_entry = {
            "model": "persistence",
            "ticker": ticker,
            "origin_row": origin_row,
            "origin_date": origin_date_iso,
            "point": {c: 0.0 for c in ("open", "high", "low", "close")},
            "quantiles": band,
            "quantiles_raw": band,
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.cursor = len(panel.calendar)  # exhausted: no step() will run again
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [fake_entry]
        # Every OTHER panel ticker would otherwise look "newly admitted" to
        # rebind_unresolved()'s separate terminal-row check (Codex review:
        # "Treat newly admitted terminal rows as pending work") -- this
        # test's single hand-placed fake_entry is the only real outstanding
        # state here, unlike a real run where step() already predicted every
        # eligible ticker. Suppress that separate path to keep this test
        # isolated to the multi-wave drain behavior it's actually testing.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])

        assert harness.rebind_unresolved() == 1
        remaining = [
            queued
            for entries in harness.outstanding.values()
            for queued in entries
            if queued["ticker"] == ticker
        ]
        # Every intermediate recovered row was scored and superseded by the
        # next; only the ticker's true final row is left outstanding, and
        # it is parked under UNRESOLVED_KEY (its own target isn't known
        # yet) rather than stranded partway through the chain.
        assert [queued["origin_row"] for queued in remaining] == [final_row]
        assert harness_module.UNRESOLVED_KEY in harness.outstanding
        assert harness.outstanding[harness_module.UNRESOLVED_KEY][0]["origin_row"] == final_row

        records = _all_records(tmp_path / "run")
        scored_dates = {record["origin_date"] for record in records["score"] if record["ticker"] == ticker}
        expected_scored_dates = {
            str(np.datetime_as_string(series.dates[row], unit="D")) for row in range(origin_row, final_row)
        }
        assert expected_scored_dates.issubset(scored_dates)
        forecast_dates = {record["origin_date"] for record in records["forecast"] if record["ticker"] == ticker}
        expected_forecast_dates = {
            str(np.datetime_as_string(series.dates[row], unit="D")) for row in range(origin_row + 1, final_row + 1)
        }
        assert expected_forecast_dates.issubset(forecast_dates)

    def test_rebind_past_target_matures_at_current_cursor(self, panel, tmp_path):
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        origin_row = 151
        harness.cursor = 155
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [{
            "ticker": "AAA", "origin_date": str(panel.series["AAA"].dates[origin_row]),
            "origin_row": origin_row, "model": "persistence",
        }]
        assert harness.rebind_unresolved() == 1
        cursor_iso = np.datetime_as_string(panel.calendar[harness.cursor], unit="D")
        assert harness.outstanding[cursor_iso]

    def test_rebind_scores_immediately_when_calendar_is_exhausted(self, panel, tmp_path, monkeypatch):
        # If the global calendar is already fully processed (cursor ==
        # len(calendar)), no future step() call will ever pop a parked
        # bucket again -- a late-discovered target must be scored right
        # here instead of stranding it forever (Codex review regression:
        # "Score rebounds when no calendar step remains").
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        # Near the panel's last date, not row 151: every ticker's series
        # ends at the same final calendar date (see conftest), so stepping
        # here means the recovered row's OWN next row is each ticker's true
        # final row -- a single recovery hop, in isolation from the
        # multi-wave drain loop covered by TestRecoveryDrain below.
        harness.step(panel.calendar[-2])  # a genuine, fully-populated outstanding entry
        target_key = next(key for key in harness.outstanding if key)
        entry = harness.outstanding[target_key].pop()
        # Clear the rest of this dated bucket too -- rebind_unresolved()
        # now ALSO sweeps scoreable dated buckets once the calendar is
        # exhausted (see TestRecoveryDrain's own coverage of that), and
        # this test isolates the SEPARATE "no calendar step remains"
        # UNRESOLVED-entry path it's actually testing.
        del harness.outstanding[target_key]
        harness.cursor = len(panel.calendar)
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [entry]
        # rebind_unresolved() now ALSO forecasts any OTHER ticker eligible
        # on the panel's terminal row with no outstanding entry of its own
        # (Codex review: "Treat newly admitted terminal rows as pending
        # work") -- in REAL operation every such ticker already has one
        # (step() predicts every eligible ticker every day), but this
        # test's single hand-placed `entry` above is the only one, so
        # every OTHER ticker would otherwise look "newly admitted" too.
        # Suppress that separate path to keep this test isolated to the
        # single UNRESOLVED-entry path it's actually testing.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])

        update_calls: list[list[str]] = []
        original_update = harness.models["persistence"].update

        def _spy_update(revealed, *, step_index):
            update_calls.append([day.ticker for day in revealed])
            return original_update(revealed, step_index=step_index)

        harness.models["persistence"].update = _spy_update

        assert harness.rebind_unresolved() == 1
        # The scored entry itself isn't parked anywhere -- there is no
        # cursor bucket left to park it under. But the row that just
        # matured is now the ticker's newest usable row, and its own date
        # is behind the (exhausted) cursor, so step() can never predict
        # from it again on its own -- rebind_unresolved() must queue a
        # fresh forecast for it here instead (Codex review: "Predict from
        # ticker rows recovered behind the cursor").
        remaining = [
            queued
            for entries in harness.outstanding.values()
            for queued in entries
            if queued["ticker"] == entry["ticker"]
        ]
        assert all(queued["origin_row"] != entry["origin_row"] for queued in remaining)
        assert any(queued["origin_row"] == entry["origin_row"] + 1 for queued in remaining)
        records = _all_records(tmp_path / "run")["score"]
        assert any(
            record["ticker"] == entry["ticker"] and record["origin_date"] == entry["origin_date"]
            for record in records
        )
        recovered_series = panel.series[entry["ticker"]]
        recovered_origin_date = str(
            np.datetime_as_string(recovered_series.dates[entry["origin_row"] + 1], unit="D")
        )
        forecast_records = _all_records(tmp_path / "run")["forecast"]
        assert any(
            record["ticker"] == entry["ticker"] and record["origin_date"] == recovered_origin_date
            for record in forecast_records
        )
        # Flushed immediately (not left queued for a later step()'s
        # cadence): the recovered row's own reveal is incorporated into
        # the model BEFORE this same call predicts from it, unlike a
        # normal cadence-skipped day which still flushes on a later step
        # (Codex review: "Update models before forecasting from recovered
        # rows").
        assert not harness.pending_revealed
        assert any(entry["ticker"] in tickers for tickers in update_calls)

    def test_rebind_scores_dated_bucket_entries_when_calendar_is_exhausted(self, panel, tmp_path):
        # step()'s own missing-ticker branch re-queues a matured forecast
        # under its UNCHANGED dated bucket key -- not UNRESOLVED_KEY --
        # when the ticker is temporarily absent that day. A future step()
        # would naturally re-sweep it (matured_keys pops every key <=
        # that step's date), but once the cursor has no calendar step
        # left, nothing else ever will, permanently understating what
        # daily_update.py's NOOP check reports and stranding the
        # forecast at least one extra day (Codex review: "Process
        # restored dated forecasts before NOOP").
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[-2])  # populates a genuine dated outstanding bucket
        target_key = next(key for key in harness.outstanding if key)
        pending_tickers = {entry["ticker"] for entry in harness.outstanding[target_key]}
        assert pending_tickers  # sanity: something genuinely queued
        harness.cursor = len(panel.calendar)  # no calendar step remains this invocation
        rebound = harness.rebind_unresolved()
        assert rebound >= len(pending_tickers)
        assert target_key not in harness.outstanding  # fully drained
        records = _all_records(tmp_path / "run")["score"]
        scored_tickers = {record["ticker"] for record in records if record["target_date"] == target_key}
        assert scored_tickers == pending_tickers

    def test_rebind_past_target_scoring_keeps_its_own_target_date(self, panel, tmp_path):
        # A forecast rebound behind the cursor is PARKED under the cursor's
        # date so step() can reach it, but the SCORE it produces must record
        # its own real target date, not the cursor date it was rescheduled
        # under (Codex review regression: "Preserve the actual target date
        # for late scores").
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[151])  # a genuine, fully-populated outstanding entry
        target_key = next(key for key in harness.outstanding if key)
        entry = harness.outstanding[target_key].pop()
        if not harness.outstanding[target_key]:
            del harness.outstanding[target_key]
        real_target_iso = target_key
        harness.cursor = 155
        harness.outstanding[harness_module.UNRESOLVED_KEY] = [entry]
        assert harness.rebind_unresolved() == 1
        cursor_iso = np.datetime_as_string(panel.calendar[harness.cursor], unit="D")
        assert real_target_iso < cursor_iso  # sanity: genuinely behind the cursor
        assert cursor_iso in harness.outstanding  # parked under the cursor, not its own date
        result = harness.step(panel.calendar[harness.cursor])
        assert result["scored"] >= 1
        records = _all_records(tmp_path / "run")["score"]
        matches = [
            record
            for record in records
            if record["ticker"] == entry["ticker"] and record["origin_date"] == entry["origin_date"]
        ]
        assert matches
        assert matches[-1]["target_date"] == real_target_iso
        assert matches[-1]["target_date"] != cursor_iso

    def test_retrain_refits_models_and_keeps_scored_history(self, panel, tmp_path):
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        assert harness.outstanding  # sanity: something genuinely pending
        before = _all_records(tmp_path / "run")
        harness.retrain(panel)
        after = _all_records(tmp_path / "run")
        # Already-scored history is never touched by a retrain -- refitting
        # weights does not rewrite the past.
        assert after["score"] == before["score"]
        # The retrain fits fresh weights but leaves EVERY forecast record
        # that was already ledgered (the STALE, pre-retrain guesses) intact
        # -- append-only history -- while regenerating self.outstanding with
        # the just-fitted models appends brand-new forecast records on top
        # (Codex review: "Regenerate the live forecast after recovery
        # retraining"); the old ones are a strict prefix of the new set.
        assert after["forecast"][: len(before["forecast"])] == before["forecast"]
        assert len(after["forecast"]) > len(before["forecast"])

    def test_retrain_ledgers_a_marker_record_per_model(self, panel, tmp_path):
        # diagnostic_health() needs a durable, per-model marker of WHEN the
        # last recovery retrain happened, to bound its baseline window to
        # dates after that point instead of comparing the recovered
        # encoder forever against the ORIGINAL run's pre-recovery baseline
        # (Codex review regression: "Reset encoder-health baseline after
        # recovery retraining").
        config = _config(panel, n_steps=6, models=("persistence",))
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        last_processed = str(np.datetime_as_string(panel.calendar[harness.cursor - 1], unit="D"))
        harness.retrain(panel)
        markers = list(Ledger(tmp_path / "run").iter_records(model="persistence", kind="retrain"))
        assert len(markers) == 1
        assert markers[0]["origin_date"] == last_processed
        assert markers[0]["step_index"] == harness.step_index

    def test_retrain_clears_pending_revealed(self, panel, tmp_path):
        # A recovery retrain's fit() already sees everything revealed so
        # far (the caller re-freezes the fit window at the cursor); rows
        # still queued in pending_revealed would otherwise be absorbed a
        # SECOND time at the next update() cadence, duplicating residuals
        # right after a supposedly clean recovery (Codex review regression:
        # "Clear queued reveals after recovery retraining").
        config = _config(panel, n_steps=6, update_every=100)  # never auto-flushes
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[151])
        harness.step(panel.calendar[152])
        assert harness.pending_revealed
        harness.retrain(panel)
        assert harness.pending_revealed == []

    def test_retrain_preserves_reveals_for_tickers_missing_from_its_own_panel(self, panel, tmp_path):
        # fit() only sees tickers actually PRESENT in the panel passed to
        # retrain() -- a queued reveal for a ticker temporarily missing (a
        # CSV gap/unreadable day) contributes NOTHING to this retrain, so
        # the old unconditional clear discarded it anyway, losing it
        # permanently once the ticker returns and diverging from the
        # normal update() path's deliberate retention of exactly this case
        # (Codex review: "Preserve missing-ticker reveals across recovery
        # retrains").
        config = _config(panel, n_steps=6, update_every=100)  # never auto-flushes
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.step(panel.calendar[151])
        harness.step(panel.calendar[152])
        assert harness.pending_revealed  # sanity: something genuinely queued
        assert all(triple[0] in panel.series for triple in harness.pending_revealed)  # sanity
        harness.pending_revealed.append(["GHOST", 100, "2021-01-04"])  # not a real ticker in `panel`
        harness.retrain(panel)
        remaining_tickers = {triple[0] for triple in harness.pending_revealed}
        assert remaining_tickers == {"GHOST"}

    def test_retrain_regenerates_every_outstanding_forecast(self, panel, tmp_path):
        # Precise version of the "keeps_scored_history" count check above:
        # retrain() must regenerate EXACTLY the (ticker, origin_row) pairs
        # that were pending before it ran -- neither dropping one nor
        # inventing extras -- and the ledger must gain exactly one new
        # forecast record per regenerated entry (single-model config here).
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        pending_before = {
            (entry["ticker"], entry["origin_row"]) for bucket in harness.outstanding.values() for entry in bucket
        }
        assert pending_before  # sanity: something genuinely pending
        before = _all_records(tmp_path / "run")
        harness.retrain(panel)
        after = _all_records(tmp_path / "run")
        assert len(after["forecast"]) == len(before["forecast"]) + len(pending_before)
        pending_after = {
            (entry["ticker"], entry["origin_row"]) for bucket in harness.outstanding.values() for entry in bucket
        }
        assert pending_after == pending_before

    def test_retrain_does_not_duplicate_forecasts_across_models(self, panel, tmp_path):
        # self.outstanding buckets hold ONE ENTRY PER MODEL for the same
        # (ticker, origin_date) -- naively building one regeneration request
        # per ENTRY (instead of per unique ticker) queues the same target
        # once per model, and _predict_and_ledger() then runs EVERY model
        # over EVERY duplicate, producing M^2 records for M models instead
        # of M (Codex review: "Deduplicate forecast requests during
        # retraining").
        config = _config(panel, n_steps=6, models=("persistence", "gbm_quantile"))
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        pending_tickers_before = {entry["ticker"] for bucket in harness.outstanding.values() for entry in bucket}
        assert pending_tickers_before  # sanity: something genuinely pending
        n_models = len(config.models)
        before = _all_records(tmp_path / "run", model="persistence")
        harness.retrain(panel)
        after = _all_records(tmp_path / "run", model="persistence")
        # Exactly one new persistence forecast record per unique pending
        # ticker -- not one per (ticker, model) pair that was outstanding.
        assert len(after["forecast"]) == len(before["forecast"]) + len(pending_tickers_before)
        counts = Counter(entry["ticker"] for bucket in harness.outstanding.values() for entry in bucket)
        assert all(count == n_models for count in counts.values())

    def test_retrain_reanchors_outstanding_origins_by_date_not_a_stale_row_index(self, panel, tmp_path):
        # entry["origin_row"] is the checkpoint-era integer index -- exactly
        # the kind of raw index a rebuilt panel (the archive correction that
        # likely PROMPTED this recovery retrain in the first place) can
        # invalidate. Must be re-anchored against THIS panel by
        # entry["origin_date"] instead of trusted directly, the same way
        # rebind_unresolved() already re-anchors unresolved entries (Codex
        # review: "Re-anchor outstanding origins before retraining").
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        assert harness.outstanding  # sanity: something genuinely pending
        # Corrupt every stored integer row index the way an archive
        # revision would -- wildly out of bounds without date re-anchoring.
        for bucket in harness.outstanding.values():
            for entry in bucket:
                entry["origin_row"] = 999_999
        harness.retrain(panel)  # must not raise/IndexError despite the corruption
        for bucket in harness.outstanding.values():
            for entry in bucket:
                assert entry["origin_row"] != 999_999
                resolved_date = str(
                    np.datetime_as_string(panel.series[entry["ticker"]].dates[entry["origin_row"]], unit="D")
                )
                assert resolved_date == entry["origin_date"]

    def test_retrain_preserves_outstanding_forecasts_for_tickers_missing_from_its_own_panel(self, panel, tmp_path):
        # A ticker temporarily absent from the panel passed to retrain()
        # (a CSV gap/unreadable day) can't be re-predicted -- there is no
        # row to build a ForecastRequest from -- so its stale outstanding
        # entry must be left exactly as it was rather than dropped, the
        # same non-blocking-missing-ticker contract already applied to
        # pending_revealed.
        config = _config(panel, n_steps=6)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        ghost_entry = {
            "model": "persistence",
            "ticker": "GHOST",
            "origin_row": 100,
            "origin_date": "2021-01-04",
            "point": {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0},
            "quantiles": {},
            "quantiles_raw": {},
            "ref_close": 100.0,
            "adj_ratio": 1.0,
        }
        harness.outstanding.setdefault("2099-01-01", []).append(ghost_entry)
        harness.retrain(panel)
        assert ghost_entry in harness.outstanding["2099-01-01"]

    def test_retrain_with_refrozen_window_fits_through_the_cursor(self, panel, tmp_path):
        # The recovery retrain must see everything revealed so far, not the
        # original initial window (review regression).
        config = _config(panel, n_steps=6, models=("persistence", "gbm_quantile"))
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        original_cutoff = harness.models["gbm_quantile"]._last_refit_date
        cursor_date = panel.calendar[harness.cursor - 1]
        harness.retrain(panel, cutoff=cursor_date)
        new_cutoff = harness.models["gbm_quantile"]._last_refit_date
        assert new_cutoff == str(np.datetime_as_string(cursor_date, unit="D"))
        assert new_cutoff > original_cutoff
        assert panel.normalization["window_end"] == str(np.datetime_as_string(cursor_date, unit="D"))

    def test_retrain_refuses_when_wall_clock_budget_is_already_exhausted(self, panel, tmp_path):
        # retrain() re-fits every configured model on FULL history all over
        # again -- the same potentially-hours-long cost as the original
        # initial fit -- but had no wall-clock check at all, unlike
        # fresh()/resume()'s per-model setup check (Codex review: "Check
        # the wall-clock budget before recovery refits"). An already-
        # exhausted --max-hours budget must refuse BEFORE any model is
        # refit, leaving the prior (still valid) checkpoint untouched.
        config = _config(panel, n_steps=6, max_hours_per_run=1.0)
        harness = WalkForwardHarness.fresh(panel, _models(config), tmp_path / "run", config, log=_quiet)
        harness.run()
        before = (tmp_path / "run" / "checkpoint" / "harness_state.json").read_text(encoding="utf-8")
        harness.invocation_started_monotonic = -3601.0  # already 1+ hour elapsed
        with pytest.raises(HarnessError, match="budget exhausted during recovery retrain"):
            harness.retrain(panel)
        assert (tmp_path / "run" / "checkpoint" / "harness_state.json").read_text(encoding="utf-8") == before


class TestReplayEndBoundary:
    def test_run_processes_a_non_midnight_final_bar_on_the_inclusive_end_day(self, panel, tmp_path):
        # Calendar-day comparison, not a raw timestamp one: an archive can
        # carry a non-midnight Date (load_history_csv() preserves whatever
        # time-of-day component it has), so panel.calendar can too.
        # config.end is always a plain date string (day precision once
        # parsed); without normalizing BOTH sides, a non-midnight final bar
        # compared > midnight-parsed end64 stopped the replay one trading
        # day early despite --end's own inclusive-of-its-day contract
        # (Codex review regression: "Compare the replay end by calendar
        # day").
        import dataclasses

        end_day = panel.calendar[160]
        mutated_calendar = panel.calendar.copy()
        mutated_calendar[160] = end_day + np.timedelta64(16, "h")  # an intraday-stamped end day
        mutated_panel = dataclasses.replace(panel, calendar=mutated_calendar)

        config = _config(
            mutated_panel, models=("persistence",), end=str(np.datetime_as_string(end_day, unit="D"))
        )
        harness = WalkForwardHarness.fresh(mutated_panel, _models(config), tmp_path / "run", config, log=_quiet)
        assert harness.run() == "completed"
        # The loop processed THROUGH the intraday-stamped end day (index
        # 160) before stopping -- cursor lands one past it, not AT it.
        assert harness.cursor == 161
