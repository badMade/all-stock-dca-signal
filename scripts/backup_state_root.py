#!/usr/bin/env python3
"""Back up, verify, and test-restore the live artifact/state root.

``PRODUCTION_READINESS.md`` gap 2 records that the live state root (the per-stream run artifacts, the
same-day allocation inputs and the whole-share accumulation ledgers under ``cron_tracking/``) lives on one
machine with no backup, and that the restore runbook covers code and config only. This script closes the
"no backup, no tested restore" half. It is deliberately conservative:

* **READ-ONLY on the source.** Nothing under the source root is written, moved, locked, or deleted. The
  only files this script creates are the archive, its manifest, and its checksum in ``--dest-dir``.
* **Dry-run by default**, like the rest of the repo. Pass ``--write`` to create an archive.
* **Fail closed.** A backup is only reported successful after the archive was re-read and every member's
  SHA-256 matched the manifest built from the source. Any mismatch deletes nothing and exits non-zero; no
  partial archive is left under the final name (temp file + ``os.replace``).
* **Never prunes.** Retention is a separate, later decision (``docs/artifact_retention.md``). This script
  does not delete old archives.
* **Standard library only**, no network, no credentials.

Usage::

    python scripts/backup_state_root.py --dest-dir ~/dca-backups                 # dry run: lists what it would copy
    python scripts/backup_state_root.py --dest-dir ~/dca-backups --write         # create + verify an archive
    python scripts/backup_state_root.py --verify ~/dca-backups/dca_state_<ts>.tar.gz
    python scripts/backup_state_root.py --restore-test ~/dca-backups/dca_state_<ts>.tar.gz   # extract to a temp dir, compare

``--source`` defaults to ``$DCA_CRON_TRACKING_ROOT`` (via ``dca_paths.cron_tracking_root``). The whole-share
ledgers (``whole_share_accumulation/<TICKER>/ledger.json``) are not reproducible from run history, so the
manifest reports how many were captured and a run that finds none says so loudly.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

MANIFEST_NAME = "MANIFEST.json"
ARCHIVE_PREFIX = "dca_state_"
LEDGER_DIR = "whole_share_accumulation"
_CHUNK = 1024 * 1024


class BackupError(Exception):
    """Raised for any condition that must stop a backup or verification (fail closed)."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(_CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _default_source() -> Optional[str]:
    try:
        from dca_paths import cron_tracking_root
    except ImportError:  # run from outside a checkout: require --source instead of guessing
        return None
    return cron_tracking_root()


def iter_source_files(source: Path) -> Iterator[Path]:
    """Regular files under ``source``. Symlinks are skipped (never followed): a link could point outside the
    state root (the repo's ``ticker_data`` is one such link) and a backup must not silently copy it."""
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def build_manifest(source: Path) -> Dict[str, object]:
    """Hash every source file. Files that vanish between listing and reading are recorded as skipped, not fatal:
    the run artifacts are written atomically (``dca_fileio``), so a read sees either the old or the new whole
    file, never a torn one."""
    files: List[Dict[str, object]] = []
    skipped: List[str] = []
    ledgers = 0
    for path in iter_source_files(source):
        rel = path.relative_to(source).as_posix()
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError:
            skipped.append(rel)
            continue
        files.append({"path": rel, "size": size, "sha256": digest})
        if rel.startswith(f"{LEDGER_DIR}/") and rel.endswith("ledger.json"):
            ledgers += 1
    return {
        "schema": 1,
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_root_name": source.name,
        "file_count": len(files),
        "total_bytes": sum(int(f["size"]) for f in files),
        "whole_share_ledgers": ledgers,
        "skipped_unreadable": skipped,
        "files": files,
    }


