"""ConnectorSignalSuggester: computes a sizing suggestion from
Robinhood-connector-sourced data (fundamentals, options/IV, index, and
earnings data)
ingested by the market-data-ingest sibling module described in the
integration plan artifact from this conversation.

Boundary rules this file exists to demonstrate:
  * implements dca_shared_interfaces.forecasting.SizingSuggester
    structurally (no inheritance needed -- Protocol conformance is
    checked by shape, not by declared subclassing)
  * never imports near_close_dca, ml_forecaster, or wondrous_octopus
  * has exactly one public method, suggest() -- there is no write(), no
    apply_to_live_config(), nothing that could touch the live engine even
    under a careless future edit, because that capability simply is not
    on this class

market_data_ingest is the read-only sibling module proposed separately;
this file assumes it exists and only imports its reader, never a live
broker connection of its own.
"""

from __future__ import annotations

from datetime import date, datetime

from dca_shared_interfaces.forecasting import SizingSuggestion
from market_data_ingest.snapshot import MarketDataSnapshot, load_asof


class ConnectorSignalSuggester:
    """Structurally satisfies SizingSuggester. Its __init__ takes only a
    path to already-ingested local data -- there is no parameter that
    could carry a live DCA engine reference in, by construction.

    Args:
        market_data_root: path to the versioned, point-in-time-stamped
            files market_data_ingest already wrote.
        model_version: identifier recorded on every suggestion this
            instance produces.
    """

    def __init__(self, market_data_root: str, model_version: str = "connector-v1") -> None:
        self._market_data_root = market_data_root
        self._model_version = model_version

    def suggest(self, ticker: str, asof: date) -> SizingSuggestion:
        """Compute today's sizing suggestion for one ticker.

        Args:
            ticker: the equity symbol.
            asof: the date to compute the suggestion for; only data with
                knowable_asof <= asof is read (see the integration plan's
                point-in-time join discipline).

        Returns:
            A SizingSuggestion. Never writes anything -- persistence is
            SuggestionPublisher's job, not this class's.
        """
        snapshot = load_asof(self._market_data_root, ticker, asof)
        adjustment_pct, mae = self._compute(snapshot)
        return SizingSuggestion(
            ticker=ticker,
            asof=asof,
            multiplier_adjustment_pct=adjustment_pct,
            eval_metric_mae=mae,
            source="connector_signals",
            model_version=self._model_version,
            generated_at=datetime.utcnow(),
        )

    def _compute(self, snapshot: MarketDataSnapshot) -> tuple[float, float]:
        """Plug in the walk-forward-evaluated model from Phase 3/4 of the
        integration plan. snapshot now carries fundamentals, options/IV,
        index, and earnings fields, each independently gated by its own
        *_data_available flag -- check each flag before reading its
        fields, never assume every category is populated for every row.
        Left unimplemented here -- this file is about the boundary, not
        the model."""
        raise NotImplementedError
