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
