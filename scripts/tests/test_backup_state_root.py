"""Tests for scripts/backup_state_root.py: read-only source, verified archives, fail-closed on any mismatch."""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from scripts import backup_state_root as bsr


def _make_root(tmp_path: Path, *, with_ledger: bool = True) -> Path:
    root = tmp_path / "cron_tracking"
    (root / "morning_dca" / "20260713_1030ET").mkdir(parents=True)
    (root / "morning_dca" / "20260713_1030ET" / "run_state.json").write_text('{"ok": true}', encoding="utf-8")
    (root / "morning_dca" / "latest_morning_run.json").write_text('{"p": 1}', encoding="utf-8")
    if with_ledger:
        (root / "whole_share_accumulation" / "BYDDY").mkdir(parents=True)
        (root / "whole_share_accumulation" / "BYDDY" / "ledger.json").write_text('{"pending_usd": 1.5}', encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_dry_run_writes_nothing(tmp_path, capsys):
    root = _make_root(tmp_path)
    dest = tmp_path / "backups"
    assert bsr.main(["--source", str(root), "--dest-dir", str(dest)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not dest.exists()


def test_write_creates_verified_archive_and_never_modifies_the_source(tmp_path):
    root = _make_root(tmp_path)
    before = _snapshot(root)
    dest = tmp_path / "backups"

    assert bsr.main(["--source", str(root), "--dest-dir", str(dest), "--write"]) == 0

    assert _snapshot(root) == before, "the source root must be untouched"
    archives = list(dest.glob("dca_state_*.tar.gz"))
    assert len(archives) == 1
    assert (dest / f"{archives[0].name}.sha256").is_file()
    assert not list(dest.glob(".partial_*")), "no temp file may be left behind"
    manifest = bsr.verify_archive(archives[0])
    assert manifest["file_count"] == 3
    assert manifest["whole_share_ledgers"] == 1


def test_restore_test_round_trips_every_file(tmp_path):
    root = _make_root(tmp_path)
    dest = tmp_path / "backups"
    bsr.main(["--source", str(root), "--dest-dir", str(dest), "--write"])
    archive = next(dest.glob("dca_state_*.tar.gz"))
    assert bsr.main(["--restore-test", str(archive)]) == 0


def test_verify_detects_a_corrupted_archive(tmp_path, capsys):
    root = _make_root(tmp_path)
    dest = tmp_path / "backups"
    bsr.main(["--source", str(root), "--dest-dir", str(dest), "--write"])
    archive = next(dest.glob("dca_state_*.tar.gz"))
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0xFF
    archive.write_bytes(bytes(data))

    assert bsr.main(["--verify", str(archive)]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_verify_detects_a_member_that_differs_from_the_manifest(tmp_path):
    """Rebuild an archive whose file bytes no longer match its manifest (no sidecar), as a tamper would."""
    root = _make_root(tmp_path)
    manifest = bsr.build_manifest(root)
    forged = tmp_path / "forged.tar.gz"
    with tarfile.open(forged, "w:gz") as tar:
        bsr._tar_add_bytes(tar, bsr.MANIFEST_NAME, json.dumps(manifest).encode("utf-8"))
        for entry in manifest["files"]:
            bsr._tar_add_bytes(tar, f"files/{entry['path']}", b"tampered")
    with pytest.raises(bsr.BackupError, match="mismatch"):
        bsr.verify_archive(forged)


def test_verify_rejects_unsafe_member_paths(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(bsr.BackupError, match="unsafe path"):
        bsr.verify_archive(evil)


def test_refuses_a_destination_inside_the_source(tmp_path, capsys):
    root = _make_root(tmp_path)
    assert bsr.main(["--source", str(root), "--dest-dir", str(root / "backups"), "--write"]) == 1
    assert "inside the source root" in capsys.readouterr().err


def test_refuses_an_empty_source(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert bsr.main(["--source", str(empty), "--dest-dir", str(tmp_path / "b"), "--write"]) == 1
    assert "no files" in capsys.readouterr().err


def test_refuses_to_overwrite_an_existing_archive(tmp_path):
    root = _make_root(tmp_path)
    dest = tmp_path / "backups"
    manifest = bsr.build_manifest(root)
    bsr.create_archive(root, dest, manifest, "20260101T000000Z")
    with pytest.raises(bsr.BackupError, match="refusing to overwrite"):
        bsr.create_archive(root, dest, manifest, "20260101T000000Z")


def test_missing_ledger_is_a_loud_warning_not_a_silent_success(tmp_path, capsys):
    root = _make_root(tmp_path, with_ledger=False)
    assert bsr.main(["--source", str(root), "--dest-dir", str(tmp_path / "b")]) == 0
    assert "whole_share_accumulation" in capsys.readouterr().err


def test_symlinks_are_not_followed(tmp_path):
    root = _make_root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        os.symlink(outside, root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform or account")
    paths = {p.relative_to(root).as_posix() for p in bsr.iter_source_files(root)}
    assert "link.txt" not in paths


def test_dest_dir_is_required_for_a_backup(capsys):
    assert bsr.main([]) == 2
    assert "--dest-dir is required" in capsys.readouterr().err