def _tar_add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def create_archive(source: Path, dest_dir: Path, manifest: Dict[str, object], stamp: str) -> Path:
    """Write ``dca_state_<stamp>.tar.gz`` via a temp file, verify it, and only then give it its final name."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"{ARCHIVE_PREFIX}{stamp}.tar.gz"
    if final.exists():
        raise BackupError(f"refusing to overwrite existing archive {final}")
    fd, tmp_name = tempfile.mkstemp(prefix=".partial_", suffix=".tar.gz", dir=dest_dir)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            _tar_add_bytes(tar, MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
            for entry in manifest["files"]:  # type: ignore[union-attr]
                rel = str(entry["path"])  # type: ignore[index]
                tar.add(source / rel, arcname=f"files/{rel}", recursive=False)
        verify_archive(tmp)  # re-read what was written before it can ever be called a backup
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    checksum = sha256_file(final)
    (dest_dir / f"{final.name}.sha256").write_text(f"{checksum}  {final.name}\n", encoding="utf-8")
    return final


def _safe_member_name(name: str) -> str:
    """Reject absolute paths and parent traversal so a hostile archive cannot write outside the target."""
    normalised = name.replace("\\", "/")
    parts = normalised.split("/")
    if normalised.startswith("/") or ".." in parts or (len(normalised) > 1 and normalised[1] == ":"):
        raise BackupError(f"unsafe path in archive: {name!r}")
    return normalised


def verify_archive(archive: Path) -> Dict[str, object]:
    """Re-read the archive and prove every member matches the manifest embedded in it.

    Also checks the sibling ``.sha256`` file when present. Returns the manifest on success."""
    if not archive.is_file():
        raise BackupError(f"archive not found: {archive}")
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").split()[0]
        if sha256_file(archive) != expected:
            raise BackupError(f"archive checksum does not match {sidecar.name}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = {_safe_member_name(m.name): m for m in tar.getmembers()}
            if MANIFEST_NAME not in members:
                raise BackupError("archive has no MANIFEST.json")
            manifest_handle = tar.extractfile(members[MANIFEST_NAME])
            if manifest_handle is None:
                raise BackupError("MANIFEST.json is not a regular file")
            manifest = json.loads(manifest_handle.read().decode("utf-8"))
            listed = {str(f["path"]): f for f in manifest["files"]}
            seen = set()
            for rel, entry in listed.items():
                member = members.get(f"files/{rel}")
                if member is None or not member.isfile():
                    raise BackupError(f"manifest lists {rel} but the archive does not contain it")
                handle = tar.extractfile(member)
                if handle is None or _sha256_stream(handle) != entry["sha256"]:
                    raise BackupError(f"SHA-256 mismatch for {rel}")
                if member.size != entry["size"]:
                    raise BackupError(f"size mismatch for {rel}")
                seen.add(rel)
            extra = {n[len("files/"):] for n in members if n.startswith("files/")} - seen
            if extra:
                raise BackupError(f"archive contains files not in the manifest: {sorted(extra)[:5]}")
    except (tarfile.TarError, OSError, ValueError, KeyError) as exc:
        raise BackupError(f"could not read archive: {exc}") from exc
    return manifest


def restore_test(archive: Path) -> Dict[str, object]:
    """Extract into a fresh temp dir (never an existing tree), then hash the extracted files against the manifest.
    This proves a real restore works, not just that the archive is readable. The temp dir is removed after."""
    manifest = verify_archive(archive)
    with tempfile.TemporaryDirectory(prefix="dca_restore_test_") as tmp:
        target = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = _safe_member_name(member.name)
                if not name.startswith("files/") or not member.isfile():
                    continue
                dest = target / name[len("files/"):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                if handle is None:
                    raise BackupError(f"cannot read member {name}")
                with open(dest, "wb") as out:
                    shutil.copyfileobj(handle, out, _CHUNK)
        for entry in manifest["files"]:  # type: ignore[union-attr]
            rel = str(entry["path"])  # type: ignore[index]
            restored = target / rel
            if not restored.is_file() or sha256_file(restored) != entry["sha256"]:  # type: ignore[index]
                raise BackupError(f"restored file differs from manifest: {rel}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--source", help="state root to back up (default: $DCA_CRON_TRACKING_ROOT)")
    parser.add_argument("--dest-dir", help="directory that receives the archive, manifest and checksum")
    parser.add_argument("--write", action="store_true", help="create the archive (default is a dry run)")
    parser.add_argument("--verify", metavar="ARCHIVE", help="verify an existing archive and exit")
    parser.add_argument("--restore-test", metavar="ARCHIVE", help="extract an archive to a temp dir and compare")
    return parser


def _summary(manifest: Dict[str, object]) -> str:
    return (
        f"{manifest['file_count']} files, {manifest['total_bytes']} bytes, "
        f"{manifest['whole_share_ledgers']} whole-share ledger(s)"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.verify:
            print(f"OK: {args.verify} verified ({_summary(verify_archive(Path(args.verify)))})")
            return 0
        if args.restore_test:
            print(f"OK: {args.restore_test} restored and compared ({_summary(restore_test(Path(args.restore_test)))})")
            return 0
        if not args.dest_dir:
            print("error: --dest-dir is required (or use --verify / --restore-test)", file=sys.stderr)
            return 2
        source_arg = args.source or _default_source()
        if not source_arg:
            print("error: no --source given and dca_paths could not be imported", file=sys.stderr)
            return 2
        source = Path(source_arg)
        dest_dir = Path(args.dest_dir)
        if not source.is_dir():
            raise BackupError(f"source root does not exist or is not a directory: {source}")
        if _is_inside(dest_dir, source):
            raise BackupError("--dest-dir is inside the source root; the archive would back itself up")
        manifest = build_manifest(source)
        if manifest["file_count"] == 0:
            raise BackupError("source root contains no files; refusing to write an empty backup")
        if manifest["whole_share_ledgers"] == 0:
            print(
                f"WARNING: no {LEDGER_DIR}/<TICKER>/ledger.json found. That balance is not reproducible from "
                "run history; confirm it is expected before relying on this backup.",
                file=sys.stderr,
            )
        if manifest["skipped_unreadable"]:
            print(f"WARNING: {len(manifest['skipped_unreadable'])} file(s) unreadable and skipped", file=sys.stderr)  # type: ignore[arg-type]
        if not args.write:
            print(f"DRY RUN: would back up {_summary(manifest)} from {source} to {dest_dir} (pass --write)")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = create_archive(source, dest_dir, manifest, stamp)
        print(f"OK: wrote and verified {archive} ({_summary(manifest)})")
        return 0
    except BackupError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
