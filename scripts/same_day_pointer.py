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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text

_EASTERN = ZoneInfo("America/New_York")

CRON_TRACKING_ROOT = "/home/user/workspace/cron_tracking"

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

# Statuses that never count as same-day scheduled evidence.
REJECTED_STATUSES = (
    STATUS_MISSING,
    STATUS_MALFORMED,
    STATUS_STALE,
    STATUS_PRIOR_DATE,
    STATUS_FUTURE_DATE,
    STATUS_PENDING,
    STATUS_OUTSIDE_ROOT,
    STATUS_WRONG_STREAM,
)

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


def derive_valid_scheduled_evidence(run_state: Optional[Dict[str, object]]) -> bool:
    """True only when a run_state is a scheduled, non-manual run.

    Generic across streams (morning/midday/afternoon/...): checks the nested
    ``run_classification`` block for ``run_origin='scheduled'`` AND
    ``manual_run=false``. ``manual_run`` must be the explicit boolean
    ``False``; a missing, null, or truthy value is NOT evidence, so an
    incomplete classification (e.g. ``{"run_origin": "scheduled"}`` with no
    ``manual_run`` key) can never pass as scheduled evidence. This is
    deliberately looser than run_classification.is_scheduled_state (which pins
    the 15:30 afternoon window); a stream's own validator can tighten further.

    A run that the artifact contract rejected is NOT valid scheduled evidence,
    even if its classification still says scheduled: a top-level
    ``artifact_contract_failure`` marker (persisted when validate_run_artifacts
    fails, e.g. the compute-only terminal state) fails closed here so every
    consumer that discovers the run_state directly -- not just the latest-run
    pointer -- refuses to count it.
    """
    if not isinstance(run_state, dict):
        return False
    if run_state.get("artifact_contract_failure") is not None:
        return False
    rc = run_state.get("run_classification")
    if not isinstance(rc, dict):
        return False
    if rc.get("manual_run") is not False:
        return False
    return rc.get("run_origin") == "scheduled"


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
        "valid_scheduled_evidence": bool(valid_scheduled_evidence),
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

    @property
    def is_valid(self) -> bool:
        """True only for a fully-valid, same-day, scheduled-evidence pointer."""
        return self.status == STATUS_VALID and self.valid_scheduled_evidence

    def __bool__(self) -> bool:  # allow `if validate_pointer(...):`
        return self.is_valid


def _load_pointer(pointer: str | Path | Dict[str, object]) -> Optional[Dict[str, object]]:
    """Return a pointer dict from a path or dict; None if missing/unreadable."""
    if isinstance(pointer, dict):
        return pointer
    path = Path(pointer)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    pointer: str | Path | Dict[str, object],
    *,
    expected_stream: str,
    expected_root: str | Path,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
    require_run_state_exists: bool = True,
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
    ptr_stream = str(data.get("stream", "")).strip()
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
    if declared_status is not None and declared_status != STATUS_VALID:
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
            loaded = json.loads(ptr_path.read_text(encoding="utf-8"))
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


def refresh_latest_pointer(
    pointer_path: str | Path,
    *,
    run_id: Optional[str],
    run_state_path: str | Path,
    stream: str,
    trading_day: str,
    run_state: Optional[Dict[str, object]] = None,
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
    today: Optional[str] = None,
    now: Optional[datetime] = None,
    run_state_filename: str = "run_state.json",
) -> Optional[Dict[str, object]]:
    """Resolve today's scheduled run_state, preferring a date-specific file.

    Order of preference (contract): a date-specific same-day run_state file
    (under ``trading_day_dir``) is preferred over the latest pointer. Only a
    scheduled, non-manual, same-day state is returned; anything else (stale,
    manual, malformed, wrong stream, pointer to a prior day) yields ``None``.

    Returns the loaded run_state dict, or ``None`` when no valid same-day
    scheduled state can be established.
    """
    root = Path(expected_root)
    day = today or today_et(now)

    if trading_day_dir is not None:
        candidate = Path(trading_day_dir) / run_state_filename
        if candidate.exists() and _within_root(candidate, root):
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
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
        result = validate_pointer(
            pointer_path,
            expected_stream=stream,
            expected_root=root,
            today=day,
            now=now,
        )
        if result.is_valid and result.run_state_path:
            try:
                loaded = json.loads(Path(result.run_state_path).read_text(encoding="utf-8"))
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
    "REJECTED_STATUSES",
    "today_et",
    "derive_valid_scheduled_evidence",
    "build_pointer_metadata",
    "PointerValidation",
    "validate_pointer",
    "refresh_latest_pointer",
    "select_same_day_run_state",
]
