"""Execution routing for the primary buy: local first, Perplexity only on local failure.

Roles (docs/adr-plan-first-backup-executor.md, "Amendment: execution routing"):

* ``LOCAL``      -- the Mac run; sole executor of the buy whenever it is alive.
* ``CLOUD``      -- calculation only; it can read and size but never reaches ``place_order``.
* ``PERPLEXITY`` -- backup buyer; acts only when local is unreachable, off, or errored.

This module is a pure decision + log layer. It never places an order itself, reads no
credentials, and does not touch the sizing ladder or any sizing suggester. Whether local is
alive is decided by ``local_status_from_probe`` from a bounded reachability probe plus the
slot lease's heartbeat. Broker order history stays the only purchase evidence; the routing
log here is a second guard against a duplicate buy, never proof of one.

Environment (no secrets):
    DCA_LOCAL_PROBE_TIMEOUT_S      reachability probe bound, default ``MAC_REACHABILITY_TIMEOUT_S`` (60)
    DCA_LOCAL_HEARTBEAT_STALE_S    lease age after which local counts as dead, default 300
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import dca_order_plan
import dca_paths

DEFAULT_HEARTBEAT_STALE_S = 300

PATH_LOCAL = "local"
PATH_FALLBACK = "perplexity-fallback"
PATH_NONE = "none"
_BUY_PATHS = frozenset({PATH_LOCAL, PATH_FALLBACK})


class ExecutorRole(str, enum.Enum):
    LOCAL = "local"
    CLOUD = "cloud"
    PERPLEXITY = "perplexity"


class LocalStatus(str, enum.Enum):
    REACHABLE = "reachable"
    SUCCEEDED = "succeeded"
    IN_PROGRESS = "in_progress"
    UNREACHABLE = "unreachable"
    OFF = "off"
    ERRORED = "errored"
    UNKNOWN = "unknown"


# The only states in which the backup may act (the 2026-10-08 operator decision: UNKNOWN stands down).
_FALLBACK_STATES = frozenset({LocalStatus.UNREACHABLE, LocalStatus.OFF, LocalStatus.ERRORED})


class OrderPlacementForbidden(RuntimeError):
    """Raised when a role that may not place orders reaches ``place_order``."""


@dataclass(frozen=True)
class Decision:
    role: ExecutorRole
    local_status: LocalStatus
    may_place: bool
    path: str
    reason: str
    timestamp: str


@dataclass(frozen=True)
class Heartbeat:
    """What the probe read from the Mac's slot lease. ``lease`` is a ``scheduler_lease.Lease`` or None."""

    lease: Any = None
    marker: Optional[str] = None  # "success" | "failure" | None, written by the local run at release


