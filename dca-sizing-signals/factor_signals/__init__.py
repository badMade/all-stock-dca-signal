"""factor_signals: research-only factor (APT) decomposition for the graded DCA strategy.

Spec: docs/adr-factor-residual-dip-signals.md. Built so far: F1 (point-in-time,
Blume-shrunk market and orthogonalized-sector betas) and F2 (the read-only factor exposure
report). Neither is a SizingSuggester; F3 (factor-share dampener) and F5 (residual dip
z-score) will be, in shadow only. Pure functions over caller-supplied close rows: no
network, no broker, no repo config. Not wired into any live DCA path.
"""

from factor_signals.betas import (
    BLUME_INTERCEPT,
    BLUME_SLOPE,
    DEFAULT_MARKET,
    DEFAULT_MAX_STALENESS_DAYS,
    DEFAULT_MIN_SESSIONS,
    DEFAULT_WINDOW_SESSIONS,
    MODEL_VERSION,
    FactorInputError,
    FactorModel,
    SectorFit,
    SectorMap,
    TickerBetas,
    fit_factor_model,
)
from factor_signals.exposure import (
    EXPOSURE_REPORT_FILENAME,
    MARKET_ONLY,
    exposure_report,
    write_exposure_report,
)

__all__ = [
    "BLUME_INTERCEPT",
    "BLUME_SLOPE",
    "DEFAULT_MARKET",
    "DEFAULT_MAX_STALENESS_DAYS",
    "DEFAULT_MIN_SESSIONS",
    "DEFAULT_WINDOW_SESSIONS",
    "EXPOSURE_REPORT_FILENAME",
    "MARKET_ONLY",
    "MODEL_VERSION",
    "FactorInputError",
    "FactorModel",
    "SectorFit",
    "SectorMap",
    "TickerBetas",
    "exposure_report",
    "fit_factor_model",
    "write_exposure_report",
]
