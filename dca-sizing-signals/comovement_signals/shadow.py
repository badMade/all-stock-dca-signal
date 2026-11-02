"""The single shadow-record writer for co-movement signals 3 to 5 (shadow / log only).

docs/adr-comovement-signals.md lists signal 3 (pair breakdown) and signal 4 (cheaper-leg
tilt) as shadow experiments and signal 5 (staples vs semis rotation) as log only. None of
them is allowed to influence sizing yet, so this module is deliberately NOT a
SuggestionPublisher and none of the three signals is a SizingSuggester:

  * each signal appends to ONE JSON-lines file of its own, named here, under a directory
    the caller picks. Nothing defaults to the repo's ``config/``; a directory with a
    ``config`` path component is refused outright, so a mis-pointed caller cannot drop a
    file where the live loaders look;
  * a file name is accepted only if it is one of the three shadow names. That rules out
    ``ml_sizing_adjustment.json`` (what SizingAdjustmentReader reads), signal 1 and 2's
    ``*_suggestions.json`` files and anything else, and a ``.json`` file at all: the
    reader loads whole JSON objects, and these files are JSON lines;
  * the files are append-only. A line is never rewritten; an identical record (same
    content hash) is not appended twice, so a re-run of the same day is idempotent;
  * every line carries ``as_of``, ``inputs_known_at`` (the newest data date the record
    used), ``signal``, ``payload`` and a SHA-256 ``content_hash`` over the rest of the
    line, plus ``"live_effect": "none"``. A record whose inputs were known after its own
    as-of date is refused here as a last lookahead guard, whatever built it.

The shared ``ShadowRecord`` / ``ShadowResult`` shapes and the point-in-time input checks
the three signals share also live here, so each signal module stays a pure function.
Stdlib plus the shared lock primitive only; no network, no clock.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from comovement_signals.returns import ComovementInputError, ReturnsMatrix, returns_matrix

# Reusing the shared lock keeps one locking strategy for every file this package writes.
from dca_shared_interfaces.publishing import _acquire_lock

SIGNAL_PAIR_BREAKDOWN = "pair_breakdown"
SIGNAL_CHEAPER_LEG_TILT = "cheaper_leg_tilt"
SIGNAL_ROTATION_LOG = "rotation_log"
SHADOW_FILENAMES = {
    SIGNAL_PAIR_BREAKDOWN: "comovement_pair_breakdown_shadow.jsonl",
    SIGNAL_CHEAPER_LEG_TILT: "comovement_tilt_shadow.jsonl",
    SIGNAL_ROTATION_LOG: "comovement_rotation_log.jsonl",
}
SHADOW_SCHEMA = "comovement_shadow/v1"
# Same 4-calendar-day rule as signals 1 and 2 (ComovementConfig.max_price_staleness_days):
# a Friday close read on a Tuesday after a Monday holiday is still fresh.
DEFAULT_MAX_PRICE_STALENESS_DAYS = 4


@dataclass(frozen=True)
class ShadowRecord:
    """One shadow observation, before hashing. ``payload`` must be JSON-serialisable
    with finite numbers only."""

    signal: str
    as_of: date
    inputs_known_at: date
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ShadowResult:
    """One signal's evaluation for one as-of date.

    ``ok`` False means the whole run failed closed (``reason`` says why) and nothing is
    written. ``skipped`` maps a pair key ("A|B") to the fail-closed reason that pair got
    no record, so a missing pair is always explained rather than silently absent.
    """

    signal: str
    as_of: date
    ok: bool
    reason: str
    records: tuple[ShadowRecord, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteResult:
    written: bool
    status: str  # "written" | "duplicate"
    path: Path
    content_hash: str


def validate_shadow_filename(name: str) -> str:
    """Return ``name`` if it is one of the three shadow file names, else raise ValueError."""
    if name not in SHADOW_FILENAMES.values():
        raise ValueError(f"{name!r} is not a co-movement shadow file; allowed: {sorted(SHADOW_FILENAMES.values())}")
    return name


def content_hash(fields: Mapping[str, object]) -> str:
    """SHA-256 of the canonical JSON (sorted keys, no whitespace) of ``fields``."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ShadowWriter:
    """Appends shadow records to each signal's own JSON-lines file under ``directory``.

    Raises:
        ValueError: if ``directory`` has a ``config`` path component.
    """

    def __init__(self, directory: Path) -> None:
        directory = Path(directory)
        # Check both the path as given and as resolved, so neither a relative "config"
        # nor a symlink into a config/ tree slips through.
        for candidate in (directory, directory.resolve()):
            if "config" in candidate.parts:
                raise ValueError(f"shadow records never go under a config/ directory: {directory}")
        self.directory = directory

    def path_for(self, signal: str) -> Path:
        if signal not in SHADOW_FILENAMES:
            raise ValueError(f"unknown shadow signal {signal!r}")
        return self.directory / validate_shadow_filename(SHADOW_FILENAMES[signal])

    def append(self, record: ShadowRecord) -> WriteResult:
        """Append one record to its signal's file, unless an identical one is there.

        Every check runs before the file is touched, so a refused record leaves the
        directory exactly as it was.
        """
        path = self.path_for(record.signal)
        if record.inputs_known_at > record.as_of:
            raise ValueError(
                f"inputs known at {record.inputs_known_at.isoformat()} are after as_of {record.as_of.isoformat()}")
        body = {
            "schema": SHADOW_SCHEMA,
            "signal": record.signal,
            "as_of": record.as_of.isoformat(),
            "inputs_known_at": record.inputs_known_at.isoformat(),
            "payload": record.payload,
            "live_effect": "none",
        }
        try:
            digest = content_hash(body)
        except (TypeError, ValueError) as exc:
            # allow_nan=False turns a NaN/inf anywhere in the payload into a ValueError.
            raise ValueError(f"payload is not finite, serialisable JSON: {exc}") from exc
        line = json.dumps({**body, "content_hash": digest}, sort_keys=True, separators=(",", ":"))

        self.directory.mkdir(parents=True, exist_ok=True)
        with _acquire_lock(path.with_name(path.name + ".lock")):
            if digest in _existing_hashes(path):
                return WriteResult(False, "duplicate", path, digest)
            # "a" never truncates: earlier lines stay byte-for-byte as they were.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return WriteResult(True, "written", path, digest)


