"""Tests for wondrous-octopus/run_sharded.py."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_sharded


def test_build_shard_cmd_daily_mode(tmp_path: Path):
    shard_dir = tmp_path / "shard_1"
    (shard_dir / "checkpoint").mkdir(parents=True)
    args = argparse.Namespace(daily=True, now="2026-10-06", dry_run=True, force=False)
    cmd, mode = run_sharded.build_shard_cmd([], shard_dir, args)
    assert mode == "daily"
    assert cmd is not None
    assert "daily_update.py" in cmd[2]
    assert "--run-dir" in cmd
    assert str(shard_dir) in cmd
    assert "--now" in cmd
    assert "2026-10-06" in cmd
    assert "--dry-run" in cmd


def test_build_shard_cmd_daily_mode_no_checkpoint(tmp_path: Path):
    shard_dir = tmp_path / "shard_1"
    shard_dir.mkdir(parents=True)
    args = argparse.Namespace(daily=True, now=None, dry_run=False, force=False)
    cmd, mode = run_sharded.build_shard_cmd([], shard_dir, args)
    assert mode == "no_checkpoint"
    assert cmd is None


def test_merge_shard_tomorrow_forecasts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run_dir_base = tmp_path / "runs"
    run_dir_base.mkdir()
    shard_1 = run_dir_base / "shard_1"
    shard_2 = run_dir_base / "shard_2"
    shard_1.mkdir()
    shard_2.mkdir()

    fieldnames = ["ticker", "model", "pred_close", "ref_close"]
    rows = {
        "shard_1": {"ticker": "AAPL", "model": "octopus", "pred_close": "150.0", "ref_close": "148.0"},
        "shard_2": {"ticker": "MSFT", "model": "octopus", "pred_close": "300.0", "ref_close": "295.0"},
    }

    # Fake inspect_run: write the shard's tomorrow CSV to the requested --csv path (stale files are unlinked first)
    def fake_run(cmd, *a, **kw):
        shard_name = Path(cmd[cmd.index("--run-dir") + 1]).name
        with open(cmd[cmd.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(rows[shard_name])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_sharded.subprocess, "run", fake_run)

    merged = run_sharded.merge_shard_tomorrow_forecasts(run_dir_base, [shard_1, shard_2])
    assert merged is not None
    assert merged.is_file()

    with open(merged, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2
        assert reader[0]["ticker"] == "AAPL"
        assert reader[0]["shard"] == "shard_1"
        assert reader[1]["ticker"] == "MSFT"
        assert reader[1]["shard"] == "shard_2"


def test_daily_run_dry_run_discovers_shards(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir_base = tmp_path / "test-run"
    run_dir_base.mkdir()
    for i in (1, 2):
        s_dir = run_dir_base / f"shard_{i}"
        (s_dir / "checkpoint").mkdir(parents=True)
        state = {"last_processed_date": f"2026-09-2{i}"}
        (s_dir / "checkpoint" / "harness_state.json").write_text(json.dumps(state), encoding="utf-8")

    code = run_sharded.main(["--daily", "--dry-run", "--run-dir-base", str(run_dir_base)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "Daily update across 2 shards" in captured
    assert "DAILY UPDATE on shard_1" in captured
    assert "DAILY UPDATE on shard_2" in captured


# --- Copilot's review of #1640 (written test-first) -----------------------------------------------------------------


def _fresh_args(**over):
    base = dict(daily=False, now=None, force=False, dry_run=False, end=None, device=None, retrain=False,
                start="2024-01-01", window_len=60, update_every=5, refit_every=20, checkpoint_every=10,
                model="gbm", gbm_row_cap=0, online_steps=0, diagnostic_sample=0)
    base.update(over)
    return argparse.Namespace(**base)


def test_now_is_forwarded_to_a_fresh_replay_command(tmp_path: Path):
    """An as-of sharded replay must not see archive rows after the requested cutoff."""
    cmd, mode = run_sharded.build_shard_cmd(["AAPL"], tmp_path / "shard_1", _fresh_args(now="2026-10-06"))
    assert mode == "fresh" and "run_walkforward.py" in " ".join(cmd)
    assert cmd[cmd.index("--now") + 1] == "2026-10-06"


def test_now_is_forwarded_to_a_resumed_replay_command(tmp_path: Path):
    shard_dir = tmp_path / "shard_1"
    (shard_dir / "checkpoint").mkdir(parents=True)
    cmd, mode = run_sharded.build_shard_cmd(["AAPL"], shard_dir, _fresh_args(now="2026-10-06"))
    assert mode == "resume"
    assert cmd[cmd.index("--now") + 1] == "2026-10-06"


@pytest.mark.parametrize("resume", [False, True])
def test_no_now_flag_is_added_when_none_was_given(tmp_path: Path, resume: bool):
    shard_dir = tmp_path / "shard_1"
    if resume:
        (shard_dir / "checkpoint").mkdir(parents=True)
    cmd, _ = run_sharded.build_shard_cmd(["AAPL"], shard_dir, _fresh_args())
    assert "--now" not in cmd


def _two_shards(tmp_path: Path):
    base = tmp_path / "runs"
    base.mkdir()
    dirs = []
    for i in (1, 2):
        d = base / f"shard_{i}"
        (d / "checkpoint").mkdir(parents=True)
        state = json.dumps({"last_processed_date": "2026-09-20"})
        (d / "checkpoint" / "harness_state.json").write_text(state, encoding="utf-8")
        dirs.append(d)
    return base, dirs


def _exporting(failing: set[str], monkeypatch: pytest.MonkeyPatch):
    """Fake inspect_run: a shard in ``failing`` exits 1 and writes nothing; the others export one row."""
    def fake_run(cmd, *a, **kw):
        name = Path(cmd[cmd.index("--run-dir") + 1]).name
        if name in failing:
            return SimpleNamespace(returncode=1, stderr="boom", stdout="")
        with open(cmd[cmd.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["ticker", "pred_close"])
            writer.writeheader()
            writer.writerow({"ticker": name.upper(), "pred_close": "1.0"})
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(run_sharded.subprocess, "run", fake_run)


def test_one_failed_shard_export_invalidates_the_merge_instead_of_publishing_a_partial_csv(tmp_path, monkeypatch):
    base, dirs = _two_shards(tmp_path)
    (base / "all_shards_tomorrow.csv").write_text("stale\n", encoding="utf-8")
    _exporting({"shard_2"}, monkeypatch)
    with pytest.raises(run_sharded.ShardExportError, match="shard_2"):
        run_sharded.merge_shard_tomorrow_forecasts(base, dirs)
    assert not (base / "all_shards_tomorrow.csv").exists()  # neither the partial merge nor the stale file survives


def test_every_shard_failing_also_clears_the_previous_merged_file(tmp_path, monkeypatch):
    base, dirs = _two_shards(tmp_path)
    (base / "all_shards_tomorrow.csv").write_text("previous run\n", encoding="utf-8")
    _exporting({"shard_1", "shard_2"}, monkeypatch)
    with pytest.raises(run_sharded.ShardExportError):
        run_sharded.merge_shard_tomorrow_forecasts(base, dirs)
    assert not (base / "all_shards_tomorrow.csv").exists()


def test_a_shard_that_exits_zero_but_writes_no_export_is_a_failure_too(tmp_path, monkeypatch):
    base, dirs = _two_shards(tmp_path)
    ok = SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(run_sharded.subprocess, "run", lambda *a, **k: ok)
    with pytest.raises(run_sharded.ShardExportError):
        run_sharded.merge_shard_tomorrow_forecasts(base, dirs)


def test_a_daily_run_with_a_failed_export_exits_nonzero_and_publishes_nothing(tmp_path, monkeypatch):
    base, _ = _two_shards(tmp_path)

    def fake_run(cmd, *a, **kw):
        if "inspect_run.py" in " ".join(map(str, cmd)):
            name = Path(cmd[cmd.index("--run-dir") + 1]).name
            if name == "shard_2":
                return SimpleNamespace(returncode=1, stderr="boom", stdout="")
            with open(cmd[cmd.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker"])
                writer.writeheader()
                writer.writerow({"ticker": "AAPL"})
        return SimpleNamespace(returncode=0, stderr="", stdout="")  # the daily updates themselves succeed

    monkeypatch.setattr(run_sharded.subprocess, "run", fake_run)
    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])
    assert code != 0
    assert not (base / "all_shards_tomorrow.csv").exists()


def test_a_clean_daily_run_still_merges_and_exits_zero(tmp_path, monkeypatch):
    base, _ = _two_shards(tmp_path)

    def fake_run(cmd, *a, **kw):
        if "inspect_run.py" in " ".join(map(str, cmd)):
            name = Path(cmd[cmd.index("--run-dir") + 1]).name
            with open(cmd[cmd.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["ticker"])
                writer.writeheader()
                writer.writerow({"ticker": name})
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(run_sharded.subprocess, "run", fake_run)
    assert run_sharded.main(["--daily", "--run-dir-base", str(base)]) == 0
    assert (base / "all_shards_tomorrow.csv").is_file()


# --- 90-day horizons, a frozen snapshot and run_walkforward presets pass through a sharded replay ---------------------


def test_horizon_history_dir_and_preset_are_forwarded_to_a_fresh_shard(tmp_path: Path):
    args = _fresh_args(preset="full", max_horizon=90, history_dir=tmp_path / "snap", diagnostic_sample=100)
    cmd, mode = run_sharded.build_shard_cmd(["AAPL"], tmp_path / "shard_1", args)
    assert mode == "fresh"
    assert cmd[cmd.index("--preset") + 1] == "full"
    assert cmd[cmd.index("--max-horizon") + 1] == "90"
    assert cmd[cmd.index("--history-dir") + 1] == str(tmp_path / "snap")


def test_a_preset_owns_the_cadence_flags_so_they_are_not_sent(tmp_path: Path):
    """An explicit --update-every etc. would silently override the preset's value."""
    args = _fresh_args(preset="full", diagnostic_sample=100)
    cmd, _ = run_sharded.build_shard_cmd(["AAPL"], tmp_path / "shard_1", args)
    for flag in ("--window-len", "--update-every", "--refit-every", "--diagnostic-sample"):
        assert flag not in cmd


