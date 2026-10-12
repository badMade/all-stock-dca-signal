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
