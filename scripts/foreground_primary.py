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
REASON_POINTER_MALFORMED = "pointer_malformed_or_missing_required_keys"
REASON_POINTER_STALE = "pointer_stale_or_not_declared_valid"
REASON_POINTER_OUTSIDE_ROOT = "pointer_target_outside_expected_root"
REASON_POINTER_MISSING_TARGET = "pointer_target_run_state_missing_or_unreadable"
REASON_POINTER_UNKNOWN_STREAM = "pointer_stream_not_in_expected_roots"

# pointer_status values a same-day latest-pointer artifact may declare and
# still be trusted (mirrors scripts/same_day_pointer.py's STATUS_VALID and
# STATUS_VALID_RECOVERY_CONTEXT). A foreground/recovery-context pointer must
# not claim scheduled evidence, so it is schema-valid at "valid_recovery_context"
# rather than "valid"; rejecting it here made a marker carried by one of the
# documented latest-pointer sources unable to activate foreground mode (Codex
# review: "Allow a valid foreground-context pointer status").
_POINTER_ACCEPTED_STATUSES = ("valid", "valid_recovery_context")

# Keys that mark a payload as a same-day latest-pointer artifact (see
# scripts/same_day_pointer.py) rather than a run_state/run_summary itself. A
# pointer is a thin indirection to a run_state, so it needs its own structural
# validation before its marker/trading_day fields are trusted.
_POINTER_REQUIRED_KEYS = ("run_id", "run_state_path", "trading_day", "stream")

# The single field that distinguishes a pointer-shaped payload from a
# run_state/run_summary. Neither of those carries run_state_path -- a pointer
# names the run_state it targets, a run_state IS the target.
_POINTER_SHAPE_KEY = "run_state_path"


def _looks_like_pointer(payload: Mapping[str, Any]) -> bool:
    """True when ``payload`` is shaped like a same-day latest-pointer artifact.

    Deliberately keyed on presence of :data:`_POINTER_SHAPE_KEY` alone, NOT on
    every key in :data:`_POINTER_REQUIRED_KEYS` being present. A pointer
    missing one of the other required keys (e.g. no ``stream``) is exactly the
    malformed case :func:`_validate_pointer_artifact` must reject -- requiring
    the full key set here made that check unreachable for precisely the
    malformed pointers it exists to catch, so a same-day marker on an
    incomplete pointer still activated foreground mode (Codex review:
    "Validate malformed pointer candidates before activation").
    """
    return _POINTER_SHAPE_KEY in payload


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
    # The raw artifact objects (paths/mappings) accepted into this result, in
    # the same order as artifact_paths -- NOT serialised by to_report(). A
    # source label in artifact_paths is "<inline>" for every mapping artifact,
    # so it cannot by itself identify which caller-supplied object was
    # accepted; a consumer that needs to act only on accepted artifacts (e.g.
    # reconcile_foreground_orders, which must not scan a rejected prior-day or
    # unmarked candidate) uses this field instead (Codex review: "Reconcile
    # only accepted foreground artifacts").
    accepted_artifacts: List[Any] = field(default_factory=list)

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
        loaded = json.loads(path.read_bytes())
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