def heartbeat_stale_seconds() -> int:
    raw = os.environ.get("DCA_LOCAL_HEARTBEAT_STALE_S", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HEARTBEAT_STALE_S
    return value if value > 0 else DEFAULT_HEARTBEAT_STALE_S


def probe_timeout_seconds() -> int:
    raw = os.environ.get("DCA_LOCAL_PROBE_TIMEOUT_S", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return dca_order_plan.MAC_REACHABILITY_TIMEOUT_S
    return value if value > 0 else dca_order_plan.MAC_REACHABILITY_TIMEOUT_S


def default_log_dir() -> Path:
    return Path(dca_paths.cron_tracking_root()) / "execution_routing"


def local_status_from_probe(
    mac_status: str, heartbeat: Optional[Heartbeat], *, now_epoch: float, stale_s: Optional[int] = None
) -> LocalStatus:
    """Map the probe result (``choose_plan_path``'s status vocabulary) plus the lease to a LocalStatus.

    An unrecognized probe status, or an online Mac whose lease was not read, is UNKNOWN: acting on it
    risks a duplicate buy, so the backup stands down and alerts.
    """
    stale = heartbeat_stale_seconds() if stale_s is None else stale_s
    if mac_status in ("offline", "not_found"):
        return LocalStatus.OFF
    if mac_status == "timeout":
        return LocalStatus.UNREACHABLE
    if mac_status == "device_error":
        return LocalStatus.ERRORED
    if mac_status != "online" or heartbeat is None:
        return LocalStatus.UNKNOWN
    if heartbeat.marker == "success":
        return LocalStatus.SUCCEEDED
    if heartbeat.marker == "failure":
        return LocalStatus.ERRORED
    if heartbeat.lease is None:
        return LocalStatus.OFF  # Mac awake but the slot never fired: act (operator decision)
    age = now_epoch - float(heartbeat.lease.acquired_at_epoch)
    return LocalStatus.IN_PROGRESS if age < stale else LocalStatus.ERRORED


def fallback_plan_source(decision: Decision, mac_status: str, *, plan=None, elapsed_s: float = 0.0) -> Optional[str]:
    """Plan source for an approved fallback, delegated to ``dca_order_plan.choose_plan_path`` (no new logic)."""
    if decision.path != PATH_FALLBACK:
        return None
    return dca_order_plan.choose_plan_path(mac_status, plan=plan, elapsed_s=elapsed_s)


def _log_file(log_dir: Path, trading_day: str) -> Path:
    return Path(log_dir) / f"routing_{trading_day}.jsonl"


def _already_bought(log_dir: Path, trading_day: str, slot: str) -> Optional[str]:
    path = _log_file(log_dir, trading_day)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("slot") == slot and row.get("path") in _BUY_PATHS:
            return str(row["path"])
    return None


def decide(
    role: ExecutorRole,
    local_status: LocalStatus,
    *,
    now: datetime,
    cloud_calc_available: bool = True,
    log_dir: Optional[Path] = None,
    trading_day: Optional[str] = None,
    slot: Optional[str] = None,
) -> Decision:
    """Decide whether ``role`` may place the buy for this slot."""
    stamp = now.isoformat()

    def out(may: bool, path: str, reason: str) -> Decision:
        return Decision(role, local_status, may, path, reason, stamp)

    if role is ExecutorRole.LOCAL:
        return out(True, PATH_LOCAL, "local run is the sole executor")
    if role is ExecutorRole.CLOUD:
        return out(False, PATH_NONE, "cloud run is calculation-only; order placement forbidden")
    if local_status not in _FALLBACK_STATES:
        return out(False, PATH_NONE, f"local status {local_status.value}: backup stands down")
    if not cloud_calc_available:
        return out(False, PATH_NONE, "no cloud calculation outputs available; refusing to invent sizes")
    if log_dir is not None and trading_day and slot:
        prior = _already_bought(Path(log_dir), trading_day, slot)
        if prior:
            return out(False, PATH_NONE, f"buy already recorded for this slot via {prior}")
    return out(True, PATH_FALLBACK, f"local {local_status.value}: perplexity fallback")


def record(decision: Decision, *, log_dir: Optional[Path] = None, trading_day: str, slot: str) -> Decision:
    """Append the decision (path, reason, timestamp) to the day's routing log and return it."""
    directory = Path(log_dir) if log_dir is not None else default_log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "trading_day": trading_day,
        "slot": slot,
        "role": decision.role.value,
        "local_status": decision.local_status.value,
        "may_place": decision.may_place,
        "path": decision.path,
        "reason": decision.reason,
        "timestamp": decision.timestamp,
    }
    with open(_log_file(directory, trading_day), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return decision


class _GuardedBroker:
    def __init__(self, broker: Any, decision: Decision) -> None:
        self._broker = broker
        self._decision = decision

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        if not self._decision.may_place:
            raise OrderPlacementForbidden(self._decision.reason)
        return self._broker.place_order(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._broker, name)


def guard_broker(broker: Any, role: ExecutorRole, local_status: LocalStatus, *, now: datetime, **kwargs: Any) -> Any:
    """Wrap ``broker`` so ``place_order`` raises unless the routing decision permits it."""
    return _GuardedBroker(broker, decide(role, local_status, now=now, **kwargs))
