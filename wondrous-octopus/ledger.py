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
from pathlib import Path
from typing import Any, Iterator

_LEDGER_DIRNAME = "ledger"

RECORD_KINDS = ("forecast", "score", "diagnostic", "repair", "retrain")


def _shard_relpath(model: str, kind: str, year: str) -> str:
    return f"{model}/{kind}_{year}.jsonl.gz"


class LedgerError(RuntimeError):
    """Raised (never swallowed) when the ledger refuses to proceed."""


class Ledger:
    def __init__(self, run_dir: Path, *, read_fingerprints: dict[str, dict[str, Any]] | None = None) -> None:
        self.root = Path(run_dir) / _LEDGER_DIRNAME
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
    @classmethod
    def create(cls, run_dir: Path) -> "Ledger":
        ledger = cls(run_dir)
        if ledger.root.exists() and any(ledger.root.rglob("*.jsonl.gz")):
            raise LedgerError(f"REFUSED: ledger already exists at {ledger.root}; use resume")
        ledger.root.mkdir(parents=True, exist_ok=True)
        return ledger

    @classmethod
    def resume(cls, run_dir: Path, recorded: dict[str, dict[str, Any]]) -> "Ledger":
        """Open an existing ledger against the checkpoint's shard records."""
        ledger = cls(run_dir)
        # .as_posix(), not str(): _shard_relpath() (the write-time key,
        # embedded in every checkpoint) always uses forward slashes, but
        # str(Path) uses the platform separator -- on Windows this makes
        # every recorded shard here look "missing" against a checkpoint's
        # forward-slash keys, refusing every resume (Codex review: "Use
        # POSIX separators for ledger fingerprint keys").
        on_disk = {
            path.relative_to(ledger.root).as_posix(): path for path in ledger.root.rglob("*.jsonl.gz")
        }
        for relpath, expectation in recorded.items():
            path = on_disk.get(relpath)
            expected_size = int(expectation["size"])
            expected_hash = expectation["sha256"]
            if path is None or path.stat().st_size < expected_size:
                raise LedgerError(f"REFUSED: ledger shard {relpath} is missing or shorter than the checkpoint records")
            prefix_hasher = hashlib.sha256()
            with path.open("rb") as handle:
                remaining = expected_size
                while remaining:
                    chunk = handle.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    prefix_hasher.update(chunk)
                    remaining -= len(chunk)
            if prefix_hasher.hexdigest() != expected_hash:
                raise LedgerError(f"REFUSED: ledger shard {relpath} does not match checkpoint; refusing to resume")
            if path.stat().st_size > expected_size:
                # Bytes past the checkpoint boundary belong to un-checkpointed
                # steps the resumed run will re-execute deterministically.
                with path.open("rb+") as handle:
                    handle.truncate(expected_size)
            ledger._hashers[relpath] = prefix_hasher
            ledger._sizes[relpath] = expected_size
        for relpath, path in on_disk.items():
            if relpath not in recorded:
                path.unlink()  # created entirely after the last checkpoint
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
        integrity during daily dry runs"). Duplicates resume()'s
        verification logic rather than resume() calling this and then
        redoing the same SHA-256 pass -- a full-universe multi-model
        ledger is multi-GB, and a real resume() should hash each shard
        once, not twice.
        """
        root = Path(run_dir) / _LEDGER_DIRNAME
        on_disk = {path.relative_to(root).as_posix(): path for path in root.rglob("*.jsonl.gz")}
        for relpath, expectation in recorded.items():
            path = on_disk.get(relpath)
            expected_size = int(expectation["size"])
            expected_hash = expectation["sha256"]
            if path is None or path.stat().st_size < expected_size:
                raise LedgerError(f"REFUSED: ledger shard {relpath} is missing or shorter than the checkpoint records")
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                remaining = expected_size
                while remaining:
                    chunk = handle.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    hasher.update(chunk)
                    remaining -= len(chunk)
            if hasher.hexdigest() != expected_hash:
                raise LedgerError(f"REFUSED: ledger shard {relpath} does not match checkpoint; refusing to resume")

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
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
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
            expectation: dict[str, Any] | None = None
            if self._read_fingerprints is not None:
                expectation = self._read_fingerprints.get(relpath)
                if expectation is None:
                    # Created entirely after the last checkpoint -- the same
                    # shards resume() would delete outright; not durable.
                    continue
                limit = int(expectation["size"])
            with path.open("rb") as raw_handle:
                payload = raw_handle.read(limit) if limit is not None else raw_handle.read()
            if limit is not None and expectation is not None:
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
