"""Tests for the run_walkforward.py CLI: demo, dry-run, refusals, resume."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import daily_update
import features as _features
import numpy as np
import pytest
import run_walkforward

features: Any = _features


class TestArgumentRefusals:
    @pytest.mark.parametrize("flag", ["--alpha", "--beta", "--gamma"])
    @pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "-1.0"])
    def test_invalid_octopus_loss_weight_is_refused(self, flag, bad_value):
        # A non-finite loss weight multiplied straight into the training
        # objective makes the first loss and every gradient non-finite,
        # checkpointing corrupted weights instead of refusing up front
        # (Codex review regression: "Reject non-finite Octopus loss
        # weights"). A finite NEGATIVE weight is just as broken: it makes
        # gradient descent MAXIMIZE that loss term instead of disabling it
        # (Codex review regression: "Reject negative Octopus loss weights").
        parser = run_walkforward.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([flag, bad_value])

    @pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "-1.0"])
    def test_non_finite_or_negative_max_hours_is_refused(self, bad_value):
        # _breaker_tripped() evaluates elapsed_hours >= max_hours_per_run;
        # a NaN limit makes that comparison always False, silently
        # disabling the advertised hard wall-clock ceiling (Codex review
        # regression: "Reject non-finite wall-clock limits").
        parser = run_walkforward.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--max-hours", bad_value])

    @pytest.mark.parametrize("model", ["octopus", "all"])
    def test_octopus_objective_with_every_loss_weight_disabled_is_refused(
        self, model, synthetic_history_dir, tmp_path, capsys
    ):
        # Each of --alpha/--beta/--gamma is individually valid at 0 (the
        # documented way to disable ONE loss term), but with all three
        # disabled every training batch backpropagates a zero-weighted
        # objective -- no data-derived gradient reaches the network, yet
        # the run is still checkpointed and reported as trained (Codex
        # review regression: "Refuse an Octopus objective with every loss
        # weight disabled"). Uses a REAL archive so this refusal -- not an
        # unrelated missing-history-dir one -- is what's actually firing.
        run_dir = tmp_path / "run"
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", model, "--alpha", "0", "--beta", "0", "--gamma", "0",
                "--start", "2021-11-01", "--run-dir", str(run_dir),
            ]
        )
        assert code == 1
        err = capsys.readouterr().err
        assert "REFUSED" in err
        assert "alpha" in err
        assert not run_dir.exists()

    def test_bad_now_is_refused_exit_1(self, tmp_path, capsys):
        code = run_walkforward.main(
            ["--history-dir", str(tmp_path), "--all-tickers-in-archive", "--now", "not-a-date"]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_missing_history_dir_is_refused(self, tmp_path, capsys):
        code = run_walkforward.main(
            ["--history-dir", str(tmp_path / "absent"), "--all-tickers-in-archive"]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_no_universe_selection_is_refused(self, synthetic_history_dir, capsys):
        code = run_walkforward.main(["--history-dir", str(synthetic_history_dir)])
        assert code == 1
        assert "no universe selected" in capsys.readouterr().err

    def test_retrain_without_resume_is_refused(self, synthetic_history_dir, capsys):
        # --retrain is only ever consulted inside the args.resume branch;
        # without --resume it used to silently fall through to the
        # fresh-run path and perform an ordinary new fit instead of
        # rejecting the invalid combination -- on a whole-market run that
        # typo can consume hours before anyone notices (Codex review
        # regression: "Refuse --retrain unless a resume target is
        # supplied").
        code = run_walkforward.main(
            ["--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive", "--retrain"]
        )
        assert code == 1
        assert "--retrain requires --resume" in capsys.readouterr().err

    def test_demo_with_resume_is_refused(self, tmp_path, capsys):
        # _run_demo() never reads args.resume -- it always dispatches to
        # WalkForwardHarness.fresh() against args.run_dir (a DEFAULT-
        # generated directory when --run-dir is omitted, not the --resume
        # target). Without this refusal, --demo --resume <run_dir> silently
        # started an unrelated fresh demo and exited 0, leaving the
        # checkpoint the operator meant to continue completely untouched
        # (Codex review regression: "Reject or honor --resume when --demo
        # is set").
        code = run_walkforward.main(["--demo", "--resume", str(tmp_path / "some-run")])
        assert code == 1
        assert "--demo does not support --resume" in capsys.readouterr().err


    def test_archive_with_zero_usable_tickers_is_refused_exit_1_not_2(self, tmp_path, capsys):
        # features.build_panel() raises a plain ValueError already prefixed
        # "REFUSED: no usable tickers" for this exact condition; left
        # uncaught it fell through to main()'s generic Exception handler
        # and reported exit 2 "RUNTIME FAILURE" instead of the documented
        # exit 1 "REFUSED" for a data problem, so automation would retry a
        # data issue as if it were a crash (Codex review regression:
        # "Classify unusable archives as refusals").
        archive = tmp_path / "ticker_data"
        archive.mkdir()
        (archive / "AAA.csv").write_text("Date,Open,High,Low,Close\n")  # header only -- unusable
        code = run_walkforward.main(
            ["--history-dir", str(archive), "--all-tickers-in-archive", "--model", "persistence"]
        )
        assert code == 1
        stderr = capsys.readouterr().err
        assert "REFUSED" in stderr
        assert "no usable tickers" in stderr

    def test_start_beyond_the_visible_calendar_is_refused_before_writing_anything(
        self, synthetic_history_dir, tmp_path, capsys
    ):
        # A --start later than every visible date leaves zero simulation
        # steps; this must refuse BEFORE fitting models / creating the
        # ledger, checkpoint, or run_meta.json under --run-dir, not after
        # write_report() discovers an empty ledger post-hoc (Codex review
        # regression: "Refuse starts beyond the visible calendar").
        run_dir = tmp_path / "run"
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "persistence", "--start", "2099-01-01", "--run-dir", str(run_dir),
            ]
        )
        assert code == 1
        assert "beyond the visible calendar" in capsys.readouterr().err
        assert not run_dir.exists()


class TestDryRun:
    def test_dry_run_prints_plan_and_estimate(self, synthetic_history_dir, capsys):
        code = run_walkforward.main(
            [
                "--history-dir",
                str(synthetic_history_dir),
                "--all-tickers-in-archive",
                "--model",
                "persistence",
                "--start",
                "2021-06-01",
                "--dry-run",
            ]
        )
        assert code == 0
        output = capsys.readouterr().out
        assert "universe: 5 tickers" in output
        assert "rough estimate" in output
        assert "nothing written" in output

    def test_dry_run_resume_does_not_mutate_the_checkpoint_or_ledger(self, synthetic_history_dir, tmp_path, capsys):
        # --dry-run --resume must return before WalkForwardHarness.resume():
        # Ledger.resume() truncates/deletes shard files and GBM's load()
        # refits, both violating "fits/writes nothing" (Codex review
        # regression: "Honor --dry-run when resuming a replay").
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        state_path = run_dir / "checkpoint" / "harness_state.json"
        before_state = state_path.read_bytes()
        before_shards = {
            path: path.read_bytes() for path in sorted((run_dir / "ledger").rglob("*.jsonl.gz"))
        }
        code = run_walkforward.main(
            base + ["--end", "2021-12-01", "--resume", str(run_dir), "--dry-run"]
        )
        assert code == 0
        assert "dry run: nothing resumed, nothing written" in capsys.readouterr().out
        assert state_path.read_bytes() == before_state
        after_shards = {
            path: path.read_bytes() for path in sorted((run_dir / "ledger").rglob("*.jsonl.gz"))
        }
        assert after_shards == before_shards

    def test_dry_run_resume_estimates_only_remaining_steps_not_the_whole_replay(
        self, synthetic_history_dir, tmp_path, capsys
    ):
        # _estimate_cost() anchored its step count at config.start (the
        # ORIGINAL replay's first prediction date) even for a --resume
        # --dry-run, so a checkpoint resumed near (or past) the end of a
        # long replay reported almost the entire original replay's step
        # count again -- even a FULLY CAUGHT-UP checkpoint with nothing
        # left to do reported the whole original span, making the
        # estimate unusable for deciding whether a continuation fits the
        # available window (Codex review: "Estimate only the remaining
        # resume steps").
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-12-01", "--run-dir", str(run_dir)]) == 0
        capsys.readouterr()
        # Resume against the SAME --end: the checkpoint is already fully
        # caught up, zero calendar days remain to process.
        code = run_walkforward.main(base + ["--end", "2021-12-01", "--resume", str(run_dir), "--dry-run"])
        assert code == 0
        assert "~0 steps" in capsys.readouterr().out

    def test_dry_run_resume_refuses_a_tampered_checkpointed_shard_instead_of_approving_it(
        self, synthetic_history_dir, tmp_path, capsys
    ):
        # The checks --dry-run --resume already ran (run_meta/universe/
        # hyperparameters/config-hash) are all metadata-level -- none of
        # them hash the ledger shards or fingerprint the model artifact
        # files the way the equivalent REAL --resume does via
        # WalkForwardHarness.resume()'s own verify_checkpoint_integrity()
        # call, so a checkpointed ledger shard tampered with WITHIN the
        # checkpointed boundary used to print a clean cost estimate here
        # even though the real resume immediately refuses (Codex review:
        # "Verify artifacts during replay resume dry runs").
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        shard = next((run_dir / "ledger").rglob("forecast_*.jsonl.gz"))
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        tampered = bytes(raw)
        shard.write_bytes(tampered)
        code = run_walkforward.main(base + ["--end", "2021-12-01", "--resume", str(run_dir), "--dry-run"])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err
        assert shard.read_bytes() == tampered  # untouched, not "recovered"

    def test_dry_run_resume_reports_an_incompatible_window_len_would_refuse(
        self, synthetic_history_dir, tmp_path, capsys
    ):
        # --window-len is frozen into the checkpoint's config hash; a real
        # resume with a changed value refuses immediately. --dry-run used
        # to return its estimate BEFORE any of the checks that would catch
        # this -- reporting a valid-looking plan for a resume that would
        # actually be refused, making the documented validation mode
        # unusable to verify a planned continuation (Codex review
        # regression: "Validate frozen resume settings during dry runs").
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        code = run_walkforward.main(
            base + [
                "--end", "2021-12-01", "--resume", str(run_dir), "--dry-run",
                "--window-len", "16",
            ]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_demo_dry_run_writes_nothing_under_run_dir(self, tmp_path, capsys):
        # --demo --dry-run bypassed both dry-run branches entirely and fit
        # models, wrote a checkpoint/ledger, and generated reports under
        # --run-dir (Codex review regression: "Honor dry-run before
        # dispatching the demo").
        run_dir = tmp_path / "demo-dry"
        code = run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--dry-run"])
        assert code == 0
        output = capsys.readouterr().out
        assert "dry run: nothing fitted, nothing written" in output
        assert not run_dir.exists()


class TestParseIsoDate:
    def test_end_before_start_is_refused(self, synthetic_history_dir, capsys):
        # Letting this through would create the run dir and fit/checkpoint
        # every model before write_report() eventually refused on an empty
        # ledger -- violating "exit 1, nothing written" (Codex review
        # regression: "Reject end dates before the prediction start").
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "persistence", "--start", "2021-11-01", "--end", "2021-10-01",
            ]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err
    def test_nat_literal_is_refused_not_silently_accepted(self, synthetic_history_dir, capsys):
        # numpy accepts the literal "NaT" as a valid datetime64 without
        # raising; left unchecked it round-trips into a real NaT that
        # silently falls back to full-history normalization -- a typo
        # becomes undetected lookahead contamination (Codex review
        # regression: "Reject NaT as an ISO date cutoff").
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "persistence", "--start", "2021-11-01",
                "--initial-train-end", "NaT", "--dry-run",
            ]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_zero_trainable_windows_fit_refusal_exits_1_not_2(self, synthetic_history_dir, capsys):
        # GbmQuantileModel.fit()/OctopusModel.fit() raise plain ValueError
        # (message already starting "REFUSED") when the cutoff leaves zero
        # trainable rows; main()'s exception handler didn't recognize a
        # bare ValueError as a refusal, so this reported exit 2 "RUNTIME
        # FAILURE" -- after fresh() had already created the run directory
        # and ledger -- instead of the documented exit 1 "REFUSED" for a
        # data/config problem.
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        # calendar[0] exactly: normalization still succeeds (row 0's
        # close_position doesn't need a previous close, so the relaxed
        # fallback finds a usable row -- this is NOT the "archive too
        # small" refusal build_panel() itself already raises), but no
        # row's NEXT day (its label_end_date) is this early, so GBM's
        # trainable-row count is genuinely zero.
        impossible_cutoff = str(np.datetime_as_string(probe.calendar[0], unit="D"))
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "gbm", "--start", "2021-11-01",
                "--initial-train-end", impossible_cutoff,
            ]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err


class TestDemo:
    def test_main_restores_the_process_signal_handlers(self, tmp_path, capsys):
        # _install_early_signal_handlers()/_install_signal_handlers() replace
        # SIGINT/SIGTERM so a signal can reach the (possibly not-yet-existing)
        # harness. In a long-lived process that calls main() more than once,
        # leaving them installed means a later signal is delivered to a run
        # that already finished -- effectively ignored -- while the host's own
        # handlers never come back (Codex review: "Restore the process signal
        # handlers after the run"). A full demo run is the cheapest path that
        # actually gets past the install, so assert on it rather than on a
        # refusal that returns before any handler is replaced.
        def _sentinel_int(*_args):  # pragma: no cover - never invoked
            pass

        def _sentinel_term(*_args):  # pragma: no cover - never invoked
            pass

        previous_int = signal.signal(signal.SIGINT, _sentinel_int)
        previous_term = signal.signal(signal.SIGTERM, _sentinel_term)
        try:
            assert run_walkforward.main(["--demo", "--run-dir", str(tmp_path / "demo-run")]) == 0
            capsys.readouterr()
            assert signal.getsignal(signal.SIGINT) is _sentinel_int
            assert signal.getsignal(signal.SIGTERM) is _sentinel_term
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)

    def test_demo_run_meta_is_written_before_the_initial_checkpoint(self, tmp_path, monkeypatch):
        # Same fix, same reasoning, as the non-demo fresh-run path (Codex
        # review: "Publish run metadata with the initial checkpoint"): a
        # process killed between fresh() publishing its initial checkpoint
        # and run_meta.json being written left a demo run directory neither
        # a fresh() retry nor a --resume could ever get past.
        import harness as harness_module

        run_dir = tmp_path / "demo-run"
        seen_run_meta_exists_at_fresh_time = []
        original_fresh = harness_module.WalkForwardHarness.fresh.__func__

        def _spy_fresh(cls, *args, **kwargs):
            seen_run_meta_exists_at_fresh_time.append((run_dir / "run_meta.json").exists())
            return original_fresh(cls, *args, **kwargs)

        monkeypatch.setattr(harness_module.WalkForwardHarness, "fresh", classmethod(_spy_fresh))
        code = run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"])
        assert code == 0
        assert seen_run_meta_exists_at_fresh_time == [True]

    def test_demo_end_to_end(self, tmp_path, capsys):
        run_dir = tmp_path / "demo-run"
        code = run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"])
        assert code == 0
        assert (run_dir / "report" / "report.json").exists()
        assert (run_dir / "report" / "report.md").exists()
        report = json.loads((run_dir / "report" / "report.json").read_bytes())
        assert set(report["models"]) == {"persistence", "gbm_quantile"}
        # Both models scored a meaningful number of forecasts.
        assert min(report["n_scored"].values()) > 100
        # The demo's ledger supports report-only re-rendering.
        (run_dir / "report" / "report.json").unlink()
        assert run_walkforward.main(["--report-only", str(run_dir)]) == 0
        assert (run_dir / "report" / "report.json").exists()

    def test_holds_the_run_lock_through_report_publication(self, tmp_path, monkeypatch):
        # harness.run() used to release the per-run lock in its own finally,
        # BEFORE write_report() reads the ledger/checkpoint this run just
        # wrote -- a second process could win that gap and start mutating
        # the run directory while write_report() was still reading it
        # (Codex review: "Hold the run lock through artifact publication").
        # Prove the lock is still held by attempting a second acquire from
        # INSIDE write_report() itself: a concurrent holder must see the
        # same "already holds the lock" refusal a real second process would.
        import harness as harness_module

        run_dir = tmp_path / "demo-run"
        original = run_walkforward.write_report
        attempted = []

        def _spy(run_dir_arg, *args, **kwargs):
            attempted.append(True)
            with pytest.raises(harness_module.HarnessError, match="already holds the lock"):
                harness_module._acquire_run_lock(run_dir_arg)
            return original(run_dir_arg, *args, **kwargs)

        monkeypatch.setattr(run_walkforward, "write_report", _spy)
        code = run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"])
        assert code == 0
        assert attempted  # sanity: the spy actually ran
        # The lock is released afterward -- a later invocation is not blocked.
        # Restore the real write_report first: the spy's own concurrent-
        # acquire probe would otherwise (correctly) find no lock held on
        # THIS second, unrelated invocation and fail for the wrong reason.
        monkeypatch.setattr(run_walkforward, "write_report", original)
        assert run_walkforward.main(["--report-only", str(run_dir)]) == 0

    def test_demo_persists_the_reduced_gbm_iteration_count(self, tmp_path):
        # The demo path keeps GBM tiny by mutating model._hist_params
        # directly (bypassing the normal constructor) so the whole smoke
        # test stays in seconds; save() persists the SEPARATE self.max_iter
        # attribute into metadata.json, which stayed at its original,
        # un-reduced value. A demo interrupted and later resumed would then
        # refit with the ORIGINAL max_iter instead of the 20 actually used
        # before the interruption, breaking resume-equals-uninterrupted for
        # the demo path (Codex review regression: "Persist the reduced
        # demo GBM iteration count").
        run_dir = tmp_path / "demo-run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        metadata = json.loads((run_dir / "checkpoint" / "models" / "gbm_quantile" / "metadata.json").read_bytes())
        assert metadata["max_iter"] == 20

    def test_demo_records_the_archive_path_as_absolute(self, tmp_path, monkeypatch):
        # The synthetic archive lives at run_dir/demo_archive and that path
        # is persisted into run_meta.json's history_dir verbatim. A
        # relative --run-dir left the recorded path relative too; a later
        # bare `--resume <run_dir>` invocation (not through --demo, which
        # refuses combined with --resume) restores it and re-resolves
        # against ITS OWN working directory, reading the wrong archive or
        # refusing a healthy one -- the same failure _resolve_history_dir()
        # already closed for the non-demo path (Codex review regression:
        # "Persist the demo archive as an absolute path").
        monkeypatch.chdir(tmp_path)
        relative_run_dir = Path("demo-run")
        assert run_walkforward.main(["--demo", "--run-dir", str(relative_run_dir), "--checkpoint-every", "50"]) == 0
        recorded = json.loads((relative_run_dir / "run_meta.json").read_bytes())["history_dir"]
        assert Path(recorded).is_absolute()
        assert Path(recorded) == (tmp_path / relative_run_dir / "demo_archive").resolve()

    def test_demo_carries_the_invocation_start_time_into_the_harness(self, tmp_path, monkeypatch):
        # WalkForwardHarness's own default (time.monotonic() AT
        # CONSTRUCTION time, inside fresh()) restarts the wall-clock budget
        # only after the demo's archive/panel setup -- a --max-hours
        # circuit breaker configured for the demo would then receive the
        # full budget again for replay even if setup alone had already
        # consumed it, with no refusal or audit signal (Codex review
        # regression: "Carry the wall-clock start into demo harnesses").
        import harness as harness_module

        captured = {}
        original_fresh = harness_module.WalkForwardHarness.fresh.__func__

        def spy_fresh(cls, *args, **kwargs):
            captured["invocation_started_monotonic"] = kwargs.get("invocation_started_monotonic")
            return original_fresh(cls, *args, **kwargs)

        monkeypatch.setattr(harness_module.WalkForwardHarness, "fresh", classmethod(spy_fresh))
        run_dir = tmp_path / "demo-run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        assert captured["invocation_started_monotonic"] is not None

    def test_report_only_without_ledger_is_refused(self, tmp_path, capsys):
        assert run_walkforward.main(["--report-only", str(tmp_path)]) == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_report_only_without_any_checkpoint_is_refused(self, tmp_path, capsys):
        # Ledger shards can exist with zero checkpoint fingerprints for
        # them (both checkpoint/ and checkpoint.prev/ lost or corrupted,
        # or a run crashed before its first checkpoint) -- reading them
        # unrestricted would accept bytes with zero durability authority
        # instead of refusing (Codex review regression: "Refuse reports
        # when no checkpoint fingerprints exist").
        import shutil

        run_dir = tmp_path / "demo-run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        shutil.rmtree(run_dir / "checkpoint", ignore_errors=True)
        shutil.rmtree(run_dir / "checkpoint.prev", ignore_errors=True)
        code = run_walkforward.main(["--report-only", str(run_dir)])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_demo_interrupted_bare_resume_succeeds_with_default_models(self, tmp_path):
        # The default --demo run (--model all) narrows to two torch-free
        # baselines internally, but run_meta.json used to still record
        # model_selection="all" -- a bare --resume after an interrupt then
        # rebuilt three models against a two-model checkpoint and was
        # refused (Codex review regression: "Record the actual model set
        # used by the default demo").
        run_dir = tmp_path / "demo-run"
        code = run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--max-steps", "2"])
        assert code == 3
        assert run_walkforward.main(["--resume", str(run_dir)]) == 0

    def test_report_only_ignores_bytes_past_the_checkpoint_fingerprint(self, tmp_path):
        # A runtime failure between Ledger.append() and the next checkpoint
        # can leave a complete (or partial) gzip member past the durable
        # boundary; --report-only must not count it (Codex review
        # regression: "Bound report-only reads to the checkpoint ledger").
        import gzip
        import json as json_module

        run_dir = tmp_path / "demo-run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        before = run_walkforward.write_report(run_dir)
        before_report = json.loads(before.read_bytes())
        from ledger import Ledger

        shard = next((run_dir / "ledger" / "persistence").glob("score_*.jsonl.gz"))
        template = next(Ledger(run_dir).iter_records(model="persistence", kind="score"))
        fake = dict(template, origin_date="2099-01-01", target_date="2099-01-02")
        with shard.open("ab") as handle:
            handle.write(gzip.compress((json_module.dumps(fake) + "\n").encode(), mtime=0))
        after = run_walkforward.write_report(run_dir)
        after_report = json.loads(after.read_bytes())
        assert after_report["n_scored"] == before_report["n_scored"]

    def test_report_files_are_written_with_explicit_utf8(self, tmp_path, monkeypatch):
        # render_markdown() includes non-ASCII characters (an en dash, a
        # minus sign); Path.write_text() without an explicit encoding
        # defers to the platform's preferred encoding, which is not
        # always UTF-8 (Codex review regression: "Write generated reports
        # using UTF-8").
        import pathlib

        run_dir = tmp_path / "demo-run"
        assert run_walkforward.main(["--demo", "--run-dir", str(run_dir), "--checkpoint-every", "50"]) == 0
        seen_encodings = []
        original_write_text = pathlib.Path.write_text

        def spy_write_text(self, data, *args, **kwargs):
            if self.name in ("report.json", "report.md"):
                seen_encodings.append(kwargs.get("encoding"))
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", spy_write_text)
        run_walkforward.write_report(run_dir)
        assert len(seen_encodings) == 2
        assert all(encoding == "utf-8" for encoding in seen_encodings)


class TestResumeCli:
    def test_bare_resume_restores_recorded_flags_and_universe(self, synthetic_history_dir, tmp_path):
        run_dir = tmp_path / "run"
        original = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
            "--window-len", "96", "--max-steps", "2", "--run-dir", str(run_dir),
        ]
        assert run_walkforward.main(original) == 3
        # No history/universe/model/config flags need to be remembered.
        assert run_walkforward.main(["--resume", str(run_dir)]) == 0

    def test_resume_metadata_reads_use_explicit_utf8(self, synthetic_history_dir, tmp_path, monkeypatch):
        # json.loads(path.read_bytes()) without an explicit encoding defers
        # to the platform's preferred encoding, which is not always UTF-8;
        # a non-ASCII history path stored in run_meta.json could then fail
        # decoding or mojibake under a non-UTF-8 locale, even though
        # _write_run_meta() explicitly wrote it as UTF-8 (Codex review
        # regression: "Decode persisted run metadata as UTF-8").
        import pathlib

        run_dir = tmp_path / "run"
        original = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
            "--window-len", "96", "--max-steps", "2", "--run-dir", str(run_dir),
        ]
        assert run_walkforward.main(original) == 3

        seen_encodings = []
        original_read_text = pathlib.Path.read_text

        def spy_read_text(self, *args, **kwargs):
            if self.name in ("run_meta.json", "harness_state.json"):
                seen_encodings.append(kwargs.get("encoding"))
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", spy_read_text)
        assert run_walkforward.main(["--resume", str(run_dir)]) == 0
        assert seen_encodings  # sanity: the spied files were actually read
        assert all(encoding == "utf-8" for encoding in seen_encodings)

    def test_resume_without_run_meta_is_refused(self, synthetic_history_dir, tmp_path, capsys):
        # A resume without run_meta.json has no universe/model-hyper
        # integrity check to run at all (e.g. the process died between
        # fresh()'s first checkpoint and _write_run_meta() running); a
        # resume supplied with a DIFFERENT --tickers/archive/selection
        # could otherwise pass the harness's own config-hash check
        # unnoticed and continue ledgering a different experiment (Codex
        # review regression: "Refuse replay resumes without frozen run
        # metadata").
        run_dir = tmp_path / "run"
        original = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
            "--max-steps", "2", "--run-dir", str(run_dir),
        ]
        assert run_walkforward.main(original) == 3
        (run_dir / "run_meta.json").unlink()
        # Explicit flags bypass the "no universe selected" refusal that
        # would otherwise short-circuit before reaching this check.
        code = run_walkforward.main(
            ["--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive", "--resume", str(run_dir)]
        )
        assert code == 1
        stderr = capsys.readouterr().err
        assert "REFUSED" in stderr
        assert "run_meta.json" in stderr

    def test_run_meta_is_written_before_the_initial_checkpoint(self, synthetic_history_dir, tmp_path, monkeypatch):
        # run_meta.json used to be written AFTER WalkForwardHarness.fresh()
        # published its initial checkpoint -- fresh()'s own expensive
        # initial fit, potentially hours at full-universe scale. A process
        # killed between the two writes left a run directory neither a
        # fresh() retry (refused: already holds a checkpoint) nor a
        # --resume (refused: no run_meta.json) could ever get past,
        # permanently discarding the completed initial fit (Codex review:
        # "Publish run metadata with the initial checkpoint"). Verify the
        # ordering directly: run_meta.json must already exist by the time
        # fresh() is even called.
        import harness as harness_module

        run_dir = tmp_path / "run"
        seen_run_meta_exists_at_fresh_time = []
        original_fresh = harness_module.WalkForwardHarness.fresh.__func__

        def _spy_fresh(cls, *args, **kwargs):
            seen_run_meta_exists_at_fresh_time.append((run_dir / "run_meta.json").exists())
            return original_fresh(cls, *args, **kwargs)

        monkeypatch.setattr(harness_module.WalkForwardHarness, "fresh", classmethod(_spy_fresh))
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "persistence", "--start", "2021-11-01", "--end", "2021-11-15",
                "--run-dir", str(run_dir),
            ]
        )
        assert code == 0
        assert seen_run_meta_exists_at_fresh_time == [True]

    def test_a_crash_right_after_the_initial_checkpoint_still_leaves_a_resumable_run(
        self, synthetic_history_dir, tmp_path
    ):
        # End-to-end version of the ordering test above: simulate a
        # process kill immediately after fresh() publishes its initial
        # checkpoint (the exact interruption point the finding describes)
        # and confirm --resume can still pick the run back up instead of
        # being refused for a missing run_meta.json.
        import harness as harness_module

        run_dir = tmp_path / "run"

        class _KilledAfterCheckpoint(SystemExit):
            pass

        original_fresh = harness_module.WalkForwardHarness.fresh.__func__

        def _crashing_fresh(cls, *args, **kwargs):
            original_fresh(cls, *args, **kwargs)  # the initial checkpoint IS published
            raise _KilledAfterCheckpoint(1)

        import unittest.mock

        with unittest.mock.patch.object(
            harness_module.WalkForwardHarness, "fresh", classmethod(_crashing_fresh)
        ):
            try:
                run_walkforward.main(
                    [
                        "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                        "--model", "persistence", "--start", "2021-11-01", "--end", "2021-11-15",
                        "--run-dir", str(run_dir),
                    ]
                )
            except _KilledAfterCheckpoint:
                pass
        assert (run_dir / "run_meta.json").exists()
        assert (run_dir / "checkpoint").exists()
        code = run_walkforward.main(
            ["--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive", "--resume", str(run_dir)]
        )
        assert code == 0

    def test_fresh_retry_against_an_existing_checkpoint_does_not_overwrite_run_meta(
        self, synthetic_history_dir, tmp_path
    ):
        # _write_run_meta() used to run unconditionally BEFORE fresh()'s own
        # "already holds a checkpoint" refusal check -- a non-concurrent
        # retry (different flags, no other process involved) against a
        # run_dir that already holds a VALID checkpoint from a past
        # invocation exited REFUSED as documented, but not before rewriting
        # that checkpoint's authoritative run_meta.json to describe THIS
        # invocation's args/model_selection, which was never actually
        # applied. A later bare --resume or daily_update.py would then
        # reconstruct the wrong model_selection/knobs for the checkpoint
        # that is actually on disk (Codex review: "Acquire the fresh-run
        # lock before replacing metadata"). Assert the retry is refused AND
        # run_meta.json is byte-identical to what the first run wrote.
        run_dir = tmp_path / "run"
        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "persistence", "--start", "2021-11-01", "--end", "2021-11-15",
                "--run-dir", str(run_dir),
            ]
        )
        assert code == 0
        before = (run_dir / "run_meta.json").read_text()
        assert json.loads(before)["model_selection"] == "persistence"

        code = run_walkforward.main(
            [
                "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
                "--model", "gbm", "--start", "2021-11-01", "--end", "2021-11-15",
                "--run-dir", str(run_dir),
            ]
        )
        assert code == 1
        after = (run_dir / "run_meta.json").read_text()
        assert after == before
        assert json.loads(after)["model_selection"] == "persistence"

    def test_resume_universe_mismatch_is_refused(self, synthetic_history_dir, tmp_path, capsys):
        run_dir = tmp_path / "run"
        code = run_walkforward.main(
            [
                "--history-dir",
                str(synthetic_history_dir),
                "--all-tickers-in-archive",
                "--model",
                "persistence",
                "--start",
                "2021-11-01",
                "--end",
                "2021-11-15",
                "--run-dir",
                str(run_dir),
            ]
        )
        assert code == 0
        code = run_walkforward.main(
            [
                "--history-dir",
                str(synthetic_history_dir),
                "--tickers",
                "AAA",
                "BBB",
                "--model",
                "persistence",
                "--start",
                "2021-11-01",
                "--end",
                "2021-12-15",
                "--resume",
                str(run_dir),
            ]
        )
        assert code == 1
        assert "ticker set hash mismatch" in capsys.readouterr().err

    def test_resume_rebinds_boundary_forecasts_so_they_get_scored(self, synthetic_history_dir, tmp_path):
        # Forecasts made from the last visible row of an --end-truncated run
        # have no target yet; extending the run must re-bind and score them
        # (review regression: they previously never matured).
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        assert run_walkforward.main(base + ["--end", "2021-12-01", "--resume", str(run_dir)]) == 0
        from ledger import Ledger

        scored_origins = {
            record["origin_date"]
            for record in Ledger(run_dir).iter_records(model="persistence", kind="score")
        }
        assert "2021-11-15" in scored_origins

    def test_resume_with_changed_model_hyper_is_refused(self, synthetic_history_dir, tmp_path, capsys):
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        code = run_walkforward.main(
            base + ["--end", "2021-12-01", "--online-steps", "9", "--resume", str(run_dir)]
        )
        assert code == 1
        assert "online_steps" in capsys.readouterr().err

    def test_resume_with_now_before_cursor_is_refused(self, synthetic_history_dir, tmp_path, capsys):
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-12-01", "--run-dir", str(run_dir)]) == 0
        # An earlier --now truncates the panel to before the checkpoint's
        # already-processed cursor -- must not silently rewind the durable
        # cursor backward while ledger/model state still reflect the later
        # dates (Codex review regression: "Reject as-of cutoffs before the
        # resumed cursor").
        code = run_walkforward.main(base + ["--now", "2021-11-05T18:00", "--resume", str(run_dir)])
        assert code == 1
        assert "before the checkpoint's last processed date" in capsys.readouterr().err

    def test_bare_resume_recovers_config_from_checkpoint_prev_after_an_interrupted_swap(
        self, synthetic_history_dir, tmp_path
    ):
        # checkpoint() writes checkpoint.tmp, renames checkpoint/ to
        # checkpoint.prev, then renames checkpoint.tmp into checkpoint/. A
        # crash between those two renames leaves checkpoint/ absent with the
        # complete, authoritative previous checkpoint sitting in
        # checkpoint.prev/ -- simulate that disk state directly. A bare
        # --resume must still recover this run's non-default flags (here
        # --window-len) from THAT checkpoint, or it silently rebuilds the
        # request from argparse defaults and gets falsely REFUSED for a
        # "flags differ" mismatch the harness's own recovery would
        # otherwise sail through (Codex review regression: "Recover
        # checkpoint metadata before restoring resume arguments").
        run_dir = tmp_path / "run"
        original = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
            "--window-len", "96", "--max-steps", "2", "--run-dir", str(run_dir),
        ]
        assert run_walkforward.main(original) == 3
        assert (run_dir / "checkpoint").exists()
        assert not (run_dir / "checkpoint.prev").exists()
        (run_dir / "checkpoint").rename(run_dir / "checkpoint.prev")
        assert run_walkforward.main(["--resume", str(run_dir)]) == 0

    def test_resume_refuses_a_checkpoint_whose_config_hash_does_not_match_its_config(
        self, synthetic_history_dir, tmp_path, capsys
    ):
        # state["config_hash"] is sealed in at the same write that produced
        # state["config"]; if the file is later corrupted or hand-edited so
        # the two disagree, a resume must refuse rather than silently trust
        # the (possibly tampered) config -- the sealed-spec contract as good
        # as unenforced otherwise (Codex review regression: "Verify the
        # checkpoint's recorded config hash").
        run_dir = tmp_path / "run"
        base = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_bytes())
        state["config"]["seed"] = state["config"]["seed"] + 1  # config_hash now stale
        state_path.write_text(json.dumps(state, sort_keys=True))
        code = run_walkforward.main(base + ["--end", "2021-12-01", "--resume", str(run_dir)])
        assert code == 1
        assert "corrupted" in capsys.readouterr().err

    def test_resume_extends_a_finished_run(self, synthetic_history_dir, tmp_path):
        run_dir = tmp_path / "run"
        base = [
            "--history-dir",
            str(synthetic_history_dir),
            "--all-tickers-in-archive",
            "--model",
            "persistence",
            "--start",
            "2021-11-01",
        ]
        assert run_walkforward.main(base + ["--end", "2021-11-15", "--run-dir", str(run_dir)]) == 0
        assert run_walkforward.main(base + ["--end", "2021-12-01", "--resume", str(run_dir)]) == 0
        report = json.loads((run_dir / "report" / "report.json").read_bytes())
        assert report["n_scored"]["persistence"] > 0


def test_max_steps_circuit_breaker_exits_3_with_checkpoint(synthetic_history_dir, tmp_path, capsys):
    run_dir = tmp_path / "run"
    code = run_walkforward.main(
        [
            "--history-dir",
            str(synthetic_history_dir),
            "--all-tickers-in-archive",
            "--model",
            "persistence",
            "--start",
            "2021-11-01",
            "--max-steps",
            "3",
            "--run-dir",
            str(run_dir),
        ]
    )
    assert code == 3
    output = capsys.readouterr().out
    assert "CIRCUIT BREAKER" in output
    assert "budget_tripped" in output
    assert (run_dir / "checkpoint" / "harness_state.json").exists()
    # And the budget is per-invocation: a fresh resume without it completes.
    code = run_walkforward.main(
        [
            "--history-dir",
            str(synthetic_history_dir),
            "--all-tickers-in-archive",
            "--model",
            "persistence",
            "--start",
            "2021-11-01",
            "--end",
            "2021-11-19",
            "--resume",
            str(run_dir),
        ]
    )
    assert code == 0


def test_preset_knobs_resolve_with_explicit_flag_override():
    parser = run_walkforward.build_parser()
    args = parser.parse_args(["--preset", "quick", "--window-len", "96"])
    knobs = run_walkforward._apply_preset(args)
    assert knobs["window_len"] == 96  # explicit flag wins
    assert knobs["update_every"] == 5  # preset value stands
    assert knobs["max_tickers"] == 500


def test_initial_training_cutoff_must_precede_prediction_start(capsys):
    code = run_walkforward.main(["--start", "2020-01-01", "--initial-train-end", "2020-01-01"])
    assert code == 1
    assert "prevent lookahead" in capsys.readouterr().err


class TestWallClockBudgetBeforeReport:
    def test_defers_report_generation_when_the_budget_is_already_exhausted(
        self, synthetic_history_dir, tmp_path, capsys, monkeypatch
    ):
        # write_report() decompresses and intersects a full-universe,
        # multi-GB score ledger, real unbounded work the breaker never
        # covers once the replay loop itself exits -- a replay whose last
        # step lands just under --max-hours can still blow well past the
        # advertised ceiling generating the report, even though the
        # checkpoint is already complete and valid (Codex review: "Recheck
        # the wall-clock ceiling before report generation"). Simulates the
        # "already exhausted by the time the loop exits" case directly: a
        # harness that has ALREADY completed its replay (so run()'s own
        # internal breaker never gets a next step to check), with the
        # budget fields set to already-exceeded values before _run() is
        # invoked.
        import time

        import baseline_model
        from harness import HarnessConfig, WalkForwardHarness

        panel = features.build_panel(synthetic_history_dir, min_history=60)
        config = HarnessConfig(
            start=str(np.datetime_as_string(panel.calendar[151], unit="D")),
            end=str(np.datetime_as_string(panel.calendar[152], unit="D")),
            initial_train_end=str(np.datetime_as_string(panel.calendar[150], unit="D")),
            models=("persistence",),
            window_len=16,
            min_history=60,
            checkpoint_every=6,
            diagnostic_sample=10,
            seed=0,
        )
        run_dir = tmp_path / "run"
        harness = WalkForwardHarness.fresh(panel, {"persistence": baseline_model.PersistenceModel()}, run_dir, config)
        assert harness.run() == "completed"  # fully caught up; no budget set yet
        harness.config.max_hours_per_run = 1.0
        harness.invocation_started_monotonic = time.monotonic() - 3600.0 * 2  # already 2h over a 1h budget

        called = []
        monkeypatch.setattr(run_walkforward, "write_report", lambda *a, **k: called.append(True))
        code = run_walkforward._run(harness, run_dir)
        assert code == 3
        assert not called  # write_report() must not run
        out = capsys.readouterr().out
        assert "wall-clock budget" in out
        assert "--report-only" in out

    def test_generates_the_report_normally_when_the_budget_has_headroom(
        self, synthetic_history_dir, tmp_path, monkeypatch
    ):
        # Sanity: the recheck must not block the ordinary case.
        import baseline_model
        from harness import HarnessConfig, WalkForwardHarness

        panel = features.build_panel(synthetic_history_dir, min_history=60)
        config = HarnessConfig(
            start=str(np.datetime_as_string(panel.calendar[151], unit="D")),
            end=str(np.datetime_as_string(panel.calendar[152], unit="D")),
            initial_train_end=str(np.datetime_as_string(panel.calendar[150], unit="D")),
            models=("persistence",),
            window_len=16,
            min_history=60,
            checkpoint_every=6,
            diagnostic_sample=10,
            seed=0,
        )
        run_dir = tmp_path / "run"
        harness = WalkForwardHarness.fresh(panel, {"persistence": baseline_model.PersistenceModel()}, run_dir, config)
        assert harness.run() == "completed"
        harness.config.max_hours_per_run = 100.0  # ample headroom
        code = run_walkforward._run(harness, run_dir)
        assert code == 0
        assert (run_dir / "report" / "report.json").exists()


class TestSignalHandlerRestoration:
    """main() must hand SIGINT/SIGTERM back to whoever owned them."""

    @staticmethod
    def _dispositions():
        import signal

        return (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))

    def test_main_restores_original_handlers_after_a_successful_run(self, tmp_path, capsys):
        # _dispatch() re-points both signals at a temporary _InterruptFlag and
        # then at the harness; leaving them installed on the way out disarms a
        # long-lived host process that calls main() twice or embeds this CLI
        # as a library -- its NEXT Ctrl-C reaches stale state that nothing
        # reads any more and is effectively ignored (Codex review: "Restore
        # process signal handlers after walk-forward runs").
        import signal

        def _sentinel(_signum, _frame):  # pragma: no cover - never invoked
            raise AssertionError("test sentinel handler must not run")

        previous = [signal.signal(number, _sentinel) for number in (signal.SIGINT, signal.SIGTERM)]
        try:
            assert self._dispositions() == (_sentinel, _sentinel)
            code = run_walkforward.main(["--demo", "--run-dir", str(tmp_path / "run")])
            capsys.readouterr()
            assert code == 0
            assert self._dispositions() == (_sentinel, _sentinel)
        finally:
            for number, handler in zip((signal.SIGINT, signal.SIGTERM), previous, strict=True):
                signal.signal(number, handler)

    def test_main_restores_original_handlers_after_a_refusal(self, tmp_path, capsys):
        # Restoration happens on EVERY exit path, including the ones main()
        # turns into an exit code rather than letting propagate.
        import signal

        def _sentinel(_signum, _frame):  # pragma: no cover - never invoked
            raise AssertionError("test sentinel handler must not run")

        previous = [signal.signal(number, _sentinel) for number in (signal.SIGINT, signal.SIGTERM)]
        try:
            code = run_walkforward.main(
                ["--run-dir", str(tmp_path / "run"), "--history-dir", str(tmp_path / "nope")]
            )
            capsys.readouterr()
            assert code == 1  # REFUSED
            assert self._dispositions() == (_sentinel, _sentinel)
        finally:
            for number, handler in zip((signal.SIGINT, signal.SIGTERM), previous, strict=True):
                signal.signal(number, handler)


class TestEarlySignalHandling:
    def test_interrupt_flag_handler_sets_requested(self):
        # _InterruptFlag exists precisely because a signal can arrive
        # BEFORE any harness object does (during the initial fit); its
        # handler must be a plain, side-effect-only callable so installing
        # it doesn't itself require a harness (Codex review regression:
        # "Install signal handlers before initial fitting").
        flag = run_walkforward._InterruptFlag()
        assert not flag.requested
        flag.handler(None, None)
        assert flag.requested

    def test_run_honors_an_interrupt_flag_set_before_the_harness_existed(
        self, synthetic_history_dir, tmp_path, capsys
    ):
        # A signal caught during the (now-complete) initial fit -- before
        # any harness object existed to attach request_interrupt() to --
        # must be honored on run()'s very first iteration rather than lost
        # (Codex review regression: "Install signal handlers before
        # initial fitting").
        import baseline_model
        import numpy as np
        from harness import HarnessConfig, WalkForwardHarness

        panel = features.build_panel(synthetic_history_dir, min_history=60)
        config = HarnessConfig(
            start=str(np.datetime_as_string(panel.calendar[151], unit="D")),
            initial_train_end=str(np.datetime_as_string(panel.calendar[150], unit="D")),
            models=("persistence",),
            window_len=16,
            min_history=60,
            checkpoint_every=6,
            diagnostic_sample=10,
            seed=0,
        )
        run_dir = tmp_path / "run"
        harness = WalkForwardHarness.fresh(panel, {"persistence": baseline_model.PersistenceModel()}, run_dir, config)
        flag = run_walkforward._InterruptFlag()
        flag.requested = True
        code = run_walkforward._run(harness, run_dir, interrupt_flag=flag)
        assert code == 3
        assert "interrupted" in capsys.readouterr().out
        # No replay step actually executed -- the interrupt was honored
        # immediately, not after doing wasted step work.
        assert harness.step_index == 0


class TestHistoryDirAnchoring:
    def test_relative_history_dir_is_recorded_as_absolute(
        self, synthetic_history_dir, tmp_path, monkeypatch
    ):
        # A relative --history-dir persisted into run_meta.json VERBATIM
        # used to be reconstructed later relative to daily_update.py's OWN
        # working directory -- a scheduler launched from elsewhere would
        # then either refuse a healthy archive or, worse, silently read a
        # different same-named directory (Codex review regression: "Record
        # the archive path independently of the launch directory").
        monkeypatch.chdir(tmp_path)
        relative = Path(os.path.relpath(synthetic_history_dir, tmp_path))
        assert not relative.is_absolute()  # sanity: genuinely relative
        run_dir = tmp_path / "run"
        code = run_walkforward.main(
            [
                "--history-dir", str(relative), "--all-tickers-in-archive",
                "--model", "persistence", "--run-dir", str(run_dir),
                "--start", "2021-11-01", "--checkpoint-every", "50",
            ]
        )
        assert code == 0
        recorded = json.loads((run_dir / "run_meta.json").read_bytes())["history_dir"]
        assert Path(recorded).is_absolute()
        assert Path(recorded) == synthetic_history_dir.resolve()


class TestRunMetaUniverseRefresh:
    def test_atomic_write_text_fsyncs_the_file_and_the_directory(self, tmp_path, monkeypatch):
        # write_text()'s own close() only reaches the OS page cache, not
        # stable storage, and the rename's own directory-entry update is
        # buffered separately from both -- checkpoint()'s later fsync of
        # run_dir can make THIS rename's directory entry durable without
        # the file's DATA being durable, so a crash in between could leave
        # a valid checkpoint beside an empty or truncated run_meta.json
        # (Codex review: "Fsync mandatory run metadata before publishing
        # checkpoints"). Assert both fsyncs actually happen.
        synced_fds = []
        original_fsync = os.fsync

        def _spy_fsync(fd):
            synced_fds.append(fd)
            return original_fsync(fd)

        synced_dirs = []
        original_fsync_dir = run_walkforward._fsync_dir

        def _spy_fsync_dir(directory):
            synced_dirs.append(Path(directory))
            return original_fsync_dir(directory)

        monkeypatch.setattr(run_walkforward.os, "fsync", _spy_fsync)
        monkeypatch.setattr(run_walkforward, "_fsync_dir", _spy_fsync_dir)

        target = tmp_path / "run_meta.json"
        run_walkforward._atomic_write_text(target, "hello")

        assert target.read_text() == "hello"
        # At least one fsync for the temp file's own contents, plus
        # whatever _fsync_dir()'s own os.fsync(fd) call on the directory
        # adds on top.
        assert len(synced_fds) >= 2
        assert synced_dirs == [tmp_path]  # the parent directory, after the rename


    def test_daily_cycle_admits_newly_eligible_tickers_into_run_meta_universe(self, synthetic_history_dir, tmp_path):
        # The daily cycle re-applies the selection rule against a grown
        # archive and can admit newly eligible tickers (e.g. CCC, the
        # synthetic archive's late-IPO ticker, crossing its warm-up
        # threshold); run_meta.json's universe snapshot must reflect that
        # instead of the ORIGINAL fit-time ticker list forever, and
        # inspect_run.py's ribbon-ticker fallback reads this file
        # directly.
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        all_dates = [str(np.datetime_as_string(date, unit="D")) for date in probe.calendar]
        run_dir = tmp_path / "run"
        original = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", all_dates[145],
            "--now", f"{all_dates[150]}T18:00", "--run-dir", str(run_dir),
            "--checkpoint-every", "50",
        ]
        assert run_walkforward.main(original) == 0
        original_meta = json.loads((run_dir / "run_meta.json").read_bytes())
        assert "CCC" not in original_meta["universe"]["tickers"]  # too short at this cutoff

        later_now = f"{all_dates[200]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(synthetic_history_dir), "--now", later_now]
        )
        assert code == 0
        refreshed_meta = json.loads((run_dir / "run_meta.json").read_bytes())
        assert "CCC" in refreshed_meta["universe"]["tickers"]
        assert refreshed_meta["universe"]["n_tickers"] > original_meta["universe"]["n_tickers"]
        # The frozen selection RULE itself must stay untouched.
        assert refreshed_meta["selection"] == original_meta["selection"]

    def test_a_crash_mid_refresh_never_corrupts_run_meta(self, synthetic_history_dir, tmp_path, monkeypatch):
        # A kill between opening run_meta.json and finishing a direct write
        # used to leave a truncated/empty file there. That file is
        # mandatory for every later --resume/daily_update invocation (the
        # frozen ticker selection, archive path, and model hyperparameters
        # those verify against, plus bare-resume argument reconstruction),
        # so subsequent commands would fail before they could even reach
        # the otherwise-valid checkpoint's recovery path.
        probe = features.build_panel(synthetic_history_dir, min_history=60)
        all_dates = [str(np.datetime_as_string(date, unit="D")) for date in probe.calendar]
        run_dir = tmp_path / "run"
        original = [
            "--history-dir", str(synthetic_history_dir), "--all-tickers-in-archive",
            "--model", "persistence", "--start", all_dates[145],
            "--now", f"{all_dates[150]}T18:00", "--run-dir", str(run_dir),
            "--checkpoint-every", "50",
        ]
        assert run_walkforward.main(original) == 0
        before = (run_dir / "run_meta.json").read_text()

        # _atomic_write_text() no longer uses Path.write_text() (it now
        # fsyncs the handle directly -- Codex review: "Fsync mandatory run
        # metadata before publishing checkpoints"), so the crash is
        # simulated one level down, at the text-file-handle write() call
        # its `with tmp_path.open("w", ...) as handle: handle.write(...)`
        # goes through. io.TextIOWrapper's write() cannot be monkeypatched
        # at the CLASS level (an immutable C type), so the INSTANCE's
        # write is overridden instead, only for the run_meta.json.tmp
        # handle itself.
        import pathlib

        original_open = pathlib.Path.open

        def _crashing_open(self, *args, **kwargs):
            handle = original_open(self, *args, **kwargs)
            if "run_meta.json.tmp" in self.name:
                class _CrashingFile:
                    def __init__(self, inner):
                        self._inner = inner

                    def write(self, data):
                        self._inner.write(data[: len(data) // 2])
                        raise OSError("simulated kill mid-write")

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return self._inner.__exit__(*args)

                    def __getattr__(self, name):
                        return getattr(self._inner, name)

                return _CrashingFile(handle)
            return handle

        monkeypatch.setattr(pathlib.Path, "open", _crashing_open)
        later_now = f"{all_dates[200]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(synthetic_history_dir), "--now", later_now]
        )
        assert code == 2  # runtime failure, per daily_update.main()'s documented contract
        assert (run_dir / "run_meta.json").read_text() == before  # untouched, not corrupted