def test_without_a_preset_the_legacy_cadence_flags_and_next_day_horizon_are_kept(tmp_path: Path):
    cmd, _ = run_sharded.build_shard_cmd(["AAPL"], tmp_path / "shard_1", _fresh_args(diagnostic_sample=100))
    assert cmd[cmd.index("--window-len") + 1] == "60"
    assert cmd[cmd.index("--update-every") + 1] == "5"
    assert cmd[cmd.index("--diagnostic-sample") + 1] == "100"
    assert "--preset" not in cmd and "--max-horizon" not in cmd and "--history-dir" not in cmd


def test_dry_run_discovers_tickers_from_the_given_history_dir(tmp_path: Path, capsys):
    snap = tmp_path / "snap"
    snap.mkdir()
    for t in ("AAA", "BBB", "CCC", "DDD"):
        (snap / f"{t}.csv").write_text("Date,Open,High,Low,Close,Volume\n2026-01-02,1,1,1,1,1\n", encoding="utf-8")
    code = run_sharded.main(
        ["--dry-run", "--shards", "2", "--history-dir", str(snap), "--run-dir-base", str(tmp_path / "out")]
    )
    assert code == 0
    assert f"Archive: {snap}" in capsys.readouterr().out


# --- --shards auto ----------------------------------------------------------------------------------------------------


