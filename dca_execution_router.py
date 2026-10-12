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
DEFAULT_CLAIM_TTL_S = 600

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


def claim_ttl_seconds() -> int:
    """How long an unconfirmed backup claim blocks other backups (``DCA_BACKUP_CLAIM_TTL_S``, default 600)."""
    raw = os.environ.get("DCA_BACKUP_CLAIM_TTL_S", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CLAIM_TTL_S
    return value if value > 0 else DEFAULT_CLAIM_TTL_S


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


def _rows(log_dir: Path, trading_day: str, slot: str):
    try:
        lines = _log_file(log_dir, trading_day).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("slot") == slot:
            yield row


def _kind(row: dict) -> str:
    """``probe`` / ``claim`` / ``buy``. Rows written before ``kind`` existed fall back to the ``probe`` flag."""
    kind = row.get("kind")
    if kind in ("probe", "claim", "buy"):
        return kind
    return "probe" if row.get("probe") is True else "buy"


def _already_bought(log_dir: Path, trading_day: str, slot: str) -> Optional[str]:
    for row in _rows(log_dir, trading_day, slot):
        # Only a CONFIRMED buy counts: a status probe's decision and an unconfirmed claim are not purchases
        # (reviews of PR 1639: a claim recorded before any order must not read as a completed buy).
        if row.get("path") in _BUY_PATHS and _kind(row) == "buy":
            return str(row["path"])
    return None


def confirmed_buy_path(log_dir: Path, trading_day: str, slot: str) -> Optional[str]:
    """The path of a confirmed buy recorded for this slot, or None. Public view of ``_already_bought``."""
    return _already_bought(Path(log_dir), trading_day, slot)


def has_claim(log_dir: Path, trading_day: str, slot: str) -> bool:
    """True when any backup claim (expired or not) was recorded for this slot."""
    return any(_kind(row) == "claim" for row in _rows(Path(log_dir), trading_day, slot))


def live_claim(log_dir: Path, trading_day: str, slot: str, now: datetime) -> bool:
    """True when a backup claimed this slot within the claim TTL and has not yet confirmed a buy.

    A claim left behind by an interrupted run expires, so a later backup may claim again; broker order
    history (rule 7 of the prompts) still excludes anything already bought, so a re-claim cannot duplicate.
    """
    ttl = claim_ttl_seconds()
    for row in _rows(Path(log_dir), trading_day, slot):
        if _kind(row) != "claim":
            continue
        try:
            claimed = datetime.fromisoformat(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            return True  # an unreadable claim time is not provably expired: fail closed
        if (now - claimed).total_seconds() < ttl:
            return True
    return False


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
        if live_claim(Path(log_dir), trading_day, slot, now):
            return out(False, PATH_NONE, "another backup run holds an unexpired claim for this slot")
    return out(True, PATH_FALLBACK, f"local {local_status.value}: perplexity fallback")


def record(
    decision: Decision,
    *,
    log_dir: Optional[Path] = None,
    trading_day: str,
    slot: str,
    probe: bool = False,
    kind: Optional[str] = None,
) -> Decision:
    """Append the decision (path, reason, timestamp) to the day's routing log and return it.

    ``kind`` is ``probe`` (a status probe's decision), ``claim`` (the backup is about to place; blocks other
    backups until the claim TTL lapses) or ``buy`` (a confirmed placement, the default). Only ``buy`` is purchase
    evidence for ``_already_bought``. ``probe=True`` is the older spelling of ``kind="probe"``.
    """
    kind = kind or ("probe" if probe else "buy")
    if kind not in ("probe", "claim", "buy"):
        raise ValueError(f"unknown routing row kind {kind!r}")
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
        "probe": kind == "probe",
        "kind": kind,
    }
    with open(_log_file(directory, trading_day), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return decision


def confirm_buy(*, log_dir: Optional[Path] = None, trading_day: str, slot: str, now: datetime) -> Decision:
    """Record the fallback buy as CONFIRMED, once the first order has been accepted by the broker."""
    decision = Decision(
        ExecutorRole.PERPLEXITY, LocalStatus.UNKNOWN, True, PATH_FALLBACK, "first fallback order accepted", now.isoformat()
    )
    return record(decision, log_dir=log_dir, trading_day=trading_day, slot=slot, kind="buy")


class _GuardedBroker:
    """A broker whose every order-placing method checks the routing decision first.

    The repo's brokers place through ``place_market_buy`` and ``place_whole_share_buy``
    (``near_close_dca.BrokerAdapter``, ``dca_live_adapters.RobinhoodBroker``); ``place_order`` is kept for
    callers written against the original contract. Anything else (reads, account lookups) is forwarded
    untouched through ``__getattr__``, so a calculation-only role can still read but cannot reach an order.
    """

    def __init__(self, broker: Any, decision: Decision) -> None:
        self._broker = broker
        self._decision = decision

    def _checked(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self._decision.may_place:
            raise OrderPlacementForbidden(self._decision.reason)
        return getattr(self._broker, method)(*args, **kwargs)

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        return self._checked("place_order", *args, **kwargs)

    def place_market_buy(self, *args: Any, **kwargs: Any) -> Any:
        return self._checked("place_market_buy", *args, **kwargs)

    def place_whole_share_buy(self, *args: Any, **kwargs: Any) -> Any:
        return self._checked("place_whole_share_buy", *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._broker, name)


def guard_broker(broker: Any, role: ExecutorRole, local_status: LocalStatus, *, now: datetime, **kwargs: Any) -> Any:
    """Wrap ``broker`` so ``place_order`` raises unless the routing decision permits it."""
    return _GuardedBroker(broker, decide(role, local_status, now=now, **kwargs))
