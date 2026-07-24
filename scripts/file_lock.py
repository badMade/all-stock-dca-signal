#!/usr/bin/env python3
"""Cross-process advisory file lock: POSIX ``flock`` fast path, portable fallback.

Several reliability engines need a *cross-process* critical section -- the run
ledger serializes sequence allocation + append, and the scheduler lease
serializes its check-then-mutate operations. Both ran on ``fcntl.flock`` on
POSIX and silently degraded to a no-op wherever ``fcntl`` was unavailable, so
concurrent processes on a non-POSIX host lost the guarantee entirely.

This module is the single implementation both use:

  * On POSIX, an advisory ``fcntl.flock`` on a sidecar lock file, acquired
    non-blocking in a poll loop so ``timeout_seconds`` is honored (a plain
    blocking ``flock`` would wait forever on a hung holder). The kernel releases
    the lock automatically if the holder crashes, so it can never wedge.
  * Where ``fcntl`` is absent, a portable ``O_CREAT | O_EXCL`` lock file: exactly
    one writer can create it, the rest spin until it is released. It deliberately
    does NOT reclaim a lock by mtime or pid. Reclaiming a lock that is actually
    still live would let two writers into the critical section, and every
    ownership check races the reclaim itself. So a crashed holder is never
    stolen: waiters simply fail closed (``TimeoutError``) after
    ``timeout_seconds`` rather than risk a double writer. This trades
    availability for provable safety and only affects exotic non-POSIX hosts --
    the supported Linux-cron deployment always takes the ``flock`` path, where
    the kernel releases a crashed holder's lock automatically. A wedged non-POSIX
    lock is cleared by removing the stale ``*.lock`` file.

Standard library only. No trading logic, no network, no secrets.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

try:  # POSIX advisory file locking; absent on non-POSIX platforms.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_POLL_SECONDS = 0.02


@contextmanager
def cross_process_lock(
    lock_path: Union[str, Path],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``lock_path`` for the block's body.

    Uses ``fcntl.flock`` when available and a portable ``O_EXCL`` lock file
    otherwise. Both honor ``timeout_seconds``: a waiter that cannot acquire within
    the window raises ``TimeoutError`` (fail closed) rather than blocking forever
    or stealing a possibly-live lock.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with _flock(lock_path, timeout_seconds, poll_seconds):
            yield
    else:  # pragma: no cover - exercised via monkeypatched fcntl in tests
        with _portable_lock(lock_path, timeout_seconds, poll_seconds):
            yield


@contextmanager
def _flock(lock_path: Path, timeout_seconds: float, poll_seconds: float) -> Iterator[None]:
    """POSIX critical section via ``fcntl.flock`` (kernel-released on crash).

    Acquired non-blocking (``LOCK_EX | LOCK_NB``) in a poll loop so a hung holder
    cannot block a waiter indefinitely; the blocking form would ignore
    ``timeout_seconds``. Uncontended acquisition succeeds on the first attempt.
    """
    start = time.monotonic()
    # Append mode: flock locks the file-table entry regardless of size, and "a"
    # avoids truncating the sidecar on every acquisition.
    with open(lock_path, "a", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - start > timeout_seconds:
                    raise TimeoutError(
                        f"could not acquire flock on {lock_path} within {timeout_seconds:g}s"
                    ) from None
                time.sleep(poll_seconds)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _portable_lock(lock_path: Path, timeout_seconds: float, poll_seconds: float) -> Iterator[None]:
    """Portable ``O_CREAT | O_EXCL`` lock. No reclaim: only the creator ever removes
    it, so no process can delete another's lock; a crashed holder makes waiters
    fail closed (``TimeoutError``) instead of risking a double writer."""
    start = time.monotonic()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            if time.monotonic() - start > timeout_seconds:
                raise TimeoutError(
                    f"could not acquire lock {lock_path} within {timeout_seconds:g}s"
                ) from None
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except FileNotFoundError:  # pragma: no cover - defensive
            pass


__all__ = ["cross_process_lock"]
