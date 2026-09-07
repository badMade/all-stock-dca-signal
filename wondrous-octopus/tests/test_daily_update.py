"""Tests for daily_update.py: the live one-day cycle, NOOP idempotence."""

from __future__ import annotations

import csv
import json
import math
from types import SimpleNamespace

import pytest
import conftest
import daily_update
import harness as harness_module
import run_walkforward
from harness import UNRESOLVED_KEY
from ledger import Ledger

make_synthetic_ohlcv = conftest.make_synthetic_ohlcv
write_ticker_csv = conftest.write_ticker_csv


def test_dollar_clamps_non_finite_log_returns_instead_of_raising():
    # Regression (Antigravity review: "Uncaught OverflowError risk in
    # _dollar()"): a divergent model could emit a non-finite or extreme
    # log-return; math.exp() on it used to raise OverflowError/crash the
    # daily forecast-CSV write instead of degrading gracefully.
    assert daily_update._dollar(float("inf"), 100.0) == pytest.approx(100.0 * math.exp(50.0))
    assert daily_update._dollar(float("-inf"), 100.0) == pytest.approx(100.0 * math.exp(-50.0))
    assert daily_update._dollar(float("nan"), 100.0) == pytest.approx(100.0)
    assert daily_update._dollar(1e300, 100.0) == pytest.approx(100.0 * math.exp(50.0))
    # Ordinary values are unaffected.
    assert daily_update._dollar(0.01, 100.0) == pytest.approx(100.0 * math.exp(0.01))


def test_write_next_day_forecasts_includes_an_older_origin_date(tmp_path):
    # A ticker recovered behind the cursor (or first crossing warm-up
    # there) gets its fresh "tomorrow" forecast queued with an origin_date
    # that predates this invocation's own last_date -- the old filter
    # (origin_date == last_date, scanning every bucket) silently dropped
    # it from the headline CSV even though it's the ticker's current,
    # still-unresolved forecast (Codex review: "Predict from ticker rows
    # recovered behind the cursor"). Sourcing from outstanding[UNRESOLVED_
    # KEY] instead must include it, and must NOT include an entry that has
    # already resolved into a real target-date bucket.
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    recovered_entry = {
        "model": "persistence",
        "ticker": "LAGGY",
        "origin_date": "2021-05-28",  # several days before last_date
        "point": point,
        "quantiles": band,
        "ref_close": 50.0,
    }
    already_resolved_entry = {
        "model": "persistence",
        "ticker": "RESOLVED",
        "origin_date": "2021-06-01",
        "point": point,
        "quantiles": band,
        "ref_close": 60.0,
    }
    fake_harness = SimpleNamespace(
        outstanding={
            UNRESOLVED_KEY: [recovered_entry],
            "2021-06-02": [already_resolved_entry],
        },
        panel=SimpleNamespace(series={"LAGGY": object(), "RESOLVED": object()}),
    )
    path = daily_update._write_next_day_forecasts(tmp_path / "run", fake_harness, "2021-06-01")
    assert path is not None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["ticker"] for row in rows} == {"LAGGY"}


def test_write_next_day_forecasts_excludes_a_ticker_dropped_from_a_capped_universe(tmp_path):
    # rebind_unresolved() deliberately RETAINS an unresolved entry for a
    # ticker temporarily absent from the panel, in case it returns -- but
    # a capped --max-tickers universe's longest-history ranking can swap a
    # ticker out for good, and that permanently-excluded ticker's stale
    # forecast used to be exported on the daily CSV forever even though
    # run_meta.json's refreshed universe snapshot no longer lists it
    # (Codex review: "Filter next-day exports to the rebuilt universe").
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    current_entry = {
        "model": "persistence",
        "ticker": "KEPT",
        "origin_date": "2021-06-01",
        "point": point,
        "quantiles": band,
        "ref_close": 50.0,
    }
    dropped_entry = {
        "model": "persistence",
        "ticker": "DROPPED",
        "origin_date": "2021-05-20",  # its last forecast before losing its universe slot
        "point": point,
        "quantiles": band,
        "ref_close": 60.0,
    }
    fake_harness = SimpleNamespace(
        outstanding={UNRESOLVED_KEY: [current_entry, dropped_entry]},
        panel=SimpleNamespace(series={"KEPT": object()}),  # DROPPED is no longer in the rebuilt panel
    )
    path = daily_update._write_next_day_forecasts(tmp_path / "run", fake_harness, "2021-06-01")
    assert path is not None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["ticker"] for row in rows} == {"KEPT"}


def test_write_next_day_forecasts_marks_a_nan_component_invalid_instead_of_a_fabricated_price(tmp_path):
    # A NaN model output used to sanitize to 0.0 log-return (a "no
    # change" price identical to ref_close) in the exported CSV, hiding a
    # numerically failed model behind a plausible-looking row a user
    # reading the export had no way to distinguish from a genuine flat
    # forecast (Codex review: "Reject NaN forecasts before exporting
    # prices"). The scoring path's own sanitizer already penalizes this
    # as an extreme miss for accuracy purposes; the daily CSV is a
    # separate, human-facing publication path that must say so
    # explicitly instead.
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    point["close"] = float("nan")  # the model's close point head diverged
    entry = {
        "model": "persistence",
        "ticker": "BROKEN",
        "origin_date": "2021-06-01",
        "point": point,
        "quantiles": band,
        "ref_close": 50.0,
    }
    fake_harness = SimpleNamespace(
        outstanding={UNRESOLVED_KEY: [entry]},
        panel=SimpleNamespace(series={"BROKEN": object()}),
    )
    path = daily_update._write_next_day_forecasts(tmp_path / "run", fake_harness, "2021-06-01")
    assert path is not None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    row = rows[0]
    assert row["pred_close"] == "N/A"
    assert row["pred_close"] != f"{50.0:.4f}"  # not fabricated as ref_close's flat price
    # Untouched components still export a real dollar figure.
    assert float(row["pred_open"]) == pytest.approx(50.0)


def test_emit_daily_artifacts_counts_recovered_forecasts_in_predicted_tickers(tmp_path):
    # n_predicted must match _write_next_day_forecasts()'s own selection
    # (outstanding[UNRESOLVED_KEY]): a recovered or newly warmed-up
    # ticker's forecast deliberately keeps an OLDER origin_date than
    # last_date, and the old origin_date == last_date filter silently
    # zeroed out predicted_tickers and the digest line even though the CSV
    # this same invocation just wrote was non-empty (Codex review: "Count
    # recovered forecasts in the daily summary").
    run_dir = tmp_path / "run"
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    recovered_entry = {
        "model": "persistence",
        "ticker": "LAGGY",
        "origin_date": "2021-05-28",  # predates last_date below
        "point": point,
        "quantiles": band,
        "ref_close": 50.0,
    }
    fake_config = SimpleNamespace(models=("persistence",))
    fake_harness = SimpleNamespace(
        config=fake_config,
        running={"persistence": SimpleNamespace(in_band_close=0, n=0)},
        outstanding={UNRESOLVED_KEY: [recovered_entry]},
        panel=SimpleNamespace(series={"LAGGY": object()}),
    )
    daily_update._emit_daily_artifacts(
        run_dir, fake_harness, fake_config, "2021-06-01", 1, ["2021-06-01"], new_records=[]
    )
    summary = json.loads((run_dir / "daily" / "2021-06-01_summary.json").read_bytes())
    assert summary["predicted_tickers"] == 1


