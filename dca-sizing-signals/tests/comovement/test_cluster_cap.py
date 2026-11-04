"""Signal 1: cluster concentration cap (ADR question 3: no cluster above 35% of the day's budget)."""

from __future__ import annotations

import pytest

from comovement_signals import cluster_cap_suggestions

CLUSTERS = (("A", "B", "C"), ("D",), ("E", "F"))


def test_over_cap_cluster_scaled_to_cap():
    # Budget $200, cap 35% = $70. Cluster {A,B,C} proposes 40 + 30 + 30 = $100 > $70, so
    # every member is scaled by 70 / 100 = 0.7 (A $28, B $21, C $21, total $70).
    # {E,F} proposes 20 + 10 = $30 <= $70 and singleton D is exempt: both untouched.
    res = cluster_cap_suggestions(CLUSTERS, {"A": 40, "B": 30, "C": 30, "D": 50, "E": 20, "F": 10}, 200.0)
    assert res.cap_usd == pytest.approx(70.0)
    assert set(res.multipliers) == {"A", "B", "C"}
    for t in ("A", "B", "C"):
        assert res.multipliers[t] == pytest.approx(0.7)
    (capped,) = res.clusters
    assert capped.members == ("A", "B", "C")
    assert capped.proposed_usd == pytest.approx(100.0)
    assert capped.capped_usd == pytest.approx(70.0)
    assert capped.floor_bound is False


def test_cluster_exactly_at_cap_untouched():
    # $70 is not above the $70 cap.
    res = cluster_cap_suggestions(CLUSTERS, {"A": 30, "B": 20, "C": 20}, 200.0)
    assert res.multipliers == {}
    assert res.clusters == ()


def test_floor_binds():
    # {A,B,C} proposes $400 against a $70 cap: 70 / 400 = 0.175 is below the 0.25 floor, so
    # the multiplier is 0.25 and the cluster still spends 0.25 * 400 = $100 (> cap, flagged).
    res = cluster_cap_suggestions(CLUSTERS, {"A": 200, "B": 100, "C": 100}, 200.0, floor=0.25)
    assert res.multipliers == {"A": pytest.approx(0.25), "B": pytest.approx(0.25), "C": pytest.approx(0.25)}
    assert res.clusters[0].floor_bound is True
    assert res.clusters[0].capped_usd == pytest.approx(100.0)


def test_multiplier_never_above_one_or_below_floor():
    res = cluster_cap_suggestions(CLUSTERS, {"A": 1000, "B": 1000, "C": 1, "E": 500, "F": 500}, 100.0)
    assert res.multipliers
    assert all(0.25 <= m <= 1.0 for m in res.multipliers.values())


def test_singleton_exempt_and_unknown_ticker_is_singleton():
    # D alone proposes $150 (75% of $200) and X is in no cluster: both are singletons, which
    # carry no co-movement information, so neither is capped.
    res = cluster_cap_suggestions(CLUSTERS, {"D": 150, "X": 150}, 200.0)
    assert res.multipliers == {}


def test_partial_cluster_only_proposed_members_scaled():
    # Only A and B are buying: 50 + 30 = $80 > $70, so 70 / 80 = 0.875. C is not buying and
    # gets no entry.
    res = cluster_cap_suggestions(CLUSTERS, {"A": 50, "B": 30}, 200.0)
    assert res.multipliers == {"A": pytest.approx(0.875), "B": pytest.approx(0.875)}


def test_custom_cap_share():
    # cap_share 0.5 of $200 = $100; {A,B,C} at $100 is not above it.
    assert cluster_cap_suggestions(CLUSTERS, {"A": 40, "B": 30, "C": 30}, 200.0, cap_share=0.5).multipliers == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"day_budget_usd": 0.0},
        {"day_budget_usd": float("nan")},
        {"cap_share": 0.0},
        {"cap_share": 1.5},
        {"floor": 0.0},
        {"floor": 1.1},
    ],
)
def test_invalid_parameters_raise(kwargs):
    args = {"clusters": CLUSTERS, "proposed_buys_usd_by_ticker": {"A": 10}, "day_budget_usd": 100.0}
    args.update(kwargs)
    with pytest.raises(ValueError):
        cluster_cap_suggestions(**args)


@pytest.mark.parametrize("amount", [-1.0, float("inf"), float("nan")])
def test_invalid_buy_amount_raises(amount):
    with pytest.raises(ValueError):
        cluster_cap_suggestions(CLUSTERS, {"A": amount}, 100.0)


def test_ticker_in_two_clusters_raises():
    with pytest.raises(ValueError):
        cluster_cap_suggestions((("A", "B"), ("B", "C")), {"A": 10}, 100.0)
