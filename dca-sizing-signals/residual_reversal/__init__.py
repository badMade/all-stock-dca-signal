"""residual_reversal: research-only, budget-neutral sector-ETF residual-reversal suggester.

Spec: docs/adr-residual-reversal-suggester.md. Pure stdlib functions over caller-supplied
close rows (no network, no broker, no repo config), one SizingSuggester, and a publisher
that writes only ``residual_reversal_suggestions.json``. Not wired into any live DCA path.
"""

from residual_reversal.model import (
    OUFit,
    RegressionFit,
    budget_neutral,
    cumulate,
    fit_ou,
    ols,
    raw_multiplier,
    s_score,
)
from residual_reversal.publish import (
    FILENAME,
    ResidualReversalPublisher,
    publish_outcome,
    residual_reversal_publisher,
)
from residual_reversal.suggester import (
    EVENT_KINDS,
    GUARD_REASONS,
    SIGNAL,
    SOURCE,
    UNSCORED_MAE,
    CorporateEvent,
    ResidualReversalConfig,
    ResidualReversalInputs,
    ResidualReversalOutcome,
    ResidualReversalSuggester,
    ResidualReversalSuggestion,
    ResidualReversalUnavailable,
)

__all__ = [
    "EVENT_KINDS",
    "FILENAME",
    "GUARD_REASONS",
    "SIGNAL",
    "SOURCE",
    "UNSCORED_MAE",
    "CorporateEvent",
    "OUFit",
    "RegressionFit",
    "ResidualReversalConfig",
    "ResidualReversalInputs",
    "ResidualReversalOutcome",
    "ResidualReversalPublisher",
    "ResidualReversalSuggester",
    "ResidualReversalSuggestion",
    "ResidualReversalUnavailable",
    "budget_neutral",
    "cumulate",
    "fit_ou",
    "ols",
    "publish_outcome",
    "raw_multiplier",
    "residual_reversal_publisher",
    "s_score",
]
