"""Append-only history of what each independent sizing suggester recommended.

The sizing ladder is something the operator is still *determining*: several
suggesters (``ml-forecaster``, ``wondrous-octopus``, and any future ones) each
propose per-ticker multipliers, and a human decides over at least six months of
evidence which of them -- or which blend -- earns a place in live sizing. That
comparison only works if the suggesters never overwrite one another and never
overwrite live sizing, and if every recommendation is kept, not just the latest.

So this module is the one place suggesters write to. Each run appends ONE JSON
line to ``<cron root>/sizing_suggestions/<suggester>/<YYYY>.jsonl``. Nothing here
reads or writes ``config/ml_sizing_adjustment.json`` (the file
``dca_config.load_ml_sizing_adjustment`` feeds to ``graded_buy_multiplier``), and
nothing ever rewrites or deletes an existing line.

Record shape (``schema_version`` 1), one object per line::

    {"schema_version": 1, "suggester": "octopus", "run_id": "...",
     "recorded_at": "<UTC ISO>", "generated_at": "<UTC ISO>",
     "model_version": "...", "status": "published",
     "bounds": {"min_multiplier": 0.8, "max_multiplier": 1.2},
     "adjustments": {"AAPL": 1.1},
     "source": {...}, "metrics": {...}}

``status`` is the suggester's own outcome for the run (``published`` or a
``blocked_*`` reason), so blocked runs stay on the record: a model the gate kept
rejecting is itself evidence. ``source`` and ``metrics`` are free-form and carry
the provenance each suggester has (forecast file, forecast date, eval MAE, ...).
The files are plain JSONL so DuckDB (``read_json_auto``) or pandas can query
them directly, with no dependency added here.

Standard library only; imports only the dependency-free :mod:`dca_paths` and
:mod:`scripts.file_lock`.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

import dca_paths
from scripts.file_lock import cross_process_lock

SCHEMA_VERSION = 1

# Directory under the cron-tracking root. A late-bound call (not an import-time
# constant) so $DCA_CRON_TRACKING_ROOT overrides -- including a test's -- apply.
HISTORY_DIRNAME = "sizing_suggestions"

# Suggester names become directory names: restrict them so a name can never
# escape the history root (``../``) or collide across case-insensitive volumes.
_SUGGESTER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def history_root() -> Path:
    """``<cron tracking root>/sizing_suggestions`` (honours ``$DCA_CRON_TRACKING_ROOT``)."""
    return Path(dca_paths.stream_root(HISTORY_DIRNAME))


def _validate_suggester(suggester: str) -> str:
    if not isinstance(suggester, str) or not _SUGGESTER_RE.match(suggester):
        raise ValueError(f"suggester {suggester!r} must match {_SUGGESTER_RE.pattern}")
    return suggester


def _utc(value: Optional[datetime]) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clean_adjustments(adjustments: Mapping[str, float]) -> Dict[str, float]:
    """Upper-case, sorted, finite numbers only -- fail closed on anything else.

    A NaN/inf or bool would round-trip through ``json`` and silently poison every
    later comparison, so refuse the whole record rather than write a bad line.
    """
    cleaned: Dict[str, float] = {}
    for ticker, value in adjustments.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"adjustment for {ticker!r} must be a finite number, got {value!r}")
        key = str(ticker).upper()
        if key in cleaned:
            raise ValueError(f"duplicate adjustment ticker after upper-casing: {key}")
        cleaned[key] = round(float(value), 6)
    return dict(sorted(cleaned.items()))


def record_suggestions(
    suggester: str,
    adjustments: Mapping[str, float],
    *,
    status: str = "published",
    model_version: Optional[str] = None,
    bounds: Optional[Mapping[str, float]] = None,
    generated_at: Optional[datetime] = None,
    source: Optional[Mapping[str, Any]] = None,
    metrics: Optional[Mapping[str, Any]] = None,
    run_id: Optional[str] = None,
    root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Append one run's recommendations to ``suggester``'s history and return the file.

    ``generated_at`` is when the suggester's *source data* was produced (defaults
    to now); ``recorded_at`` is always the wall clock at append time, so a stale
    forecast republished later is visible as ``recorded_at`` far after
    ``generated_at``. Raises ``ValueError`` on a bad suggester name or
    non-finite adjustment; never touches an existing line.
    """
    name = _validate_suggester(suggester)
    record = {
        "schema_version": SCHEMA_VERSION,
        "suggester": name,
        "run_id": run_id or uuid.uuid4().hex,
        "recorded_at": _utc(now).isoformat(),
        "generated_at": _utc(generated_at).isoformat(),
        "model_version": model_version,
        "status": status,
        "bounds": dict(bounds) if bounds is not None else None,
        "adjustments": _clean_adjustments(adjustments),
        "source": dict(source) if source is not None else {},
        "metrics": dict(metrics) if metrics is not None else {},
    }
    # allow_nan=False: a NaN smuggled in through ``metrics`` must fail here, not
    # write a line a strict JSON reader (DuckDB included) later rejects.
    line = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"

    directory = (root or history_root()) / name
    path = directory / f"{record['recorded_at'][:4]}.jsonl"
    # One lock per suggester directory; the append itself is a single write+fsync
    # so a crash can leave at worst a torn final line, which readers skip.
    with cross_process_lock(directory / ".history.lock"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return path


def iter_history(suggester: str, *, root: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
    """Yield every well-formed record for ``suggester``, oldest file first.

    A torn or non-object line (e.g. a crash mid-append) is skipped, not raised:
    one bad line must not hide years of history from the comparison.
    """
    directory = (root or history_root()) / _validate_suggester(suggester)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record


def read_history(suggester: str, *, root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """All records for ``suggester`` as a list, in append order."""
    return list(iter_history(suggester, root=root))


__all__ = ["HISTORY_DIRNAME", "SCHEMA_VERSION", "history_root", "iter_history", "read_history", "record_suggestions"]
