#!/usr/bin/env python3
"""Slot heartbeat for the local (primary) run, and the status probe the Perplexity backup calls.

The local run wraps its slot with ``start`` / ``beat`` / ``finish``; the backup, on the Mac, calls ``status``.
State is a ``scheduler_lease`` file (the heartbeat) plus a result marker, both under
``<cron root>/scheduler_leases`` (``dca_paths.cron_tracking_root()``). Mapping to a ``LocalStatus`` is
``dca_execution_router.local_status_from_probe``; see ``docs/adr-plan-first-backup-executor.md``
("Amendment: execution routing"). Files here are inputs only, never purchase evidence. No credentials.

    python3 -m scripts.local_heartbeat start  --trading-day D --slot S      # prints {"ok", "token"}
    python3 -m scripts.local_heartbeat beat   --trading-day D --slot S --token T   # at least every 60s
    python3 -m scripts.local_heartbeat finish --trading-day D --slot S --token T --outcome success|failure
    python3 -m scripts.local_heartbeat status --trading-day D --slot S [--mac-status online]   # repeatable probe
    python3 -m scripts.local_heartbeat claim  --trading-day D --slot S [--mac-status online]   # once, before the first order
    python3 -m scripts.local_heartbeat confirm --trading-day D --slot S   # once, after the first order is accepted

A ``start`` after a successful fallback ``claim`` for the same slot is refused (``fallback_claimed``).

Every ``start`` mints a unique run token and is the lease holder id. ``scheduler_lease`` treats a same-holder
re-acquire as success, so a holder id shared by every run of a slot would let two local runs both start (and
both place orders) and let either release the other's lease. With a per-run token a duplicate ``start`` finds
the lease held and is refused, and ``beat``/``finish`` only act for the run that owns the lease.

Every command prints one JSON object and exits 0, except a failed ``start``/``beat``/``finish`` (exit 1).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import dca_execution_router as router
import dca_paths
from scripts.file_lock import cross_process_lock
from scripts.scheduler_lease import SchedulerLease, window_key

STREAM = "primary"
# Fail closed: a state lock that cannot be taken within its timeout is UNKNOWN and the backup stands down.
_STAND_DOWN = {"local_status": "unknown", "backup_may_act": False}


def lease_dir() -> Path:
    return Path(dca_paths.cron_tracking_root()) / "scheduler_leases"


def _lease(day: str, slot: str) -> tuple[SchedulerLease, Path]:
    key = window_key(day, STREAM, slot)
    base = lease_dir()
    return SchedulerLease(base, key), base / f"{key}.result"


@contextlib.contextmanager
def _state_lock(day: str, slot: str):
    """One cross-process critical section for a slot's heartbeat state (lease + result marker + routing claim).

    ``start`` (acquire + marker reset), ``finish``, every probe read, and ``claim`` (check + record) take it, so a
    probe never sees a fresh lease beside a stale marker and two claimants never both pass the check (review of
    PR 1639). Lock order is always this lock, then the lease's own guard; ``beat`` takes only the lease guard.
    """
    base = lease_dir()
    base.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(base / f"{window_key(day, STREAM, slot)}.state.lock"):
        yield


def _holder(slot: str, token: str) -> str:
    return f"local-{slot}-{token}"


def _read_marker(path: Path):
    """``(value, readable)``: absent is ``(None, True)``; an unreadable or unrecognized marker is not readable."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    return (text, True) if text in ("success", "failure") else (None, False)