def test_emit_daily_artifacts_excludes_a_dropped_ticker_from_predicted_tickers(tmp_path):
    # predicted_tickers must match the CSV's own selection (Codex review:
    # "Filter next-day exports to the rebuilt universe"): a ticker
    # permanently displaced from a capped --max-tickers universe keeps a
    # stale unresolved entry internally (in case it returns), but that
    # entry must not inflate the reported predicted_tickers count once
    # it's no longer in the current panel.
    run_dir = tmp_path / "run"
    band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
    point = {c: 0.0 for c in ("open", "high", "low", "close")}
    kept_entry = {
        "model": "persistence",
        "ticker": "KEPT",
        "origin_date": "2021-06-01",
        "point": point,
        "quantiles": band,
        "ref_close": 50.0,
    }
    dropped_entry = {
        "model": "persistence",
        "ticker": "DROPPED",
        "origin_date": "2021-05-20",
        "point": point,
        "quantiles": band,
        "ref_close": 60.0,
    }
    fake_config = SimpleNamespace(models=("persistence",))
    fake_harness = SimpleNamespace(
        config=fake_config,
        running={"persistence": SimpleNamespace(in_band_close=0, n=0)},
        outstanding={UNRESOLVED_KEY: [kept_entry, dropped_entry]},
        panel=SimpleNamespace(series={"KEPT": object()}),
    )
    daily_update._emit_daily_artifacts(
        run_dir, fake_harness, fake_config, "2021-06-01", 1, ["2021-06-01"], new_records=[]
    )
    summary = json.loads((run_dir / "daily" / "2021-06-01_summary.json").read_bytes())
    assert summary["predicted_tickers"] == 1


@pytest.fixture
def live_setup(tmp_path, market_factor):
    """A replay run caught up to an as-of cutoff, plus the fuller archive."""
    history = tmp_path / "ticker_data"
    history.mkdir()
    write_ticker_csv(
        history, "AAA", make_synthetic_ohlcv(seed=1, drift=0.0006, market_returns=market_factor, market_beta=1.0)
    )
    write_ticker_csv(
        history, "BBB", make_synthetic_ohlcv(seed=2, drift=-0.0002, market_returns=market_factor, market_beta=1.0)
    )
    write_ticker_csv(history, "ZZZ", make_synthetic_ohlcv(seed=5, market_beta=0.0))
    full = make_synthetic_ohlcv(seed=1, drift=0.0006, market_returns=market_factor, market_beta=1.0)
    all_dates = sorted(full["Date"].astype(str))
    # "Today" during the initial replay: everything after day 200 is unseen.
    replay_now = f"{all_dates[200]}T18:00"
    run_dir = tmp_path / "run"
    code = run_walkforward.main(
        [
            "--history-dir",
            str(history),
            "--all-tickers-in-archive",
            "--model",
            "persistence",
            "--start",
            all_dates[150],
            "--now",
            replay_now,
            "--run-dir",
            str(run_dir),
            "--checkpoint-every",
            "50",
        ]
    )
    assert code == 0
    return history, run_dir, all_dates