def _validate_pointer_artifact(
    payload: Mapping[str, Any],
    trading_day: str,
    expected_root: Optional[Path],
) -> Optional[str]:
    """Structurally validate a same-day latest-pointer artifact.

    Mirrors the load-bearing checks of ``same_day_pointer.validate_pointer``
    (required keys, a declared ``pointer_status`` outside
    :data:`_POINTER_ACCEPTED_STATUSES`, root containment when ``expected_root`` is known, and a
    same-day target that actually exists and is readable) without importing
    that module -- this module stays standard-library-only by contract.

    Returns a rejection reason, or ``None`` when the pointer and its target
    may be trusted. A pointer whose own ``trading_day``/marker fields look
    right must still fail here when it is stale, malformed, escapes its
    expected root, or targets a missing run_state -- those are explicitly
    meant to stay blocking, not silently activate foreground primary mode
    (Codex review: "Reject invalid pointers before activating foreground
    mode").
    """
    missing_keys = [key for key in _POINTER_REQUIRED_KEYS if key not in payload]
    if missing_keys:
        return REASON_POINTER_MALFORMED

    declared_status = payload.get("pointer_status")
    if declared_status is not None and declared_status not in _POINTER_ACCEPTED_STATUSES:
        return REASON_POINTER_STALE

    raw_path = str(payload.get("run_state_path", "")).strip()
    if not raw_path:
        return REASON_POINTER_MALFORMED
    target_path = Path(raw_path)

    if expected_root is not None:
        try:
            target_path.resolve().relative_to(expected_root.resolve())
        except (ValueError, OSError):
            return REASON_POINTER_OUTSIDE_ROOT

    if not target_path.exists():
        return REASON_POINTER_MISSING_TARGET
    try:
        target_payload = json.loads(target_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return REASON_POINTER_MISSING_TARGET
    if not isinstance(target_payload, dict):
        return REASON_POINTER_MISSING_TARGET

    target_day = str(target_payload.get("trading_day", "")).strip()
    if target_day != trading_day:
        return REASON_POINTER_STALE

    return None


def detect_foreground_primary_context(
    artifacts: Optional[Iterable[str | Path | Mapping[str, Any]]],
    *,
    trading_day: str,
    expected_roots: Optional[Mapping[str, str | Path]] = None,
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

    A latest-pointer artifact (one carrying the same_day_pointer required keys
    -- ``run_id``, ``run_state_path``, ``trading_day``, ``stream``) gets an
    additional structural pass before its marker/day fields are trusted: it is
    rejected when it declares a ``pointer_status`` outside
    :data:`_POINTER_ACCEPTED_STATUSES`, when its target run_state is missing or
    unreadable, when the target's own ``trading_day`` does not match, when
    ``expected_roots`` is supplied but the pointer's declared ``stream`` is not
    one of its keys, or -- when ``expected_roots`` supplies a root for the
    pointer's declared ``stream`` -- when ``run_state_path`` resolves outside
    that root. See :func:`_validate_pointer_artifact`. Without this a stale,
    malformed, unmapped-stream, or out-of-root pointer that merely carries
    today's date and a marker field would activate foreground primary mode and
    suppress a missing-scheduled-evidence alert that is explicitly meant to keep
    escalating.

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
        if _looks_like_pointer(payload):
            stream = payload.get("stream")
            if expected_roots is not None:
                # When a caller supplies expected_roots at all, every permitted
                # stream must resolve to a validated root. Falling through to
                # expected_root=None for a stream absent from the map (a typo
                # or a malformed/unknown stream) would silently disable the
                # containment check instead of rejecting the pointer, letting
                # it target any readable same-day JSON file (Codex review:
                # "Reject pointers whose stream has no expected root").
                if not isinstance(stream, str) or stream not in expected_roots:
                    context.rejected.append((source, REASON_POINTER_UNKNOWN_STREAM))
                    continue
                expected_root = Path(expected_roots[stream])
            else:
                expected_root = None
            pointer_rejection = _validate_pointer_artifact(payload, trading_day, expected_root)
            if pointer_rejection is not None:
                context.rejected.append((source, pointer_rejection))
                continue
        context.active = True
        context.artifact_paths.append(source)
        context.accepted_artifacts.append(artifact)
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


def _order_ids(fills: Sequence[Mapping[str, Any]]) -> set:
    """Order IDs a broker fill/order record identifies itself by."""
    ids = set()
    for fill in fills:
        if not isinstance(fill, Mapping):
            continue
        oid = fill.get("order_id") or fill.get("broker_order_id")
        if oid:
            ids.add(str(oid))
    return ids


def _candidate_source_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the payload whose ``buy_candidates`` should be reconciled.

    A pointer's own JSON never carries ``buy_candidates`` -- those live on the
    run_state it targets, not on the thin pointer wrapper. Dereferencing here
    means reconciliation checks the orders the pointer actually names instead
    of silently finding nothing on every pointer-shaped artifact (Codex
    review: "Reconcile the run state targeted by foreground pointers"). Falls
    back to the pointer payload itself (which has no buy_candidates, so
    contributes no drift finding) when the target is missing or unreadable --
    :func:`detect_foreground_primary_context`'s own pointer validation is what
    rejects that case; this helper stays best-effort.
    """
    if not _looks_like_pointer(payload):
        return payload
    raw_path = str(payload.get("run_state_path", "")).strip()
    if not raw_path:
        return payload
    try:
        target = json.loads(Path(raw_path).read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return payload
    return target if isinstance(target, dict) else payload


def reconcile_foreground_orders(
    artifacts: Optional[Iterable[str | Path | Mapping[str, Any]]],
    fills: Sequence[Mapping[str, Any]] = (),
) -> List[str]:
    """Reconcile foreground artifact order claims against broker fill data.

    A foreground artifact's ``buy_candidates`` can carry an ``order_id`` that
    claims a broker order was placed on the foreground path. That claim is
    trusted only when the same order ID also appears in the caller-supplied
    broker ``fills``/orders; a same-day artifact must not be allowed to assert
    an order was placed while the broker record disagrees. A pointer-shaped
    artifact is dereferenced to its target run_state first (see
    :func:`_candidate_source_payload`) since the pointer itself never carries
    ``buy_candidates``.

    Returns ``[FINDING_FOREGROUND_DRIFT]`` when at least one foreground
    artifact claims an order ID with no matching broker record, and an empty
    list when every claimed order is backed by a broker fill (including when
    there is nothing to reconcile).
    """
    if not artifacts:
        return []
    fill_ids = _order_ids(fills)
    for artifact in artifacts:
        _, payload, _rejection = _load_artifact(artifact)
        if payload is None:
            continue
        candidates = _candidate_source_payload(payload).get("buy_candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            order_id = candidate.get("order_id")
            if order_id is None or not str(order_id).strip():
                continue
            if str(order_id) not in fill_ids:
                return [FINDING_FOREGROUND_DRIFT]
    return []


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
    "REASON_POINTER_MALFORMED",
    "REASON_POINTER_STALE",
    "REASON_POINTER_OUTSIDE_ROOT",
    "REASON_POINTER_MISSING_TARGET",
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
    "reconcile_foreground_orders",
    "ScheduledEvidenceAlert",
    "evaluate_scheduled_evidence_alert",
]