def test_auto_shard_count_is_the_smallest_resource_ceiling():
    count, why = run_sharded.auto_shard_count(6140, cpu_count=16, ram_gb=128)
    assert count == 8 and "limited by cores" in why  # cores 8, size 8, ram 9
    count, why = run_sharded.auto_shard_count(6140, cpu_count=64, ram_gb=32)
    assert count == 2 and "limited by ram" in why  # 32*0.6/8 = 2
    count, why = run_sharded.auto_shard_count(1000, cpu_count=64, ram_gb=512)
    assert count == 1 and "min-shard-size" in why  # a small universe is not split into tiny shards


def test_auto_shard_count_never_returns_zero():
    assert run_sharded.auto_shard_count(10, cpu_count=1, ram_gb=1)[0] == 1
    assert run_sharded.auto_shard_count(10, cpu_count=None, ram_gb=None)[0] >= 1


def test_an_explicit_shard_count_is_used_as_given(tmp_path: Path):
    assert run_sharded.resolve_shard_count(6, 6140, tmp_path)[0] == 6


def test_auto_reuses_an_existing_partition_instead_of_redealing_tickers(tmp_path: Path):
    for n in (1, 2, 3):
        (tmp_path / f"shard_{n}").mkdir()
    assert run_sharded.resolve_shard_count("auto", 6140, tmp_path)[0] == 3


