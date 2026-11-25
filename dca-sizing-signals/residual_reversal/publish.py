"""Publishing residual-reversal suggestions to this suggester's own file only.

Mirrors comovement_signals/publish.py (which this package may not import): the shared
SuggestionPublisher base class, its lock and tmp-then-replace write, one file holding
the day's batch, a stale-as-of guard in place of the MAE regression gate (the rule is
unscored), and ``"mae": null, "scored": false`` in the file. Two differences:

  * the multiplier range is [min, max] around 1.0 (default [0.75, 1.25]) rather than
    capped at 1.0, because this is a tilt, not a cap;
  * the batch must be budget neutral: sum(proposed_usd * multiplier) must equal
    sum(proposed_usd) to within a relative 1e-6. A batch that would add or remove
    dollars is refused even if every row is in bounds.

The directory is always the caller's choice; nothing defaults to the repo's ``config/``
and nothing in the live DCA paths reads this file.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from dca_shared_interfaces.forecasting import SizingSuggestion
from dca_shared_interfaces.publishing import PublishResult, SuggestionPublisher, _acquire_lock
from residual_reversal.suggester import (
    SIGNAL,
    SOURCE,
    UNSCORED_MAE,
    ResidualReversalConfig,
    ResidualReversalOutcome,
    ResidualReversalSuggestion,
)

FILENAME = "residual_reversal_suggestions.json"
SCHEMA = "residual_reversal_suggestions/v1"
_BOUND_EPS = 1e-9
_NEUTRALITY_TOLERANCE = 1e-6


class ResidualReversalPublisher(SuggestionPublisher):
    """Publishes the day's batch to ``residual_reversal_suggestions.json``.

    Raises:
        ValueError: if ``output_path`` is named anything else.
    """

    def __init__(self, output_path: Path, min_multiplier: float | None = None,
                 max_multiplier: float | None = None) -> None:
        output_path = Path(output_path)
        if output_path.name != FILENAME:
            raise ValueError(f"{SIGNAL} publishes only to {FILENAME}, not {output_path.name}")
        defaults = ResidualReversalConfig()
        super().__init__(output_path)
        self.signal = SIGNAL
        self._lo = defaults.min_multiplier if min_multiplier is None else min_multiplier
        self._hi = defaults.max_multiplier if max_multiplier is None else max_multiplier

    def validate_bounds(self, suggestion: SizingSuggestion) -> bool:
        pct = suggestion.multiplier_adjustment_pct
        if isinstance(pct, bool) or not (isinstance(pct, (int, float)) and math.isfinite(pct)):
            return False
        m = 1.0 + pct / 100.0
        return (
            bool(suggestion.ticker)
            and suggestion.source == SOURCE
            and self._lo - _BOUND_EPS <= m <= self._hi + _BOUND_EPS
        )

    def publish(self, suggestion: SizingSuggestion) -> PublishResult:
        """Single-row form of publish_batch."""
        return self.publish_batch([suggestion], suggestion.asof)

    def publish_batch(
        self,
        suggestions: Sequence[SizingSuggestion],
        as_of: date,
        detail: Mapping[str, object] | None = None,
    ) -> PublishResult:
        """Validate every row and budget neutrality, then atomically replace the file."""
        if not suggestions:
            return PublishResult(False, "blocked_invalid_bounds", "empty batch")
        tickers = [s.ticker for s in suggestions]
        if len(set(tickers)) != len(tickers):
            return PublishResult(False, "blocked_invalid_bounds", "duplicate ticker in batch")
        for s in suggestions:
            if not (isinstance(s, ResidualReversalSuggestion) and s.scored is False
                    and s.eval_metric_mae == UNSCORED_MAE):
                return PublishResult(False, "blocked_invalid_bounds",
                                     f"{s.ticker}: not an unscored residual-reversal suggestion")
            if s.asof != as_of:
                return PublishResult(False, "blocked_invalid_bounds", f"{s.ticker}: asof {s.asof} != {as_of}")
            if not self.validate_bounds(s):
                return PublishResult(False, "blocked_invalid_bounds", f"{s.ticker}: failed bounds/source check")
            if not (math.isfinite(s.proposed_usd) and s.proposed_usd > 0.0):
                return PublishResult(False, "blocked_invalid_bounds", f"{s.ticker}: proposed_usd must be positive")
        total = math.fsum(s.proposed_usd for s in suggestions)
        tilted = math.fsum(s.proposed_usd * (1.0 + s.multiplier_adjustment_pct / 100.0) for s in suggestions)
        if abs(tilted - total) > _NEUTRALITY_TOLERANCE * total:
            return PublishResult(False, "blocked_not_budget_neutral", f"tilted ${tilted:.6f} vs proposed ${total:.6f}")
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
            tmp_path.replace(self._output_path)
        return PublishResult(True, "published", "ok")

    def _read_incumbent_asof(self) -> date | None:
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
            "signal": SIGNAL,
            "research_only": True,
            "budget_neutral": True,
            "asof": as_of.isoformat(),
            "generated_at": max(s.generated_at for s in rows).isoformat(),
            "source": SOURCE,
            "model_version": rows[0].model_version,
            "eval_metric": {"mae": None, "scored": False, "status": "unscored_rule_based"},
            "bounds": {"min_multiplier": self._lo, "max_multiplier": self._hi},
            "suggestions": [
                {"ticker": s.ticker,
                 "multiplier": 1.0 + s.multiplier_adjustment_pct / 100.0,
                 "multiplier_adjustment_pct": s.multiplier_adjustment_pct,
                 "s_score": s.s_score,
                 "proposed_usd": s.proposed_usd}
                for s in rows
            ],
            "detail": dict(detail),
        }


def residual_reversal_publisher(directory: Path, config: ResidualReversalConfig | None = None
                                ) -> ResidualReversalPublisher:
    cfg = config or ResidualReversalConfig()
    return ResidualReversalPublisher(Path(directory) / FILENAME, cfg.min_multiplier, cfg.max_multiplier)


def publish_outcome(outcome: ResidualReversalOutcome, publisher: ResidualReversalPublisher) -> PublishResult:
    """Publish an evaluation's suggestions, or nothing at all if it failed closed or
    every ticker was guarded."""
    if outcome.signal != publisher.signal:
        return PublishResult(False, "blocked_wrong_signal", f"{outcome.signal} cannot go to the {SIGNAL} file")
    if not outcome.ok:
        return PublishResult(False, "blocked_fail_closed", outcome.reason)
    if not outcome.suggestions:
        return PublishResult(False, "blocked_all_guarded", f"{len(outcome.guarded)} tickers guarded, none active")
    return publisher.publish_batch(outcome.suggestions, outcome.as_of, outcome.detail)
