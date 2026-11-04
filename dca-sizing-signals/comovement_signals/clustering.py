"""Correlation and average-linkage hierarchical clustering, in pure Python.

ADR question 2: average-linkage (UPGMA) clustering on distance 1 - correlation, cut at
distance 0.5 (about correlation 0.5). For the 91-ticker universe a naive O(n^3)
agglomeration is a few hundred thousand float operations, so no scipy/numpy dependency
is justified (dca-sizing-signals core stays stdlib only).

Determinism: every tie is broken by ticker. Each cluster is identified by its sorted
member tuple, and among equal-distance candidate merges the pair whose (first member,
first member) is alphabetically smallest wins, so the output never depends on input or
dict order.

Note on the distance: the 2026-11-02 research clustered on sqrt((1 - rho) / 2). The ADR
chose 1 - rho with a 0.5 cut, which is the same ordering of pairs but a different scale,
so this module follows the ADR, not the research script.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date

from comovement_signals.returns import DEFAULT_MIN_SESSIONS, ReturnsMatrix

DEFAULT_CUT = 0.5
# A pair with no usable correlation (too little overlap, or a constant series) is treated
# as uncorrelated: distance 1.0. That never merges two tickers at the 0.5 cut on its own,
# which is the conservative reading of "no evidence they move together".
UNKNOWN_DISTANCE = 1.0

Cluster = tuple[str, ...]


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation of two equal-length sequences, or None if undefined.

    Undefined means fewer than two points or a zero-variance series.
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError("pearson() needs equal-length sequences")
    if n < 2:
        return None
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sxx = math.fsum(v * v for v in dx)
    syy = math.fsum(v * v for v in dy)
    if sxx == 0.0 or syy == 0.0:
        return None
    r = math.fsum(a * b for a, b in zip(dx, dy, strict=True)) / math.sqrt(sxx * syy)
    # Round-off can push |r| a hair past 1; clamp so 1 - r stays in [0, 2].
    return max(-1.0, min(1.0, r))


def pairwise_correlations(
    returns: Mapping[str, Mapping[date, float]],
    tickers: Iterable[str],
    min_overlap: int = DEFAULT_MIN_SESSIONS,
) -> dict[tuple[str, str], float | None]:
    """Correlation for every pair (a, b) with a < b, over the sessions both have.

    Returns None for a pair with fewer than ``min_overlap`` shared sessions or an
    undefined correlation.
    """
    names = sorted(set(tickers))
    out: dict[tuple[str, str], float | None] = {}
    for i, a in enumerate(names):
        ra = returns.get(a, {})
        for b in names[i + 1:]:
            rb = returns.get(b, {})
            shared = sorted(set(ra) & set(rb))
            if len(shared) < min_overlap:
                out[(a, b)] = None
                continue
            out[(a, b)] = pearson([ra[d] for d in shared], [rb[d] for d in shared])
    return out


def _pair_distance(distances: Mapping[tuple[str, str], float], a: str, b: str) -> float:
    d = distances.get((a, b) if a < b else (b, a))
    if d is None or not math.isfinite(d):
        return UNKNOWN_DISTANCE
    return d


def average_linkage_clusters(
    tickers: Iterable[str],
    distances: Mapping[tuple[str, str], float],
    cut: float = DEFAULT_CUT,
) -> tuple[Cluster, ...]:
    """Agglomerate with average linkage and stop at the cut.

    Average linkage has no inversions (merge heights never decrease), so "keep merging
    while the closest pair of clusters is within ``cut``" is the same as cutting the full
    dendrogram at ``cut``. The comparison is inclusive (<=), matching scipy's
    ``fcluster(..., criterion="distance")``.

    Args:
        tickers: the tickers to cluster (duplicates are collapsed).
        distances: distance per (a, b) pair with a < b; a missing or non-finite entry
            counts as UNKNOWN_DISTANCE.
        cut: the dendrogram cut height.

    Returns:
        Clusters as sorted member tuples, ordered by first member.
    """
    clusters: list[Cluster] = [(t,) for t in sorted(set(tickers))]
    # Sum of member-pair distances between two clusters; the average is sum / (|A| * |B|).
    # Keyed by the two clusters' sorted-tuple identities.
    link: dict[tuple[Cluster, Cluster], float] = {}
    for i, a in enumerate(clusters):
        for b in clusters[i + 1:]:
            link[(a, b)] = _pair_distance(distances, a[0], b[0])

    while len(clusters) > 1:
        best: tuple[float, Cluster, Cluster] | None = None
        for i, a in enumerate(clusters):
            for b in clusters[i + 1:]:
                avg = link[(a, b)] / (len(a) * len(b))
                # clusters is kept sorted, so (a, b) iterates in ticker order and a strict
                # "<" keeps the alphabetically first pair on a tie.
                if best is None or avg < best[0]:
                    best = (avg, a, b)
        assert best is not None
        avg, a, b = best
        if avg > cut:
            break
        merged = tuple(sorted(a + b))
        remaining = [c for c in clusters if c not in (a, b)]
        for c in remaining:
            total = link[_key(a, c)] + link[_key(b, c)]
            link[_key(merged, c)] = total
        clusters = sorted(remaining + [merged])
    return tuple(clusters)


def _key(x: Cluster, y: Cluster) -> tuple[Cluster, Cluster]:
    return (x, y) if x < y else (y, x)


def build_clusters(
    matrix: ReturnsMatrix,
    cut: float = DEFAULT_CUT,
    min_overlap: int = DEFAULT_MIN_SESSIONS,
) -> tuple[Cluster, ...]:
    """Cluster ``matrix.eligible`` on 1 - correlation and append each short-history
    ticker as its own singleton cluster.

    Returns:
        Eligible clusters ordered by first member, then singletons in ticker order.
    """
    corr = pairwise_correlations(matrix.returns, matrix.eligible, min_overlap=min_overlap)
    distances = {pair: 1.0 - r for pair, r in corr.items() if r is not None}
    clustered = average_linkage_clusters(matrix.eligible, distances, cut=cut)
    return clustered + tuple((t,) for t in matrix.singletons)
