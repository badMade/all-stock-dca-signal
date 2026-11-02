"""Signal 2: correlated selloff detector (ADR questions 4 and 5)."""

from __future__ import annotations

import pytest

from comovement_signals import correlated_selloff

FIVE = (("A", "B", "C", "D", "E"),)


def test_exactly_sixty_percent_marks():
    # 3 of 5 down at least 2% (A exactly -2%) = 60%, which meets the >= 60% trigger.
    res = correlated_selloff(FIVE, {"A": -0.02, "B": -0.03, "C": -0.05, "D": 0.01, "E": 0.0})
    (marked,) = res.marked_clusters
    assert marked.members == FIVE[0]
    assert marked.scored == 5
    assert marked.down == 3
    assert marked.share_down == pytest.approx(0.6)


def test_just_above_minus_two_percent_does_not_count():
    # A at -1.99% is not "down at least 2%": 2 of 5 = 40% < 60%.
    res = correlated_selloff(FIVE, {"A": -0.0199, "B": -0.03, "C": -0.05, "D": 0.01, "E": 0.0})
    assert res.marked_clusters == ()


def test_below_sixty_percent_does_not_mark():
    # 4 of 7 = 57.1% < 60%.
    seven = (tuple("ABCDEFG"),)
    returns = {"A": -0.03, "B": -0.03, "C": -0.03, "D": -0.03, "E": 0.0, "F": 0.0, "G": 0.0}
    assert correlated_selloff(seven, returns).marked_clusters == ()


def test_small_cluster_never_marked():
    # Both members of a 2-name cluster down 5%: below the 3-member minimum.
    assert correlated_selloff((("A", "B"),), {"A": -0.05, "B": -0.05}).marked_clusters == ()


def test_unscored_members_do_not_count_toward_minimum():
    # Cluster of 4, but only A and B have a return today: 2 scored < 3, not marked.
    res = correlated_selloff((("A", "B", "C", "D"),), {"A": -0.05, "B": -0.05})
    assert res.marked_clusters == ()


def test_escalation_shared_at_largest_member():
    # Marked cluster {A,B,C}: escalations $10, $20, $30, total $60. The shared budget is the
    # largest single escalation, $30, so each is scaled by 30 / 60 = 0.5: caps $5, $10, $15.
    # D's cluster is not marked, so D gets no cap.
    clusters = (("A", "B", "C"), ("D", "E", "F"))
    returns = {"A": -0.03, "B": -0.04, "C": -0.025, "D": 0.01, "E": 0.0, "F": -0.03}
    res = correlated_selloff(clusters, returns, escalation_usd_by_ticker={"A": 10, "B": 20, "C": 30, "D": 40})
    assert res.escalation_caps_usd == {"A": pytest.approx(5.0), "B": pytest.approx(10.0), "C": pytest.approx(15.0)}
    assert res.marked_clusters[0].escalation_total_usd == pytest.approx(60.0)
    assert res.marked_clusters[0].escalation_budget_usd == pytest.approx(30.0)


def test_single_escalating_member_keeps_full_escalation():
    # Only B escalates ($20): the largest member's escalation is the whole total, cap $20.
    res = correlated_selloff((("A", "B", "C"),), {"A": -0.03, "B": -0.03, "C": -0.03},
                             escalation_usd_by_ticker={"B": 20})
    assert res.escalation_caps_usd == {"B": pytest.approx(20.0)}


def test_universe_flag_at_sixty_percent():
    # 10 scored tickers across clusters and singletons; 6 down >= 2% = 60%: flag. 5 = 50%: no flag.
    clusters = (("A", "B", "C"), ("D",), ("E",), ("F", "G"), ("H",), ("I",), ("J",))
    six_down = {t: (-0.03 if t in "ABCDEF" else 0.0) for t in "ABCDEFGHIJ"}
    res = correlated_selloff(clusters, six_down)
    assert res.universe_flag is True
    assert (res.universe_scored, res.universe_down) == (10, 6)
    five_down = {t: (-0.03 if t in "ABCDE" else 0.0) for t in "ABCDEFGHIJ"}
    assert correlated_selloff(clusters, five_down).universe_flag is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_return_raises(bad):
    with pytest.raises(ValueError):
        correlated_selloff(FIVE, {"A": bad})


def test_negative_escalation_raises():
    with pytest.raises(ValueError):
        correlated_selloff(FIVE, {"A": -0.05}, escalation_usd_by_ticker={"A": -1.0})