class TestDailyCycle:
    def test_one_new_day_is_processed_and_artifacts_written(self, live_setup, capsys):
        history, run_dir, all_dates = live_setup
        # One more closed trading day becomes visible.
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        output = capsys.readouterr().out
        assert "scored" in output and "predicted next day" in output
        processed_date = all_dates[200]
        summary_path = run_dir / "daily" / f"{processed_date}_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_bytes())
        assert summary["days_processed"] == [processed_date]
        forecasts_path = run_dir / "daily" / f"{processed_date}_next_day_forecasts.csv"
        assert forecasts_path.exists()
        header = forecasts_path.read_text().splitlines()[0]
        for column in ("ticker", "model", "pred_open", "pred_high", "pred_low", "pred_close", "close_p10", "close_p90"):
            assert column in header

    def test_rerun_is_a_noop(self, live_setup, capsys):
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        capsys.readouterr()
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        assert "NOOP" in capsys.readouterr().out

    def test_refuses_the_noop_check_while_another_process_holds_the_run_lock(self, live_setup, capsys):
        # The NOOP integrity pre-check calls WalkForwardHarness.verify_
        # checkpoint_integrity(), whose own Ledger.resume() truncates any
        # ledger bytes appended past the checkpoint boundary -- used to
        # run WITHOUT holding the per-run lock a concurrent replay/daily-
        # update process relies on, racing that mutation against whatever
        # the lock holder is doing (Codex review: "Acquire the run lock
        # before the NOOP integrity pass"). Simulate a concurrent holder
        # by acquiring the SAME lock directly before invoking main().
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        capsys.readouterr()
        lock = harness_module._acquire_run_lock(run_dir)
        try:
            code = daily_update.main(
                ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]
            )
            assert code == 1
            assert "already holds the lock" in capsys.readouterr().err
        finally:
            harness_module._release_run_lock(lock)
        # The lock-holder's release lets a later, genuinely uncontended
        # invocation succeed normally.
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0

    def test_holds_the_run_lock_through_artifact_publication(self, live_setup, capsys, monkeypatch):
        # harness.run() used to release the per-run lock in its own finally,
        # BEFORE _emit_daily_artifacts() reads the just-written ledger/
        # checkpoint to build the CSV + summary -- a second process could
        # win that gap and start mutating the run directory while this
        # invocation was still reading it (Codex review: "Hold the run lock
        # through artifact publication"). Prove the lock is still held by
        # attempting a second acquire from INSIDE _emit_daily_artifacts()
        # itself: a concurrent holder must see the same "already holds the
        # lock" refusal main() would print if a second process raced it for
        # real.
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        original = daily_update._emit_daily_artifacts
        attempted = []

        def _spy(run_dir_arg, *args, **kwargs):
            attempted.append(True)
            with pytest.raises(harness_module.HarnessError, match="already holds the lock"):
                harness_module._acquire_run_lock(run_dir_arg)
            return original(run_dir_arg, *args, **kwargs)

        monkeypatch.setattr(daily_update, "_emit_daily_artifacts", _spy)
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        assert attempted  # sanity: the spy actually ran
        # The lock is released afterward -- a later invocation is not blocked.
        capsys.readouterr()
        next_now2 = f"{all_dates[202]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now2])
        assert code == 0

    def test_same_day_retry_never_reloads_models(self, live_setup, capsys, monkeypatch):
        # A same-day retry against an unchanged archive has no work: no
        # pending calendar dates, no rebind would fire, and the summary
        # already exists -- the checkpoint alone already proves this is a
        # confirmed NOOP. WalkForwardHarness.resume() deterministically
        # refits every GBM estimator on load, so paying that cost just to
        # reach the identical "NOOP: already processed" wastes the full
        # GBM load/refit on every retry (Codex review: "Avoid refitting
        # GBM before a same-day NOOP").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        capsys.readouterr()

        def _refuse_resume(*args, **kwargs):
            raise AssertionError("WalkForwardHarness.resume() must not run on a confirmed same-day NOOP")

        monkeypatch.setattr(daily_update.WalkForwardHarness, "resume", classmethod(_refuse_resume))
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        assert "NOOP" in capsys.readouterr().out

    def test_same_day_retry_refuses_instead_of_noop_on_ledger_corruption(self, live_setup, capsys):
        # A confirmed-NOOP short circuit that skips ALL checkpoint
        # verification (not just the expensive GBM load) could report
        # NOOP for a run whose ledger shard was corrupted or tampered
        # with AFTER its summary was written, postponing the REFUSED
        # signal a scheduled retry should have surfaced immediately
        # (Codex review: "Validate checkpoint integrity before returning
        # NOOP"). The short circuit must still perform the cheap,
        # read-only integrity checks before trusting what it reads.
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        capsys.readouterr()

        shard = next((run_dir / "ledger" / "persistence").glob("forecast_*.jsonl.gz"))
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        shard.write_bytes(bytes(raw))

        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_run_meta_is_not_refreshed_when_checkpoint_integrity_fails(self, live_setup, capsys, monkeypatch):
        # refresh_run_meta_universe() overwrites run_meta.json in place;
        # running it BEFORE checkpoint integrity is verified meant a
        # REFUSED invocation (ledger shard or model artifact corrupted
        # after the last summary) still mutated the frozen metadata with
        # a universe snapshot from a panel that was never actually
        # applied (Codex review regression: "Validate the checkpoint
        # before refreshing run metadata").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        capsys.readouterr()

        shard = next((run_dir / "ledger" / "persistence").glob("forecast_*.jsonl.gz"))
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        shard.write_bytes(bytes(raw))

        def _refuse_refresh(*args, **kwargs):
            raise AssertionError("refresh_run_meta_universe() must not run before checkpoint integrity is verified")

        monkeypatch.setattr(daily_update, "refresh_run_meta_universe", _refuse_refresh)
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_catchup_processes_all_missed_days(self, live_setup):
        history, run_dir, all_dates = live_setup
        # Operator skipped three days; one invocation catches up all of them.
        later_now = f"{all_dates[204]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", later_now]) == 0
        summary = json.loads((run_dir / "daily" / f"{all_dates[203]}_summary.json").read_bytes())
        assert summary["days_processed"] == all_dates[200:204]

    def test_reads_and_writes_use_explicit_utf8(self, live_setup, capsys, monkeypatch):
        # Path.read_text()/write_text() without an explicit encoding defer
        # to the platform's preferred encoding, which is not always UTF-8
        # (Antigravity review: "Missing Explicit encoding='utf-8' in File
        # Operations"). JSON state reads now go through
        # Path.read_bytes() + json.loads(bytes) (perf: skip the redundant
        # text decode), which sidesteps the platform-locale question
        # entirely rather than relying on it -- json.loads decodes raw
        # bytes per RFC 8259's own UTF-8/16/32 auto-detection, never
        # locale.getpreferredencoding(). Assert those reads go through
        # read_bytes (never a bare, locale-dependent read_text()) and
        # that every JSON write in the daily cycle still passes
        # encoding="utf-8" explicitly.
        import pathlib

        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        seen_read: list[str] = []
        seen_write: list[object] = []
        original_read_text = pathlib.Path.read_text
        original_read_bytes = pathlib.Path.read_bytes
        original_write_text = pathlib.Path.write_text

        def spy_read_text(self, *args, **kwargs):
            if self.name in ("harness_state.json", "run_meta.json"):
                seen_read.append("read_text")
            return original_read_text(self, *args, **kwargs)

        def spy_read_bytes(self, *args, **kwargs):
            if self.name in ("harness_state.json", "run_meta.json"):
                seen_read.append("read_bytes")
            return original_read_bytes(self, *args, **kwargs)

        def spy_write_text(self, data, *args, **kwargs):
            if "_summary.json" in self.name:
                seen_write.append(kwargs.get("encoding"))
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", spy_read_text)
        monkeypatch.setattr(pathlib.Path, "read_bytes", spy_read_bytes)
        monkeypatch.setattr(pathlib.Path, "write_text", spy_write_text)
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        capsys.readouterr()
        assert code == 0
        assert seen_read and all(kind == "read_bytes" for kind in seen_read)
        assert seen_write and all(encoding == "utf-8" for encoding in seen_write)

    def test_a_crash_mid_write_never_leaves_a_partial_summary_at_the_real_path(
        self, live_setup, capsys, monkeypatch
    ):
        # If the process is killed while the summary is being written, a
        # direct Path.write_text() to the real destination can leave a
        # truncated file there; the NOOP path's existence-only check would
        # then treat that corrupt artifact as "already recovered" and never
        # repair it (Codex review regression: "Validate daily summaries
        # before treating them as recovered"). Simulate the kill by writing
        # HALF the intended bytes and then raising, and assert the real
        # <date>_summary.json path was never created -- only the atomic
        # write's temp sibling sees the partial bytes.
        import pathlib

        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        original_write_text = pathlib.Path.write_text

        def _crash_after_partial_write(self, data, *args, **kwargs):
            if "_summary.json" in self.name:
                original_write_text(self, data[: len(data) // 2], *args, **kwargs)
                raise OSError("simulated kill mid-write")
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", _crash_after_partial_write)
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 2  # runtime failure, per main()'s documented contract -- not a clean write
        assert "simulated kill mid-write" in capsys.readouterr().err
        processed_date = all_dates[200]
        summary_path = run_dir / "daily" / f"{processed_date}_summary.json"
        assert not summary_path.exists()

    def test_a_crash_mid_write_never_leaves_a_partial_forecasts_csv_at_the_real_path(
        self, live_setup, capsys, monkeypatch
    ):
        # Same failure mode as the summary.json crash above, but for the
        # headline next_day_forecasts.csv: a direct write to the real
        # destination could leave a truncated CSV there if killed mid-
        # write, and nothing besides a later rebound or a missing summary
        # ever regenerates it (Codex review regression: "Replace the
        # next-day forecast CSV atomically").
        import pathlib

        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        original_write_text = pathlib.Path.write_text

        def _crash_after_partial_write(self, data, *args, **kwargs):
            if "_next_day_forecasts.csv" in self.name:
                original_write_text(self, data[: len(data) // 2], *args, **kwargs)
                raise OSError("simulated kill mid-write")
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", _crash_after_partial_write)
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 2
        assert "simulated kill mid-write" in capsys.readouterr().err
        processed_date = all_dates[200]
        forecasts_path = run_dir / "daily" / f"{processed_date}_next_day_forecasts.csv"
        assert not forecasts_path.exists()

    def test_a_signal_during_setup_is_honored_not_lost(self, live_setup, capsys, monkeypatch):
        # A signal that arrives during the panel rebuild or
        # WalkForwardHarness.resume() (both can run for a while -- a large
        # archive, or a GBM refit-on-load) used to have no handler
        # installed at all until AFTER resume() completed: a SIGTERM would
        # kill the process immediately with no checkpoint, and a SIGINT's
        # KeyboardInterrupt isn't caught by main()'s (Refused, HarnessError,
        # LedgerError, TrainingDataChanged) handler -- both lose the cycle
        # instead of the documented clean-checkpoint exit 3 (Codex review
        # regression: "Install daily signal handlers before rebuilding or
        # resuming"). Simulate the signal by having the early-handler
        # installer itself return an already-tripped flag.
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"

        def fake_early_handlers():
            flag = run_walkforward._InterruptFlag()
            flag.requested = True  # simulate a signal caught during setup
            return flag

        monkeypatch.setattr(daily_update, "_install_early_signal_handlers", fake_early_handlers)
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 3
        assert "interrupted" in capsys.readouterr().out

    def test_dry_run_reports_pending_and_writes_nothing(self, live_setup, capsys):
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now, "--dry-run"]
        )
        assert code == 0
        output = capsys.readouterr().out
        assert "would process 1 day(s)" in output
        assert not (run_dir / "daily").exists()

    def test_dry_run_does_not_perform_ledger_recovery(self, live_setup, capsys):
        # Crash simulation: un-checkpointed bytes sit past the ledger
        # fingerprint. A dry run must leave them for the REAL run's recovery
        # (review regression: resume-time truncation ran before the dry-run
        # branch).
        import gzip

        history, run_dir, all_dates = live_setup
        shard = next((run_dir / "ledger" / "persistence").glob("forecast_*.jsonl.gz"))
        with shard.open("ab") as handle:
            handle.write(gzip.compress(b'{"crashed": true}\n', mtime=0))
        before = shard.read_bytes()
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now, "--dry-run"]
        )
        assert code == 0
        assert "nothing recovered" in capsys.readouterr().out
        assert shard.read_bytes() == before

    def test_dry_run_does_not_promote_checkpoint_prev_into_place(self, live_setup, capsys):
        # A crash between checkpoint()'s two renames leaves only
        # checkpoint.prev/ (checkpoint/ never re-appeared). The equivalent
        # real invocation's resume() promotes it back into checkpoint/ as
        # part of its documented recovery -- but --dry-run's own "nothing
        # written, nothing recovered" contract forbids that same rename
        # (Codex review: "Keep checkpoint fallback recovery read-only in
        # dry runs").
        history, run_dir, all_dates = live_setup
        (run_dir / "checkpoint").rename(run_dir / "checkpoint.prev")
        assert not (run_dir / "checkpoint").exists()
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now, "--dry-run"]
        )
        assert code == 0
        assert "nothing recovered" in capsys.readouterr().out
        assert not (run_dir / "checkpoint").exists()  # NOT promoted into place
        assert (run_dir / "checkpoint.prev").exists()  # untouched

    def test_dry_run_refuses_a_tampered_checkpointed_shard_instead_of_approving_it(self, live_setup, capsys):
        # The old dry-run branch read only harness_state.json -- a
        # checkpointed ledger shard that was tampered with or corrupted
        # WITHIN the checkpointed boundary (not the un-checkpointed-tail
        # case above) was never hashed, so --dry-run reported a clean plan
        # even though the equivalent real invocation immediately refuses
        # via WalkForwardHarness.resume()'s SHA-256 verification (Codex
        # review: "Verify checkpoint integrity during daily dry runs").
        history, run_dir, all_dates = live_setup
        shard = next((run_dir / "ledger" / "persistence").glob("forecast_*.jsonl.gz"))
        raw = bytearray(shard.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        tampered = bytes(raw)
        shard.write_bytes(tampered)
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now, "--dry-run"]
        )
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err
        assert shard.read_bytes() == tampered  # untouched, not "recovered"

    def test_dry_run_discloses_a_pending_rebind_instead_of_reporting_a_false_noop(self, live_setup, capsys):
        # A lagging ticker can gain its next row on a date that's already
        # present in the global calendar -- no pending calendar dates, so
        # the naive plan reports NOOP -- but the REAL invocation would still
        # call rebind_unresolved(), which immediately scores the ticker,
        # appends to the ledger, and checkpoints. The dry-run plan must
        # disclose that work rather than claiming nothing would happen
        # (Codex review regression: "Include rebound scoring in daily
        # dry-run plans").
        history, run_dir, all_dates = live_setup
        same_now = f"{all_dates[200]}T18:00"  # identical to live_setup's own --now: zero new calendar days
        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_bytes())
        ticker, origin_row = "AAA", 150
        origin_date = all_dates[origin_row]
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        state["outstanding"].setdefault("", []).append(
            {
                "model": "persistence",
                "ticker": ticker,
                "origin_row": origin_row,
                "origin_date": origin_date,
                "point": {c: 0.0 for c in ("open", "high", "low", "close")},
                "quantiles": band,
                "quantiles_raw": band,
                "ref_close": 100.0,
                "adj_ratio": 1.0,
            }
        )
        state_path.write_text(json.dumps(state))
        before = state_path.read_text()

        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", same_now, "--dry-run"]
        )
        assert code == 0
        output = capsys.readouterr().out
        assert "would rebind and score 1" in output
        assert "would be a NOOP" not in output
        assert not (run_dir / "daily").exists()  # still genuinely non-destructive
        assert state_path.read_text() == before  # and nothing was mutated

    def test_count_pending_rebinds_matches_despite_an_intraday_stamped_origin_date(self, live_setup, monkeypatch):
        # A read-only mirror of rebind_unresolved()'s own matching logic;
        # must stay in sync with its calendar-day fix or the dry-run
        # disclosure would disagree with what the real invocation does
        # (Codex review regression: "Rebind live forecasts by calendar
        # day").
        import dataclasses

        import features
        import numpy as np

        history, run_dir, all_dates = live_setup
        panel = features.build_panel(history, min_history=60)
        ticker = "AAA"
        series = panel.series[ticker]
        origin_row = 100
        origin_date_iso = str(np.datetime_as_string(series.dates[origin_row], unit="D"))

        intraday_dates = series.dates.copy()
        intraday_dates[origin_row] = intraday_dates[origin_row] + np.timedelta64(16, "h")
        panel.series[ticker] = dataclasses.replace(series, dates=intraday_dates)

        # This test's fake `state` only carries one ticker's entry, unlike a
        # real checkpoint where every panel ticker already has one -- without
        # this, BBB/ZZZ would otherwise look "newly admitted" to the separate
        # terminal-row check (Codex review: "Treat newly admitted terminal
        # rows as pending work"), which is irrelevant here.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])
        state = {"outstanding": {"": [{"ticker": ticker, "origin_date": origin_date_iso}]}}
        assert daily_update._count_pending_rebinds(state, panel) == 1

    def test_count_pending_rebinds_counts_scoreable_dated_bucket_entries(self, live_setup, monkeypatch):
        # step()'s own missing-ticker branch re-queues a matured forecast
        # under its UNCHANGED dated bucket key -- not UNRESOLVED_KEY --
        # when the ticker was temporarily absent that day. Once the panel
        # has no pending calendar dates left, rebind_unresolved() now
        # sweeps these too; this read-only mirror must count them the
        # same way or the NOOP short-circuit disagrees with what a real
        # invocation actually does (Codex review: "Process restored
        # dated forecasts before NOOP").
        import features

        history, run_dir, all_dates = live_setup
        panel = features.build_panel(history, min_history=60)
        # See the intraday-stamp test above for why this is suppressed.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])
        state = {"outstanding": {"2021-08-05": [{"ticker": "AAA", "origin_date": "2021-08-04"}]}}
        assert daily_update._count_pending_rebinds(state, panel) == 1

    def test_count_pending_rebinds_counts_a_deleted_origin_row_as_pending_work(self, live_setup, monkeypatch):
        # When an archive correction deletes an unresolved forecast's
        # origin row while its ticker remains in the panel,
        # rebind_unresolved() now unconditionally drops the stale entry
        # AND may issue a fresh terminal-row replacement forecast for the
        # ticker -- real, checkpointed state changes either way. This
        # read-only mirror used to `continue` without counting that case at
        # all, so if the daily summary already existed, the NOOP short-
        # circuit fired before WalkForwardHarness.resume() +
        # rebind_unresolved() ever ran, leaving the stale forecast
        # checkpointed forever and the corrected ticker without its
        # replacement (Codex review: "Count deleted-origin forecasts as
        # pending work").
        import features

        history, run_dir, all_dates = live_setup
        panel = features.build_panel(history, min_history=60)
        # Isolates this test to the specific loop under fix -- see the
        # intraday-stamp test above for why the terminal-row check (a
        # SEPARATE mirror of a different rebind_unresolved() path) is
        # suppressed here.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])
        state = {"outstanding": {"": [{"ticker": "AAA", "origin_date": "1999-01-01"}]}}
        assert daily_update._count_pending_rebinds(state, panel) == 1

    def test_count_pending_rebinds_skips_dated_entries_for_a_still_missing_ticker(self, live_setup, monkeypatch):
        import features

        history, run_dir, all_dates = live_setup
        panel = features.build_panel(history, min_history=60)
        # See the intraday-stamp test above for why this is suppressed.
        monkeypatch.setattr(panel, "eligible_on", lambda date: [])
        state = {"outstanding": {"2021-08-05": [{"ticker": "GHOST", "origin_date": "2021-08-04"}]}}
        assert daily_update._count_pending_rebinds(state, panel) == 0

    def test_count_pending_rebinds_counts_a_newly_eligible_ticker_with_no_prior_forecast(self, live_setup):
        # A ticker can join the CURRENT panel's terminal row eligible for
        # its first-ever forecast without any new calendar date -- a
        # restored CSV, a backfilled IPO crossing min_history, or a
        # capped-universe ranking swap. It has NO outstanding entry at all
        # yet, so neither of the two loops above ever sees it; without this,
        # the NOOP short-circuit fired before WalkForwardHarness.resume() +
        # rebind_unresolved() ever ran, permanently skipping its only
        # chance at a forecast for that terminal row (Codex review: "Treat
        # newly admitted terminal rows as pending work").
        import pandas as pd

        import features

        history, run_dir, all_dates = live_setup
        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_bytes())
        as_of = pd.Timestamp(f"{all_dates[200]}T18:00")

        panel = features.build_panel(history, min_history=60, as_of=as_of)
        assert daily_update._count_pending_rebinds(state, panel) == 0  # baseline: same as a real rerun's NOOP

        # Same default start/length as AAA/BBB/ZZZ, so its business-day
        # calendar lands on the exact same dates -- including all_dates[200],
        # the panel's terminal row -- with well over min_history rows before it.
        write_ticker_csv(history, "NEWCO", make_synthetic_ohlcv(seed=99))
        grown_panel = features.build_panel(history, min_history=60, as_of=as_of)
        assert "NEWCO" in grown_panel.tickers
        assert daily_update._count_pending_rebinds(state, grown_panel) == 1

    def test_dry_run_anchors_at_configured_start_when_no_step_has_ever_run(self, live_setup, capsys):
        # last_processed_date is null only when NO step has ever completed
        # (self.cursor stayed 0 at checkpoint time -- e.g. config.start at
        # or before the archive's very first date). The real resume path
        # falls back to searchsorted(panel.calendar, config.start) in that
        # case; the dry-run branch fell back to cursor 0 instead, reporting
        # every PRE-START archive date as "pending" (Codex review
        # regression: "Anchor first-step daily dry runs at the configured
        # start"). Hand-edits the checkpoint to this precise, narrow
        # condition rather than relying on a CLI flag combination to
        # reproduce it.
        history, run_dir, all_dates = live_setup
        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_bytes())
        config_start = state["config"]["start"]
        assert config_start > all_dates[0]  # sanity: pre-start dates genuinely exist in this archive
        state["last_processed_date"] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")

        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now, "--dry-run"]
        )
        assert code == 0
        line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("would process"))
        reported_dates = [token.strip() for token in line.split(": ", 1)[1].split(",")]
        assert all(date >= config_start for date in reported_dates)

    def test_missing_checkpoint_is_refused(self, tmp_path, capsys):
        code = daily_update.main(["--run-dir", str(tmp_path / "nope")])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_missing_run_meta_is_refused_not_silently_reinterpreted(self, live_setup, capsys):
        # If the process died between fresh()'s checkpoint and
        # _write_run_meta() running, run_meta.json can be absent even
        # though a valid-looking checkpoint exists. Silently falling back
        # to "the whole archive" for selection / "the machine's configured
        # default archive" for history_dir can expand the vocabulary,
        # refit GBM, and ledger forecasts for a DIFFERENT universe or
        # dataset than the run was ever validated against -- refuse
        # instead (Codex review regression: "Refuse daily updates without
        # frozen run metadata").
        history, run_dir, all_dates = live_setup
        (run_dir / "run_meta.json").unlink()
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 1
        stderr = capsys.readouterr().err
        assert "REFUSED" in stderr
        assert "run_meta.json" in stderr

    def test_now_before_the_checkpoint_cursor_is_refused(self, live_setup, capsys):
        # An earlier --now (or a transiently shrunk archive) truncates the
        # panel; without this guard, resume() would re-anchor the cursor
        # at the truncated panel's own end -- EARLIER than what was
        # already processed -- and a later normal invocation would
        # reprocess those dates and append duplicate forecasts/scores,
        # the same failure mode run_walkforward.py's --resume path
        # already guards against (Codex review regression: "Reject daily
        # panels that end before the checkpoint cursor").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        capsys.readouterr()
        earlier_now = f"{all_dates[199]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", earlier_now])
        assert code == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_dry_run_before_the_checkpoint_cursor_is_also_refused(self, live_setup, capsys):
        # --dry-run's whole purpose is to preview what the real invocation
        # would do, but the cursor-truncation check ran AFTER dry-run's own
        # early return, so an earlier --now (or a transiently shrunk
        # archive) reported "would be a NOOP" / "would process N days" from
        # --dry-run while the equivalent real invocation immediately
        # refused -- the opposite result from the operation being previewed
        # (Codex review: "Validate truncated panels during daily dry
        # runs").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        capsys.readouterr()
        earlier_now = f"{all_dates[199]}T18:00"
        code = daily_update.main(
            ["--run-dir", str(run_dir), "--history-dir", str(history), "--now", earlier_now, "--dry-run"]
        )
        assert code == 1
        err = capsys.readouterr().err
        assert "REFUSED" in err
        assert "nothing written" not in err  # never reached the dry-run report at all

    def test_refused_invocation_does_not_mutate_run_meta_universe(self, live_setup, capsys, monkeypatch):
        # refresh_run_meta_universe() overwrites run_meta.json's universe
        # snapshot in place; running it BEFORE the cursor-truncation check
        # meant a refused invocation (earlier --now, or a transiently
        # shrunk archive) still mutated the frozen metadata with the
        # invalid/truncated panel's universe on its way out (Codex review
        # regression: "Refresh run metadata only after resume validation").
        # Spies on the call directly rather than diffing run_meta.json's
        # bytes: with this fixture's fixed ticker set, the universe
        # snapshot a premature refresh would write happens to be byte-
        # identical to the one already there, which would make a bytes
        # comparison pass regardless of call order.
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        capsys.readouterr()

        called: bool = False
        original = daily_update.refresh_run_meta_universe

        def spy(*args, **kwargs):
            nonlocal called
            called = True
            return original(*args, **kwargs)

        monkeypatch.setattr(daily_update, "refresh_run_meta_universe", spy)
        earlier_now = f"{all_dates[199]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", earlier_now])
        assert code == 1
        assert not called

    def test_zero_usable_tickers_is_refused_exit_1_not_2(self, live_setup, capsys):
        # features.build_panel() raises a plain ValueError already prefixed
        # "REFUSED: no usable tickers" when every selected ticker becomes
        # unusable (e.g. a bad archive sync overwrote every CSV with
        # header-only data). daily_update.py didn't translate it the way
        # run_walkforward.py's _build_panel() already did, so it fell
        # through to main()'s generic Exception handler and reported exit 2
        # "RUNTIME FAILURE" instead of the documented exit 1 "REFUSED" for a
        # data problem -- automation would then retry a data issue as if it
        # were a crash (Codex review regression: "Classify unusable daily
        # archives as refusals").
        history, run_dir, all_dates = live_setup
        for csv_path in history.glob("*.csv"):
            csv_path.write_text("Date,Open,High,Low,Close\n")
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 1
        stderr = capsys.readouterr().err
        assert "REFUSED" in stderr
        assert "no usable tickers" in stderr

    def test_noop_checkpoints_immediate_rebound_scores(self, live_setup, capsys):
        # live_setup already leaves the harness fully caught up to its own
        # --now truncation (cursor == len(calendar)). Inject an unresolved
        # forecast whose origin is an already-fully-visible earlier day, so
        # rebind_unresolved() resolves it WITHOUT a new calendar day
        # appearing -- hitting the "no calendar step remains" immediate-
        # scoring path while daily_update.py's cursor check still lands on
        # NOOP. Without a checkpoint there, the score is silently discarded
        # on every subsequent invocation (Codex review regression:
        # "Checkpoint immediate rebound scores before returning NOOP").
        history, run_dir, all_dates = live_setup
        same_now = f"{all_dates[200]}T18:00"  # identical to live_setup's own --now
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", same_now]) == 0
        capsys.readouterr()

        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_bytes())
        ticker, origin_row = "AAA", 150
        origin_date = all_dates[origin_row]
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        state["outstanding"].setdefault("", []).append(
            {
                "model": "persistence",
                "ticker": ticker,
                "origin_row": origin_row,
                "origin_date": origin_date,
                "point": {c: 0.0 for c in ("open", "high", "low", "close")},
                "quantiles": band,
                "quantiles_raw": band,
                "ref_close": 100.0,
                "adj_ratio": 1.0,
            }
        )
        state_path.write_text(json.dumps(state))

        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", same_now])
        assert code == 0
        assert "NOOP" in capsys.readouterr().out
        new_state = json.loads(state_path.read_bytes())
        assert not any(
            entry.get("ticker") == ticker and entry.get("origin_date") == origin_date
            for entries in new_state["outstanding"].values()
            for entry in entries
        )
        # A later, unrelated invocation must not lose the score to
        # Ledger.resume()'s truncate-to-old-fingerprint contract.
        code2 = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", same_now])
        assert code2 == 0

    def test_noop_regenerates_daily_artifacts_after_a_rebound_even_if_summary_exists(self, live_setup, capsys):
        # A rebound during the NOOP path appends a score and mutates
        # calibrator/running/outstanding state; if the day's summary.json
        # already exists (from an EARLIER invocation, before the rebound),
        # the existence check must not skip regenerating it -- otherwise
        # scored_today/rebound_forecasts/health and the operator digest
        # stay permanently stale relative to the checkpoint (Codex review
        # regression: "Regenerate daily artifacts after immediate
        # rebounds").
        history, run_dir, all_dates = live_setup
        same_now = f"{all_dates[200]}T18:00"  # identical to live_setup's own --now: already fully caught up
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", same_now]) == 0
        capsys.readouterr()
        processed_date = all_dates[199]  # last VISIBLE day under this --now cutoff
        summary_path = run_dir / "daily" / f"{processed_date}_summary.json"
        assert summary_path.exists()
        before_summary = json.loads(summary_path.read_bytes())
        assert before_summary["rebound_forecasts"] == 0

        state_path = run_dir / "checkpoint" / "harness_state.json"
        state = json.loads(state_path.read_bytes())
        ticker, origin_row = "AAA", 150
        origin_date = all_dates[origin_row]
        band = {c: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for c in ("open", "high", "low", "close")}
        state["outstanding"].setdefault("", []).append(
            {
                "model": "persistence",
                "ticker": ticker,
                "origin_row": origin_row,
                "origin_date": origin_date,
                "point": {c: 0.0 for c in ("open", "high", "low", "close")},
                "quantiles": band,
                "quantiles_raw": band,
                "ref_close": 100.0,
                "adj_ratio": 1.0,
            }
        )
        state_path.write_text(json.dumps(state))

        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", same_now])
        assert code == 0
        assert "NOOP" in capsys.readouterr().out
        after_summary = json.loads(summary_path.read_bytes())
        assert after_summary["rebound_forecasts"] == 1

    def test_daily_update_reuses_the_runs_recorded_history_dir(self, live_setup, capsys):
        # The documented daily command supplies only --run-dir; it must
        # reuse the archive the run was ORIGINALLY built from instead of
        # falling back to the machine's configured default, which does not
        # exist in this sandbox (Codex review regression: "Reuse the run's
        # history directory for daily updates").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--now", next_now])  # no --history-dir
        assert code == 0
        assert "scored" in capsys.readouterr().out

    def test_noop_recovers_missing_daily_artifacts(self, live_setup, capsys):
        # A prior invocation may have advanced the cursor (harness.run()
        # checkpointed) and then been killed before the daily artifacts were
        # written; rerunning must not NOOP silently forever (Codex review
        # regression: "Retry daily artifact generation after a completed
        # run").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        processed_date = all_dates[200]
        summary_path = run_dir / "daily" / f"{processed_date}_summary.json"
        forecasts_path = run_dir / "daily" / f"{processed_date}_next_day_forecasts.csv"
        assert summary_path.exists() and forecasts_path.exists()
        summary_path.unlink()
        forecasts_path.unlink()
        capsys.readouterr()
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        output = capsys.readouterr().out
        assert "NOOP" in output and "recovering missing daily artifacts" in output
        assert summary_path.exists()
        assert forecasts_path.exists()

    def test_noop_recovers_a_missing_forecast_csv_even_though_the_summary_survives(self, live_setup, capsys):
        # summary_path.exists() alone used to be treated as proof the WHOLE
        # artifact set was recovered -- but the summary and the next-day
        # forecasts CSV are two separately written files, and a power loss
        # between the two (or an external deletion of just the CSV) can
        # leave a valid summary referencing a next_day_forecasts_csv path
        # that no longer exists. Every later same-day NOOP used to accept
        # that forever, so the headline forecast CSV was never regenerated
        # even though the checkpoint held everything needed to rebuild it
        # (Codex review: "Verify the forecast CSV before returning NOOP").
        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        processed_date = all_dates[200]
        summary_path = run_dir / "daily" / f"{processed_date}_summary.json"
        forecasts_path = run_dir / "daily" / f"{processed_date}_next_day_forecasts.csv"
        assert summary_path.exists() and forecasts_path.exists()
        forecasts_path.unlink()  # summary survives; only the CSV is gone
        capsys.readouterr()
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        output = capsys.readouterr().out
        assert "NOOP" in output and "recovering missing daily artifacts" in output
        assert forecasts_path.exists()

    def test_daily_artifacts_are_complete_checks_the_referenced_csv(self, tmp_path):
        # Unit-level coverage of the helper itself: a null
        # next_day_forecasts_csv is legitimate (nothing was pending that
        # day); a non-null path that doesn't exist, or a summary that
        # doesn't parse at all, means recovery is still needed.
        missing = tmp_path / "missing_summary.json"
        assert daily_update._daily_artifacts_are_complete(missing) is False

        malformed = tmp_path / "malformed_summary.json"
        malformed.write_text("{not valid json")
        assert daily_update._daily_artifacts_are_complete(malformed) is False

        null_csv = tmp_path / "null_csv_summary.json"
        null_csv.write_text(json.dumps({"next_day_forecasts_csv": None}))
        assert daily_update._daily_artifacts_are_complete(null_csv) is True

        dangling = tmp_path / "dangling_summary.json"
        dangling.write_text(json.dumps({"next_day_forecasts_csv": str(tmp_path / "gone.csv")}))
        assert daily_update._daily_artifacts_are_complete(dangling) is False

        real_csv = tmp_path / "real.csv"
        real_csv.write_text("ticker\nAAA\n")
        complete = tmp_path / "complete_summary.json"
        complete.write_text(json.dumps({"next_day_forecasts_csv": str(real_csv)}))
        assert daily_update._daily_artifacts_are_complete(complete) is True

    def test_day_scores_finds_forecasts_from_an_older_origin_shard(self, live_setup):
        # A forecast's ORIGIN shard year can be more than one calendar year
        # behind its TARGET date if a ticker resumes trading after a long
        # gap; _day_scores must not guess which years to scan (Codex review
        # regression: "Scan all origin-year shards for daily scores").
        import gzip
        import json as json_module
        from types import SimpleNamespace

        history, run_dir, all_dates = live_setup
        next_now = f"{all_dates[201]}T18:00"
        assert daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now]) == 0
        processed_date = all_dates[200]

        # Move the just-written score record into a shard several years
        # older than its target date, simulating a long-gap origin shard.
        shard_dir = run_dir / "ledger" / "persistence"
        current_shard = next(shard_dir.glob(f"score_{processed_date[:4]}.jsonl.gz"))
        records = list(
            json_module.loads(line)
            for line in gzip.decompress(current_shard.read_bytes()).decode("utf-8").splitlines()
            if line
        )
        expected = [record for record in records if record["target_date"] == processed_date]
        assert expected
        old_year = str(int(processed_date[:4]) - 5)
        old_shard = shard_dir / f"score_{old_year}.jsonl.gz"
        old_shard.write_bytes(gzip.compress(("\n".join(json_module.dumps(r) for r in records) + "\n").encode()))
        current_shard.unlink()

        # A minimal stand-in exposing only what _day_scores reads off the
        # harness -- avoids the ledger fingerprint/checkpoint machinery a
        # real resume() would (correctly) refuse after shards were moved.
        fake_harness = SimpleNamespace(
            config=SimpleNamespace(models=("persistence",)),
            running={"persistence": SimpleNamespace(in_band_close=0, n=0)},
        )
        scores = daily_update._day_scores(run_dir, fake_harness, processed_date)
        assert scores["persistence"]["scored_today"] == len(expected)

    def test_day_scores_uses_provided_records_without_rescanning_the_ledger(self, tmp_path):
        # On a full-universe replay the score ledger holds tens of millions
        # of records; the "one-day live cycle" must not decompress and
        # parse every one of them on every invocation just to find today's
        # handful (Codex review regression: "Avoid rescanning the full
        # score ledger after every daily run"). Prove records= is actually
        # consulted -- not silently ignored in favor of a ledger scan -- by
        # handing it something the on-disk ledger does NOT contain and
        # confirming that's what gets reported, while a record that IS on
        # disk but absent from records= is ignored.
        from types import SimpleNamespace

        from ledger import Ledger

        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        on_disk = {
            "schema_version": 1,
            "record_type": "score",
            "model": "persistence",
            "ticker": "ONDISK",
            "origin_date": "2021-06-01",
            "target_date": "2021-06-02",
            "seed": 0,
            "score": {"abs_err": {"close": 0.01}, "in_band": {"close": True}},
        }
        ledger.append([on_disk])
        fake_harness = SimpleNamespace(
            config=SimpleNamespace(models=("persistence",)),
            running={"persistence": SimpleNamespace(in_band_close=0, n=0)},
        )
        fresh_record = {
            "schema_version": 1,
            "record_type": "score",
            "model": "persistence",
            "ticker": "FRESH",
            "origin_date": "2021-06-01",
            "target_date": "2021-06-02",
            "seed": 0,
            "score": {"abs_err": {"close": 0.05}, "in_band": {"close": False}},
        }
        summary = daily_update._day_scores(run_dir, fake_harness, "2021-06-02", records=[fresh_record])
        assert summary["persistence"]["scored_today"] == 1
        assert summary["persistence"]["close_mae_today"] == pytest.approx(0.05)
        assert summary["persistence"]["in_band_today"] == 0.0

    def test_beats_persistence_is_computed_against_a_zero_error_baseline(self, tmp_path):
        # persistence["close_mae_today"] == 0.0 (an exact prediction, e.g.
        # a flat single-ticker archive) is a valid zero-error baseline, not
        # a missing one; the old truthy check on that value silently
        # skipped every beats_persistence comparison instead of correctly
        # marking another model's positive error as not beating a perfect
        # baseline (Codex review regression: "Compare against a zero-error
        # persistence baseline").
        from types import SimpleNamespace

        run_dir = tmp_path / "run"
        fake_harness = SimpleNamespace(
            config=SimpleNamespace(models=("persistence", "gbm_quantile")),
            running={
                "persistence": SimpleNamespace(in_band_close=0, n=0),
                "gbm_quantile": SimpleNamespace(in_band_close=0, n=0),
            },
        )
        records = [
            {
                "schema_version": 1, "record_type": "score", "model": "persistence", "ticker": "AAA",
                "origin_date": "2021-06-01", "target_date": "2021-06-02", "seed": 0,
                "score": {"abs_err": {"close": 0.0}, "in_band": {"close": True}},
            },
            {
                "schema_version": 1, "record_type": "score", "model": "gbm_quantile", "ticker": "AAA",
                "origin_date": "2021-06-01", "target_date": "2021-06-02", "seed": 0,
                "score": {"abs_err": {"close": 0.02}, "in_band": {"close": True}},
            },
        ]
        summary = daily_update._day_scores(run_dir, fake_harness, "2021-06-02", records=records)
        assert summary["persistence"]["close_mae_today"] == 0.0
        assert summary["gbm_quantile"]["beats_persistence"] is False