def test_auto_prefers_the_recorded_shard_plan(tmp_path: Path):
    (tmp_path / run_sharded.SHARD_PLAN).write_text('{"shards": 5}', encoding="utf-8")
    (tmp_path / "shard_1").mkdir()
    assert run_sharded.resolve_shard_count("auto", 6140, tmp_path)[0] == 5


@pytest.mark.parametrize("bad", ["0", "-2", "many"])
def test_shards_argument_rejects_nonsense(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        run_sharded._shards_arg(bad)


def test_shards_argument_accepts_auto_and_positive_integers():
    assert run_sharded._shards_arg("auto") == "auto" and run_sharded._shards_arg("8") == 8


def test_a_dry_run_does_not_write_a_shard_plan(tmp_path: Path):
    snap = tmp_path / "snap"
    snap.mkdir()
    for t in ("AAA", "BBB"):
        (snap / f"{t}.csv").write_text("Date,Open,High,Low,Close,Volume\n2026-01-02,1,1,1,1,1\n", encoding="utf-8")
    out = tmp_path / "out"
    run_sharded.main(["--shard", "1", "--dry-run", "--history-dir", str(snap), "--run-dir-base", str(out)])
    assert not (out / run_sharded.SHARD_PLAN).exists()  # a dry run writes nothing


# --- periodic aggregate status (parallel launcher) ---------------------------------------------------------------------


def test_parse_shard_progress_reads_the_last_step_line(tmp_path: Path):
    log = tmp_path / "stdout.log"
    log.write_text("loading\n[>   ] 10/100 (10%) | x\n[>   ] 11/100 (11%) | y\n", encoding="utf-8")
    assert run_sharded.parse_shard_progress(log) == (11, 100)
    assert run_sharded.parse_shard_progress(tmp_path / "missing.log") is None
    log.write_text("still loading\n", encoding="utf-8")
    assert run_sharded.parse_shard_progress(log) is None


def test_format_status_reports_percent_rate_and_slowest_shard_eta():
    line = run_sharded.format_status({1: (50, 100), 2: (25, 100)}, 3600.0, {1: 0, 2: 0})
    assert "37.5%" in line and "75 steps/h" in line and "ETA" in line and "elapsed 1.0h" in line


def test_format_status_before_any_step_line_says_so():
    assert "no step lines yet" in run_sharded.format_status({1: None, 2: None}, 120.0, {})


def test_eval_horizons_is_forwarded_only_when_given(tmp_path: Path):
    given, _ = run_sharded.build_shard_cmd(["AAPL"], tmp_path / "s1", _fresh_args(max_horizon=90, eval_horizons="2,3,90"))
    assert given[given.index("--eval-horizons") + 1] == "2,3,90"
    omitted, _ = run_sharded.build_shard_cmd(["AAPL"], tmp_path / "s2", _fresh_args(max_horizon=90))
    assert "--eval-horizons" not in omitted