def _write_marker_atomically(path: Path, outcome: str) -> None:
    """Write the marker via a temp file and ``os.replace`` so a crash never leaves a partial marker at ``path``.

    A torn marker would read as unrecognized (UNKNOWN), and a truncated one that happened to parse could flip the
    run's recorded outcome, so the marker path only ever holds a complete value (PR 1644 review).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    try:
        try:
            os.write(fd, (outcome + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp_name, 0o644)  # mkstemp creates 0600; match the lease file
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def start(day: str, slot: str, *, now: float | None = None) -> dict:
    lease, marker = _lease(day, slot)
    token = uuid.uuid4().hex  # unique per run: see the module docstring
    try:
        with _state_lock(day, slot):  # acquire and marker reset are one step to every probe
            log_dir = router.default_log_dir()
            stamp = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
            if (
                router.confirmed_buy_path(log_dir, day, slot) == router.PATH_FALLBACK
                or router.live_claim(log_dir, day, slot, stamp)
            ):
                # The backup claimed this slot (claim and start share this lock) and has either bought or may still
                # be about to, so a late primary must not also buy: refuse rather than reclaim the lease. A claim
                # abandoned past its TTL no longer blocks (Codex adversarial reviews of PR 1639).
                return {"ok": False, "reason": "fallback_claimed"}
            result = lease.acquire(_holder(slot, token), ttl_seconds=router.heartbeat_stale_seconds(), now=now)
            if not result.acquired:
                # A live run owns the slot: leave its lease AND any result marker untouched.
                return {"ok": False, "reason": result.reason}
            try:
                marker.unlink(missing_ok=True)  # a re-run of the same slot starts clean (only once we own the lease)
            except OSError:
                # A fresh lease beside a stale marker reads as the previous run's outcome (the marker wins), so the
                # backup could buy while this run is live. Give the lease back rather than run in that state
                # (adversarial review of PR 1639).
                if not lease.release(_holder(slot, token)):
                    # The take failed too, so the fresh lease is still held and blocks retries until its TTL:
                    # say so rather than report only the marker failure (Copilot review of PR 1648).
                    return {"ok": False, "reason": "marker_reset_failed", "lease_released": False}
                return {"ok": False, "reason": "marker_reset_failed"}
    except TimeoutError:
        return {"ok": False, "reason": "state_lock_timeout"}
    return {"ok": True, "reason": result.reason, "token": token}


def beat(day: str, slot: str, token: str, *, now: float | None = None) -> dict:
    lease, _ = _lease(day, slot)
    return {"ok": lease.refresh(_holder(slot, token), now=now)}


def finish(day: str, slot: str, token: str, outcome: str) -> dict:
    lease, marker = _lease(day, slot)

    def write_marker() -> None:
        _write_marker_atomically(marker, outcome)  # marker first: a crash between the two reads as finished

    # Ownership check, marker write and release are one critical section under the lease guard: a superseded run
    # (stale, then reclaimed by a new run) writes no marker and releases nothing (review of PR 1639).
    try:
        with _state_lock(day, slot):
            released = lease.release_with(_holder(slot, token), write_marker)
    except TimeoutError:
        return {"ok": False, "released": False}
    return {"ok": released, "released": released}


def _probe(day: str, slot: str, mac_status: str, now: float | None):
    """Caller must hold ``_state_lock``."""
    lease, marker = _lease(day, slot)
    stamp = time.time() if now is None else now
    current = lease.read()
    value, marker_ok = _read_marker(marker)
    if (current is None and lease.path.exists()) or not marker_ok:
        # A lease or marker that exists but cannot be parsed is not "no lease": it may be the live primary mid-write.
        # No heartbeat read means UNKNOWN for an online Mac, and the backup stands down (review of PR 1639).
        heartbeat = None
    else:
        heartbeat = router.Heartbeat(lease=current, marker=value)
    local = router.local_status_from_probe(mac_status, heartbeat, now_epoch=stamp)
    log_dir = router.default_log_dir()
    decision = router.decide(
        router.ExecutorRole.PERPLEXITY,
        local,
        now=datetime.fromtimestamp(stamp, tz=timezone.utc),
        log_dir=log_dir,
        trading_day=day,
        slot=slot,
    )
    return local, decision, log_dir


def status(day: str, slot: str, mac_status: str = "online", *, now: float | None = None) -> dict:
    """Read-only probe: logged as a probe, never counted as a buy, so it can be repeated (late-start guard)."""
    try:
        with _state_lock(day, slot):
            local, decision, log_dir = _probe(day, slot, mac_status, now)
            router.record(decision, log_dir=log_dir, trading_day=day, slot=slot, probe=True)
    except TimeoutError:
        return dict(_STAND_DOWN)
    return {"local_status": local.value, "backup_may_act": decision.may_place}


def claim(day: str, slot: str, mac_status: str = "online", *, now: float | None = None) -> dict:
    """Re-check and, if the backup may still act, record an unconfirmed CLAIM on the slot.

    Called once, right before the first order. The claim blocks a second backup run and a late primary ``start``
    until its TTL lapses, but it is not purchase evidence: ``confirm`` records the buy once an order is accepted,
    so a run interrupted between claim and order leaves a claim that expires instead of a false buy record.
    """
    try:
        with _state_lock(day, slot):  # check and record are one step: exactly one concurrent claimant wins
            local, decision, log_dir = _probe(day, slot, mac_status, now)
            if decision.may_place:
                router.record(decision, log_dir=log_dir, trading_day=day, slot=slot, kind="claim")
    except TimeoutError:
        return dict(_STAND_DOWN)
    return {"local_status": local.value, "backup_may_act": decision.may_place}


def confirm(day: str, slot: str, *, now: float | None = None) -> dict:
    """Record the fallback buy as confirmed. Call once, after the first order is accepted by the broker."""
    stamp = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    try:
        with _state_lock(day, slot):
            log_dir = router.default_log_dir()
            if router.confirmed_buy_path(log_dir, day, slot) == router.PATH_FALLBACK:
                return {"ok": True, "already": True}
            if not router.has_claim(log_dir, day, slot):
                return {"ok": False, "reason": "no_claim"}  # nothing to confirm: claim first
            router.confirm_buy(log_dir=log_dir, trading_day=day, slot=slot, now=stamp)
    except TimeoutError:
        return {"ok": False, "reason": "state_lock_timeout"}
    return {"ok": True, "already": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "beat", "finish", "status", "claim", "confirm"):
        p = sub.add_parser(name)
        p.add_argument("--trading-day", required=True)
        p.add_argument("--slot", required=True)
        if name in ("beat", "finish"):
            p.add_argument("--token", required=True)
        if name == "finish":
            p.add_argument("--outcome", required=True, choices=("success", "failure"))
        if name in ("status", "claim"):
            p.add_argument("--mac-status", default="online")
    args = parser.parse_args(argv)
    if args.command == "start":
        out = start(args.trading_day, args.slot)
    elif args.command == "beat":
        out = beat(args.trading_day, args.slot, args.token)
    elif args.command == "finish":
        out = finish(args.trading_day, args.slot, args.token, args.outcome)
    elif args.command == "claim":
        out = claim(args.trading_day, args.slot, args.mac_status)
    elif args.command == "confirm":
        out = confirm(args.trading_day, args.slot)
    else:
        out = status(args.trading_day, args.slot, args.mac_status)
    print(json.dumps(out, sort_keys=True))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
