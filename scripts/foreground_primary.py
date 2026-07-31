#!/usr/bin/env python3
"""Foreground primary surrogate context detection for the close-of-day validators.

JUL 30 2026 FOREGROUND PRIMARY SURROGATE CONTEXT PATCH. On a day the operator
drives execution in the foreground (a manual/foreground recovery standing in for
the background scheduled stream), the 3:35 post-trade validator and the 3:40
watchdog would previously escalate "no valid scheduled run_state" even though
the day executed exactly as the operator intended. That alert is noise, and
noise trains an operator to ignore the alert that matters.

This module is the shared gate both validators use to tell that day apart. It
detects an INTENTIONAL foreground primary mode from markers persisted on the
day's own artifacts, and it decides whether a missing-scheduled-evidence finding
should still escalate.

Two rules keep the relaxation narrow:

  * Evidence separation is absolute. A foreground artifact is execution CONTEXT.
    It is never scheduled evidence. Every context this module returns reports
    ``valid_scheduled_evidence=false``, ``valid_afternoon_scheduled_evidence=false``
    and ``top_up_context_only=true``, and there is no code path that flips them.
    Foreground primary mode explains why the background stream produced nothing.
    It never claims the background stream ran.
  * Only the missing-scheduled-evidence finding is suppressed. Safety failures,
    broker/order mismatches, missing required foreground artifacts, stale
    same-day pointers, invalid schema, and drift (a foreground signal claiming an
    order that no broker or artifact record backs) escalate exactly as before.
    See :func:`evaluate_scheduled_evidence_alert`.

Standard library only. Read-only: this module reads artifacts, decides, and
returns. It never writes, never touches a broker, and never places, reviews,
cancels, sells, or modifies an order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# The marker string an artifact may carry verbatim (as a bare string in a list
# such as `markers`, or as a string field value) to declare foreground primary
# mode. Also the name of the boolean field spelling of the same declaration.
FOREGROUND_PRIMARY_MARKER = "foreground_primary_surrogate_context"

# run_origin values that declare foreground primary mode on their own.
FOREGROUND_PRIMARY_RUN_ORIGINS: Tuple[str, ...] = (
    "foreground_primary_surrogate",
    "manual_foreground_primary",
)

# The boolean field spelling used by an artifact that asserts it was the day's
# primary executor.
PRIMARY_EXECUTOR_EVIDENCE_FIELD = "valid_primary_executor_evidence"

# Nested blocks searched for markers in addition to the artifact's top level. A
# run_state persists its origin under run_classification; a run_summary and a
# manifest entry vary, so the common containers are all covered.
_NESTED_MARKER_BLOCKS = (
    "run_classification",
    "classification",
    "foreground_primary",
    "foreground_primary_context",
    "run_summary",
    "summary",
)

# Reason codes emitted on a detection result. Stable strings so a consumer can
# branch on them without parsing prose.
REASON_NO_ARTIFACTS = "no_foreground_artifacts_supplied"
REASON_UNREADABLE = "artifact_unreadable"
REASON_NOT_A_MAPPING = "artifact_is_not_a_json_object"
REASON_NO_MARKER = "artifact_carries_no_foreground_primary_marker"
REASON_WRONG_DAY = "artifact_trading_day_is_not_today"
REASON_MISSING_TRADING_DAY = "artifact_declares_no_trading_day"


@dataclass(frozen=True)
class ForegroundMarkerHit:
    """One marker found on one artifact."""

    source: str
    marker: str
    field_path: str


@dataclass
class ForegroundPrimaryContext:
    """Whether the trading day is running in intentional foreground primary mode.

    ``active`` is true only when at least one same-day artifact carries a
    recognised marker. The three evidence-separation fields are constants, not
    derivations: they are carried on the result so a consumer that serialises it
    into a report cannot accidentally publish a foreground artifact as scheduled
    evidence.
    """

    active: bool
    trading_day: Optional[str] = None
    artifact_paths: List[str] = field(default_factory=list)
    markers: List[ForegroundMarkerHit] = field(default_factory=list)
    rejected: List[Tuple[str, str]] = field(default_factory=list)

    # Evidence separation, invariant on every result this module can produce.
    valid_scheduled_evidence: bool = False
    valid_afternoon_scheduled_evidence: bool = False
    top_up_context_only: bool = True

    def __bool__(self) -> bool:
        return self.active

    def to_report(self) -> Dict[str, Any]:
        """Serialise for a validator report artifact."""
        return {
            "foreground_primary_mode_active": self.active,
            "trading_day": self.trading_day,
            "foreground_artifact_paths": list(self.artifact_paths),
            "markers_found": [
                {"source": hit.source, "marker": hit.marker, "field_path": hit.field_path}
                for hit in self.markers
            ],
            "rejected_artifacts": [
                {"source": source, "reason": reason} for source, reason in self.rejected
            ],
            "classification": FOREGROUND_PRIMARY_MARKER,
            # Repeated verbatim in the artifact so a downstream reader of the
            # report, not just a caller of this module, sees the separation.
            "valid_scheduled_evidence": self.valid_scheduled_evidence,
            "valid_afternoon_scheduled_evidence": self.valid_afternoon_scheduled_evidence,
            "top_up_context_only": self.top_up_context_only,
            "evidence_note": (
                "Foreground primary artifacts are execution context for "
                "user-approved foreground primary operation. They are not valid "
                "scheduled evidence for the background stream."
            ),
        }


def _load_artifact(artifact: str | Path | Mapping[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(source_label, payload, rejection_reason)`` for one artifact."""
    if isinstance(artifact, Mapping):
        return ("<inline>", dict(artifact), None)
    path = Path(artifact)
    if not path.exists():
        return (str(path), None, REASON_UNREADABLE)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return (str(path), None, REASON_UNREADABLE)
    if not isinstance(loaded, dict):
        return (str(path), None, REASON_NOT_A_MAPPING)
    return (str(path), loaded, None)


