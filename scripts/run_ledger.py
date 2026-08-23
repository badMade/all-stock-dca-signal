#!/usr/bin/env python3
"""Chronological run ledger for the daily DCA streams.

The plan calls for ONE chronological ledger showing scheduled runs, recovery
reruns, order attempts, fills, and artifact publication -- a single append-only
record an operator (or the reliability dashboard) can read top-to-bottom to see
what actually happened across a trading day, instead of reconstructing it from
scattered per-run ``run_state.json`` files.

The ledger is an append-only JSONL file. Each line is one immutable event with a
monotonic sequence number, a timestamp, the incident ``correlation_id`` (see
:mod:`scripts.correlation`), and a typed payload. Events are never mutated or
deleted; a correction is a new event that supersedes an earlier one.

Standard library only. No trading logic, no network, no secrets. Timestamps may
be injected for deterministic tests.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Generator, List, Optional, TypeGuard

from scripts.file_lock import cross_process_lock

# --- Event types --------------------------------------------------------------

SCHEDULED_RUN = "scheduled_run"
RECOVERY_RERUN = "recovery_rerun"
STATE_TRANSITION = "state_transition"
ORDER_ATTEMPT = "order_attempt"
FILL = "fill"
ARTIFACT_PUBLISHED = "artifact_published"
RECONCILIATION = "reconciliation"
BLOCKED_DECISION = "blocked_decision"

EVENT_TYPES: FrozenSet[str] = frozenset(
    {
        SCHEDULED_RUN,
        RECOVERY_RERUN,
        STATE_TRANSITION,
        ORDER_ATTEMPT,
        FILL,
        ARTIFACT_PUBLISHED,
        RECONCILIATION,
        BLOCKED_DECISION,
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_seq(value: Any) -> TypeGuard[int]:
    """True only for a real integer ``seq`` -- ``bool`` is an ``int`` subclass and
    must be rejected so ``{"seq": true}`` cannot be read as sequence ``1``."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass
class LedgerEvent:
    """One immutable ledger event."""

    event_type: str
    trading_day: str
    stream: str
    correlation_id: str
    run_id: Optional[str] = None
    window: Optional[str] = None
    state: Optional[str] = None
    reason: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    seq: Optional[int] = None
    ts: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "seq": self.seq,
            "ts": self.ts,
            "event_type": self.event_type,
            "trading_day": self.trading_day,
            "stream": self.stream,
            "window": self.window,
            "correlation_id": self.correlation_id,
            "run_id": self.run_id,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
        }
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LedgerEvent":
        return cls(
            event_type=data.get("event_type", ""),
            trading_day=data.get("trading_day", ""),
            stream=data.get("stream", ""),
            correlation_id=data.get("correlation_id", ""),
            run_id=data.get("run_id"),
            window=data.get("window"),
            state=data.get("state"),
            reason=data.get("reason"),
            detail=data.get("detail") or {},
            seq=data.get("seq"),
            ts=data.get("ts"),
        )


