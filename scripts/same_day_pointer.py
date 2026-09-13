#!/usr/bin/env python3
"""Same-day latest-pointer validation and refresh for the daily DCA streams.

A ``latest_*.json`` pointer (``latest_morning_run.json``,
``latest_midday_run.json``, ``latest_scheduled_run.json``, the monitor /
slippage / watchdog equivalents, ...) is a convenience shortcut to the most
recent run_state for a stream. The reliability problem it creates: a pointer
left over from a prior trading day, half-written, or aimed at the wrong stream
looks superficially valid and can be mistaken for evidence that *today's*
scheduled run happened. This module is the shared runtime gate that stops
that: a pointer is trusted as same-day scheduled evidence ONLY when it names
today's America/New_York trading day, resolves inside the expected stream
root, and points at a run_state that is itself a scheduled (non-manual) run.

Contract (see config/scheduled_tasks.json -> same_day_pointer_validation_policy
and schemas/latest_pointer.schema.json):

  * A latest pointer is valid same-day evidence only when its ``trading_day``
    and ``run_state_path`` match the current ET date and the expected stream
    directory.
  * Stale, malformed, missing, pending, prior-date, or outside-root pointers
    are ignored: they must not be used as scheduled evidence, fill evidence,
    top-up proof, slippage evidence, reliability evidence, or confirmation
    that a stream ran.
  * A date-specific same-day run_state file is preferred over the latest
    pointer when both are present.
  * A pending same-day placeholder has ``valid_scheduled_evidence=false``
    until the target run_state exists AND has ``run_origin='scheduled'`` and
    ``manual_run=false``.

Standard library only. No trading logic, no network, no secrets. This module
never raises into a caller doing safety-critical work: validation returns a
result object describing *why* a pointer was rejected rather than throwing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text
from dca_paths import cron_tracking_root

_EASTERN = ZoneInfo("America/New_York")

CRON_TRACKING_ROOT = cron_tracking_root()

# pointer_status values recorded on a refreshed pointer and returned by
# validate_pointer. Mirrors schemas/latest_pointer.schema.json.
STATUS_VALID = "valid"
STATUS_MISSING = "missing"
STATUS_MALFORMED = "malformed"
STATUS_STALE = "stale"
STATUS_PRIOR_DATE = "prior_date"
STATUS_FUTURE_DATE = "future_date"
STATUS_PENDING = "pending"
STATUS_OUTSIDE_ROOT = "outside_root"
STATUS_WRONG_STREAM = "wrong_stream"
# A resolvable pointer to an explicitly NON-scheduled recovery artifact (e.g.
# latest_midday_recovery_run.json for the Jul 27 2026 12:30 recovery pass). The
# target exists, is same-day, and sits inside its expected root -- but it must
# never be scheduled evidence, and STATUS_VALID is pinned by both this module
# and schemas/latest_pointer.schema.json to valid_scheduled_evidence=true. Such
# a pointer therefore had no representable state before this status existed:
# 'valid' was forbidden by the invariant and every other status means unusable,
# so schema-aware consumers could not discover the artifact at all (Codex
# review: "Define a usable contract for the recovery pointer").
STATUS_VALID_RECOVERY_CONTEXT = "valid_recovery_context"

# Recovery streams. A pointer on one of these is context only, never scheduled
# evidence for the slot it recovered.
# Mirrors run_classification.RECOVERY_STREAMS, including the slot-qualified
# intraday variants (the intraday task fires at 11:30/13:30/14:30, so its
# recovery stream names the slot -- e.g. intraday_1430_recovery).
RECOVERY_STREAMS = (
    "market_open_recovery",
    "morning_recovery",
    "midday_recovery",
    "intraday_recovery",
    "intraday_1130_recovery",
    "intraday_1330_recovery",
    "intraday_1430_recovery",
)

_STREAM_ALIAS_POINTER_FILENAMES = {
    "market_open": "latest_market_open_run.json",
    "morning": "latest_morning_run.json",
    "midday": "latest_midday_run.json",
    "intraday": "latest_intraday_run.json",
    "afternoon": "latest_scheduled_run.json",
}

# Statuses that never count as same-day SCHEDULED evidence. STATUS_VALID_RECOVERY_CONTEXT
# is listed because it is exactly that: usable, but not scheduled evidence. A
# scheduled consumer must keep rejecting it; a recovery-context consumer selects
# on it explicitly (see is_recovery_context_status).
REJECTED_STATUSES = (
    STATUS_MISSING,
    STATUS_MALFORMED,
    STATUS_STALE,
    STATUS_PRIOR_DATE,
    STATUS_FUTURE_DATE,
    STATUS_PENDING,
    STATUS_OUTSIDE_ROOT,
    STATUS_WRONG_STREAM,
    STATUS_VALID_RECOVERY_CONTEXT,
)


def is_recovery_stream(stream: Optional[str]) -> bool:
    """True for a recovery-context stream name."""
    return isinstance(stream, str) and stream in RECOVERY_STREAMS


def is_recovery_context_status(status: Optional[str]) -> bool:
    """True for the one status that marks a usable recovery-context pointer.

    The positive counterpart to :data:`REJECTED_STATUSES`: a recovery-context
    consumer selects on this, while a scheduled consumer keeps rejecting it.
    """
    return status == STATUS_VALID_RECOVERY_CONTEXT

# Keys a well-formed pointer must carry to be considered at all.
_REQUIRED_POINTER_KEYS = ("run_id", "run_state_path", "trading_day", "stream")


def today_et(now: Optional[datetime] = None) -> str:
    """Current America/New_York trading day as YYYY-MM-DD."""
    if now is None:
        moment = datetime.now(_EASTERN)
    elif now.tzinfo is None:
        moment = now.replace(tzinfo=_EASTERN)
    else:
        moment = now.astimezone(_EASTERN)
    return moment.strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_valid_scheduled_evidence(run_state: Optional[Mapping[str, Any]]) -> bool:
    """True only when a run_state is a scheduled, non-manual run.

    Generic across streams (morning/midday/afternoon/...): checks the nested
    ``run_classification`` block for ``run_origin='scheduled'`` AND
    ``manual_run=false``. ``manual_run`` must be the explicit boolean
    ``False``; a missing, null, or truthy value is NOT evidence, so an
    incomplete classification (e.g. ``{"run_origin": "scheduled"}`` with no
    ``manual_run`` key) can never pass as scheduled evidence. This is
    deliberately looser than run_classification.is_scheduled_state (which pins
    the 15:30 afternoon window); a stream's own validator can tighten further.

    A run that explicitly denies its own evidence (``scheduled_evidence_denied``)
    is NOT valid scheduled evidence, and neither is one the artifact contract
    rejected: a top-level
    ``artifact_contract_failure`` marker (persisted when validate_run_artifacts
    fails, e.g. the compute-only terminal state) fails closed here so every
    consumer that discovers the run_state directly -- not just the latest-run
    pointer -- refuses to count it.
    """
    if not isinstance(run_state, dict):
        return False
    if run_state.get("artifact_contract_failure") is not None:
        return False
    # An explicit denial from the writer, mirroring artifact_contract_failure.
    # A synthesized startup-failure artifact proves the slot FIRED but is never
    # scheduled evidence, and select_same_day_run_state prefers the date-specific
    # state over the pointer, so without this the failure artifact would be
    # returned as valid evidence to every consumer (Codex review). The nested
    # classification's own valid_scheduled_evidence field cannot carry this: the
    # morning writer seeds it False as a placeholder and finalizes it from THIS
    # derivation, so honoring that field here would lock every run to False.
    if run_state.get("scheduled_evidence_denied") is True:
        return False
    rc = run_state.get("run_classification")
    if not isinstance(rc, dict):
        return False
    if rc.get("manual_run") is not False:
        return False
    return rc.get("run_origin") == "scheduled"


# The full RFC 3339 date-time grammar the schema's `format: date-time` means:
# extended-format date, 'T' separator, two-digit time, optional fractional
# seconds, and a 'Z' or +/-HH:MM offset with no seconds field.
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?"
    # \Z, not $: Python's `$` also matches before a trailing newline.
    r"([Zz]|[+-][0-9]{2}:[0-9]{2})\Z"
)


def _is_iso_utc_timestamp(value: object) -> bool:
    """True only for an RFC 3339 date-time that establishes a UTC instant.

    The schema marks updated_at_utc ``format: date-time``, which requires BOTH
    a time component and a timezone offset. ``datetime.fromisoformat`` alone is
    far looser: it happily accepts a bare date (``2026-07-27``) and a naive
    timestamp (``2026-07-27T12:00:00``), neither of which pins an instant, so
    the helper trusted pointers a schema-aware consumer rejects (Codex review:
    "Require a timezone-bearing date-time for recovery pointers").

    Accepts the trailing ``Z`` spelling the pointer writer emits, which
    fromisoformat rejects before Python 3.11.
    """
    if not isinstance(value, str):
        return False
    # SYNTAX first, against the PERSISTED value -- no strip(), because the
    # schema's pattern is the whole string and a padded timestamp is invalid
    # there too. Presence-of-'T' plus a tz-aware parse was not the same test as
    # `format: date-time`: fromisoformat also accepts basic-format
    # '20260727T120000+00:00', ISO week dates '2026-W31-1T12:00:00+00:00', and
    # offsets carrying seconds, none of which RFC 3339 permits, so the helper
    # still trusted pointers a schema-aware consumer rejects (Codex review:
    # "Enforce RFC 3339 syntax for recovery timestamps").
    if not _RFC3339_RE.match(value):
        return False
    # Then SEMANTICS: the regex cannot rule out month 13 or day 32. Normalize
    # the trailing Z, which fromisoformat rejects before Python 3.11.
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_recovery_state(state: Optional[Mapping[str, object]], trading_day: Optional[str] = None) -> bool:
    """Delegate to run_classification's canonical recovery-contract validator.

    Imported lazily and by both module paths: this file is imported as
    ``same_day_pointer`` (tests, with scripts/ on sys.path) and as
    ``scripts.same_day_pointer`` (near_close_dca, with the repo root on
    sys.path), and only one of the two spellings resolves in each case.
    """
    try:
        from scripts.run_classification import is_valid_recovery_context_state
    except ImportError:  # pragma: no cover - exercised via un-packaged scripts execution
        from run_classification import is_valid_recovery_context_state  # type: ignore
    return is_valid_recovery_context_state(state, trading_day)


def derive_valid_recovery_context(
    run_state: Optional[Mapping[str, Any]], trading_day: Optional[str] = None
) -> bool:
    """True only when a run_state is a valid scheduled-RECOVERY context run.

    The recovery-side counterpart to :func:`derive_valid_scheduled_evidence`,
    and deliberately not a relaxation of it: a run must POSITIVELY assert
    recovery context to pass here, and must simultaneously disclaim scheduled
    evidence. The artifact-contract failure marker fails closed here exactly as
    it does for scheduled evidence.

    Delegates the classification check to
    ``run_classification.validate_recovery_classification``, the single source
    of truth for the recovery contract, so this module cannot drift into
    trusting artifacts the top-up guard rejects (or vice versa). That contract
    covers the origin/flag markers, ``stream``/``run_type``/
    ``state_schema_version`` mutual correspondence (so ``run_type='bogus'`` is
    rejected rather than merely non-blank), ``run_id``, and the classification's
    own ``trading_day`` (Codex review: "Validate recovery identity values
    against the contract").

    Adds this module's own state-level check on top: the
    ``artifact_contract_failure`` marker fails closed here exactly as it does
    for scheduled evidence.

    A run that passes here is still NEVER scheduled evidence: it is valid only
    as same-day, ticker-scoped top-up context for later same-day streams.
    """
    return _validate_recovery_state(run_state, trading_day)


def build_pointer_metadata(
    *,
    run_id: Optional[str],
    run_state_path: str | Path,
    stream: str,
    trading_day: str,
    valid_scheduled_evidence: bool,
    pointer_status: str = STATUS_VALID,
    updated_at_utc: Optional[str] = None,
    extra: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Build the same-day pointer payload required after each run.

    Carries the fields the contract mandates: ``run_id``, ``run_state_path``,
    ``stream``, ``trading_day``, ``pointer_status``, ``same_day_required``
    (always True), ``updated_at_utc``, and ``valid_scheduled_evidence`` derived
    from the target run_state. ``extra`` merges in stream-specific fields
    (e.g. lineage cron ids) without dropping the required ones.
    """
    payload: Dict[str, object] = {
        "run_id": run_id,
        "run_state_path": str(run_state_path),
        "stream": stream,
        "trading_day": trading_day,
        "pointer_status": pointer_status,
        "same_day_required": True,
        "updated_at_utc": updated_at_utc or _utc_now_iso(),
        "valid_scheduled_evidence": valid_scheduled_evidence,
    }
    if extra:
        for key, value in extra.items():
            payload.setdefault(key, value)
    return payload


@dataclass
class PointerValidation:
    """Outcome of validating a latest pointer as same-day scheduled evidence."""

    status: str
    valid_scheduled_evidence: bool
    trading_day: Optional[str] = None
    run_state_path: Optional[str] = None
    stream: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    # True only on a result produced by the RECOVERY validation path
    # (validate_recovery_pointer / validate_pointer(recovery_context=True)).
    # Deliberately NOT inferred from `status`: validate_pointer in its normal
    # scheduled mode returns STATUS_VALID_RECOVERY_CONTEXT when it rejects a
    # recovery pointer's declared status, and that rejection must stay falsey
    # for the scheduled consumer that asked. Only the caller who explicitly
    # asked for recovery validation gets a truthy result.
    recovery_context: bool = False

    @property
    def is_valid(self) -> bool:
        """True only for a fully-valid, same-day, scheduled-evidence pointer."""
        return self.status == STATUS_VALID and self.valid_scheduled_evidence

    @property
    def is_valid_recovery_context(self) -> bool:
        """True only for a fully-valid, same-day RECOVERY-context pointer.

        Never true for a scheduled pointer, and never implies scheduled
        evidence -- ``valid_scheduled_evidence`` stays False on these results.
        """
        return (
            self.recovery_context
            and self.status == STATUS_VALID_RECOVERY_CONTEXT
            and not self.valid_scheduled_evidence
        )

    def __bool__(self) -> bool:  # allow `if validate_pointer(...):`
        # Both entry points support the `if validate_...(...)` idiom. Without
        # the recovery arm a caller of validate_recovery_pointer would discard
        # every valid recovery pointer, since is_valid requires scheduled
        # evidence the artifact must never claim (Codex review: "Expose a
        # truthy recovery-validation result"). A scheduled consumer is
        # unaffected: recovery_context is False on every result it can get.
        return self.is_valid or self.is_valid_recovery_context


def _load_pointer(pointer: str | Path | Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a pointer dict from a path or dict; None if missing/unreadable."""
    if isinstance(pointer, (dict, Mapping)):
        return dict(pointer)
    path = Path(pointer)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"__malformed__": True}
    return data if isinstance(data, dict) else {"__malformed__": True}


def _within_root(candidate: Path, root: Path) -> bool:
    """True if ``candidate`` resolves inside ``root`` (no path escape)."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def validate_pointer(
    pointer: str | Path | Mapping[str, Any],
    *,
    expected_stream: str,
    expected_root: str | Path,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
    require_run_state_exists: bool = True,
    recovery_context: bool = False,
) -> PointerValidation:
    """Validate a latest pointer as same-day scheduled evidence.

    ``expected_stream`` is the stream the caller expects (``morning``,
    ``midday``, ``monitor``, ``afternoon``, ``slippage``, ``watchdog``, ...).
    ``expected_root`` is the stream's cron_tracking root; the pointer's
    ``run_state_path`` must resolve inside it. ``today`` (YYYY-MM-DD) pins the
    ET trading day for tests; when omitted it is derived from ``now`` or the
    wall clock.

    Returns a :class:`PointerValidation`. Only a ``STATUS_VALID`` result with
    ``valid_scheduled_evidence=True`` may be used as scheduled/fill/top-up/
    slippage/reliability/ran-confirmation evidence. Every rejection path
    reports ``valid_scheduled_evidence=False``.

    Set ``recovery_context=True`` to validate a recovery pointer instead (see
    :func:`validate_recovery_pointer`, the preferred entry point). The same
    structural checks run -- required keys, stream identity, root containment,
    same-day on both the pointer and its target -- but the accepted declared
    status becomes ``STATUS_VALID_RECOVERY_CONTEXT`` and the target is checked
    against the recovery contract. A recovery result ALWAYS reports
    ``valid_scheduled_evidence=False``; this mode can never return
    ``STATUS_VALID``.
    """
    day = today or today_et(now)
    root = Path(expected_root)

    data = _load_pointer(pointer)
    if data is None:
        return PointerValidation(STATUS_MISSING, False, reasons=["pointer file missing"])
    if data.get("__malformed__"):
        return PointerValidation(STATUS_MALFORMED, False, reasons=["pointer is not readable JSON object"])

    missing_keys = [k for k in _REQUIRED_POINTER_KEYS if k not in data]
    if missing_keys:
        return PointerValidation(
            STATUS_MALFORMED,
            False,
            reasons=[f"pointer missing required keys: {missing_keys}"],
        )

    ptr_day = str(data.get("trading_day", "")).strip()
    # The PERSISTED stream, deliberately NOT stripped. latest_pointer.schema.json
    # constrains `stream` by an exact enum, so ' midday_recovery ' is
    # schema-invalid; normalizing it here made this helper accept a pointer a
    # schema-aware consumer rejects, and recovery discovery then depended on
    # which validator a caller reached for (Codex review: "Reject padded
    # recovery pointer stream names"). Rejecting is the fail-closed direction --
    # the pointer is reported wrong_stream and ignored, never wrongly trusted.
    # Applied to every stream, not just recovery: the enum is exact for all of
    # them, and no in-repo writer emits a padded stream (they are module
    # constants), so this cannot invalidate a pointer the codebase produces.
    ptr_stream = data.get("stream")
    ptr_stream = ptr_stream if isinstance(ptr_stream, str) else str(ptr_stream or "")
    ptr_path_raw = str(data.get("run_state_path", "")).strip()
    ptr_path = Path(ptr_path_raw)

    # Stream identity must match before anything else is trusted.
    if ptr_stream != expected_stream:
        return PointerValidation(
            STATUS_WRONG_STREAM,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=[f"pointer stream {ptr_stream!r} != expected {expected_stream!r}"],
        )

    # The run_state_path must live inside the expected stream root.
    if not _within_root(ptr_path, root):
        return PointerValidation(
            STATUS_OUTSIDE_ROOT,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=[f"run_state_path {ptr_path_raw!r} is outside expected root {root}"],
        )

    # Same-day check. A prior date is the common stale case; a future date is
    # also rejected (clock skew / bad backfill) and never counts as evidence.
    if ptr_day != day:
        status = STATUS_PRIOR_DATE if ptr_day < day else STATUS_FUTURE_DATE
        return PointerValidation(
            status,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=[f"pointer trading_day {ptr_day!r} != current ET day {day!r}"],
        )

    # A pointer's own declared pointer_status is honored even when the
    # recomputed stream/root/date checks above happen to pass: a pointer
    # tagged (by its writer or an earlier validation pass) as anything other
    # than "valid" -- pending, stale, malformed, outside_root, wrong_stream,
    # ... -- is never trusted as evidence. Pointers that omit the field
    # altogether (written before this contract existed) fall through to the
    # checks below rather than being rejected outright.
    declared_status = data.get("pointer_status")
    accepted_status = STATUS_VALID_RECOVERY_CONTEXT if recovery_context else STATUS_VALID
    if recovery_context and declared_status is None:
        # The missing-status allowance below exists only for LEGACY scheduled
        # pointers written before pointer_status was contractual. A recovery
        # pointer cannot predate a status introduced alongside it, so a recovery
        # pointer with no pointer_status is schema-invalid and must not be
        # promoted to a usable result just because its target validates (Codex
        # review: "Require the recovery pointer status discriminator").
        return PointerValidation(
            STATUS_MALFORMED,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=[
                "recovery pointer must declare pointer_status="
                f"{STATUS_VALID_RECOVERY_CONTEXT!r}"
            ],
        )
    if declared_status is not None and declared_status != accepted_status:
        status = declared_status if declared_status in REJECTED_STATUSES else STATUS_MALFORMED
        return PointerValidation(
            status,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=[f"pointer_status {declared_status!r} is not valid"],
        )

    # The target run_state must actually exist to prove the stream ran.
    if require_run_state_exists and not ptr_path.exists():
        return PointerValidation(
            STATUS_PENDING,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=["target run_state does not exist yet (pending)"],
        )

    # Derive evidence from the target run_state itself, never from the pointer's
    # own claim: a pointer that asserts valid_scheduled_evidence=true but points
    # at a manual/malformed state is not trusted.
    run_state: Optional[Dict[str, object]] = None
    if ptr_path.exists():
        try:
            loaded = json.loads(ptr_path.read_bytes())
            run_state = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return PointerValidation(
                STATUS_MALFORMED,
                False,
                trading_day=ptr_day,
                run_state_path=ptr_path_raw,
                stream=ptr_stream,
                reasons=["target run_state is not readable JSON"],
            )

    # The pointer's own trading_day was already checked above; the target
    # run_state it names must independently carry the same trading_day, since
    # the contract requires the TARGET -- not merely the pointer metadata --
    # to establish same-day evidence. Without this, a malformed or stale
    # pointer could claim today while pointing at a prior-day scheduled
    # run_state anywhere under the stream root and still be returned valid.
    if run_state is not None:
        target_day = str(run_state.get("trading_day", "")).strip()
        if target_day != day:
            status = STATUS_PRIOR_DATE if target_day < day else STATUS_FUTURE_DATE
            return PointerValidation(
                status,
                False,
                trading_day=ptr_day,
                run_state_path=ptr_path_raw,
                stream=ptr_stream,
                reasons=[
                    f"target run_state trading_day {target_day!r} != current ET day {day!r}"
                ],
            )

    # The pointer's OWN declared stream was already checked above, but a
    # misplaced/copied artifact can sit under the expected root with a pointer
    # that lies about which stream it belongs to. The target's own
    # run_classification.stream must independently match expected_stream, or a
    # same-day, scheduled, non-manual artifact from a DIFFERENT stream could
    # still be accepted as this stream's evidence.
    if run_state is not None:
        rc = run_state.get("run_classification")
        target_stream = rc.get("stream") if isinstance(rc, dict) else None
        if target_stream is not None and target_stream != expected_stream:
            return PointerValidation(
                STATUS_WRONG_STREAM,
                False,
                trading_day=ptr_day,
                run_state_path=ptr_path_raw,
                stream=ptr_stream,
                reasons=[
                    f"target run_classification.stream {target_stream!r} != expected {expected_stream!r}"
                ],
            )

    if recovery_context:
        # The pointer's OWN declared invariants, checked before the target is
        # accepted. schemas/latest_pointer.schema.json pins same_day_required to
        # true for every pointer and valid_scheduled_evidence to false for a
        # recovery one, so a pointer contradicting either is schema-invalid;
        # accepting it here would make helper validation and schema validation
        # disagree and let recovery discovery vary by consumer (Codex review:
        # "Reject contradictory recovery pointer metadata"). Scoped to the
        # recovery path deliberately: the scheduled path intentionally derives
        # evidence from the TARGET and ignores the pointer's own claim, and it
        # carries legacy pointers this status cannot have.
        contradictions = []
        if data.get("valid_scheduled_evidence") is not False:
            # The schema pins this to the boolean constant false for a recovery
            # pointer, so an omitted field or a non-boolean like the STRING
            # "false" is schema-invalid too. Rejecting only literal True would
            # leave the helper trusting pointers a schema-aware consumer
            # refuses (Codex review: "Require the recovery pointer evidence
            # field").
            contradictions.append(
                "recovery pointer must declare valid_scheduled_evidence=false "
                f"(got {data.get('valid_scheduled_evidence')!r})"
            )
        if data.get("same_day_required") is not True:
            # Same reasoning: the schema pins same_day_required to the boolean
            # constant true and marks it required, so omitted / "true" / 1 are
            # all schema-invalid, not just an explicit false.
            contradictions.append(
                "recovery pointer must declare same_day_required=true "
                f"(got {data.get('same_day_required')!r})"
            )
        if not _is_iso_utc_timestamp(data.get("updated_at_utc")):
            # updated_at_utc is required with format date-time in the schema.
            # A recovery pointer cannot be a legacy artifact, so it is held to
            # that here too -- otherwise a pointer missing or mangling the
            # field is trusted by this helper and rejected by a schema-aware
            # consumer, making discovery depend on which validator ran (Codex
            # review: "Require recovery pointer update timestamps").
            contradictions.append(
                "recovery pointer must declare a valid ISO-8601 updated_at_utc "
                f"(got {data.get('updated_at_utc')!r})"
            )
        if contradictions:
            return PointerValidation(
                STATUS_MALFORMED,
                False,
                trading_day=ptr_day,
                run_state_path=ptr_path_raw,
                stream=ptr_stream,
                reasons=contradictions,
            )
        # Recovery targets are validated against the recovery contract, never
        # the scheduled one, and always report valid_scheduled_evidence=False:
        # the whole point of the status is that the artifact is usable WITHOUT
        # being scheduled evidence for the slot it recovered.
        if not derive_valid_recovery_context(run_state, day):
            return PointerValidation(
                STATUS_PENDING,
                False,
                trading_day=ptr_day,
                run_state_path=ptr_path_raw,
                stream=ptr_stream,
                reasons=["target run_state is not a valid scheduled-recovery context run"],
            )
        # The pointer's run_id must name the run its run_state_path resolves to.
        # Nothing above compares them, so stale or corrupted metadata could
        # attribute discovered recovery context to one run while the target is
        # another, producing inconsistent audit and reconciliation lineage. A
        # recovery pointer has no legacy-compatibility reason to omit it, so a
        # null or mismatched run_id is rejected rather than tolerated (Codex
        # review: "Match the recovery pointer run ID to its target").
        target_rc = run_state.get("run_classification") if isinstance(run_state, dict) else None
        target_run_id = target_rc.get("run_id") if isinstance(target_rc, dict) else None
        if not isinstance(target_run_id, str) or not target_run_id.strip():
            target_run_id = run_state.get("run_id") if isinstance(run_state, dict) else None
        ptr_run_id = data.get("run_id")
        # Non-blank checks still use strip(), because "is this string empty in
        # substance" is genuinely a whitespace-insensitive question. The
        # IDENTITY comparison does not: both recovery schemas accept a padded
        # non-blank ID (`pattern: "\\S"`), so ' run-1 ' and 'run-1' are each
        # schema-valid yet are DIFFERENT persisted strings. Comparing stripped
        # copies let this validator promise an identity match while the audit
        # and reconciliation metadata named two different runs (Codex review:
        # "Compare recovery run IDs without normalizing them").
        if (
            not isinstance(ptr_run_id, str)
            or not ptr_run_id.strip()
            or not isinstance(target_run_id, str)
            or not target_run_id.strip()
            or ptr_run_id != target_run_id
        ):
            return PointerValidation(
                STATUS_MALFORMED,
                False,
                trading_day=ptr_day,
                run_state_path=ptr_path_raw,
                stream=ptr_stream,
                reasons=[
                    "recovery pointer run_id must match its target's run_id "
                    f"(pointer {ptr_run_id!r}, target {target_run_id!r})"
                ],
            )
        return PointerValidation(
            STATUS_VALID_RECOVERY_CONTEXT,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            recovery_context=True,
        )

    evidence = derive_valid_scheduled_evidence(run_state)
    if not evidence:
        return PointerValidation(
            STATUS_PENDING,
            False,
            trading_day=ptr_day,
            run_state_path=ptr_path_raw,
            stream=ptr_stream,
            reasons=["target run_state is not a scheduled non-manual run"],
        )

    return PointerValidation(
        STATUS_VALID,
        True,
        trading_day=ptr_day,
        run_state_path=ptr_path_raw,
        stream=ptr_stream,
    )


def validate_recovery_pointer(
    pointer: str | Path | Mapping[str, Any],
    *,
    expected_stream: str,
    expected_root: str | Path,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
    require_run_state_exists: bool = True,
) -> PointerValidation:
    """Validate a recovery pointer (e.g. latest_midday_recovery_run.json).

    The entry point a recovery-context consumer should use to DISCOVER a
    recovery artifact. ``expected_stream`` must be one of
    :data:`RECOVERY_STREAMS`; anything else is rejected outright rather than
    silently validated under the scheduled contract.

    A success returns ``STATUS_VALID_RECOVERY_CONTEXT`` with
    ``valid_scheduled_evidence=False``. There is no code path here that returns
    ``STATUS_VALID``, so a recovery pointer can never be mistaken for the
    scheduled pointer of the slot it recovered.
    """
    if not is_recovery_stream(expected_stream):
        return PointerValidation(
            STATUS_WRONG_STREAM,
            False,
            stream=expected_stream,
            reasons=[
                f"expected_stream {expected_stream!r} is not a recovery stream "
                f"{RECOVERY_STREAMS!r}"
            ],
        )
    return validate_pointer(
        pointer,
        expected_stream=expected_stream,
        expected_root=expected_root,
        today=today,
        now=now,
        require_run_state_exists=require_run_state_exists,
        recovery_context=True,
    )


def refresh_latest_pointer(
    pointer_path: str | Path,
    *,
    run_id: Optional[str],
    run_state_path: str | Path,
    stream: str,
    trading_day: str,
    run_state: Optional[Mapping[str, Any]] = None,
    valid_scheduled_evidence: Optional[bool] = None,
    pointer_status: str = STATUS_VALID,
    updated_at_utc: Optional[str] = None,
    extra: Optional[Dict[str, object]] = None,
) -> Path:
    """Write/refresh a latest pointer with same-day metadata after a run.

    ``valid_scheduled_evidence`` is derived from ``run_state`` when not passed
    explicitly, so the pointer's evidence claim always reflects the target
    state rather than the caller's optimism. Returns the pointer path written.
    """
    if valid_scheduled_evidence is None:
        valid_scheduled_evidence = derive_valid_scheduled_evidence(run_state)
    if pointer_status == STATUS_VALID and not valid_scheduled_evidence:
        pointer_status = STATUS_PENDING
    payload = build_pointer_metadata(
        run_id=run_id,
        run_state_path=run_state_path,
        stream=stream,
        trading_day=trading_day,
        valid_scheduled_evidence=valid_scheduled_evidence,
        pointer_status=pointer_status,
        updated_at_utc=updated_at_utc,
        extra=extra,
    )
    path = Path(pointer_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def select_same_day_run_state(
    *,
    stream: str,
    expected_root: str | Path,
    trading_day_dir: Optional[str | Path] = None,
    pointer_path: Optional[str | Path] = None,
    fallback_pointer_paths: Optional[Iterable[str | Path | Tuple[str | Path, str | Path]]] = None,
    fallback_expected_root: Optional[str | Path] = None,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
    run_state_filename: str = "run_state.json",
) -> Optional[Dict[str, object]]:
    """Resolve today's scheduled run_state, preferring a date-specific file.

    Order of preference (contract): a date-specific same-day run_state file
    (under ``trading_day_dir``) is preferred over the canonical latest pointer.
    If both are unavailable or invalid, callers may supply same-day fallback
    pointers (for example the Perplexity-primary alias root used by the 11:30
    catch-up run). Only a scheduled, non-manual, same-day state is returned;
    anything else (stale, manual, malformed, wrong stream, pointer to a prior
    day) yields ``None``.

    ``fallback_pointer_paths`` entries may be either a pointer path or
    ``(pointer_path, expected_root)``. The latter is the safe form for alias
    roots that deliberately sit outside the canonical stream root. A bare
    fallback path is validated against ``fallback_expected_root`` when provided,
    otherwise against ``expected_root``.

    Returns the loaded run_state dict, or ``None`` when no valid same-day
    scheduled state can be established.
    """
    root = Path(expected_root)
    day = today or today_et(now)

    if trading_day_dir is not None:
        candidate = Path(trading_day_dir) / run_state_filename
        if candidate.exists() and _within_root(candidate, root):
            try:
                loaded = json.loads(candidate.read_bytes())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                loaded = None
            # A date-specific file living in today's expected directory is not
            # itself proof of same-day evidence: verify the loaded state's own
            # trading_day and stream before trusting it, so a prior-day or
            # wrong-stream run_state left (or copied) into today's directory
            # (e.g. a midday state under a morning root) is not returned as
            # today's morning evidence. Unlike validate_pointer's target-stream
            # check, this direct path has no separate pointer-level stream
            # declaration as a first line of defense, so a missing/absent
            # classification stream fails closed rather than being let through.
            if isinstance(loaded, dict):
                rc = loaded.get("run_classification")
                target_stream = rc.get("stream") if isinstance(rc, dict) else None
                if (
                    str(loaded.get("trading_day", "")).strip() == day
                    and target_stream == stream
                    and derive_valid_scheduled_evidence(loaded)
                ):
                    return loaded

    if pointer_path is not None:
        loaded = _load_valid_state_from_pointer(
            pointer_path,
            expected_stream=stream,
            expected_root=root,
            today=day,
            now=now,
        )
        if loaded is not None:
            return loaded

    fallback_candidates = list(
        _iter_fallback_pointers(
            fallback_pointer_paths,
            default_root=fallback_expected_root or root,
        )
    )
    fallback_candidates.extend(_perplexity_primary_alias_pointer_candidates(stream, day))

    for fallback_pointer, fallback_root in fallback_candidates:
        loaded = _load_valid_state_from_pointer(
            fallback_pointer,
            expected_stream=stream,
            expected_root=fallback_root,
            today=day,
            now=now,
        )
        if loaded is not None:
            return loaded

    return None


def _perplexity_primary_alias_pointer_candidates(
    stream: str,
    trading_day: str,
) -> Iterable[Tuple[Path, Path]]:
    """Yield safe same-day Perplexity-primary alias pointer candidates.

    The Aug 3 2026 11:30 catch-up incident showed that the canonical stream
    pointer can be absent while the Perplexity-primary alias root has a
    pointer and root artifacts. This default fallback lets orchestration
    self-heal that class of pointer-missing event WHEN the alias run is
    itself genuinely scheduled evidence, without accepting arbitrary fallback
    paths: the alias root is derived only from the current trading day and the
    pointer filename is derived only from the expected stream.

    This is deliberately narrower than "self-heal the 11:30 catch-up": like
    every other candidate ``select_same_day_run_state`` considers, an alias
    candidate still has to satisfy :func:`derive_valid_scheduled_evidence`
    (``run_origin='scheduled'``, ``manual_run=false``) through
    :func:`_load_valid_state_from_pointer`. The 174c4163 catch-up's own
    classification (config/scheduled_tasks.json:
    perplexity_primary_1130_catchup_2026_08_03) is correctly ``run_origin=
    'manual'``/``manual_run=true`` -- it never claims to be the background
    scheduled run, per this same repo's evidence-separation contract
    (scripts/foreground_primary.py) -- so this fallback returning ``None`` for
    that specific run_state is correct, not a bug: a manual/foreground catch-up
    must never be surfaced as SCHEDULED evidence, through this path or any
    other (Codex review: "Accept the manual catch-up through the alias
    fallback"). Same-day TOP-UP context for a manual/foreground run (which is
    what the catch-up's own manifest block actually needs from later runs) is
    resolved separately, by the existing prior_allocation_roots directory scan
    in morning_dca.py/near_close_dca.py, which is not scoped to scheduled-only
    evidence and already covers this alias root when it is listed there.
    """
    pointer_filename = _STREAM_ALIAS_POINTER_FILENAMES.get(stream)
    if pointer_filename is None:
        return
    compact_day = trading_day.replace("-", "")
    alias_root = Path(CRON_TRACKING_ROOT) / f"perplexity_primary_{compact_day}"
    yield alias_root / pointer_filename, alias_root


def _iter_fallback_pointers(
    fallback_pointer_paths: Optional[Iterable[str | Path | Tuple[str | Path, str | Path]]],
    *,
    default_root: str | Path,
) -> Iterable[Tuple[str | Path, Path]]:
    """Yield ``(pointer_path, expected_root)`` pairs for fallback alias pointers."""
    if not fallback_pointer_paths:
        return
    for entry in fallback_pointer_paths:
        if isinstance(entry, tuple):
            pointer, root = entry
            yield pointer, Path(root)
        else:
            yield entry, Path(default_root)


def _load_valid_state_from_pointer(
    pointer_path: str | Path,
    *,
    expected_stream: str,
    expected_root: str | Path,
    today: str,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, object]]:
    """Load a run_state from a pointer only after full same-day validation."""
    result = validate_pointer(
        pointer_path,
        expected_stream=expected_stream,
        expected_root=expected_root,
        today=today,
        now=now,
    )
    if not result.is_valid or not result.run_state_path:
        return None
    try:
        loaded = json.loads(Path(result.run_state_path).read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(loaded, dict) and derive_valid_scheduled_evidence(loaded):
        return loaded
    return None


__all__ = [
    "CRON_TRACKING_ROOT",
    "STATUS_VALID",
    "STATUS_MISSING",
    "STATUS_MALFORMED",
    "STATUS_STALE",
    "STATUS_PRIOR_DATE",
    "STATUS_FUTURE_DATE",
    "STATUS_PENDING",
    "STATUS_OUTSIDE_ROOT",
    "STATUS_WRONG_STREAM",
    "STATUS_VALID_RECOVERY_CONTEXT",
    "RECOVERY_STREAMS",
    "is_recovery_stream",
    "is_recovery_context_status",
    "derive_valid_recovery_context",
    "validate_recovery_pointer",
    "REJECTED_STATUSES",
    "today_et",
    "derive_valid_scheduled_evidence",
    "build_pointer_metadata",
    "PointerValidation",
    "validate_pointer",
    "refresh_latest_pointer",
    "select_same_day_run_state",
]
