#!/usr/bin/env python3
"""File-based scheduler leases for the daily DCA windows.

The plan requires that a scheduled run and a recovery run for the SAME window can
never execute concurrently -- the 2026-07-23 incident included a background
recovery and a foreground recovery racing each other's placement. There is no
central scheduler here (the windows are independent cron tasks), so the lease is
a file: a runner must hold the window's lease before it reconciles or submits,
and a second runner for the same window finds the lease held and stands down.

Acquisition is atomic (``O_CREAT | O_EXCL``). A lease carries the holder id, the
incident correlation id, and a TTL so a crashed holder's stale lease can be
reclaimed rather than wedging the window forever -- but only explicitly, and only
once the TTL has elapsed. Release and refresh are holder-checked so one runner
can never release or extend another's lease.

Standard library only. No trading logic, no network, no secrets. ``now`` (epoch
seconds) is injectable for deterministic tests.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from scripts.file_lock import cross_process_lock

DEFAULT_TTL_SECONDS = 900  # 15 minutes: longer than a window's work, short enough to reclaim.

_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def _safe(value: str) -> str:
    return _UNSAFE.sub("-", str(value).strip().lower()).strip("-") or "na"


def window_key(trading_day: str, stream: str, window: str) -> str:
    """Stable, filesystem-safe key identifying one window."""
    return "__".join([_safe(trading_day), _safe(stream), _safe(window)])


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text_safe(path: Path) -> str:
    """Read a file's text, returning '' on any OS error (missing/unreadable)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclass
