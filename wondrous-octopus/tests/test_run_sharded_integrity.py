"""wondrous-octopus/run_sharded.py --daily: never report success on a partial or stale forecast set.

Copilot review of #1641 (``run_sharded.py`` lines 150 and 398):

* ``merge_shard_tomorrow_forecasts`` skips any shard whose ``inspect_run.py`` export fails, publishes a partial
  ``all_shards_tomorrow.csv`` that claims to merge all shards, and, when every export fails, leaves an OLDER merged
  file in place while every caller still exits 0. Downstream sizing can then read stale forecasts after a run that
  reported success.
* Daily discovery silently drops any numbered shard whose checkpoint is missing, so a four-shard run with a lost
  ``shard_2/checkpoint`` updates the other three and exits 0 with incomplete forecasts.

The tests state the required behaviour (non-zero exit, nothing launched, no stale or partial merged file) and do not
prescribe how the code gets there.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest
import run_sharded

FIELDNAMES = ["ticker", "model", "pred_close", "ref_close"]
MERGED = "all_shards_tomorrow.csv"


def _make_shard(base: Path, number: int, *, checkpoint: bool = True) -> Path:
    shard = base / f"shard_{number}"
    shard.mkdir(parents=True)
    if checkpoint:
        (shard / "checkpoint").mkdir()
    return shard


def _write_forecast(csv_path: str, ticker: str) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerow({"ticker": ticker, "model": "octopus", "pred_close": "1.0", "ref_close": "1.0"})


def _fake_subprocess(monkeypatch, *, failing_exports=(), silent_exports=()):
    """Daily updates succeed; inspect_run exports succeed unless the shard is listed.

    ``failing_exports``: exit non-zero. ``silent_exports``: exit 0 but write no CSV.
    """

    def fake_run(cmd, *a, **kw):
        script = Path(cmd[2] if cmd[1] == "-u" else cmd[1]).name
        if script == "daily_update.py":
            return SimpleNamespace(returncode=0)
        assert script == "inspect_run.py", cmd
        shard = Path(cmd[cmd.index("--run-dir") + 1]).name
        if shard in failing_exports:
            return SimpleNamespace(returncode=1)
        if shard in silent_exports:
            return SimpleNamespace(returncode=0)
        _write_forecast(cmd[cmd.index("--csv") + 1], ticker=shard.upper())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_sharded.subprocess, "run", fake_run)


def _forbid_subprocess(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a shard process was launched although the run should have been refused")

    monkeypatch.setattr(run_sharded.subprocess, "run", boom)
    monkeypatch.setattr(run_sharded.subprocess, "Popen", boom)


def test_daily_run_is_refused_when_a_shard_directory_has_no_checkpoint(tmp_path, monkeypatch, capsys):
    base = tmp_path / "runs"
    _make_shard(base, 1)
    _make_shard(base, 2, checkpoint=False)  # its durable checkpoint was lost
    _make_shard(base, 3)
    _forbid_subprocess(monkeypatch)

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code != 0
    assert "REFUSED" in capsys.readouterr().err
    assert not (base / MERGED).exists()


def test_daily_run_is_refused_when_the_shard_numbering_has_a_gap(tmp_path, monkeypatch, capsys):
    base = tmp_path / "runs"
    _make_shard(base, 1)
    _make_shard(base, 3)  # shard_2 is gone entirely: its tickers would silently get no update
    _forbid_subprocess(monkeypatch)

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code != 0
    assert "REFUSED" in capsys.readouterr().err


def test_a_failed_shard_export_fails_the_daily_run_and_publishes_no_partial_file(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    for n in (1, 2, 3):
        _make_shard(base, n)
    _fake_subprocess(monkeypatch, failing_exports={"shard_2"})

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code != 0, "the run reported success although shard_2's forecasts were never exported"
    assert not (base / MERGED).exists(), "a partial merged forecast file was left behind"


def test_a_failed_export_removes_a_stale_merged_file_from_a_previous_day(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    for n in (1, 2):
        _make_shard(base, n)
    stale = base / MERGED
    stale.write_text("ticker,shard\nYESTERDAY,shard_1\n", encoding="utf-8")
    _fake_subprocess(monkeypatch, failing_exports={"shard_2"})  # shard_1 exports fine, so a partial merge is possible

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code != 0
    assert not stale.exists(), "yesterday's merged forecasts survived a run that did not export every shard"


def test_single_shard_daily_run_fails_when_any_shard_export_fails(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    for n in (1, 2, 3):
        _make_shard(base, n)
    (base / MERGED).write_text("ticker,shard\nYESTERDAY,shard_1\n", encoding="utf-8")
    _fake_subprocess(monkeypatch, failing_exports={"shard_3"})

    code = run_sharded.main(["--daily", "--shard", "2", "--run-dir-base", str(base)])  # updates shard_2 only

    assert code != 0, "--shard exited 0 although the merged forecasts could not be rebuilt from every shard"
    assert not (base / MERGED).exists()


def test_parallel_daily_run_fails_when_any_shard_export_fails(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    for n in (1, 2):
        _make_shard(base, n)
    (base / MERGED).write_text("ticker,shard\nYESTERDAY,shard_1\n", encoding="utf-8")
    _fake_subprocess(monkeypatch, failing_exports={"shard_2"})
    # --parallel launches the daily updates with Popen; both complete successfully here
    done = SimpleNamespace(wait=lambda: 0, poll=lambda: 0)
    monkeypatch.setattr(run_sharded.subprocess, "Popen", lambda *a, **kw: done)

    code = run_sharded.main(["--daily", "--parallel", "--run-dir-base", str(base)])

    assert code != 0, "--parallel exited 0 although shard_2's forecasts were never exported"
    assert not (base / MERGED).exists()


@pytest.mark.parametrize("mode_args", [["--parallel"], ["--shard", "1"]], ids=["parallel", "single-shard"])
def test_parallel_and_single_shard_healthy_runs_still_merge_every_shard(tmp_path, monkeypatch, mode_args):
    base = tmp_path / "runs"
    for n in (1, 2):
        _make_shard(base, n)
    _fake_subprocess(monkeypatch)
    done = SimpleNamespace(wait=lambda: 0, poll=lambda: 0)
    monkeypatch.setattr(run_sharded.subprocess, "Popen", lambda *a, **kw: done)

    code = run_sharded.main(["--daily", *mode_args, "--run-dir-base", str(base)])

    assert code == 0
    with open(base / MERGED, encoding="utf-8") as f:
        assert [r["shard"] for r in csv.DictReader(f)] == ["shard_1", "shard_2"]


def test_an_export_that_exits_zero_but_writes_no_file_also_fails_the_run(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    for n in (1, 2):
        _make_shard(base, n)
    _fake_subprocess(monkeypatch, silent_exports={"shard_1"})

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code != 0
    assert not (base / MERGED).exists()


def test_exports_that_succeed_but_contain_no_rows_fail_the_run_instead_of_reporting_success(tmp_path, monkeypatch):
    """Every export exits 0 and writes a header-only CSV: there is nothing to size from, which is not a success."""
    base = tmp_path / "runs"
    for n in (1, 2):
        _make_shard(base, n)
    (base / MERGED).write_text("ticker,shard\nYESTERDAY,shard_1\n", encoding="utf-8")

    def fake_run(cmd, *a, **kw):
        script = Path(cmd[2] if cmd[1] == "-u" else cmd[1]).name
        if script == "inspect_run.py":
            with open(cmd[cmd.index("--csv") + 1], "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(run_sharded.subprocess, "run", fake_run)

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code != 0
    assert not (base / MERGED).exists()


def test_a_stale_merged_file_never_survives_a_merge_where_every_export_fails(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    shards = [_make_shard(base, n) for n in (1, 2)]
    stale = base / MERGED
    stale.write_text("ticker,shard\nYESTERDAY,shard_1\n", encoding="utf-8")
    _fake_subprocess(monkeypatch, failing_exports={"shard_1", "shard_2"})

    try:  # whether a failed merge raises or returns None is the implementation's choice
        run_sharded.merge_shard_tomorrow_forecasts(base, shards)
    except Exception:  # noqa: BLE001
        pass

    assert not stale.exists(), "yesterday's merged forecasts were left in place after a run that exported nothing"


def test_a_healthy_daily_run_still_merges_every_shard_and_exits_zero(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    for n in (1, 2, 3):
        _make_shard(base, n)
    _fake_subprocess(monkeypatch)

    code = run_sharded.main(["--daily", "--run-dir-base", str(base)])

    assert code == 0
    with open(base / MERGED, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["shard"] for r in rows] == ["shard_1", "shard_2", "shard_3"]
