"""Tests for the atomic file-write helpers in dca_fileio."""

import csv

import pytest

from dca_fileio import atomic_open_w, atomic_write_text


def test_atomic_write_text_creates_file(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_text(target, '{"ok": true}')
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_atomic_write_text_replaces_existing(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_text(target, "data")
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


def test_atomic_open_w_writes_csv(tmp_path):
    target = tmp_path / "rows.csv"
    with atomic_open_w(target, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["a", "b"])
        writer.writeheader()
        writer.writerow({"a": "1", "b": "2"})
    with target.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [{"a": "1", "b": "2"}]


def test_atomic_open_w_failure_preserves_original(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"prior": 1}', encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_open_w(target) as handle:
            handle.write("partial garbage")
            raise RuntimeError("crash mid-write")
    # The destination keeps its previous contents and no temp file is left behind.
    assert target.read_text(encoding="utf-8") == '{"prior": 1}'
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_atomic_open_w_failure_without_original_leaves_nothing(tmp_path):
    target = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        with atomic_open_w(target) as handle:
            handle.write("partial")
            raise RuntimeError("crash mid-write")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
