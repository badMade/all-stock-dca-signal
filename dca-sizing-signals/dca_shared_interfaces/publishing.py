"""Shared, reusable fail-closed guarded-publish mechanics, factored out so
every independent SizingSuggester gets the same safety property -- never
regress the live file, refuse on missing or non-finite metrics, atomic
write -- without copy-pasting ml_forecaster/publish.py's logic, and
without any suggester needing to know how another suggester's gate is
configured or what it last published.

Design rule, same as forecasting.py: this module never imports
near_close_dca or any concrete suggester package. Zero new third-party
dependencies -- stdlib only (json, math, pathlib, os), per Section 8's
"no new dependencies without written justification."

The lock primitive below is a minimal stdlib stand-in (atomic exclusive
file creation with a retry loop). If ml_forecaster/publish.py already
wraps a specific lock utility, swap _acquire_lock to call that instead --
don't run two different locking strategies against files that might one
day share a directory.
"""

from __future__ import annotations

import json
import math
import os
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .forecasting import SizingSuggestion


@dataclass(frozen=True, slots=True)
class PublishResult:
    published: bool
    status: str  # "published" | "blocked_regression" | "blocked_invalid_bounds"
    # | "blocked_invalid_adjustments" | "blocked_missing_eval_metric"
    reason: str


@contextmanager
def _acquire_lock(lock_path: Path, timeout_s: float = 30.0, poll_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    fd = None
    try:
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire {lock_path} within {timeout_s}s")
                time.sleep(poll_s)
        yield
    finally:
        if fd is not None:
            os.close(fd)
            lock_path.unlink(missing_ok=True)


class SuggestionPublisher(ABC):
    """Template Method: publish() owns the fail-closed algorithm end to
    end. Each concrete subclass supplies only WHERE it writes and HOW it
    validates its own bounds. Two different suggesters each get their own
    instance, pointed at their own file -- they never see or affect each
    other's gate, lock, or publish history.

    Args:
        output_path: this publisher's own config file. Never shared with
            another publisher.
    """

    def __init__(self, output_path: Path) -> None:
        self._output_path = output_path
        self._lock_path = output_path.with_suffix(output_path.suffix + ".lock")

    @abstractmethod
    def validate_bounds(self, suggestion: SizingSuggestion) -> bool:
        """Schema/range check specific to this suggester's output.

        Returns:
            True if suggestion is within this suggester's declared bounds.
        """

    def publish(self, suggestion: SizingSuggestion) -> PublishResult:
        """Fail-closed guarded publish, mirroring ml_forecaster/publish.py.

        Args:
            suggestion: the candidate suggestion to publish.

        Returns:
            PublishResult describing whether the write happened and why.
            On any refusal, the existing file is never opened for writing.
        """
        if not math.isfinite(suggestion.eval_metric_mae):
            return PublishResult(False, "blocked_missing_eval_metric",
                                  "eval_metric_mae is missing or non-finite")
        if not self.validate_bounds(suggestion):
            return PublishResult(False, "blocked_invalid_bounds",
                                  "suggestion failed schema/range validation")

        with _acquire_lock(self._lock_path):
            incumbent_mae = self._read_incumbent_mae()
            if incumbent_mae is not None and not (suggestion.eval_metric_mae < incumbent_mae):
                return PublishResult(
                    False, "blocked_regression",
                    f"{suggestion.eval_metric_mae} does not beat incumbent {incumbent_mae}",
                )
            self._write(suggestion)
            return PublishResult(True, "published", "ok")

    def _read_incumbent_mae(self) -> float | None:
        if not self._output_path.exists():
            return None
        try:
            data = json.loads(self._output_path.read_bytes())
            return data.get("eval_metric", {}).get("mae")
        except (json.JSONDecodeError, OSError):
            return None  # an unreadable prior file imposes no floor, same as today's publish.py

    def _write(self, suggestion: SizingSuggestion) -> None:
        payload = {
            "ticker": suggestion.ticker,
            "asof": suggestion.asof.isoformat(),
            "multiplier_adjustment_pct": suggestion.multiplier_adjustment_pct,
            "eval_metric": {"mae": suggestion.eval_metric_mae},
            "source": suggestion.source,
            "model_version": suggestion.model_version,
            "generated_at": suggestion.generated_at.isoformat(),
        }
        tmp_path = self._output_path.with_suffix(self._output_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(self._output_path)  # atomic on POSIX and Windows
