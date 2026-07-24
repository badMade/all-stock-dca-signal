#!/usr/bin/env python3
"""Incident correlation IDs carried across the DCA scheduler/runner/broker/recovery.

The plan asks for one stable identifier that ties together everything a single
logical window touches -- the scheduler firing, the runner, the broker calls, the
artifacts, and any recovery activity -- so an incident can be traced end to end.

The primary constructor is deterministic: the scheduler and the runner can each
derive the SAME correlation id from ``(trading_day, stream, window)`` without
having to pass it through a side channel. Recovery activity derives a *child* id
from the scheduled parent so a recovery is always attributable to the window it
recovers, and never mistaken for the scheduled run itself.

Standard library only. No trading logic, no network, no secrets.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

_PREFIX = "dca"
#: Characters allowed in a normalized id segment; everything else becomes ``-``.
_SEGMENT_UNSAFE = re.compile(r"[^A-Za-z0-9]+")


def _segment(value: str) -> str:
    """Normalize one id segment: lowercase, non-alphanumerics collapsed to ``-``."""
    cleaned = _SEGMENT_UNSAFE.sub("-", str(value).strip().lower()).strip("-")
    return cleaned or "na"


def build_correlation_id(trading_day: str, stream: str, window: str) -> str:
    """Deterministic correlation id for a scheduled window.

    ``dca-{trading_day}-{stream}-{window}`` after per-segment normalization, e.g.
    ``build_correlation_id("2026-07-23", "midday", "1230ET")`` ->
    ``dca-2026-07-23-midday-1230et``. Deterministic so independent processes
    reconstruct the same id for the same window.
    """
    return "-".join([_PREFIX, _segment(trading_day), _segment(stream), _segment(window)])


def child_correlation_id(parent: str, kind: str) -> str:
    """Correlation id for recovery/reconciliation activity under a scheduled parent.

    The child keeps the parent id and appends a ``/kind`` suffix (e.g.
    ``dca-2026-07-23-midday-1230et/recovery``) so it is unambiguously linked to,
    but distinct from, the scheduled run -- a recovery id can never be counted as
    the scheduled run's own evidence.
    """
    if not parent:
        raise ValueError("parent correlation id is required")
    return f"{parent}/{_segment(kind)}"


def new_correlation_id(stream: str = "adhoc") -> str:
    """Random correlation id for activity with no deterministic window key.

    Used when a run is not tied to a specific scheduled window (ad-hoc manual
    activity). Format: ``dca-{stream}-{uuid4hex12}``.
    """
    return "-".join([_PREFIX, _segment(stream), uuid.uuid4().hex[:12]])


def parent_of(correlation_id: str) -> Optional[str]:
    """Return the scheduled-parent id for a child correlation id, else None."""
    if not correlation_id or "/" not in correlation_id:
        return None
    return correlation_id.split("/", 1)[0]


def is_child(correlation_id: str) -> bool:
    """True when ``correlation_id`` is a recovery/reconciliation child id."""
    return bool(correlation_id) and "/" in correlation_id


__all__ = [
    "build_correlation_id",
    "child_correlation_id",
    "new_correlation_id",
    "parent_of",
    "is_child",
]
