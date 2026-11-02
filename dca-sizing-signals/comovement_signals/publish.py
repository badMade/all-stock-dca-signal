"""Publishing co-movement suggestions through the shared SuggestionPublisher conventions.

What is reused from dca_shared_interfaces.publishing: the Template-Method base class (its
abstract ``validate_bounds``), the lock primitive, the tmp-then-replace atomic write and
the PublishResult shape. What differs, and why:

  * One file per SIGNAL, holding the whole day's per-ticker batch, rather than one
    suggestion per file. Each signal's file name is fixed here, and a publisher refuses to
    be pointed at any other name -- in particular ``ml_sizing_adjustment.json`` or the
    other co-movement signal's file (ADR rule 2: each signal publishes its own file and
    never writes into another suggester's).
  * No MAE regression gate. These are unscored rules, so there is no metric to beat; the
    gate is replaced by a stale-date guard (an older as-of never overwrites a newer one)
    and the file states ``"mae": null, "scored": false``. Every row must be an unscored
    ComovementSuggestion carrying the +inf UNSCORED_MAE sentinel (review round 1 on
    PR #2106); a row with a finite MAE would be claiming a score nobody computed, so it is
    refused. The sentinel also means the base class's own publish() refuses these rows,
    keeping them out of every MAE-gated path.
  * A failed (fail-closed) outcome never reaches the write path at all (publish_outcome).

The output directory is always the caller's choice. Nothing here defaults to the repo's
``config/`` directory, and nothing in the live DCA paths reads these files.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from comovement_signals.signals import DEFAULT_FLOOR
from comovement_signals.suggester import (
    SIGNAL_CLUSTER_CAP,
    SIGNAL_SELLOFF,
    SOURCE_PREFIX,
    UNSCORED_MAE,
    ComovementOutcome,
    ComovementSuggestion,
)
from dca_shared_interfaces.forecasting import SizingSuggestion

# Reusing the shared lock keeps one locking strategy for every suggestion file.
from dca_shared_interfaces.publishing import PublishResult, SuggestionPublisher, _acquire_lock

CLUSTER_CAP_FILENAME = "comovement_cluster_cap_suggestions.json"
SELLOFF_FILENAME = "comovement_correlated_selloff_suggestions.json"
SIGNAL_FILENAMES = {SIGNAL_CLUSTER_CAP: CLUSTER_CAP_FILENAME, SIGNAL_SELLOFF: SELLOFF_FILENAME}
SCHEMA = "comovement_suggestions/v1"
# Float slack for the floor comparison only; a multiplier above 1.0 is never tolerated.
_BOUND_EPS = 1e-9


class ComovementSuggestionPublisher(SuggestionPublisher):
    """Publishes one signal's daily batch to that signal's own file.

    Args:
        output_path: must be named exactly SIGNAL_FILENAMES[signal].
        signal: "cluster_cap" or "correlated_selloff".
        floor: the lowest multiplier accepted.

    Raises:
        ValueError: for an unknown signal or any other file name.
    """

    def __init__(self, output_path: Path, signal: str, floor: float = DEFAULT_FLOOR) -> None:
        if signal not in SIGNAL_FILENAMES:
            raise ValueError(f"unknown co-movement signal {signal!r}")
        output_path = Path(output_path)
        if output_path.name != SIGNAL_FILENAMES[signal]:
            raise ValueError(f"{signal} publishes only to {SIGNAL_FILENAMES[signal]}, not {output_path.name}")
        super().__init__(output_path)
        self.signal = signal
        self._floor = floor
        self._source = SOURCE_PREFIX + signal

    def validate_bounds(self, suggestion: SizingSuggestion) -> bool:
        pct = suggestion.multiplier_adjustment_pct
        # Copilot review on PR #2106: bool subclasses int, so False would pass as a 0%
        # adjustment and land in the file as a JSON boolean. Reject it like the inputs do.
        if isinstance(pct, bool) or not (isinstance(pct, (int, float)) and math.isfinite(pct)):
            return False
        m = 1.0 + pct / 100.0
        return (
            bool(suggestion.ticker)
            and suggestion.source == self._source
            and self._floor - _BOUND_EPS <= m <= 1.0
        )

    def publish(self, suggestion: SizingSuggestion) -> PublishResult:
        """Single-suggestion form of publish_batch (a one-row file)."""
        return self.publish_batch([suggestion], suggestion.asof)

    def publish_batch(
        self,
        suggestions: Sequence[SizingSuggestion],
        as_of: date,
        detail: Mapping[str, object] | None = None,
    ) -> PublishResult:
        """Validate the whole batch, then atomically replace this signal's file.

        Nothing is written unless every row passes: a bounded multiplier in
        [floor, 1.0], this signal's source, the batch's as-of date, a unique ticker, and
        an unscored ComovementSuggestion carrying the UNSCORED_MAE sentinel.
        """
        if not suggestions:
            return PublishResult(False, "blocked_invalid_bounds", "empty batch")
        tickers = [s.ticker for s in suggestions]
        if len(set(tickers)) != len(tickers):
            return PublishResult(False, "blocked_invalid_bounds", "duplicate ticker in batch")
        for s in suggestions:
            if not (isinstance(s, ComovementSuggestion) and s.scored is False
                    and s.eval_metric_mae == UNSCORED_MAE):
                return PublishResult(False, "blocked_invalid_bounds",
                                     f"{s.ticker}: not an unscored co-movement suggestion")
            if s.asof != as_of:
                return PublishResult(False, "blocked_invalid_bounds", f"{s.ticker}: asof {s.asof} != {as_of}")
            if not self.validate_bounds(s):
                return PublishResult(False, "blocked_invalid_bounds", f"{s.ticker}: failed bounds/source check")
        try:
            text = json.dumps(self._payload(suggestions, as_of, detail or {}), indent=2, sort_keys=True,
                              allow_nan=False)
        except (TypeError, ValueError) as exc:
            return PublishResult(False, "blocked_invalid_bounds", f"payload not serialisable: {exc}")

        with _acquire_lock(self._lock_path):
            incumbent = self._read_incumbent_asof()
            if incumbent is not None and incumbent > as_of:
                return PublishResult(False, "blocked_stale_asof",
                                     f"{as_of.isoformat()} is older than published {incumbent.isoformat()}")
            tmp_path = self._output_path.with_suffix(self._output_path.suffix + ".tmp")
            tmp_path.write_text(text)
            tmp_path.replace(self._output_path)  # atomic on POSIX and Windows
        return PublishResult(True, "published", "ok")

    def _read_incumbent_asof(self) -> date | None:
        # Same policy as the base class's incumbent read: an unreadable prior file imposes
        # no floor, so a corrupt file can be replaced by the next good run.
        if not self._output_path.exists():
            return None
        try:
            return date.fromisoformat(json.loads(self._output_path.read_bytes())["asof"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _payload(self, suggestions, as_of, detail) -> dict[str, object]:
        rows = sorted(suggestions, key=lambda s: s.ticker)
        return {
            "schema": SCHEMA,
            "signal": self.signal,
            "research_only": True,
            "asof": as_of.isoformat(),
            "generated_at": max(s.generated_at for s in rows).isoformat(),
            "source": self._source,
            "model_version": rows[0].model_version,
            "eval_metric": {"mae": None, "scored": False, "status": "unscored_rule_based"},
            "bounds": {"min_multiplier": self._floor, "max_multiplier": 1.0},
            "suggestions": [
                {"ticker": s.ticker,
                 "multiplier": 1.0 + s.multiplier_adjustment_pct / 100.0,
                 "multiplier_adjustment_pct": s.multiplier_adjustment_pct}
                for s in rows
            ],
            "detail": dict(detail),
        }


def cluster_cap_publisher(directory: Path, floor: float = DEFAULT_FLOOR) -> ComovementSuggestionPublisher:
    return ComovementSuggestionPublisher(Path(directory) / CLUSTER_CAP_FILENAME, SIGNAL_CLUSTER_CAP, floor)


def selloff_publisher(directory: Path, floor: float = DEFAULT_FLOOR) -> ComovementSuggestionPublisher:
    return ComovementSuggestionPublisher(Path(directory) / SELLOFF_FILENAME, SIGNAL_SELLOFF, floor)


def publish_outcome(outcome: ComovementOutcome, publisher: ComovementSuggestionPublisher) -> PublishResult:
    """Publish an evaluation's suggestions, or nothing at all if it failed closed."""
    if outcome.signal != publisher.signal:
        return PublishResult(False, "blocked_wrong_signal",
                             f"{outcome.signal} outcome cannot go to the {publisher.signal} file")
    if not outcome.ok:
        return PublishResult(False, "blocked_fail_closed", outcome.reason)
    return publisher.publish_batch(outcome.suggestions, outcome.as_of, outcome.detail)
