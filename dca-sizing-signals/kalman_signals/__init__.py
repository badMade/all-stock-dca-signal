"""kalman_signals: shadow-only Kalman residual-dip signal (Phase 1, offline).

Accepted design: docs/adr-kalman-residual-dip-shadow.md. A time-varying (alpha, beta) of each
ticker against the market and its co-movement cluster, scored on one session as a
standardized prior innovation and a dip label, written only to its own append-only JSONL
shadow file. Pure functions over caller-supplied rows; no network, no broker, no repo config.

What it must never become without a later ADR (D2, D7): a SizingSuggester. It defines no
suggest() or publish(), imports none of the sizing types, and nothing on the live side imports
it -- not even sizing_adjustment_reader.py. tests/test_architecture_boundaries.py enforces all three.
"""

from kalman_signals.kalman_filter import (
    BetaState,
    BetaStep,
    FilterParams,
    beta_step,
    fit_params,
    predict,
    run_filter,
    steady_state_gain,
)
from kalman_signals.residual_dip import (
    LABEL_CLUSTER,
    LABEL_IDIOSYNCRATIC,
    LABEL_MARKET,
    LABEL_NONE,
    KalmanInputError,
    ResidualDipConfig,
    ResidualDipInputs,
    residual_dip_shadow,
)
from kalman_signals.shadow import (
    SHADOW_FILENAME,
    ShadowRecord,
    ShadowResult,
    ShadowWriter,
    WriteResult,
    write_shadow_result,
)

__all__ = [
    "LABEL_CLUSTER", "LABEL_IDIOSYNCRATIC", "LABEL_MARKET", "LABEL_NONE", "SHADOW_FILENAME",
    "BetaState", "BetaStep", "FilterParams", "KalmanInputError", "ResidualDipConfig", "ResidualDipInputs",
    "ShadowRecord", "ShadowResult", "ShadowWriter", "WriteResult",
    "beta_step", "fit_params", "predict", "residual_dip_shadow", "run_filter", "steady_state_gain",
    "write_shadow_result",
]