class TestGbmMissingTickerResilience:
    def test_daily_update_survives_a_temporarily_missing_ticker_with_gbm(self, tmp_path, market_factor, capsys):
        # A daily archive rebuild can transiently skip a previously-trained
        # ticker (its CSV missing/unreadable that day); GbmQuantileModel's
        # strict fingerprint check does not distinguish that from a
        # genuine archive correction, so with the default strict=True the
        # ENTIRE daily cycle for every model refuses -- not just this
        # ticker (Codex review regression: "Keep missing tickers from
        # blocking GBM daily resumes").
        history = tmp_path / "ticker_data"
        history.mkdir()
        write_ticker_csv(
            history, "AAA", make_synthetic_ohlcv(seed=1, drift=0.0006, market_returns=market_factor, market_beta=1.0)
        )
        write_ticker_csv(
            history, "BBB", make_synthetic_ohlcv(seed=2, drift=-0.0002, market_returns=market_factor, market_beta=1.0)
        )
        full = make_synthetic_ohlcv(seed=1, drift=0.0006, market_returns=market_factor, market_beta=1.0)
        all_dates = sorted(full["Date"].astype(str))
        replay_now = f"{all_dates[200]}T18:00"
        run_dir = tmp_path / "run"
        code = run_walkforward.main(
            [
                "--history-dir", str(history), "--all-tickers-in-archive",
                "--model", "gbm", "--start", all_dates[150], "--now", replay_now,
                "--run-dir", str(run_dir), "--checkpoint-every", "50",
            ]
        )
        assert code == 0

        # BBB's CSV is transiently gone the next day -- a network hiccup on
        # the mounted archive, not an archive correction.
        (history / "BBB.csv").unlink()
        next_now = f"{all_dates[201]}T18:00"
        code = daily_update.main(["--run-dir", str(run_dir), "--history-dir", str(history), "--now", next_now])
        assert code == 0
        assert "scored" in capsys.readouterr().out


