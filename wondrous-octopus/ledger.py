"""Append-only, gzip-member JSONL forecast/score/diagnostic ledger.

Layout: ``<run_dir>/ledger/<model>/<kind>_<year>.jsonl.gz`` -- sharded per
model, record kind (forecast/score/diagnostic), and the ORIGIN date's year
(one uniform sharding rule; readers that bucket by target_date just read and
filter). Shards are gzip files built from CONCATENATED GZIP MEMBERS: every
append compresses its record block into one complete member and appends it,
which a) is valid gzip (readers decompress the concatenation transparently)
and b) means a crash mid-append can only ever damage the final, not-yet-
checkpointed member -- never history.

Compression is a v1 requirement, not an optimization: a full-universe
multi-model replay ledger is ~10-15 GB raw and ~2-3 GB compressed, and this
tool is built to run on a personal machine.

Crash-consistency contract with the harness checkpoint:
- the checkpoint records, per shard, the byte size and SHA-256 at
  checkpoint time;
- on resume, a shard LONGER than its recorded size has its recorded prefix
  verified and is TRUNCATED back to it -- those extra bytes belong to steps
  after the last checkpoint, which the resumed run will deterministically
  re-execute (this is what makes append + crash + resume idempotent);
- a shard whose recorded prefix does NOT hash to the recorded value, or a
  recorded shard that is missing/short, is tampering or corruption:
  REFUSED, fail closed;
- a shard file present on disk but absent from the checkpoint was created
  entirely after the last checkpoint: deleted on resume for the same
  reason truncation is safe.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

_LEDGER_DIRNAME = "ledger"

RECORD_KINDS = ("forecast", "score", "diagnostic", "repair", "retrain")
_DIR_FD_AVAILABLE = all(func in os.supports_dir_fd for func in (os.open, os.stat, os.mkdir, os.unlink))
_SCANDIR_FD_AVAILABLE = os.scandir in os.supports_fd


def _shard_relpath(model: str, kind: str, year: str) -> str:
    relpath = f"{model}/{kind}_{year}.jsonl.gz"
    _shard_parts(relpath)
    return relpath


class LedgerError(RuntimeError):
    """Raised (never swallowed) when the ledger refuses to proceed."""


def _shard_parts(relpath: str) -> tuple[str, str]:
    """Validate the portable, two-component key before any filesystem lookup."""
    if not isinstance(relpath, str) or "\\" in relpath or ":" in relpath or "\x00" in relpath:
        raise LedgerError(f"REFUSED: invalid ledger shard path {relpath!r}")
    parts = relpath.split("/")
    if (len(parts) != 2 or parts[0] in ("", ".", "..")
            or not re.fullmatch(r"(?:forecast|score|diagnostic|repair|retrain)_[0-9]{4}\.jsonl\.gz", parts[1])):
        raise LedgerError(f"REFUSED: invalid ledger shard path {relpath!r}")
    return parts[0], parts[1]


def _require_secure_filesystem() -> None:
    if (not _DIR_FD_AVAILABLE or not _SCANDIR_FD_AVAILABLE
            or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")):
        raise LedgerError("REFUSED: this platform lacks secure descriptor-relative ledger operations")


@contextmanager
def _directory(parent_fd: int | None, name: str, *, create: bool = False) -> Iterator[int]:
    """Pin a directory without following an alias in its final component."""
    if create:
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _regular_shard(info: os.stat_result, relpath: str) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise LedgerError(f"REFUSED: ledger shard {relpath} is not a regular, unaliased file")


@contextmanager
def _shard_handle(root_fd: int, relpath: str, *, write: bool = False, append: bool = False):
    model, filename = _shard_parts(relpath)
    with _directory(root_fd, model, create=append) as model_fd:
        # No O_TRUNC: inspect the actual opened inode before any mutation.
        flags = os.O_RDWR if write else os.O_RDONLY
        if append:
            flags |= os.O_APPEND | os.O_CREAT
        # Nonblocking avoids hanging on a FIFO swapped in just before open().
        fd = os.open(filename, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666, dir_fd=model_fd)
        try:
            _regular_shard(os.fstat(fd), relpath)
            handle = os.fdopen(fd, "rb+" if write else "rb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            yield handle


def _scan_shards(root_fd: int, prefix: str = "") -> set[str]:
    """Enumerate only pinned directories; refuse aliases even in empty checkpoints."""
    shards: set[str] = set()
    with os.scandir(root_fd) as entries:
        for entry in entries:
            relpath = f"{prefix}{entry.name}"
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise LedgerError(f"REFUSED: ledger path {relpath} is a symbolic link")
            if stat.S_ISDIR(info.st_mode):
                if prefix:
                    raise LedgerError(f"REFUSED: unexpected nested ledger path {relpath}")
                with _directory(root_fd, entry.name) as child_fd:
                    shards.update(_scan_shards(child_fd, f"{relpath}/"))
            elif entry.name.endswith(".jsonl.gz"):
                _shard_parts(relpath)
                _regular_shard(info, relpath)
                shards.add(relpath)
    return shards


def _validate_recorded(recorded: dict[str, dict[str, Any]]) -> None:
    for relpath, expectation in recorded.items():
        _shard_parts(relpath)
        size = expectation.get("size")
        if type(size) is not int or size < 0:
            raise LedgerError(f"REFUSED: invalid checkpoint size for ledger shard {relpath}")
        digest = expectation.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise LedgerError(f"REFUSED: invalid checkpoint hash for ledger shard {relpath}")


def _verified_prefix(handle, relpath: str, expectation: dict[str, Any]):
    remaining = expectation["size"]
    if os.fstat(handle.fileno()).st_size < remaining:
        raise LedgerError(f"REFUSED: ledger shard {relpath} is missing or shorter than the checkpoint records")
    hasher = hashlib.sha256()
    while remaining:
        chunk = handle.read(min(1 << 20, remaining))
        if not chunk:
            raise LedgerError(f"REFUSED: ledger shard {relpath} is missing or shorter than the checkpoint records")
        hasher.update(chunk)
        remaining -= len(chunk)
    if hasher.hexdigest() != expectation["sha256"]:
        raise LedgerError(f"REFUSED: ledger shard {relpath} does not match checkpoint; refusing to resume")
    return hasher


class Ledger:
    def __init__(self, run_dir: Path, *, read_fingerprints: dict[str, dict[str, Any]] | None = None) -> None:
        self.root = Path(run_dir) / _LEDGER_DIRNAME
        # Bind operator authority once. Re-resolving on append would treat
        # a replacement run-directory symlink as a newly authorized root.
        self._selected_run_dir = Path(run_dir).resolve(strict=False)
        self._hashers: dict[str, "hashlib._Hash"] = {}
        self._sizes: dict[str, int] = {}
        # When set, iter_records() bounds each shard to the byte size the
        # checkpoint fingerprinted -- bytes past that point belong to steps
        # taken after the last checkpoint and are not durable (a runtime
        # failure can leave a complete or PARTIAL gzip member past that
        # boundary; the plain unrestricted reader would either report
        # not-yet-durable numbers or fail decompressing a truncated member).
        # See resume()'s truncate-or-refuse contract for the write-side
        # analogue (Codex review: "Bound report-only reads to the
        # checkpoint ledger").
        self._read_fingerprints = read_fingerprints

    # -- lifecycle ---------------------------------------------------------
    @contextmanager
    def _root_handle(self, *, create: bool = False) -> Iterator[int]:
        _require_secure_filesystem()
        try:
            # The caller selects the run directory (which may itself be an
            # alias). Pin the cached canonical path component-by-component;
            # creation also uses pinned parents, never recursive Path.mkdir.
            selected = self._selected_run_dir
            with ExitStack() as stack:
                fd = stack.enter_context(_directory(None, selected.anchor))
                for part in selected.parts[1:]:
                    fd = stack.enter_context(_directory(fd, part, create=create))
                yield stack.enter_context(_directory(fd, _LEDGER_DIRNAME, create=create))
        except OSError as exc:
            raise LedgerError(f"REFUSED: unsafe or inaccessible ledger path under {self.root}") from exc

    @classmethod
    def create(cls, run_dir: Path) -> "Ledger":
        ledger = cls(run_dir)
        with ledger._root_handle(create=True) as root_fd:
            if _scan_shards(root_fd):
                raise LedgerError(f"REFUSED: ledger already exists at {ledger.root}; use resume")
        return ledger

    @classmethod
    def resume(cls, run_dir: Path, recorded: dict[str, dict[str, Any]]) -> "Ledger":
        """Open an existing ledger against the checkpoint's shard records."""
        ledger = cls(run_dir)
        _validate_recorded(recorded)
        with ledger._root_handle() as root_fd:
            on_disk = _scan_shards(root_fd)
            for relpath, expectation in recorded.items():
                if relpath not in on_disk:
                    raise LedgerError(
                        f"REFUSED: ledger shard {relpath} is missing or shorter than the checkpoint records"
                    )
                with _shard_handle(root_fd, relpath, write=True) as handle:
                    prefix_hasher = _verified_prefix(handle, relpath, expectation)
                    expected_size = expectation["size"]
                    if os.fstat(handle.fileno()).st_size > expected_size:
                        # The descriptor still names the verified inode even
                        # if a pathname is replaced while its bytes are read.
                        _regular_shard(os.fstat(handle.fileno()), relpath)
                        handle.truncate(expected_size)
                ledger._hashers[relpath] = prefix_hasher
                ledger._sizes[relpath] = expected_size
            for relpath in on_disk - recorded.keys():
                model, filename = _shard_parts(relpath)
                with _directory(root_fd, model) as model_fd:
                    _regular_shard(os.stat(filename, dir_fd=model_fd, follow_symlinks=False), relpath)
                    os.unlink(filename, dir_fd=model_fd)
        return ledger

    @classmethod
    def verify_shards(cls, run_dir: Path, recorded: dict[str, dict[str, Any]]) -> None:
        """Read-only counterpart to resume(): the SAME missing/short/hash-
        mismatch checks (raises LedgerError on any failure, returns
        nothing on success), without truncating an oversized shard back to
        its recorded size or deleting an unrecorded one.

        Lets a caller that must not mutate anything -- daily_update.py's
        --dry-run preview -- prove the checkpoint's ledger is intact
        without performing resume()'s recovery-truncation/deletion side
        effects, which --dry-run's own "nothing written, nothing
        recovered" contract forbids (Codex review: "Verify checkpoint
        integrity during daily dry runs"). Shares the descriptor-based
        prefix check with resume(), which still hashes each shard only once.
        """
        ledger = cls(run_dir)
        _validate_recorded(recorded)
        with ledger._root_handle() as root_fd:
            on_disk = _scan_shards(root_fd)
            for relpath, expectation in recorded.items():
                if relpath not in on_disk:
                    raise LedgerError(
                        f"REFUSED: ledger shard {relpath} is missing or shorter than the checkpoint records"
                    )
                with _shard_handle(root_fd, relpath) as handle:
                    _verified_prefix(handle, relpath, expectation)

    # -- writing -----------------------------------------------------------
    def append(self, records: list[dict[str, Any]]) -> None:
        """Group records by shard and append each group as one gzip member."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            kind = record["record_type"]
            if kind not in RECORD_KINDS:
                raise LedgerError(f"REFUSED: unknown record_type {kind!r}")
            year = record["origin_date"][:4]
            groups.setdefault(_shard_relpath(record["model"], kind, year), []).append(record)
        for relpath, group in groups.items():
            payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in group)
            # mtime=0: gzip's default header embeds wall-clock time, which
            # would make byte-identical runs produce different shards and
            # break the resume-equals-uninterrupted determinism contract.
            member = gzip.compress(payload.encode("utf-8"), mtime=0)
            with self._root_handle(create=True) as root_fd, _shard_handle(
                root_fd, relpath, write=True, append=True
            ) as handle:
                handle.write(member)
                handle.flush()
                # flush() only moves these bytes into the OS page cache, not
                # onto stable storage. checkpoint() fingerprints this shard's
                # post-append size/hash and PUBLISHES that as durable -- if
                # the host loses power before the page cache is written back,
                # the shard on disk can end up shorter than the checkpoint
                # already recorded, and the next resume() sees that as
                # "missing or shorter" and permanently REFUSES instead of
                # rolling back to the last checkpoint, contradicting this
                # module's own crash-consistency contract (Codex review:
                # "Sync ledger bytes before publishing their checkpoint").
                # fsync here, not once per checkpoint: append() already
                # holds the file handle open for exactly this write, so no
                # extra reopen/bookkeeping is needed to keep every appended
                # byte durable well before any later checkpoint fingerprints it.
                os.fsync(handle.fileno())
            hasher = self._hashers.setdefault(relpath, hashlib.sha256())
            hasher.update(member)
            self._sizes[relpath] = self._sizes.get(relpath, 0) + len(member)

    # -- reading -----------------------------------------------------------
    def available_years(self, *, model: str, kind: str) -> list[str]:
        """Ascending list of origin-date years this model has shards for.

        Lives here rather than in the caller because this module owns the
        shard-naming convention (_shard_relpath): a reader that globbed and
        parsed those filenames itself would be a second copy of that format,
        free to drift from this one. Lets a streaming reader pull one year
        at a time through iter_records(..., year=...) instead of holding a
        whole run in memory.
        """
        model_dir = self.root / model
        if not model_dir.is_dir():
            return []
        years = set()
        for path in model_dir.glob(f"{kind}_*.jsonl.gz"):
            if self._read_fingerprints is not None:
                # Same checkpoint-bounded view iter_records() reads through:
                # a shard created entirely after the last checkpoint is not
                # durable and is skipped there. Reporting its year here
                # instead let a caller's "no records for this model" guard
                # pass on a shard that then yields nothing -- silence where
                # a refusal was owed (Copilot review: "available_years()
                # does not apply this Ledger's checkpoint fingerprints").
                if path.relative_to(self.root).as_posix() not in self._read_fingerprints:
                    continue
            year = path.name[len(kind) + 1 : -len(".jsonl.gz")]
            # Exactly four digits, matching _shard_parts()'s own
            # [0-9]{4} rule rather than a looser isdigit(): a name this
            # module would refuse to write or resume must not be reported
            # as a readable year either.
            if len(year) == 4 and year.isdigit():
                years.add(year)
        return sorted(years)

    def iter_records(
        self, *, model: str, kind: str, year: str | None = None
    ) -> Iterator[dict[str, Any]]:
        model_dir = self.root / model
        if not model_dir.is_dir():
            return
        pattern = f"{kind}_{year}.jsonl.gz" if year else f"{kind}_*.jsonl.gz"
        for path in sorted(model_dir.glob(pattern)):
            # .as_posix(): matches _shard_relpath()'s forward-slash keys
            # (see resume()'s identical fix) so a Windows checkpoint's
            # read_fingerprints lookup below doesn't miss every shard.
            relpath = path.relative_to(self.root).as_posix()
            expectation: dict[str, Any] | None = None
            limit: int | None = None
            if self._read_fingerprints is not None:
                expectation = self._read_fingerprints.get(relpath)
                if expectation is None:
                    # Created entirely after the last checkpoint -- the same
                    # shards resume() would delete outright; not durable.
                    continue
                limit = int(expectation["size"])
            with path.open("rb") as raw_handle:
                payload = raw_handle.read(limit) if limit is not None else raw_handle.read()
            if limit is not None:
                # The byte limit alone only bounds WHERE reading stops; a
                # shard shortened at (or before) a gzip-member boundary, or
                # replaced with different valid bytes of the same or greater
                # length, would otherwise be read and silently accepted --
                # even though the next resume() would refuse this exact
                # ledger. Apply the same size + SHA-256 check resume()
                # already does before trusting the bytes (Codex review:
                # "Verify checkpointed shards before reporting").
                if len(payload) < limit:
                    raise LedgerError(
                        f"REFUSED: ledger shard {relpath} is shorter than the checkpoint recorded; refusing to read"
                    )
                if expectation is not None and hashlib.sha256(payload).hexdigest() != expectation["sha256"]:
                    raise LedgerError(
                        f"REFUSED: ledger shard {relpath} does not match checkpoint; refusing to read"
                    )
            with gzip.open(io.BytesIO(payload), "rt", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

    def models(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.name for path in self.root.iterdir() if path.is_dir())

    # -- checkpoint support --------------------------------------------------
    def fingerprints(self) -> dict[str, dict[str, Any]]:
        return {
            relpath: {"size": self._sizes[relpath], "sha256": hasher.hexdigest()}
            for relpath, hasher in self._hashers.items()
        }
