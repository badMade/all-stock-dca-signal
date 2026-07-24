"""Tests for the shared cross-process file lock (POSIX flock + portable fallback)."""

from __future__ import annotations

import threading
import time

import pytest

from scripts import file_lock
from scripts.file_lock import cross_process_lock


def test_flock_path_is_reentrant_across_sequential_holders(tmp_path):
    lock_path = tmp_path / "x.lock"
    with cross_process_lock(lock_path):
        pass
    with cross_process_lock(lock_path):
        pass  # a second sequential acquisition succeeds


def test_flock_path_honors_timeout(tmp_path):
    if file_lock.fcntl is None:  # pragma: no cover - platform without fcntl
        pytest.skip("no fcntl on this platform")
    lock_path = tmp_path / "x.lock"
    # Hold the flock via a separate open file description, then a waiter must time
    # out rather than block indefinitely (blocking flock would ignore the timeout).
    holder = open(lock_path, "a", encoding="utf-8")
    file_lock.fcntl.flock(holder.fileno(), file_lock.fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            with cross_process_lock(lock_path, timeout_seconds=0.1, poll_seconds=0.01):
                pass
    finally:
        file_lock.fcntl.flock(holder.fileno(), file_lock.fcntl.LOCK_UN)
        holder.close()


def test_portable_lock_serializes_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)
    lock_path = tmp_path / "x.lock"
    order = []

    def worker(n):
        with cross_process_lock(lock_path):
            order.append(("enter", n))
            time.sleep(0.01)
            order.append(("exit", n))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No interleaving: every enter is immediately followed by its own exit.
    for i in range(0, len(order), 2):
        assert order[i][0] == "enter"
        assert order[i + 1] == ("exit", order[i][1])


def test_portable_lock_times_out_on_live_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)
    lock_path = tmp_path / "x.lock"
    # A lock file that never releases: a waiter must give up rather than block.
    lock_path.write_text("", encoding="utf-8")
    with pytest.raises(TimeoutError):
        with cross_process_lock(lock_path, timeout_seconds=0.1, poll_seconds=0.01):
            pass


def test_portable_lock_does_not_reclaim_stale_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)
    lock_path = tmp_path / "x.lock"
    # Even a very old lock file is NOT reclaimed -- reclaiming a possibly-live lock
    # could admit two writers, so the portable path fails closed instead. (On POSIX
    # the kernel releases a crashed holder's flock; this fallback is non-POSIX only.)
    lock_path.write_text("", encoding="utf-8")
    import os

    stale = time.time() - 86400
    os.utime(lock_path, (stale, stale))
    with pytest.raises(TimeoutError):
        with cross_process_lock(lock_path, timeout_seconds=0.1, poll_seconds=0.01):
            pass


def test_portable_lock_releases_on_normal_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)
    lock_path = tmp_path / "x.lock"
    with cross_process_lock(lock_path):
        assert lock_path.exists()  # held
    assert not lock_path.exists()  # released
    # A subsequent acquisition succeeds (nothing left wedged).
    with cross_process_lock(lock_path):
        pass


def test_portable_lock_releases_when_body_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)
    lock_path = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with cross_process_lock(lock_path):
            raise RuntimeError("boom")
    # The lock file is removed even when the guarded body raises.
    assert not lock_path.exists()
