"""Tests for file-based scheduler leases."""

from __future__ import annotations

from scripts.scheduler_lease import SchedulerLease, window_key


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
