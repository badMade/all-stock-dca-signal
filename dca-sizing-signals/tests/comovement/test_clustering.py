"""Correlation and average-linkage clustering on distance 1 - correlation (ADR question 2)."""

from __future__ import annotations

import pytest

from comovement_signals import (
    average_linkage_clusters,
    build_clusters,
    pairwise_correlations,
    pearson,
    returns_matrix,
)

from ._toy import THREE_BLOCKS, day, rows_from_returns, three_block_rows


def test_pearson_hand_computed():
    # x = 1,2,3 and y = 1,3,2: deviations (-1,0,1) and (-1,1,0); sum of products 1,
    # each sum of squares 2, so r = 1 / sqrt(2 * 2) = 0.5.
    assert pearson([1, 2, 3], [1, 3, 2]) == pytest.approx(0.5)
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_pearson_undefined_for_constant_series():
    assert pearson([1, 1, 1], [1, 2, 3]) is None


def test_pairwise_correlation_needs_minimum_overlap():
    # A has returns on day(1)..day(4); B only on day(3)..day(4): 2 overlapping sessions.
    returns = {"A": {day(i): float(i) for i in range(1, 5)}, "B": {day(3): 1.0, day(4): 2.0}}
    assert pairwise_correlations(returns, ["A", "B"], min_overlap=3)[("A", "B")] is None
    assert pairwise_correlations(returns, ["A", "B"], min_overlap=2)[("A", "B")] == pytest.approx(1.0)


def test_average_linkage_differs_from_single_linkage():
    # d(A,B)=0.2 merges first. Average linkage then compares {A,B} with C at
    # (d(A,C) + d(B,C)) / 2 = (0.45 + 0.65) / 2 = 0.55 > 0.5, so C stays alone.
    # (Single linkage would use min(0.45, 0.65) = 0.45 and wrongly merge C.)
    d = {("A", "B"): 0.2, ("A", "C"): 0.45, ("B", "C"): 0.65}
    assert average_linkage_clusters(["A", "B", "C"], d, cut=0.5) == (("A", "B"), ("C",))


def test_cut_is_inclusive():
    # A merge at exactly the cut height is kept (scipy fcluster 'distance' semantics).
    assert average_linkage_clusters(["A", "B"], {("A", "B"): 0.5}, cut=0.5) == (("A", "B"),)
    assert average_linkage_clusters(["A", "B"], {("A", "B"): 0.5000001}, cut=0.5) == (("A",), ("B",))


def test_tie_broken_by_ticker():
    # d(A,B) = d(B,C) = 0.4 tie. Ties go to the alphabetically first pair, (A,B). Then
    # {A,B} vs C = (0.7 + 0.4) / 2 = 0.55 > 0.5, so the result is {A,B},{C}. Breaking the
    # tie the other way would give {A},{B,C}, so this pins the rule.
    d = {("A", "B"): 0.4, ("B", "C"): 0.4, ("A", "C"): 0.7}
    for order in (["A", "B", "C"], ["C", "B", "A"], ["B", "C", "A"]):
        assert average_linkage_clusters(order, d, cut=0.5) == (("A", "B"), ("C",))


def test_obvious_three_block_distance_matrix():
    # Within-block distances 0.1, across-block distances 0.9: three clusters, whatever
    # order the tickers arrive in.
    tickers = [t for block in THREE_BLOCKS for t in block]
    block_of = {t: i for i, block in enumerate(THREE_BLOCKS) for t in block}
    d = {(a, b): (0.1 if block_of[a] == block_of[b] else 0.9) for a in tickers for b in tickers if a < b}
    assert average_linkage_clusters(tickers, d, cut=0.5) == THREE_BLOCKS
    assert average_linkage_clusters(list(reversed(tickers)), d, cut=0.5) == THREE_BLOCKS


def test_missing_distance_treated_as_one():
    # An unknown pair (no correlation) is distance 1.0: never merged at cut 0.5.
    assert average_linkage_clusters(["A", "B"], {}, cut=0.5) == (("A",), ("B",))


def test_build_clusters_from_three_block_returns_plus_singleton():
    # three_block_rows(): correlation 1 inside each block, 0 across (see _toy.py), so the
    # blocks come back exactly. N1 lists on day(6) (2 returns < the 8-return minimum), so
    # it is a singleton cluster appended in ticker order.
    rows = three_block_rows() + rows_from_returns("N1", [0.01, -0.01], start=6)
    m = returns_matrix(rows, as_of=day(9), window_sessions=252, min_sessions=8)
    clusters = build_clusters(m, cut=0.5, min_overlap=8)
    assert clusters == THREE_BLOCKS + (("N1",),)