def _marker_hits_in_block(block: Mapping[str, Any], source: str, prefix: str) -> List[ForegroundMarkerHit]:
    """Find every recognised marker in a single (non-recursive) mapping."""
    hits: List[ForegroundMarkerHit] = []

    # Boolean spelling: foreground_primary_surrogate_context=true. `is True`,
    # not truthiness: the string "false" and the integer 0 are both schema-
    # invalid here, and a loose check would read "false" as a declaration.
    if block.get(FOREGROUND_PRIMARY_MARKER) is True:
        hits.append(
            ForegroundMarkerHit(source, FOREGROUND_PRIMARY_MARKER, f"{prefix}{FOREGROUND_PRIMARY_MARKER}")
        )

    if block.get(PRIMARY_EXECUTOR_EVIDENCE_FIELD) is True:
        hits.append(
            ForegroundMarkerHit(
                source, PRIMARY_EXECUTOR_EVIDENCE_FIELD, f"{prefix}{PRIMARY_EXECUTOR_EVIDENCE_FIELD}"
            )
        )

    origin = block.get("run_origin")
    if isinstance(origin, str) and origin in FOREGROUND_PRIMARY_RUN_ORIGINS:
        hits.append(ForegroundMarkerHit(source, origin, f"{prefix}run_origin"))

    # Bare marker string, either as a scalar field value or as an entry in a
    # list of markers/tags. Exact equality only: a marker mentioned inside a
    # prose note is documentation, not a declaration, and substring matching
    # would let a note that says "we did NOT use
    # foreground_primary_surrogate_context" switch the mode on.
    for key, value in block.items():
        if isinstance(value, str) and value == FOREGROUND_PRIMARY_MARKER:
            hits.append(ForegroundMarkerHit(source, FOREGROUND_PRIMARY_MARKER, f"{prefix}{key}"))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, str) and item == FOREGROUND_PRIMARY_MARKER:
                    hits.append(
                        ForegroundMarkerHit(source, FOREGROUND_PRIMARY_MARKER, f"{prefix}{key}[{index}]")
                    )
    return hits