class Lease:
    """A held window lease's on-disk metadata."""

    holder_id: str
    correlation_id: Optional[str]
    window_key: str
    acquired_at_epoch: float
    ttl_seconds: int

    def is_stale(self, now: float) -> bool:
        """True when the lease's TTL has elapsed and it may be reclaimed."""
        return now >= self.acquired_at_epoch + self.ttl_seconds

    def to_json(self) -> str:
        return json.dumps(
            {
                "holder_id": self.holder_id,
                "correlation_id": self.correlation_id,
                "window_key": self.window_key,
                "acquired_at_epoch": self.acquired_at_epoch,
                "acquired_at_iso": _iso(self.acquired_at_epoch),
                "ttl_seconds": self.ttl_seconds,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Optional["Lease"]:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict) or "holder_id" not in data:
            return None
        try:
            acquired = float(data.get("acquired_at_epoch") or 0.0)
            raw_ttl = data.get("ttl_seconds")
            ttl = DEFAULT_TTL_SECONDS if raw_ttl is None else int(raw_ttl)
        except (TypeError, ValueError, OverflowError):
            # Syntactically valid JSON but a corrupted field: a non-numeric
            # ttl_seconds (ValueError), an int(NaN) (ValueError), or an int(inf)
            # from a JSON 1e999 (OverflowError). Treat as unreadable -> None, so
            # acquire reclaims it as malformed stale state instead of crashing.
            return None
        if not math.isfinite(acquired):
            # A non-finite timestamp (NaN/inf) makes is_stale() false forever, so
            # the window could never be reclaimed. Treat as malformed/recoverable.
            return None
        if ttl <= 0:
            # A non-positive persisted TTL (e.g. a 0 from a partial/corrupt write)
            # is malformed. Returning None routes it through acquire's reclaim path
            # instead of the old `or DEFAULT_TTL_SECONDS`, which silently turned it
            # into a live 900s lease and left the crashed window wedged for 15 min.
            return None
        return cls(
            holder_id=str(data.get("holder_id")),
            correlation_id=data.get("correlation_id"),
            window_key=str(data.get("window_key", "")),
            acquired_at_epoch=acquired,
            ttl_seconds=ttl,
        )


@dataclass
class LeaseResult:
    """Outcome of an acquire attempt."""

    acquired: bool
    lease: Optional[Lease] = None
    current_holder: Optional[Lease] = None
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.acquired


class SchedulerLease:
    """Acquire/release a per-window lease under ``base_dir``."""

    def __init__(self, base_dir: str | Path, key: str) -> None:
        self.base_dir = Path(base_dir)
        self.key = key
        self.path = self.base_dir / f"{key}.lease"

    def read(self) -> Optional[Lease]:
        """Return the current lease holder metadata, or None if unheld/unreadable."""
        if not self.path.exists():
            return None
        try:
            return Lease.from_json(self.path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def _write_new(self, lease: Lease) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Atomic create-exclusive: fails if another holder created it first.
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        # Wrap the fd in a buffered writer so the payload is written in full: a bare
        # os.write can short-write under signal interruption, whereas file.write
        # loops until everything is flushed. The ``with`` closes (and so releases)
        # the fd exactly once. If the write fails (disk error, KeyboardInterrupt),
        # remove the just-created file so a zero-byte/partial lease is never left
        # behind -- matching _atomic_swap's cleanup.
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(lease.to_json() + "\n")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            raise

    @contextlib.contextmanager
    def _guard(self):
        """Serialize the mutating lease operations across processes.

        acquire/refresh/release each run their whole check-then-mutate sequence
        under an exclusive cross-process lock on a sidecar ``.guard`` file, so no
        two mutators can interleave -- eliminating the check-then-act races that
        plain-file rename/replace cannot close on their own (a reclaim can never
        slip between another operation's read and write). The lock is
        :func:`scripts.file_lock.cross_process_lock`: POSIX ``fcntl.flock`` where
        available (kernel-released on crash), a portable ``O_EXCL`` + stale-reclaim
        lock elsewhere -- so the exclusion holds on non-POSIX hosts too instead of
        degrading to a no-op that reopens the refresh/reclaim race.
        """
        guard_path = self.path.with_name(f"{self.key}.guard")
        with cross_process_lock(guard_path):
            yield

    def acquire(
        self,
        holder_id: str,
        *,
        correlation_id: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        steal_stale: bool = True,
        now: Optional[float] = None,
    ) -> LeaseResult:
        """Try to acquire the window lease (serialized via :meth:`_guard`)."""
        with self._guard():
            return self._acquire_locked(
                holder_id,
                correlation_id=correlation_id,
                ttl_seconds=ttl_seconds,
                steal_stale=steal_stale,
                now=now,
            )

    def _acquire_locked(
        self,
        holder_id: str,
        *,
        correlation_id: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        steal_stale: bool = True,
        now: Optional[float] = None,
    ) -> LeaseResult:
        """Acquire implementation; MUST run under :meth:`_guard`.

        Returns an un-acquired result (with ``current_holder``) when another live
        holder owns the lease. When the existing lease is stale (past its TTL) and
        ``steal_stale`` is set, the stale lease is reclaimed. Re-acquiring a lease
        the same ``holder_id`` already holds succeeds idempotently.
        """
        if not holder_id:
            raise ValueError("holder_id is required to acquire a lease")
        if ttl_seconds <= 0:
            # A non-positive TTL makes the lease instantly stale, so a second
            # runner could reclaim it while the first still believes it holds --
            # defeating same-window exclusion. Reject it up front.
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds!r}")
        if ttl_seconds != int(ttl_seconds):
            # A fractional TTL is truncated to whole seconds when the lease is read
            # back (Lease.from_json casts to int), so the persisted value and the
            # value a reclaimer computes disagree -- e.g. 0.5s is written but read
            # as 0s, looks instantly stale, and a second runner reclaims a lease
            # the first still holds. Require a whole number of seconds.
            raise ValueError(
                f"ttl_seconds must be a whole number of seconds, got {ttl_seconds!r}"
            )
        now = time.time() if now is None else now
        lease = Lease(
            holder_id=holder_id,
            correlation_id=correlation_id,
            window_key=self.key,
            acquired_at_epoch=now,
            ttl_seconds=ttl_seconds,
        )
        try:
            self._write_new(lease)
            return LeaseResult(acquired=True, lease=lease)
        except FileExistsError:
            pass

        current = self.read()
        if current is None:
            # The O_EXCL create failed, so a lease file EXISTS, but read() cannot
            # parse it -- e.g. a creator crashed after creating the file but before
            # its JSON was written. Left alone this would wedge the window forever
            # (read() stays None, so none of the branches below ever reclaim it).
            # Treat it as malformed stale state and reclaim it exclusively.
            if steal_stale:
                return self._reclaim_stale(lease, now=now)
            return LeaseResult(acquired=False, current_holder=None, reason="malformed_lease")
        if current is not None and current.holder_id == holder_id and not current.is_stale(now):
            # Idempotent re-acquire by the same holder while the lease is still
            # live. A non-stale lease cannot be concurrently reclaimed (reclaim
            # requires staleness), so an in-place atomic swap is safe AND never
            # leaves the lease path absent (preserving exclusion). A stale
            # same-holder lease falls through to the reclaim branch below.
            self._atomic_swap(lease)
            return LeaseResult(acquired=True, lease=lease, reason="reacquired_by_same_holder")
        if current is not None and not current.is_stale(now) and steal_stale:
            return LeaseResult(
                acquired=False, current_holder=current, reason="held_by_live_holder"
            )
        if current is not None and current.is_stale(now) and steal_stale:
            return self._reclaim_stale(lease, now=now)
        return LeaseResult(
            acquired=False, current_holder=current, reason="held" if current else "race_lost"
        )

    def _reclaim_stale(self, lease: "Lease", *, now: float) -> LeaseResult:
        """Reclaim a stale lease with exclusive, single-winner semantics.

        Reclamation is made atomic by first renaming the stale lease file OUT of
        the way: ``os.rename`` removes the source, so of two racers only one can
        succeed -- the loser sees the source gone (``FileNotFoundError``) and
        stands down. The winner then re-checks that what it stole was actually
        stale (guarding against a holder that refreshed just before the rename)
        and, if so, creates a fresh lease with the same exclusive ``O_EXCL``
        create used on the fast path. This replaces the previous unconditional
        overwrite, where two racers could both "reclaim" the same lease and both
        report ``acquired=True``.
        """
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # _safe() the holder id for the temp FILENAME only (an id like "host/123"
        # contains a path separator and would make with_name raise ValueError); the
        # original holder_id is still used for ownership checks via lease content.
        steal_path = self.path.with_name(f"{self.key}.reclaim.{_safe(lease.holder_id)}.{os.getpid()}")
        try:
            os.rename(self.path, steal_path)
        except (FileNotFoundError, OSError):
            # Someone else reclaimed/released it first: the source is gone.
            return LeaseResult(acquired=False, current_holder=self.read(), reason="lost_reclaim_race")
        stolen = Lease.from_json(_read_text_safe(steal_path))
        if stolen is not None and not stolen.is_stale(now):
            # The holder refreshed between our staleness check and our rename.
            # Restore their lease without clobbering any newer holder (O_EXCL),
            # and stand down.
            with contextlib.suppress(FileExistsError):
                self._write_new(stolen)
            with contextlib.suppress(OSError):
                os.unlink(steal_path)
            return LeaseResult(
                acquired=False, current_holder=stolen, reason="holder_refreshed_during_reclaim"
            )
        with contextlib.suppress(OSError):
            os.unlink(steal_path)
        try:
            self._write_new(lease)
            return LeaseResult(acquired=True, lease=lease, reason="reclaimed_stale_lease")
        except FileExistsError:
            return LeaseResult(acquired=False, current_holder=self.read(), reason="lost_after_reclaim")

    def _atomic_take(self, holder_id: str) -> Optional[Tuple[Path, "Lease"]]:
        """Atomically remove the current lease iff it is held by ``holder_id``.

        Renames the lease file to a unique path -- ``os.rename`` is atomic and
        removes the source, so a concurrent reclaimer cannot also take it -- then
        verifies the taken content is ``holder_id``'s. On success ``self.path`` is
        now absent and the caller decides whether to delete (release) or rewrite a
        fresh lease (refresh). Returns ``None`` when the lease is not (or no
        longer) ours, restoring another holder's lease if we took it by surprise.
        This gives ``release`` a single ownership-checked mutation instead of a
        read-then-mutate race where a stale holder could delete a newer holder's
        lease. (``refresh`` renews a still-live lease in place via ``_atomic_swap``
        and does not go through here.)
        """
        current = self.read()
        if current is None or current.holder_id != holder_id:
            return None
        # _safe() the holder id for the temp FILENAME only (a path separator in the
        # id would make with_name raise ValueError); ownership is still checked
        # against the original holder_id via the lease content below.
        take_path = self.path.with_name(f"{self.key}.take.{_safe(holder_id)}.{os.getpid()}")
        try:
            os.rename(self.path, take_path)
        except OSError:
            return None
        taken = Lease.from_json(_read_text_safe(take_path))
        if taken is None or taken.holder_id != holder_id:
            # Reclaimed by another holder between our read and rename: restore it
            # without clobbering any newer holder, and stand down.
            if taken is not None:
                with contextlib.suppress(FileExistsError):
                    self._write_new(taken)
            with contextlib.suppress(OSError):
                os.unlink(take_path)
            return None
        return take_path, taken

    def _atomic_swap(self, lease: Lease) -> None:
        """Replace the lease file in place with ``lease`` -- gap-free and atomic.

        Writes to a unique temp file then ``os.replace`` onto ``self.path``. Unlike
        a rename-away-then-rewrite, the lease path is NEVER absent during the swap,
        so a concurrent runner can never see "no lease" mid-update. Used to renew a
        still-live lease (a non-stale lease cannot be concurrently reclaimed, so an
        unconditional swap of it cannot clobber a reclaimer).
        """
        self.base_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.base_dir), prefix=f"{self.key}.", suffix=".swap.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(lease.to_json() + "\n")
            # mkstemp creates the temp file 0600; match _write_new's 0644 so a
            # refresh does not silently tighten the lease file's mode on replace.
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def refresh(self, holder_id: str, *, now: Optional[float] = None) -> bool:
        """Extend a still-live lease's timestamp; only the current holder may refresh.

        Runs under :meth:`_guard`, so the ownership/staleness check and the in-place
        swap are one atomic critical section -- a reclaim cannot slip between the
        check and the write. A lease that is already STALE is still refused (the
        caller should re-acquire instead). The swap is gap-free (write-temp +
        ``os.replace``) so the lease path is never absent mid-renewal.
        """
        with self._guard():
            now = time.time() if now is None else now
            current = self.read()
            if current is None or current.holder_id != holder_id or current.is_stale(now):
                return False
            self._atomic_swap(
                Lease(
                    holder_id=holder_id,
                    correlation_id=current.correlation_id,
                    window_key=self.key,
                    acquired_at_epoch=now,
                    ttl_seconds=current.ttl_seconds,
                )
            )
            return True

    def release(self, holder_id: str) -> bool:
        """Release the lease; only the current holder may release it.

        Runs under :meth:`_guard`; the atomic take then ensures a release can never
        delete a lease a different holder reclaimed after this holder's read.
        """
        with self._guard():
            taken = self._atomic_take(holder_id)
            if taken is None:
                return False
            take_path, _ = taken
            with contextlib.suppress(OSError):
                os.unlink(take_path)
            return True

    def is_held(self, *, now: Optional[float] = None) -> bool:
        """True when a non-stale lease currently exists."""
        current = self.read()
        if current is None:
            return False
        now = time.time() if now is None else now
        return not current.is_stale(now)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "window_key",
    "Lease",
    "LeaseResult",
    "SchedulerLease",
]
