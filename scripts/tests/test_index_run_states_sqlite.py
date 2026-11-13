"""Tests for scripts/index_run_states_sqlite.py: a derived, rebuildable, read-only-on-source index."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts import index_run_states_sqlite as idx


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "cron_tracking"
    _write(root / "morning_dca" / "20260713_1030ET" / "run_state.json",
           {"run_id": "m1", "run_origin": "scheduled", "manual_run": False, "state_schema_version": 3})
    _write(root / "midday_dca" / "manual_runs" / "20260713_1230ET" / "run_state.json",
           {"run_id": "d1", "run_origin": "manual", "manual_run": True})
    _write(root / "morning_dca" / "20260714_1030ET" / "run_state.json", "{ this is not json")
    _write(root / "morning_dca" / "latest_morning_run.json", {"pointer": True})  # not a run_state file
    return root


def _snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_index_records_every_run_state_and_flags_the_unparseable_one(tmp_path):
    root = _tree(tmp_path)
    db = tmp_path / "idx.sqlite3"
    counts = idx.build_index(root, db)
    assert counts["seen"] == 3
    assert counts["added_or_updated"] == 3
    assert counts["unparseable"] == 1
    with sqlite3.connect(db) as conn:
        rows = {r[0]: r for r in conn.execute("SELECT path, run_id, run_origin, manual_run, parse_error FROM run_states")}
    good = rows["morning_dca/20260713_1030ET/run_state.json"]
    assert good[1:4] == ("m1", "scheduled", 0)
    assert good[4] is None
    assert rows["midday_dca/manual_runs/20260713_1230ET/run_state.json"][3] == 1
    assert rows["morning_dca/20260714_1030ET/run_state.json"][4] is not None


def test_index_never_modifies_the_source_tree(tmp_path):
    root = _tree(tmp_path)
    before = _snapshot(root)
    idx.build_index(root, tmp_path / "idx.sqlite3")
    assert _snapshot(root) == before


def test_rerun_is_incremental_and_idempotent(tmp_path):
    root = _tree(tmp_path)
    db = tmp_path / "idx.sqlite3"
    idx.build_index(root, db)
    counts = idx.build_index(root, db)
    assert counts["unchanged"] == 3 and counts["added_or_updated"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_states").fetchone()[0] == 3


def test_changed_file_is_reindexed_and_removed_file_is_dropped(tmp_path):
    root = _tree(tmp_path)
    db = tmp_path / "idx.sqlite3"
    idx.build_index(root, db)
    changed = root / "morning_dca" / "20260713_1030ET" / "run_state.json"
    _write(changed, {"run_id": "m1-v2", "run_origin": "scheduled", "manual_run": False, "padding": "x" * 50})
    (root / "midday_dca" / "manual_runs" / "20260713_1230ET" / "run_state.json").unlink()

    counts = idx.build_index(root, db)

    assert counts["added_or_updated"] == 1
    assert counts["removed"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT run_id FROM run_states WHERE path LIKE 'morning_dca/20260713%'").fetchone()[0] == "m1-v2"
        assert conn.execute("SELECT COUNT(*) FROM run_states").fetchone()[0] == 2


def test_db_inside_the_source_tree_is_refused(tmp_path):
    root = _tree(tmp_path)
    with pytest.raises(SystemExit, match="outside the artifact tree"):
        idx.build_index(root, root / "idx.sqlite3")


def test_missing_source_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="does not exist"):
        idx.build_index(tmp_path / "nope", tmp_path / "idx.sqlite3")


def test_summary_counts_by_stream_including_manual_and_unparseable(tmp_path, capsys):
    root = _tree(tmp_path)
    db = tmp_path / "idx.sqlite3"
    idx.build_index(root, db)
    assert idx.main(["--db", str(db), "--summary"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["total"] == 3
    streams = {s["stream_dir"]: s for s in summary["streams"]}
    assert streams["morning_dca"]["run_states"] == 2 and streams["morning_dca"]["unparseable"] == 1
    assert streams["midday_dca"]["manual"] == 1


def test_summary_of_a_missing_index_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match="index not found"):
        idx.summarize(tmp_path / "absent.sqlite3")


def test_symlinked_run_state_is_not_followed(tmp_path):
    root = _tree(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{"run_id": "evil"}', encoding="utf-8")
    try:
        os.symlink(outside, root / "morning_dca" / "20260715_1030ET_run_state.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform or account")
    assert not any("evil" in str(p) for p in idx.iter_run_state_files(root))
    assert idx.build_index(root, tmp_path / "idx.sqlite3")["seen"] == 3
