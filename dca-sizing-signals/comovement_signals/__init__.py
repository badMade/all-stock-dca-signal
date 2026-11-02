"""comovement_signals: research-only, suggest-only co-movement signals 1 and 2.

Spec: docs/adr-comovement-signals.md. Pure functions over caller-supplied rows (no
network, no broker, no repo config), SizingSuggester wrappers, and publishers that write
only this package's own two suggestion files. Not wired into any live DCA path.
"""

from comovement_signals.clustering import (
    average_linkage_clusters,
    build_clusters,
    pairwise_correlations,
    pearson,
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
    "CLUSTER_CAP_FILENAME",
    "SELLOFF_FILENAME",
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
    "ReturnsMatrix",
    "SelloffCluster",
    "SelloffResult",
    "UNSCORED_MAE",
    "average_linkage_clusters",
    "build_clusters",
    "cluster_cap_publisher",
    "cluster_cap_suggestions",
    "correlated_selloff",
    "pairwise_correlations",
    "pearson",
    "publish_outcome",
    "returns_matrix",
    "selloff_publisher",
]