class TestEncoderHealthRetrainBaseline:
    def _diagnostic_records(self, dates, abs_err_close):
        return [
            {
                "schema_version": 1,
                "record_type": "diagnostic",
                "model": "persistence",
                "ticker": "AAA",
                "origin_date": date,
                "target_date": date,
                "seed": 0,
                "mode": "time",
                "abs_err": {"close": abs_err_close},
            }
            for date in dates
        ]

    def test_encoder_health_bounds_baseline_to_after_the_latest_retrain(self, tmp_path):
        # End-to-end wiring check: _encoder_health() must fetch this
        # model's retrain markers and pass the latest one through to
        # diagnostic_health() as retrained_after, not just call it with the
        # raw diagnostic stream (Codex review regression: "Reset encoder-
        # health baseline after recovery retraining").
        run_dir = tmp_path / "run"
        ledger = Ledger.create(run_dir)
        # diagnostic_health()'s default window_days is 20, so the bounded
        # (post-retrain) side needs >= 40 distinct dates to leave the
        # first/last windows independent -- two 25-day months comfortably
        # clears that.
        records = self._diagnostic_records([f"2021-01-{day:02d}" for day in range(1, 26)], 0.01)
        records.append(
            {
                "schema_version": 1,
                "record_type": "retrain",
                "model": "persistence",
                "origin_date": "2021-02-01",
                "target_date": "2021-02-01",
                "seed": 0,
                "step_index": 10,
            }
        )
        records += self._diagnostic_records([f"2021-03-{day:02d}" for day in range(1, 26)], 0.03)
        records += self._diagnostic_records([f"2021-04-{day:02d}" for day in range(1, 26)], 0.031)
        ledger.append(records)
        harness = SimpleNamespace(config=SimpleNamespace(models=("persistence",)))
        verdict = daily_update._encoder_health(run_dir, harness)
        assert verdict["status"] == "OK"
