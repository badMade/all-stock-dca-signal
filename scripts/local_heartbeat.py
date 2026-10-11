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
    python3 -m scripts.local_heartbeat status --trading-day D --slot S [--mac-status online]

Every ``start`` mints a unique run token and is the lease holder id. ``scheduler_lease`` treats a same-holder
re-acquire as success, so a holder id shared by every run of a slot would let two local runs both start (and
both place orders) and let either release the other's lease. With a per-run token a duplicate ``start`` finds
the lease held and is refused, and ``beat``/``finish`` only act for the run that owns the lease.

Every command prints one JSON object and exits 0, except a failed ``start``/``beat``/``finish`` (exit 1).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import dca_execution_router as router
import dca_paths
from scripts.scheduler_lease import SchedulerLease, window_key

STREAM = "primary"


def lease_dir() -> Path:
    return Path(dca_paths.cron_tracking_root()) / "scheduler_leases"


def _lease(day: str, slot: str) -> tuple[SchedulerLease, Path]:
    key = window_key(day, STREAM, slot)
    base = lease_dir()
    return SchedulerLease(base, key), base / f"{key}.result"


def _holder(slot: str, token: str) -> str:
    return f"local-{slot}-{token}"


def _read_marker(path: Path):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if text in ("success", "failure") else None


def start(day: str, slot: str, *, now: float | None = None) -> dict:
    lease, marker = _lease(day, slot)
    token = uuid.uuid4().hex  # unique per run: see the module docstring
    result = lease.acquire(_holder(slot, token), ttl_seconds=router.heartbeat_stale_seconds(), now=now)
    if not result.acquired:
        # A live run owns the slot: leave its lease AND any result marker untouched.
        return {"ok": False, "reason": result.reason}
    marker.unlink(missing_ok=True)  # a re-run of the same slot starts clean (only once we own the lease)
    return {"ok": True, "reason": result.reason, "token": token}


def beat(day: str, slot: str, token: str, *, now: float | None = None) -> dict:
    lease, _ = _lease(day, slot)
    return {"ok": lease.refresh(_holder(slot, token), now=now)}


def finish(day: str, slot: str, token: str, outcome: str) -> dict:
    lease, marker = _lease(day, slot)
    holder = _holder(slot, token)
    current = lease.read()
    if current is None or current.holder_id != holder:
        # Not the run that owns the lease (superseded, never started, wrong token): no marker, no release.
        return {"ok": False, "released": False}
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(outcome + "\n", encoding="utf-8")  # marker first: a crash between the two reads as finished
    return {"ok": True, "released": lease.release(holder)}


def status(day: str, slot: str, mac_status: str = "online", *, now: float | None = None) -> dict:
    lease, marker = _lease(day, slot)
    stamp = time.time() if now is None else now
    heartbeat = router.Heartbeat(lease=lease.read(), marker=_read_marker(marker))
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
    router.record(decision, log_dir=log_dir, trading_day=day, slot=slot)
    return {"local_status": local.value, "backup_may_act": decision.may_place}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "beat", "finish", "status"):
        p = sub.add_parser(name)
        p.add_argument("--trading-day", required=True)
        p.add_argument("--slot", required=True)
        if name in ("beat", "finish"):
            p.add_argument("--token", required=True)
        if name == "finish":
            p.add_argument("--outcome", required=True, choices=("success", "failure"))
        if name == "status":
            p.add_argument("--mac-status", default="online")
    args = parser.parse_args(argv)
    if args.command == "start":
        out = start(args.trading_day, args.slot)
    elif args.command == "beat":
        out = beat(args.trading_day, args.slot, args.token)
    elif args.command == "finish":
        out = finish(args.trading_day, args.slot, args.token, args.outcome)
    else:
        out = status(args.trading_day, args.slot, args.mac_status)
    print(json.dumps(out, sort_keys=True))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