def _existing_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    hashes = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            hashes.add(json.loads(raw).get("content_hash"))
    return hashes


def write_shadow_result(result: ShadowResult, writer: ShadowWriter) -> list[WriteResult]:
    """Append every record of an ok result; a failed result writes nothing.

    Raises:
        ValueError: if a record's signal differs from the result's.
    """
    if not result.ok:
        return []
    for record in result.records:
        if record.signal != result.signal:
            raise ValueError(f"record signal {record.signal!r} != result signal {result.signal!r}")
    return [writer.append(record) for record in result.records]


# --- point-in-time input checks shared by signals 3 to 5 --------------------------------


class ShadowUnavailable(Exception):
    """The whole evaluation fails closed with ``reason``."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


def fresh_matrix(
    close_rows: Iterable[Mapping[str, object]],
    as_of: date,
    window_sessions: int,
    min_sessions: int,
    max_staleness_days: int,
) -> ReturnsMatrix:
    """returns_matrix() plus the staleness check, raising ShadowUnavailable on failure.

    Reasons: ``invalid_input`` (a malformed row; returns_matrix raises rather than skips),
    ``no_prices`` and ``stale_prices`` (the newest close used is more than
    ``max_staleness_days`` calendar days before ``as_of``).
    """
    try:
        matrix = returns_matrix(close_rows, as_of, window_sessions=window_sessions, min_sessions=min_sessions)
    except ComovementInputError as exc:
        raise ShadowUnavailable("invalid_input", str(exc)) from exc
    if matrix.latest_price_date is None or not matrix.sessions:
        raise ShadowUnavailable("no_prices", "no close dated on or before as_of")
    age = (as_of - matrix.latest_price_date).days
    if age > max_staleness_days:
        raise ShadowUnavailable("stale_prices",
                                f"newest close {matrix.latest_price_date.isoformat()} is {age} days old")
    return matrix


def pair_key(a: str, b: str) -> str:
    return f"{a}|{b}"


def finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
