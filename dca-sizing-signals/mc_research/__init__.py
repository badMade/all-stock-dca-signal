"""mc_research: research-only Monte Carlo harness with variance reduction (measurement, not edge).

Spec: docs/mc_variance_reduction.md. Seeded named streams (``rng``), a path-first generator so
every strategy sees the same paths (``paths``), estimators that always return mean/SE/CI/n/n_eff
(``estimators``: plain, antithetic, control variate, CRN paired difference, importance sampling
with an ESS floor), Black-Scholes oracles (``options``), and flat-vs-graded DCA runners scored on
terminal wealth for equal contributions (``strategies``).

Stdlib only. Never imported by the live engine, ``sizing_adjustment_reader.py`` or any suggester,
and it imports none of them (tests/test_architecture_boundaries.py). Nothing here writes sizing,
config, or orders: outputs are numbers for a research report.
"""

from mc_research.estimators import (
    Estimate,
    InsufficientEffectiveSampleSize,
    antithetic,
    antithetic_pair_correlation,
    control_variate,
    effective_sample_size,
    importance_sampling,
    independent_difference_se,
    paired_control_variate,
    paired_difference,
    plain,
)
from mc_research.options import black_scholes_call, black_scholes_put
from mc_research.paths import PathSet, gbm_paths
from mc_research.rng import RandomStreams, derive_seed
from mc_research.strategies import (
    Comparison,
    LadderVariant,
    compare,
    flat_dca,
    flat_dca_gbm_mean,
    graded_dca,
    run_strategies,
    stepped_multiplier,
)

__all__ = [
    "Comparison",
    "Estimate",
    "InsufficientEffectiveSampleSize",
    "LadderVariant",
    "PathSet",
    "RandomStreams",
    "antithetic",
    "antithetic_pair_correlation",
    "black_scholes_call",
    "black_scholes_put",
    "compare",
    "control_variate",
    "derive_seed",
    "effective_sample_size",
    "flat_dca",
    "flat_dca_gbm_mean",
    "gbm_paths",
    "graded_dca",
    "importance_sampling",
    "independent_difference_se",
    "paired_control_variate",
    "paired_difference",
    "plain",
    "run_strategies",
    "stepped_multiplier",
]
