"""The one shadow file the residual-dip signal writes (ADR D8).

Same rules as comovement_signals.ShadowWriter, re-implemented rather than imported because
the boundary test forbids kalman_signals from importing any other signal package (ADR D3):

  * exactly one file name, ``kalman_residual_dip_shadow.jsonl``, under a directory the
    caller picks; a directory with a ``config`` path component is refused outright;
  * append-only JSON lines, never a ``.json`` file the live SizingAdjustmentReader could load;
  * every line carries ``schema``, ``signal``, ``as_of``, ``inputs_known_at``, ``payload``,
    ``"live_effect": "none"`` and a SHA-256 ``content_hash``; an identical line is not
    appended twice, so re-running a day is idempotent;
  * a record whose inputs were known after its own as-of date is refused (last lookahead guard).

Stdlib plus the shared lock primitive only.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dca_shared_interfaces.publishing import _acquire_lock

SIGNAL_RESIDUAL_DIP = "kalman_residual_dip"
SHADOW_FILENAME = "kalman_residual_dip_shadow.jsonl"
SHADOW_SCHEMA = "kalman_residual_dip_shadow/v1"


@dataclass(frozen=True)
class ShadowRecord:
    """One scored (ticker, session). ``payload`` must be finite, serialisable JSON."""

    as_of: date
    inputs_known_at: date
    payload: Mapping[str, object]
    signal: str = SIGNAL_RESIDUAL_DIP


@dataclass(frozen=True)
class ShadowResult:
    """One evaluation. ``ok`` False: the whole run failed closed and nothing is written.
    ``skipped`` maps each ticker that got no record to its fail-closed reason."""

    as_of: date
    ok: bool
    reason: str
    records: tuple[ShadowRecord, ...] = ()
    skipped: Mapping[str, str] = field(default_factory=dict)
    signal: str = SIGNAL_RESIDUAL_DIP


@dataclass(frozen=True)
class WriteResult:
    written: bool
    status: str  # "written" | "duplicate"
    path: Path
    content_hash: str


def content_hash(fields: Mapping[str, object]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ShadowWriter:
    """Appends residual-dip records to ``directory / SHADOW_FILENAME``."""

    def __init__(self, directory: Path) -> None:
        directory = Path(directory)
        for candidate in (directory, directory.resolve()):
            if "config" in candidate.parts:
                raise ValueError(f"shadow records never go under a config/ directory: {directory}")
        self.directory = directory
        self.path = directory / SHADOW_FILENAME

    def append(self, record: ShadowRecord) -> WriteResult:
        """Every check runs before the file is touched, so a refused record changes nothing."""
        if record.signal != SIGNAL_RESIDUAL_DIP:
            raise ValueError(f"unknown signal {record.signal!r}")
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
            raise ValueError(f"payload is not finite, serialisable JSON: {exc}") from exc
        line = json.dumps({**body, "content_hash": digest}, sort_keys=True, separators=(",", ":"))

        self.directory.mkdir(parents=True, exist_ok=True)
        with _acquire_lock(self.path.with_name(self.path.name + ".lock")):
            _refuse_unterminated_tail(self.path)
            if digest in _existing_hashes(self.path):
                return WriteResult(False, "duplicate", self.path, digest)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return WriteResult(True, "written", self.path, digest)


def _refuse_unterminated_tail(path: Path) -> None:
    """A last line with no newline would get the next record glued onto it; refuse rather
    than repair (same rule as comovement_signals, Copilot review on PR #2106)."""
    if not path.exists():
        return
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            return
        fh.seek(-1, os.SEEK_END)
        if fh.read(1) != b"\n":
            raise ValueError(f"{path.name} has an unterminated last line; refusing to append")


def _existing_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(raw).get("content_hash") for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()}


def write_shadow_result(result: ShadowResult, writer: ShadowWriter) -> list[WriteResult]:
    """Append every record of an ok result; a failed result writes nothing."""
    if not result.ok:
        return []
    return [writer.append(record) for record in result.records]
