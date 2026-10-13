"""scripts/local_heartbeat.py: the lease and the result marker must change together.

Copilot review of #1641 (`scripts/local_heartbeat.py` lines 69 and 96): ``start`` publishes the new live lease and
only afterwards removes the previous run's ``failure`` marker, and ``finish`` checks lease ownership and then writes
the marker as a separate step. A concurrent ``status()`` (the Perplexity backup's probe) or a concurrent ``start``
can land in either gap:

* status sees a LIVE lease plus an old ``failure`` marker, reads it as errored, and authorizes the backup while the
  new local run is active (duplicate executors);
* a superseded run's ``finish`` writes its marker over a newer run that already owns the slot.

Both tests below are written from the required behaviour, not from any particular fix: they pause one operation
inside its gap on a thread and let a competing operation run, then assert only the invariant. A fix that
serializes the two steps (for example one cross-process lock shared by ``start``, ``finish`` and ``status``) makes
the competing call wait, which these tests allow; an implementation that leaves the gap open fails them.
"""

from __future__ import annotations

import threading

import pytest

from scripts import local_heartbeat as hb
from scripts.scheduler_lease import SchedulerLease

DAY, SLOT = "2026-10-08", "1530"
GAP_SECONDS = 0.5  # how long a competing call is given to run while the other is paused inside its gap


@pytest.fixture(autouse=True)
def _lease_root(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "lease_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(hb.router, "default_log_dir", lambda: tmp_path / "routing")


def _run_in_thread(name, fn, *args, **kwargs):
    box = {"done": threading.Event()}

    def target():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:  # surfaced by the test, never swallowed
            box["error"] = exc
        finally:
            box["done"].set()

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread, box


def _signal_when_entered(monkeypatch, thread_name):
    """Return an Event set when ``thread_name`` asks for a slot's heartbeat state lock.

    status(), start() and finish() all take ``_state_lock(day, slot)`` before touching any state. Waiting on this proves
    the competing call is actually running before the paused call is released, so a thread the OS was slow to schedule
    cannot make a broken implementation pass by only starting after the gap has closed. The signal fires on the request,
    before the lock is held, so it still fires when the lock makes the competing call wait.
    """
    entered = threading.Event()
    real = hb._state_lock

    def lock_signalling_request(day, slot):
        if threading.current_thread().name == thread_name:
            entered.set()
        return real(day, slot)

    monkeypatch.setattr(hb, "_state_lock", lock_signalling_request)
    return entered


def test_status_never_pairs_a_live_new_lease_with_the_previous_runs_failure_marker(monkeypatch):
    first = hb.start(DAY, SLOT, now=1000.0)["token"]
    hb.finish(DAY, SLOT, first, "failure")  # leaves a 'failure' marker behind and releases the lease

    in_gap = threading.Event()
    proceed = threading.Event()
    real_acquire = SchedulerLease.acquire

    def acquire_then_pause(self, *args, **kwargs):
        result = real_acquire(self, *args, **kwargs)
        if threading.current_thread().name == "starter" and result.acquired:
            in_gap.set()  # the new lease is now visible; start() has not removed the old marker yet
            assert proceed.wait(5), "test never released the paused start()"
        return result

    monkeypatch.setattr(SchedulerLease, "acquire", acquire_then_pause)
    probing = _signal_when_entered(monkeypatch, "prober")
    starter, start_box = _run_in_thread("starter", hb.start, DAY, SLOT, now=2000.0)
    assert in_gap.wait(5), "start() never reached the point after acquiring the lease"

    prober, probe_box = _run_in_thread("prober", hb.status, DAY, SLOT, now=2001.0)
    assert probing.wait(5), "the probe never started, so the paused start() proves nothing"
    probe_box["done"].wait(GAP_SECONDS)  # answers now if the gap is open; still blocked behind start() if it is closed
    proceed.set()
    starter.join(5)
    prober.join(5)

    assert "error" not in start_box, start_box.get("error")
    assert "error" not in probe_box, probe_box.get("error")
    assert start_box["result"]["ok"] is True
    assert probe_box["result"] == {"local_status": "in_progress", "backup_may_act": False}, (
        "the backup was told it may act while a new local run holds a live lease"
    )


def test_a_superseded_runs_finish_cannot_write_its_marker_over_a_newer_run(monkeypatch):
    """Guards the ownership check + marker write + release being one step.

    SchedulerLease.release_with already makes that one step under the lease's own guard, so this passes even without
    the heartbeat state lock; it is a regression test for that guarantee, not for the state lock (which the status
    test above covers).
    """
    ttl = hb.router.heartbeat_stale_seconds()
    old = hb.start(DAY, SLOT, now=1000.0)["token"]

    at_gate = threading.Event()
    proceed = threading.Event()
    real_read = SchedulerLease.read

    def read_then_pause(self):
        lease = real_read(self)
        if threading.current_thread().name == "finisher":
            at_gate.set()  # ownership has been checked against the OLD run; the marker is not written yet
            assert proceed.wait(5), "test never released the paused finish()"
        return lease

    monkeypatch.setattr(SchedulerLease, "read", read_then_pause)
    restarting = _signal_when_entered(monkeypatch, "restarter")
    finisher, finish_box = _run_in_thread("finisher", hb.finish, DAY, SLOT, old, "failure")
    assert at_gate.wait(5), "finish() never read the lease"

    # The old run's lease has expired, so a new run legitimately takes the slot while finish() is paused.
    starter, start_box = _run_in_thread("restarter", hb.start, DAY, SLOT, now=1000.0 + ttl + 1)
    assert restarting.wait(5), "the restart never began, so the paused finish() proves nothing"
    start_box["done"].wait(GAP_SECONDS)  # completes now if the gap is open; waits for finish() if it is closed
    proceed.set()
    finisher.join(5)
    starter.join(5)

    assert "error" not in finish_box, finish_box.get("error")
    assert "error" not in start_box, start_box.get("error")
    assert start_box["result"]["ok"] is True

    lease, marker = hb._lease(DAY, SLOT)
    holder = lease.read()
    assert holder is not None and holder.holder_id == hb._holder(SLOT, start_box["result"]["token"])
    assert not marker.exists(), "the superseded run's marker was written over the run that now owns the slot"
