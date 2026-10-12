"""Tests for file-based scheduler leases."""

from __future__ import annotations

import multiprocessing
import os

from scripts import scheduler_lease
from scripts.scheduler_lease import Lease, SchedulerLease, window_key


def _lease(tmp_path, key="2026-07-23__midday__1230et"):
    return SchedulerLease(tmp_path / "leases", key)


def test_window_key_is_stable_and_safe():
    k = window_key("2026-07-23", "midday", "12:30 ET")
    assert k == "2026-07-23__midday__12-30-et"


def test_acquire_then_second_holder_is_blocked(tmp_path):
    lease_a = _lease(tmp_path)
    lease_b = _lease(tmp_path)
    r1 = lease_a.acquire("scheduled-runner", now=1000.0)
    assert r1.acquired
    # A recovery runner for the same window cannot acquire while it's live.
    r2 = lease_b.acquire("recovery-runner", now=1001.0)
    assert not r2.acquired
    assert r2.current_holder.holder_id == "scheduled-runner"
    assert r2.reason == "held_by_live_holder"


def test_release_frees_the_lease(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("scheduled-runner", now=1000.0)
    assert lease.release("scheduled-runner")
    # Now another holder can acquire.
    assert _lease(tmp_path).acquire("recovery-runner", now=1002.0).acquired


def test_cannot_release_another_holders_lease(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("scheduled-runner", now=1000.0)
    assert not _lease(tmp_path).release("recovery-runner")
    assert _lease(tmp_path).is_held(now=1001.0)


def test_stale_lease_is_reclaimed(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("dead-runner", ttl_seconds=900, now=1000.0)
    # 901s later the lease is stale and a new runner reclaims it.
    r = _lease(tmp_path).acquire("recovery-runner", now=1000.0 + 901, ttl_seconds=900)
    assert r.acquired
    assert r.reason == "reclaimed_stale_lease"


def test_live_lease_not_stolen_before_ttl(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)
    r = _lease(tmp_path).acquire("other", now=1000.0 + 899)
    assert not r.acquired


def test_reacquire_by_same_holder_is_idempotent(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", now=1000.0)
    r = lease.acquire("runner", now=1005.0)
    assert r.acquired and r.reason == "reacquired_by_same_holder"


def test_refresh_only_by_holder(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)
    assert lease.refresh("runner", now=1400.0)
    assert not lease.refresh("intruder", now=1400.0)
    # After refresh at 1400, it is not stale at 2000 (< 1400+900).
    assert lease.is_held(now=2000.0)


def test_is_held_false_when_unheld(tmp_path):
    assert not _lease(tmp_path).is_held(now=1000.0)


def test_refresh_leaves_no_temp_files(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)
    lease.refresh("runner", now=1100.0)
    leftover = list((tmp_path / "leases").glob("*.tmp"))
    assert leftover == []
    # The lease itself is intact and held.
    assert lease.is_held(now=1200.0)


def test_reclaimed_stale_lease_has_new_holder(tmp_path):
    _lease(tmp_path).acquire("dead", ttl_seconds=900, now=1000.0)
    r = _lease(tmp_path).acquire("recovery", now=1000.0 + 901, ttl_seconds=900)
    assert r.acquired and r.reason == "reclaimed_stale_lease"
    assert _lease(tmp_path).read().holder_id == "recovery"
    # No steal/temp litter left behind.
    assert list((tmp_path / "leases").glob("*.reclaim.*")) == []


def test_release_returns_false_after_reclaimed_by_another(tmp_path):
    _lease(tmp_path).acquire("A", ttl_seconds=900, now=1000.0)
    _lease(tmp_path).acquire("B", now=1000.0 + 901, ttl_seconds=900)  # reclaims stale A
    # A must not be able to delete B's active lease.
    assert not _lease(tmp_path).release("A")
    assert _lease(tmp_path).read().holder_id == "B"


def test_release_leaves_no_take_files(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", now=1000.0)
    assert lease.release("runner")
    assert list((tmp_path / "leases").glob("*.take.*")) == []


def test_same_holder_reacquire_after_reclaim_stands_down(tmp_path):
    # A acquires, goes stale; B reclaims. A's later re-acquire must NOT clobber B.
    _lease(tmp_path).acquire("A", ttl_seconds=900, now=1000.0)
    _lease(tmp_path).acquire("B", now=1000.0 + 901, ttl_seconds=900)  # reclaims
    # A tries to re-acquire (it still believes it holds); B is the live holder.
    r = _lease(tmp_path).acquire("A", now=1000.0 + 902, ttl_seconds=900)
    assert not r.acquired
    assert _lease(tmp_path).read().holder_id == "B"


def test_acquire_rejects_non_positive_ttl(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        _lease(tmp_path).acquire("runner", ttl_seconds=0, now=1000.0)
    with pytest.raises(ValueError):
        _lease(tmp_path).acquire("runner", ttl_seconds=-5, now=1000.0)


def test_acquire_rejects_fractional_ttl(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        _lease(tmp_path).acquire("runner", ttl_seconds=0.5, now=1000.0)
    # A whole-number float TTL is fine (it equals its int form and round-trips).
    assert _lease(tmp_path).acquire("runner", ttl_seconds=900.0, now=1000.0).acquired


def test_lease_operations_hold_without_fcntl(tmp_path, monkeypatch):
    # Force the portable (non-POSIX) guard path; exclusion must still hold rather
    # than degrade to a no-op that reopens the refresh/reclaim race.
    from scripts import file_lock

    monkeypatch.setattr(file_lock, "fcntl", None)
    lease = _lease(tmp_path)
    assert lease.acquire("runner", ttl_seconds=900, now=1000.0).acquired
    # A second holder for the same live window is still blocked.
    assert not _lease(tmp_path).acquire("other", now=1001.0).acquired
    assert lease.refresh("runner", now=1100.0)
    assert lease.release("runner")
    assert not lease.is_held(now=1200.0)


def test_acquire_reclaims_malformed_lease_file(tmp_path):
    lease = _lease(tmp_path)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    lease.path.write_text("{ partial json not closed", encoding="utf-8")  # crash-corrupted
    r = lease.acquire("recovery", now=1000.0)
    assert r.acquired
    assert _lease(tmp_path).read().holder_id == "recovery"


def test_refresh_refuses_when_already_stale(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)
    # Stale at 2000; refreshing could clobber a reclaimer, so it must refuse.
    assert not lease.refresh("runner", now=2000.0)


def test_refresh_live_lease_leaves_path_present_and_no_temp(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)
    assert lease.refresh("runner", now=1100.0)
    assert lease.path.exists()  # never absent during renewal
    assert list((tmp_path / "leases").glob("*.tmp")) == []


def test_acquire_reclaims_semantically_malformed_lease(tmp_path):
    lease = _lease(tmp_path)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but a corrupted numeric field -> must not crash acquire.
    lease.path.write_text('{"holder_id":"A","ttl_seconds":"bad"}', encoding="utf-8")
    r = lease.acquire("recovery", now=1000.0)
    assert r.acquired
    assert _lease(tmp_path).read().holder_id == "recovery"


def test_lease_operations_work_under_flock_guard(tmp_path):
    # Smoke test that the flock-guarded path still acquires/refreshes/releases.
    lease = _lease(tmp_path)
    assert lease.acquire("runner", ttl_seconds=900, now=1000.0).acquired
    assert lease.refresh("runner", now=1100.0)
    assert lease.release("runner")
    assert not lease.is_held(now=1200.0)


def test_refresh_preserves_lease_file_mode(tmp_path):
    import stat

    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)
    lease.refresh("runner", now=1100.0)
    # The swap goes through a 0600 mkstemp temp; refresh must not silently tighten
    # the lease mode below what _write_new (0644) created it with.
    assert stat.S_IMODE(lease.path.stat().st_mode) == 0o644


def test_holder_id_with_path_separator_acquire_release(tmp_path):
    # A holder_id containing a path separator must not break the lease temp-file
    # names (Path.with_name raises ValueError on an unescaped separator).
    lease = _lease(tmp_path)
    assert lease.acquire("host/123", ttl_seconds=900, now=1000.0).acquired
    assert lease.refresh("host/123", now=1100.0)
    assert lease.release("host/123")  # exercises _atomic_take's .take. filename
    assert not lease.is_held(now=1200.0)


def test_reclaim_stale_lease_held_by_separator_holder_id(tmp_path):
    # Reclaiming a stale lease whose holder_id has a separator exercises the
    # .reclaim. temp filename; it must not raise ValueError.
    _lease(tmp_path).acquire("host/123", ttl_seconds=900, now=1000.0)
    r = _lease(tmp_path).acquire("recovery", now=1000.0 + 901, ttl_seconds=900)
    assert r.acquired and r.reason == "reclaimed_stale_lease"


def test_from_json_rejects_zero_persisted_ttl(tmp_path):
    from scripts.scheduler_lease import Lease

    # A persisted ttl_seconds of 0 must parse to None (malformed), not silently
    # become a live 900s lease via `or DEFAULT_TTL_SECONDS`.
    assert Lease.from_json('{"holder_id":"A","acquired_at_epoch":1000.0,"ttl_seconds":0}') is None


def test_from_json_rejects_infinite_ttl(tmp_path):
    from scripts.scheduler_lease import Lease

    # JSON 1e999 parses to inf; int(inf) raises OverflowError, which must be
    # treated as malformed (None), not propagate and crash acquire.
    assert Lease.from_json(
        '{"holder_id":"A","acquired_at_epoch":1000.0,"ttl_seconds":1e999}'
    ) is None


def test_acquire_reclaims_lease_with_infinite_ttl(tmp_path):
    lease = _lease(tmp_path)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    lease.path.write_text(
        '{"holder_id":"crashed","acquired_at_epoch":1000.0,"ttl_seconds":1e999}',
        encoding="utf-8",
    )
    # A corrupt infinite TTL must not crash acquire; the window is reclaimable.
    r = lease.acquire("recovery", now=1001.0)
    assert r.acquired
    assert _lease(tmp_path).read().holder_id == "recovery"


def test_acquire_reclaims_lease_with_zero_persisted_ttl(tmp_path):
    lease = _lease(tmp_path)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    lease.path.write_text(
        '{"holder_id":"crashed","acquired_at_epoch":1000.0,"ttl_seconds":0}',
        encoding="utf-8",
    )
    # The crashed window must be reclaimable immediately, not wedged for 15 min.
    r = lease.acquire("recovery", now=1001.0)
    assert r.acquired
    assert _lease(tmp_path).read().holder_id == "recovery"


def test_acquire_reclaims_lease_with_nonfinite_timestamp(tmp_path):
    lease = _lease(tmp_path)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    # A NaN timestamp would make is_stale() false forever -> never reclaimable.
    lease.path.write_text('{"holder_id":"A","acquired_at_epoch":"nan","ttl_seconds":900}',
                          encoding="utf-8")
    assert lease.read() is None
    r = lease.acquire("recovery", now=1000.0)
    assert r.acquired
    assert _lease(tmp_path).read().holder_id == "recovery"


# --- _reclaim_stale / _atomic_take: race interleavings ------------------------
#
# In production every acquire/refresh/release is serialized through _guard()'s
# cross-process lock, so two racers should never both reach _reclaim_stale or
# _atomic_take concurrently -- but both methods defensively handle the case
# where they do (belt-and-suspenders against a guard gap on an exotic
# platform, or a future caller of the private methods). That defensive
# handling had no test at all: every acquire/release test above only ever
# exercises a single mutator's happy path. These tests call the private
# methods directly (bypassing _guard, which the docstrings note they "MUST
# run under" in normal use) to deterministically force each interleaving
# rather than hoping a real race lands on it.


def test_reclaim_stale_loses_the_race_when_the_lease_is_already_gone(tmp_path):
    """Two reclaimers of the same stale lease: os.rename removes the source,
    so only one can succeed. The loser sees FileNotFoundError -- simulated
    here directly by reclaiming against a base_dir with no lease file at
    all, the same shape as "someone else's rename already won"."""
    lease = _lease(tmp_path)
    candidate = Lease(
        holder_id="me", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=1000.0, ttl_seconds=900,
    )
    result = lease._reclaim_stale(candidate, now=1000.0)
    assert result.acquired is False
    assert result.reason == "lost_reclaim_race"
    assert result.current_holder is None


def test_reclaim_stale_restores_a_lease_refreshed_mid_reclaim(tmp_path):
    """The holder refreshed (extended its TTL) in the window between the
    outer staleness check and this rename: the just-renamed content is NOT
    actually stale as of `now`, so the reclaim must stand down and restore
    the holder's lease rather than steal it out from under a live runner."""
    lease = _lease(tmp_path)
    live = Lease(
        holder_id="original", correlation_id="corr-1", window_key=lease.key,
        acquired_at_epoch=1000.0, ttl_seconds=900,
    )
    lease._write_new(live)  # seed as if `now` is inside [1000, 1900) -- not stale

    candidate = Lease(
        holder_id="reclaimer", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=1000.0, ttl_seconds=900,
    )
    result = lease._reclaim_stale(candidate, now=1000.0)

    assert result.acquired is False
    assert result.reason == "holder_refreshed_during_reclaim"
    assert result.current_holder.holder_id == "original"
    # The original holder's lease is restored at the canonical path, not
    # left stranded under a .reclaim. temp name.
    restored = lease.read()
    assert restored is not None
    assert restored.holder_id == "original"
    assert list((tmp_path / "leases").glob("*.reclaim.*")) == []


def test_reclaim_stale_loses_the_race_to_create_the_new_lease(tmp_path, monkeypatch):
    """Between this reclaimer confirming the stolen lease really is stale and
    its own O_CREAT|O_EXCL _write_new, a second reclaimer (or a fresh
    acquire) wins and creates the lease first. _write_new then raises
    FileExistsError, which must surface as an un-acquired result carrying
    the winner's lease, not propagate raw."""
    lease = _lease(tmp_path)
    stale = Lease(
        holder_id="dead", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=0.0, ttl_seconds=1,
    )
    lease._write_new(stale)

    real_rename = os.rename
    competitor = Lease(
        holder_id="other-reclaimer", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=1000.0, ttl_seconds=900,
    )

    def _rename_then_lose_the_recreate_race(src, dst):
        real_rename(src, dst)
        # A second reclaimer wins the create race immediately after our steal.
        lease._write_new(competitor)

    monkeypatch.setattr(scheduler_lease.os, "rename", _rename_then_lose_the_recreate_race)

    mine = Lease(
        holder_id="me", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=1000.0, ttl_seconds=900,
    )
    result = lease._reclaim_stale(mine, now=1000.0)

    assert result.acquired is False
    assert result.reason == "lost_after_reclaim"
    assert result.current_holder.holder_id == "other-reclaimer"
    assert lease.read().holder_id == "other-reclaimer"  # the winner's lease is intact


def test_release_restores_a_lease_taken_by_a_racing_reclaim(tmp_path, monkeypatch):
    """_atomic_take's read() sees "me" as the holder, but between that read
    and its os.rename, a concurrent reclaimer replaces the lease file's
    content with a different holder's. The rename then moves the OTHER
    holder's content, not "me"'s -- release must detect the ownership
    mismatch, restore the other holder's lease (never leaving the window
    lease-less), and report failure rather than silently deleting a lease
    it never actually verified as its own."""
    lease = _lease(tmp_path)
    mine = Lease(
        holder_id="me", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=1000.0, ttl_seconds=900,
    )
    lease._write_new(mine)

    real_rename = os.rename
    other = Lease(
        holder_id="other", correlation_id=None, window_key=lease.key,
        acquired_at_epoch=2000.0, ttl_seconds=900,
    )

    def _rename_after_racing_reclaim(src, dst):
        # Simulate a concurrent reclaim replacing the lease file's content
        # between _atomic_take's read() and this rename.
        from pathlib import Path

        Path(src).write_text(other.to_json() + "\n", encoding="utf-8")
        real_rename(src, dst)

    monkeypatch.setattr(scheduler_lease.os, "rename", _rename_after_racing_reclaim)

    assert lease.release("me") is False
    restored = lease.read()
    assert restored is not None
    assert restored.holder_id == "other"
    assert list((tmp_path / "leases").glob("*.take.*")) == []


class _ExplodingHandle:
    """A write-mode file object standing in for a disk failure mid-write."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def write(self, _data):
        raise OSError("simulated disk full")


def test_write_new_cleans_up_a_partial_write_on_failure(tmp_path, monkeypatch):
    """A write failure partway through _write_new (disk error,
    KeyboardInterrupt) must not leave a zero-byte/partial lease file behind
    -- the next acquire would otherwise see a lease "exists" but can never
    be parsed as anything other than malformed-stale, silently masking the
    real failure."""
    import pytest

    lease = _lease(tmp_path)

    def _fake_fdopen(fd, mode, encoding=None):
        os.close(fd)
        return _ExplodingHandle()

    monkeypatch.setattr(scheduler_lease.os, "fdopen", _fake_fdopen)
    with pytest.raises(OSError):
        lease._write_new(
            Lease(
                holder_id="me", correlation_id=None, window_key=lease.key,
                acquired_at_epoch=1000.0, ttl_seconds=900,
            )
        )
    assert not lease.path.exists()


def test_atomic_swap_cleans_up_its_temp_file_on_failure(tmp_path, monkeypatch):
    """A write failure partway through _atomic_swap's temp-file write (used
    by refresh()) must not leave a stray *.swap.tmp file behind, and the
    live lease at self.path must be untouched (os.replace never ran)."""
    import pytest

    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)

    def _fake_fdopen(fd, mode, encoding=None):
        os.close(fd)
        return _ExplodingHandle()

    monkeypatch.setattr(scheduler_lease.os, "fdopen", _fake_fdopen)
    with pytest.raises(OSError):
        lease._atomic_swap(
            Lease(
                holder_id="runner", correlation_id=None, window_key=lease.key,
                acquired_at_epoch=1100.0, ttl_seconds=900,
            )
        )
    assert list((tmp_path / "leases").glob("*.swap.tmp")) == []
    # The original lease (acquired_at_epoch=1000.0) is untouched: os.replace
    # never ran, so the swap never took effect.
    assert lease.read().acquired_at_epoch == 1000.0


# --- real cross-process exclusion ---------------------------------------------


def _mp_acquire_worker(base_dir, key, holder_id, now, queue):
    """Top-level (picklable) worker: race a real OS process against others
    for the same window lease at the identical `now`."""
    from scripts.scheduler_lease import SchedulerLease as _SchedulerLease

    result = _SchedulerLease(base_dir, key).acquire(holder_id, now=now, ttl_seconds=900)
    queue.put((holder_id, result.acquired, result.reason))


def test_concurrent_os_processes_racing_the_same_lease_have_exactly_one_winner(tmp_path):
    """Every test above proves the state machine's logic but runs single-
    process: _guard()'s exclusion is documented as CROSS-PROCESS (fcntl.flock
    or the portable O_EXCL fallback), which a single Python process can never
    actually exercise. Spawn several real OS processes racing to acquire the
    identical window at the identical `now` and confirm exactly one wins --
    the guarantee the 2026-07-23 incident (a background and a foreground
    recovery racing each other's placement) depends on."""
    base_dir = str(tmp_path / "leases")
    key = "2026-07-23__midday__1230et"
    now = 1000.0
    worker_count = 6

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_mp_acquire_worker, args=(base_dir, key, f"runner-{i}", now, queue))
        for i in range(worker_count)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker process failed: exitcode={p.exitcode}"

    results = [queue.get(timeout=10) for _ in range(worker_count)]
    winners = [r for r in results if r[1]]
    losers = [r for r in results if not r[1]]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(losers) == worker_count - 1
    assert all(reason == "held_by_live_holder" for _, _, reason in losers)


# --- remaining fail-closed edges -----------------------------------------------


def test_acquire_rejects_empty_holder_id(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        _lease(tmp_path).acquire("", now=1000.0)


def test_from_json_rejects_a_json_value_that_is_not_an_object(tmp_path):
    # Valid JSON, but not a dict at all (e.g. a bare list) -- distinct from
    # the JSONDecodeError case (test_acquire_reclaims_malformed_lease_file)
    # and from a dict missing holder_id.
    assert Lease.from_json("[1, 2, 3]") is None
    assert Lease.from_json('{"correlation_id": "x"}') is None  # no holder_id


def test_lease_result_bool_reflects_acquired():
    assert bool(scheduler_lease.LeaseResult(acquired=True))
    assert not scheduler_lease.LeaseResult(acquired=False)


def test_acquire_with_steal_stale_false_reports_a_malformed_lease_without_reclaiming(tmp_path):
    lease = _lease(tmp_path)
    lease.path.parent.mkdir(parents=True, exist_ok=True)
    lease.path.write_text("{ not closed json", encoding="utf-8")  # unparseable
    r = lease.acquire("recovery", now=1000.0, steal_stale=False)
    assert r.acquired is False
    assert r.reason == "malformed_lease"
    # Left untouched, not reclaimed.
    assert lease.path.read_text(encoding="utf-8") == "{ not closed json"


def test_acquire_with_steal_stale_false_does_not_reclaim_a_stale_lease(tmp_path):
    lease = _lease(tmp_path)
    lease.acquire("dead-runner", ttl_seconds=900, now=1000.0)
    # Stale as of 1000 + 901, but steal_stale=False must refuse rather than
    # silently reclaim it -- the caller is asking only "is it free right now".
    r = _lease(tmp_path).acquire("recovery", now=1000.0 + 901, ttl_seconds=900, steal_stale=False)
    assert r.acquired is False
    assert r.reason == "held"
    assert lease.read().holder_id == "dead-runner"  # untouched


def test_atomic_take_returns_none_on_a_plain_os_error(tmp_path, monkeypatch):
    """_atomic_take's rename can fail with an OSError that is NOT
    FileNotFoundError (e.g. a permission error, or an exotic filesystem
    error). This must still fail closed -- _atomic_take's own try/except
    OSError returns None, leaving the lease exactly as it was -- rather
    than propagate out of release()."""
    lease = _lease(tmp_path)
    lease.acquire("runner", ttl_seconds=900, now=1000.0)

    def _raise(src, dst):
        raise OSError("simulated permission error")

    monkeypatch.setattr(scheduler_lease.os, "rename", _raise)

    result = lease._atomic_take("runner")
    assert result is None
    assert lease.read().holder_id == "runner"  # unchanged
    assert lease.release("runner") is False  # same path, through the public API


def test_release_with_runs_the_action_only_for_the_current_holder(tmp_path):
    lease = SchedulerLease(tmp_path, "k")
    assert lease.acquire("a", ttl_seconds=60, now=100.0).acquired
    ran: list[str] = []
    assert lease.release_with("b", lambda: ran.append("b")) is False
    assert ran == [] and lease.read() is not None
    assert lease.release_with("a", lambda: ran.append("a")) is True
    assert ran == ["a"] and lease.read() is None