class RunLedger:
    """Append-only JSONL ledger reader/writer.

    ``append`` assigns a monotonic ``seq`` (one past the current max in the file)
    and an ISO-8601 UTC ``ts`` when not supplied, then writes exactly one JSON
    line. Existing lines are never rewritten, so the file is safe to tail. A
    malformed pre-existing line is skipped when computing the next sequence rather
    than aborting the append.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _existing_events(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        events: List[Dict[str, Any]] = []
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        events.append(parsed)
        except OSError:
            # The ledger file EXISTS but could not be read (e.g. a write-only
            # permission config, or a transient I/O error). Returning [] here would
            # let _next_seq allocate seq 1 and append a DUPLICATE onto a populated
            # audit ledger. Fail closed: propagate so the append is refused.
            raise
        return events

    def _tail_seq_max(self) -> "tuple[Optional[int], bool]":
        """Return ``(max_seq_in_tail, tail_covered_whole_file)``.

        Reads a bounded chunk from EOF and returns the MAXIMUM integer ``seq`` over
        every complete line in it -- not merely the last line's. Appends are
        monotonic so the last line normally carries the max, but a corrupt or
        out-of-order final record could hold a LOWER seq; taking the max of the
        tail is robust to that and would not move the sequence backward. The bool
        reports whether the chunk spanned the entire file: when it did, the max is
        authoritative and the caller needs no full scan; when the file is larger
        than the chunk, the caller full-scans so a max hiding earlier in the file
        is not missed. Reading a bounded chunk keeps the common (small daily
        ledger) case O(tail) instead of O(M^2) across M appends.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return None, False
        if size == 0:
            return None, True
        read_bytes = min(size, 65536)
        covers_whole = read_bytes == size
        try:
            with self.path.open("rb") as handle:
                handle.seek(size - read_bytes)
                data = handle.read(read_bytes)
        except OSError:
            return None, False
        max_seq: Optional[int] = None
        for line in data.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                # A truncated line at the chunk boundary or a crash-partial write.
                continue
            if isinstance(parsed, dict) and _is_seq(parsed.get("seq")):
                seq = parsed["seq"]
                if max_seq is None or seq > max_seq:
                    max_seq = seq
        return max_seq, covers_whole

    def _next_seq(self) -> int:
        tail_max, covers_whole = self._tail_seq_max()
        if covers_whole:
            # The tail spanned the whole file, so its max is the file's max.
            return (tail_max or 0) + 1
        # File larger than the tail chunk: full scan so a higher seq that lives
        # before the chunk (or a corrupt lower-seq tail) cannot move seq backward.
        max_seq: int = tail_max if tail_max is not None else 0
        for event in self._existing_events():
            seq = event.get("seq")
            if _is_seq(seq) and seq > max_seq:
                max_seq = seq
        return max_seq + 1

    @contextmanager
    def _seq_lock(self) -> Generator[None, None, None]:
        """Serialize sequence allocation + append across processes.

        The ledger is shared across every window, while scheduler leases only
        guard a single window, so overlapping runs could otherwise each read the
        same ``_next_seq()`` and write duplicate sequence numbers. This critical
        section (read seq, then append) is made mutually exclusive by a sidecar
        ``.lock`` file via :func:`scripts.file_lock.cross_process_lock` -- POSIX
        ``fcntl.flock`` where available, a portable ``O_EXCL`` + stale-reclaim lock
        elsewhere -- so it holds on non-POSIX hosts too, not just under fcntl.
        """
        lock_path = self.path.with_name(self.path.name + ".lock")
        with cross_process_lock(lock_path):
            yield

    def append(self, event: LedgerEvent, *, ts: Optional[str] = None) -> LedgerEvent:
        """Append one event, assigning ``seq`` and ``ts`` when absent.

        Sequence allocation and the append are performed under a cross-process
        lock (:meth:`_seq_lock`) so concurrent writers cannot assign duplicate
        sequence numbers.
        """
        if event.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown ledger event_type {event.event_type!r}")
        if not (event.correlation_id and event.correlation_id.strip()):
            # Every ledger event must carry the incident correlation id; without it
            # scheduled runs, fills, and recoveries cannot be joined by
            # filter(correlation_id=...), defeating the ledger's tracing purpose.
            raise ValueError("ledger event requires a non-empty correlation_id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._seq_lock():
            event.seq = self._next_seq()
            event.ts = ts or event.ts or _utc_now_iso()
            record = json.dumps(event.to_dict(), sort_keys=True) + "\n"
            with self.path.open("a", encoding="utf-8") as handle:
                # Terminate a crash-truncated final line (no trailing newline)
                # before appending, so the new record stays a separate, valid
                # JSONL line instead of merging into the fragment.
                if self._needs_leading_newline():
                    handle.write("\n")
                handle.write(record)
        return event

    def _needs_leading_newline(self) -> bool:
        """True when the file is non-empty and its last byte is not a newline."""
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return False
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except OSError:
            return False

    def read_all(self) -> List[LedgerEvent]:
        """Return every event in file order."""
        return [LedgerEvent.from_dict(d) for d in self._existing_events()]

    def filter(
        self,
        *,
        trading_day: Optional[str] = None,
        stream: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[LedgerEvent]:
        """Return events matching every provided field (None fields are ignored)."""
        out: List[LedgerEvent] = []
        for event in self.read_all():
            if trading_day is not None and event.trading_day != trading_day:
                continue
            if stream is not None and event.stream != stream:
                continue
            if correlation_id is not None and event.correlation_id != correlation_id:
                continue
            if event_type is not None and event.event_type != event_type:
                continue
            out.append(event)
        return out


__all__ = [
    "SCHEDULED_RUN",
    "RECOVERY_RERUN",
    "STATE_TRANSITION",
    "ORDER_ATTEMPT",
    "FILL",
    "ARTIFACT_PUBLISHED",
    "RECONCILIATION",
    "BLOCKED_DECISION",
    "EVENT_TYPES",
    "LedgerEvent",
    "RunLedger",
]
