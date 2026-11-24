"""Publishing inventory-reservation suggestions to this package's OWN file only.

Same shape and reasoning as comovement_signals/publish.py (which this package may not
import -- independence holds both ways): one file holding one slot's per-ticker batch, a
fixed file name the publisher refuses to be pointed away from, no MAE gate (the rule is
unscored) replaced by a stale-as-of guard, every row an unscored InventorySuggestion
carrying the +inf sentinel, and a failed outcome never reaching the write path.

Shadow mode by construction: nothing in the live DCA paths reads this file, and
sizing_adjustment_reader.py reads single-ticker files only, so it could not consume this
batch file even if pointed at it. Consumption needs its own change and review after the
validation plan in docs/inventory_reservation_suggester.md passes.

The output directory is always the caller's choice. Nothing here defaults to ``config/``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from dca_shared_interfaces.forecasting import SizingSuggestion

# Reusing the shared lock keeps one locking strategy for every suggestion file.
from dca_shared_interfaces.publishing import PublishResult, SuggestionPublisher, _acquire_lock
from inventory_signals.reservation import DEFAULT_M_MIN
from inventory_signals.suggester import SOURCE, UNSCORED_MAE, InventoryOutcome, InventorySuggestion

FILENAME = "inventory_reservation_suggestions.json"
SCHEMA = "inventory_reservation_suggestions/v1"
_BOUND_EPS = 1e-9


class InventoryReservationPublisher(SuggestionPublisher):
    """Publishes one slot's batch to ``inventory_reservation_suggestions.json``.

    Args:
        output_path: must be named exactly FILENAME.
        m_min: the lowest multiplier accepted (the suggester's floor).

    Raises:
        ValueError: for any other file name.
    """

    def __init__(self, output_path: Path, m_min: float = DEFAULT_M_MIN) -> None:
        output_path = Path(output_path)
        if output_path.name != FILENAME:
            raise ValueError(f"inventory reservation publishes only to {FILENAME}, not {output_path.name}")
        super().__init__(output_path)
        self._m_min = m_min

    def validate_bounds(self, suggestion: SizingSuggestion) -> bool:
        pct = suggestion.multiplier_adjustment_pct
        if isinstance(pct, bool) or not (isinstance(pct, (int, float)) and math.isfinite(pct)):
            return False
        m = 1.0 + pct / 100.0
        if not (bool(suggestion.ticker) and suggestion.source == SOURCE and self._m_min - _BOUND_EPS <= m <= 1.0):
            return False
        if isinstance(suggestion, InventorySuggestion):
            p_max = suggestion.max_buy_price
            if p_max is not None and not (math.isfinite(p_max) and p_max > 0.0):
                return False
            if not (math.isfinite(suggestion.discount) and suggestion.discount >= 0.0):
                return False
        return True

    def publish(self, suggestion: SizingSuggestion) -> PublishResult:
        """Single-suggestion form of publish_batch (a one-row file)."""
        slot = suggestion.slot if isinstance(suggestion, InventorySuggestion) else ""
        return self.publish_batch([suggestion], suggestion.asof, slot)

    def publish_batch(
        self,
        suggestions: Sequence[SizingSuggestion],
        as_of: date,
        slot: str,
        detail: Mapping[str, object] | None = None,
    ) -> PublishResult:
        """Validate the whole batch, then atomically replace the file. Nothing is written
        unless every row passes."""
        if not suggestions:
            return PublishResult(False, "blocked_invalid_bounds", "empty batch")
        tickers = [s.ticker for s in suggestions]
        if len(set(tickers)) != len(tickers):
            return PublishResult(False, "blocked_invalid_bounds", "duplicate ticker in batch")
        for s in suggestions:
            if not (isinstance(s, InventorySuggestion) and s.scored is False and s.eval_metric_mae == UNSCORED_MAE):
                return PublishResult(False, "blocked_invalid_bounds",
                                     f"{s.ticker}: not an unscored inventory suggestion")
            if s.asof != as_of or s.slot != slot:
                return PublishResult(False, "blocked_invalid_bounds",
                                     f"{s.ticker}: asof/slot {s.asof}/{s.slot} != {as_of}/{slot}")
            if not self.validate_bounds(s):
                return PublishResult(False, "blocked_invalid_bounds", f"{s.ticker}: failed bounds/source check")
        try:
            text = json.dumps(self._payload(suggestions, as_of, slot, detail or {}), indent=2, sort_keys=True,
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
        # An unreadable prior file imposes no floor, so a corrupt file can be replaced.
        if not self._output_path.exists():
            return None
        try:
            return date.fromisoformat(json.loads(self._output_path.read_bytes())["asof"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _payload(self, suggestions, as_of, slot, detail) -> dict[str, object]:
        rows = sorted(suggestions, key=lambda s: s.ticker)
        return {
            "schema": SCHEMA,
            "research_only": True,
            "shadow_mode": True,
            "asof": as_of.isoformat(),
            "slot": slot,
            "generated_at": max(s.generated_at for s in rows).isoformat(),
            "source": SOURCE,
            "model_version": rows[0].model_version,
            "eval_metric": {"mae": None, "scored": False, "status": "unscored_rule_based"},
            "bounds": {"min_multiplier": self._m_min, "max_multiplier": 1.0},
            "suggestions": [
                {"ticker": s.ticker,
                 "multiplier": 1.0 + s.multiplier_adjustment_pct / 100.0,
                 "multiplier_adjustment_pct": s.multiplier_adjustment_pct,
                 "q": s.q,
                 "discount": s.discount,
                 "remaining_variance": s.remaining_variance,
                 "max_buy_price": s.max_buy_price}
                for s in rows
            ],
            "detail": dict(detail),
        }


def inventory_publisher(directory: Path, m_min: float = DEFAULT_M_MIN) -> InventoryReservationPublisher:
    return InventoryReservationPublisher(Path(directory) / FILENAME, m_min)


def publish_outcome(outcome: InventoryOutcome, publisher: InventoryReservationPublisher) -> PublishResult:
    """Publish an evaluation's suggestions, or nothing at all if it failed closed."""
    if not outcome.ok:
        return PublishResult(False, "blocked_fail_closed", outcome.reason)
    return publisher.publish_batch(outcome.suggestions, outcome.as_of, outcome.slot, outcome.detail)
