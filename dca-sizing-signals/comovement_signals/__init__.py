"""comovement_signals: research-only co-movement signals 1 and 2 (suggest-only) and 3 to 5
(shadow / log only, never suggesters).

Spec: docs/adr-comovement-signals.md. Pure functions over caller-supplied rows (no
network, no broker, no repo config), SizingSuggester wrappers, and publishers that write
only this package's own two suggestion files. Signals 3 (pair breakdown), 4 (cheaper-leg
tilt, speculative) and 5 (rotation log) are plain functions whose records go only to
their own append-only shadow files via shadow.ShadowWriter. Not wired into any live DCA
path.
"""

from comovement_signals.cheaper_leg_tilt import (
    HedgeFit,
    TiltConfig,
    cheaper_leg_tilt_shadow,
    ols_hedge,
    tilt_weight,
)
from comovement_signals.clustering import (
    average_linkage_clusters,
    build_clusters,
    pairwise_correlations,
    pearson,
)
from comovement_signals.pair_breakdown import (
    BreakdownCheck,
    PairBreakdownConfig,
    breakdown_run,
    pair_breakdown_shadow,
    percentile,
    rolling_correlations,
)
from comovement_signals.publish import (
    CLUSTER_CAP_FILENAME,
    SELLOFF_FILENAME,
    ComovementSuggestionPublisher,
    cluster_cap_publisher,
    publish_outcome,
    selloff_publisher,
)
from comovement_signals.returns import ComovementInputError, ReturnsMatrix, returns_matrix
from comovement_signals.rotation_log import RotationConfig, regime_value, rotation_log_shadow
from comovement_signals.shadow import (
    SHADOW_FILENAMES,
    SIGNAL_CHEAPER_LEG_TILT,
    SIGNAL_PAIR_BREAKDOWN,
    SIGNAL_ROTATION_LOG,
    ShadowRecord,
    ShadowResult,
    ShadowWriter,
    WriteResult,
    content_hash,
    validate_shadow_filename,
    write_shadow_result,
)
from comovement_signals.signals import (
    CappedCluster,
    ClusterCapResult,
    SelloffCluster,
    SelloffResult,
    cluster_cap_suggestions,
    correlated_selloff,
)
from comovement_signals.suggester import (
    ClusterCapSuggester,
    ComovementConfig,
    ComovementInputs,
    ComovementOutcome,
    ComovementSuggestion,
    ComovementUnavailable,
    CorrelatedSelloffSuggester,
    UNSCORED_MAE,
)

__all__ = [
    "BreakdownCheck",
    "CLUSTER_CAP_FILENAME",
    "CappedCluster",
    "ClusterCapResult",
    "ClusterCapSuggester",
    "ComovementConfig",
    "ComovementInputError",
    "ComovementInputs",
    "ComovementOutcome",
    "ComovementSuggestion",
    "ComovementSuggestionPublisher",
    "ComovementUnavailable",
    "CorrelatedSelloffSuggester",
    "HedgeFit",
    "PairBreakdownConfig",
    "ReturnsMatrix",
    "RotationConfig",
    "SELLOFF_FILENAME",
    "SHADOW_FILENAMES",
    "SIGNAL_CHEAPER_LEG_TILT",
    "SIGNAL_PAIR_BREAKDOWN",
    "SIGNAL_ROTATION_LOG",
    "SelloffCluster",
    "SelloffResult",
    "ShadowRecord",
    "ShadowResult",
    "ShadowWriter",
    "TiltConfig",
    "WriteResult",
    "UNSCORED_MAE",
    "average_linkage_clusters",
    "breakdown_run",
    "build_clusters",
    "cheaper_leg_tilt_shadow",
    "cluster_cap_publisher",
    "cluster_cap_suggestions",
    "content_hash",
    "correlated_selloff",
    "ols_hedge",
    "pair_breakdown_shadow",
    "pairwise_correlations",
    "pearson",
    "percentile",
    "publish_outcome",
    "regime_value",
    "returns_matrix",
    "rolling_correlations",
    "rotation_log_shadow",
    "selloff_publisher",
    "tilt_weight",
    "validate_shadow_filename",
    "write_shadow_result",
]