def find_foreground_primary_markers(payload: Mapping[str, Any], source: str = "<inline>") -> List[ForegroundMarkerHit]:
    """Return every recognised foreground primary marker in one artifact payload.

    Searches the top level plus the nested blocks in
    :data:`_NESTED_MARKER_BLOCKS`. Deliberately not a full recursive walk: an
    unbounded search would pick a marker out of an arbitrarily deep
    provenance/debug blob that was never meant to declare the day's mode.
    """
    hits = _marker_hits_in_block(payload, source, "")
    for block_name in _NESTED_MARKER_BLOCKS:
        block = payload.get(block_name)
        if isinstance(block, Mapping):
            hits.extend(_marker_hits_in_block(block, source, f"{block_name}."))
    return hits


def _artifact_trading_day(payload: Mapping[str, Any]) -> Optional[str]:
    day = payload.get("trading_day")
    if isinstance(day, str) and day.strip():
        return day.strip()
    for block_name in _NESTED_MARKER_BLOCKS:
        block = payload.get(block_name)
        if isinstance(block, Mapping):
            nested = block.get("trading_day")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def detect_foreground_primary_context(
    artifacts: Optional[Iterable[str | Path | Mapping[str, Any]]],
    *,
    trading_day: str,
) -> ForegroundPrimaryContext:
    """Detect intentional foreground primary mode for ``trading_day``.

    ``artifacts`` are the day's candidate foreground records: same-day
    ``run_state.json`` / ``run_summary.json`` paths, latest-pointer paths, or
    already-loaded manifest entries. Each is accepted only when it BOTH carries a
    recognised marker AND declares ``trading_day`` equal to the day asked about.

    The same-day requirement is the load-bearing half. Without it, yesterday's
    foreground recovery would silence today's missing-scheduled-evidence alert
    forever, which is the exact failure the same-day pointer contract exists to
    prevent (see scripts/same_day_pointer.py). An artifact that carries a marker
    but declares no trading day is rejected for the same reason: it cannot
    establish that it belongs to today.

    Never raises. An unreadable or malformed artifact is recorded in
    ``rejected`` and the scan continues, so one corrupt file cannot take the
    validator down.
    """
    context = ForegroundPrimaryContext(active=False, trading_day=trading_day)
    if not artifacts:
        context.rejected.append(("<none>", REASON_NO_ARTIFACTS))
        return context

    for artifact in artifacts:
        source, payload, rejection = _load_artifact(artifact)
        if payload is None:
            context.rejected.append((source, rejection or REASON_UNREADABLE))
            continue
        hits = find_foreground_primary_markers(payload, source)
        if not hits:
            context.rejected.append((source, REASON_NO_MARKER))
            continue
        artifact_day = _artifact_trading_day(payload)
        if artifact_day is None:
            context.rejected.append((source, REASON_MISSING_TRADING_DAY))
            continue
        if artifact_day != trading_day:
            context.rejected.append((source, REASON_WRONG_DAY))
            continue
        context.active = True
        context.artifact_paths.append(source)
        context.markers.extend(hits)

    return context


# Finding codes a validator passes as `blocking_findings`. Listed so the two
# validators name the same real failure the same way.
FINDING_SAFETY_FAILURE = "safety_failure"
FINDING_BROKER_MISMATCH = "broker_order_mismatch"
FINDING_MISSING_FOREGROUND_ARTIFACT = "missing_required_foreground_artifact"
FINDING_STALE_SAME_DAY_POINTER = "stale_same_day_pointer"
FINDING_INVALID_SCHEMA = "invalid_schema"
FINDING_FOREGROUND_DRIFT = "foreground_signal_without_broker_or_artifact_record"

BLOCKING_FINDING_CODES: Tuple[str, ...] = (
    FINDING_SAFETY_FAILURE,
    FINDING_BROKER_MISMATCH,
    FINDING_MISSING_FOREGROUND_ARTIFACT,
    FINDING_STALE_SAME_DAY_POINTER,
    FINDING_INVALID_SCHEMA,
    FINDING_FOREGROUND_DRIFT,
)


@dataclass
class ScheduledEvidenceAlert:
    """Whether a close-of-day validator should escalate, and why."""

    escalate: bool
    foreground_primary_mode_active: bool
    escalation_reasons: List[str] = field(default_factory=list)
    suppressed_reasons: List[str] = field(default_factory=list)
    foreground_artifact_paths: List[str] = field(default_factory=list)

    def to_report(self) -> Dict[str, Any]:
        return {
            "escalate": self.escalate,
            "foreground_primary_mode_active": self.foreground_primary_mode_active,
            "escalation_reasons": list(self.escalation_reasons),
            "suppressed_reasons": list(self.suppressed_reasons),
            "foreground_artifact_paths": list(self.foreground_artifact_paths),
        }


def evaluate_scheduled_evidence_alert(
    *,
    scheduled_evidence_ok: bool,
    scheduled_evidence_reasons: Sequence[str] = (),
    foreground: Optional[ForegroundPrimaryContext] = None,
    blocking_findings: Sequence[str] = (),
) -> ScheduledEvidenceAlert:
    """Decide whether a missing/invalid background scheduled evidence finding escalates.

    ``blocking_findings`` are the real failures the caller already detected (see
    :data:`BLOCKING_FINDING_CODES`). They ALWAYS escalate: foreground primary
    mode explains an absent background run, it does not excuse a safety failure,
    a broker mismatch, a stale pointer, an invalid schema, or drift between a
    foreground signal and the broker record.

    Only the scheduled-evidence finding is suppressible, and only when
    ``foreground`` is active. When scheduled evidence is fine there is nothing to
    suppress and nothing to escalate on this axis.
    """
    active = bool(foreground and foreground.active)
    alert = ScheduledEvidenceAlert(
        escalate=False,
        foreground_primary_mode_active=active,
        foreground_artifact_paths=list(foreground.artifact_paths) if foreground else [],
    )

    alert.escalation_reasons.extend(str(finding) for finding in blocking_findings)

    if not scheduled_evidence_ok:
        reasons = [str(reason) for reason in scheduled_evidence_reasons] or [
            "background scheduled evidence is absent, incomplete, or "
            "valid_scheduled_evidence=false"
        ]
        if active:
            alert.suppressed_reasons.extend(reasons)
        else:
            alert.escalation_reasons.extend(reasons)

    alert.escalate = bool(alert.escalation_reasons)
    return alert


__all__ = [
    "FOREGROUND_PRIMARY_MARKER",
    "FOREGROUND_PRIMARY_RUN_ORIGINS",
    "PRIMARY_EXECUTOR_EVIDENCE_FIELD",
    "REASON_NO_ARTIFACTS",
    "REASON_UNREADABLE",
    "REASON_NOT_A_MAPPING",
    "REASON_NO_MARKER",
    "REASON_WRONG_DAY",
    "REASON_MISSING_TRADING_DAY",
    "ForegroundMarkerHit",
    "ForegroundPrimaryContext",
    "find_foreground_primary_markers",
    "detect_foreground_primary_context",
    "FINDING_SAFETY_FAILURE",
    "FINDING_BROKER_MISMATCH",
    "FINDING_MISSING_FOREGROUND_ARTIFACT",
    "FINDING_STALE_SAME_DAY_POINTER",
    "FINDING_INVALID_SCHEMA",
    "FINDING_FOREGROUND_DRIFT",
    "BLOCKING_FINDING_CODES",
    "ScheduledEvidenceAlert",
    "evaluate_scheduled_evidence_alert",
]
