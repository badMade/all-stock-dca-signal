"""Signals 1 and 2 from docs/adr-comovement-signals.md, as pure functions.

Both take clusters (from clustering.build_clusters) plus plain per-ticker numbers from
the caller, and return frozen results. Neither reads files, prices or the clock, and
neither knows the other exists.

Signal 1, cluster concentration cap (ADR question 3): when one cluster's proposed spend
is above ``cap_share`` of the day's budget, scale that cluster's buys down uniformly to
the cap, never below ``floor`` and never above 1.0.

Signal 2, correlated selloff detector (ADR questions 4 and 5): when at least
``share_down`` of a cluster (3 or more scored members) is down at least 2% on the day,
treat it as one correlated event, so the cluster shares one escalation budget equal to
its single largest member's escalation instead of N independent escalations.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

DEFAULT_CAP_SHARE = 0.35
DEFAULT_FLOOR = 0.25
# A singleton carries no co-movement information (it is either uncorrelated with every
# other name or too new to measure), so the concentration cap applies to clusters of two
# or more. Capping one large single name is a position-size question for the ladder and
# budgets, not for this signal.
DEFAULT_CAP_MIN_CLUSTER_SIZE = 2
DEFAULT_SELLOFF_MIN_CLUSTER_SIZE = 3
DEFAULT_SHARE_DOWN = 0.60
DEFAULT_DROP_THRESHOLD = -0.02


def _finite_nonneg(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v) or v < 0.0:
        raise ValueError(f"{name} must be finite and non-negative, got {value!r}")
    return v


def _cluster_index(clusters: Iterable[Sequence[str]]) -> dict[str, int]:
    """ticker -> cluster position. A ticker in two clusters is a broken partition."""
    index: dict[str, int] = {}
    for i, members in enumerate(clusters):
        for t in members:
            if t in index:
                raise ValueError(f"ticker {t} appears in more than one cluster")
            index[t] = i
    return index


# --------------------------------------------------------------------------- signal 1


@dataclass(frozen=True)
class CappedCluster:
    """One cluster whose proposed spend was above the cap.

    ``floor_bound`` is True when the floor stopped the scale-down short of the cap, so
    ``capped_usd`` is still above ``cap_usd``.
    """

    members: tuple[str, ...]
    proposed_usd: float
    cap_usd: float
    multiplier: float
    capped_usd: float
    floor_bound: bool


@dataclass(frozen=True)
class ClusterCapResult:
    """``multipliers`` holds only the scaled tickers; every other ticker is untouched (1.0)."""

    cap_usd: float
    multipliers: Mapping[str, float]
    clusters: tuple[CappedCluster, ...]


def cluster_cap_suggestions(
    clusters: Sequence[Sequence[str]],
    proposed_buys_usd_by_ticker: Mapping[str, float],
    day_budget_usd: float,
    cap_share: float = DEFAULT_CAP_SHARE,
    floor: float = DEFAULT_FLOOR,
    min_cluster_size: int = DEFAULT_CAP_MIN_CLUSTER_SIZE,
) -> ClusterCapResult:
    """Signal 1: per-ticker scale-downs that bring an over-cap cluster to the cap.

    Args:
        clusters: a partition of tickers (each ticker in at most one cluster). A proposed
            ticker in no cluster is treated as a singleton.
        proposed_buys_usd_by_ticker: the day's proposed dollars per ticker.
        day_budget_usd: the day's budget; the cap is ``cap_share`` of it.
        cap_share: in (0, 1].
        floor: the lowest multiplier ever suggested, in (0, 1].
        min_cluster_size: clusters smaller than this are exempt (see module note).

    Returns:
        ClusterCapResult. A cluster exactly at the cap is not above it and is untouched.

    Raises:
        ValueError: on an invalid parameter, amount or partition.
    """
    budget = _finite_nonneg("day_budget_usd", day_budget_usd)
    if budget <= 0.0:
        raise ValueError("day_budget_usd must be positive")
    if not (math.isfinite(cap_share) and 0.0 < cap_share <= 1.0):
        raise ValueError("cap_share must be in (0, 1]")
    if not (math.isfinite(floor) and 0.0 < floor <= 1.0):
        raise ValueError("floor must be in (0, 1]")
    buys = {t: _finite_nonneg(f"proposed buy for {t}", v) for t, v in proposed_buys_usd_by_ticker.items()}
    _cluster_index(clusters)
    cap = cap_share * budget

    multipliers: dict[str, float] = {}
    capped: list[CappedCluster] = []
    for members in clusters:
        if len(members) < min_cluster_size:
            continue
        buying = sorted(t for t in members if buys.get(t, 0.0) > 0.0)
        spend = math.fsum(buys[t] for t in buying)
        if spend <= cap:
            continue
        raw = cap / spend
        m = min(1.0, max(floor, raw))
        for t in buying:
            multipliers[t] = m
        capped.append(CappedCluster(
            members=tuple(sorted(members)),
            proposed_usd=spend,
            cap_usd=cap,
            multiplier=m,
            capped_usd=spend * m,
            floor_bound=raw < floor,
        ))
    return ClusterCapResult(cap_usd=cap, multipliers=multipliers, clusters=tuple(capped))


# --------------------------------------------------------------------------- signal 2


@dataclass(frozen=True)
class SelloffCluster:
    """One cluster marked as a correlated selloff.

    ``escalation_budget_usd`` is the shared escalation budget (the largest single member
    escalation); ``escalation_total_usd`` is what the members would have escalated
    independently.
    """

    members: tuple[str, ...]
    scored: int
    down: int
    share_down: float
    escalation_total_usd: float
    escalation_budget_usd: float


@dataclass(frozen=True)
class SelloffResult:
    """``escalation_caps_usd`` holds a cap only for escalating members of marked clusters."""

    marked_clusters: tuple[SelloffCluster, ...]
    escalation_caps_usd: Mapping[str, float]
    universe_flag: bool
    universe_scored: int
    universe_down: int


def _day_return(ticker: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"day return for {ticker} must be a finite number, got {value!r}")
    return float(value)


def correlated_selloff(
    clusters: Sequence[Sequence[str]],
    day_returns_by_ticker: Mapping[str, float],
    escalation_usd_by_ticker: Mapping[str, float] | None = None,
    min_cluster_size: int = DEFAULT_SELLOFF_MIN_CLUSTER_SIZE,
    share_down: float = DEFAULT_SHARE_DOWN,
    drop_threshold: float = DEFAULT_DROP_THRESHOLD,
) -> SelloffResult:
    """Signal 2: mark correlated selloffs and share their escalation.

    A member is "scored" when it has a day return, and "down" when that return is at or
    below ``drop_threshold`` (a simple return, -0.02 = down 2%; exactly -2% counts). A
    cluster is marked when it has at least ``min_cluster_size`` scored members and
    down / scored >= share_down (exactly 60% counts; k / n for integers is correctly
    rounded, so 3 / 5 compares equal to 0.60 with no epsilon).

    For a marked cluster the members' escalations are scaled by one common factor so
    their total equals the largest single escalation:
    cap_i = e_i * max(e) / sum(e).

    The universe flag uses the same share over every scored ticker, clustered or not,
    with the same minimum count. It is reported only; it adds no caps.

    Raises:
        ValueError: on a non-finite return, a negative or non-finite escalation, an
            invalid parameter or a broken partition.
    """
    if not (math.isfinite(share_down) and 0.0 < share_down <= 1.0):
        raise ValueError("share_down must be in (0, 1]")
    if not (math.isfinite(drop_threshold) and drop_threshold < 0.0):
        raise ValueError("drop_threshold must be a negative return")
    if min_cluster_size < 1:
        raise ValueError("min_cluster_size must be at least 1")
    returns = {t: _day_return(t, v) for t, v in day_returns_by_ticker.items()}
    escalations = {t: _finite_nonneg(f"escalation for {t}", v) for t, v in (escalation_usd_by_ticker or {}).items()}
    _cluster_index(clusters)

    marked: list[SelloffCluster] = []
    caps: dict[str, float] = {}
    for members in clusters:
        scored = [t for t in members if t in returns]
        if len(scored) < min_cluster_size:
            continue
        down = sum(1 for t in scored if returns[t] <= drop_threshold)
        share = down / len(scored)
        if share < share_down:
            continue
        escalating = sorted(t for t in members if escalations.get(t, 0.0) > 0.0)
        total = math.fsum(escalations[t] for t in escalating)
        budget = max((escalations[t] for t in escalating), default=0.0)
        if total > 0.0:
            scale = budget / total
            for t in escalating:
                caps[t] = escalations[t] * scale
        marked.append(SelloffCluster(
            members=tuple(sorted(members)),
            scored=len(scored),
            down=down,
            share_down=share,
            escalation_total_usd=total,
            escalation_budget_usd=budget,
        ))

    universe_down = sum(1 for r in returns.values() if r <= drop_threshold)
    universe_scored = len(returns)
    universe_flag = universe_scored >= min_cluster_size and universe_down / universe_scored >= share_down
    return SelloffResult(
        marked_clusters=tuple(marked),
        escalation_caps_usd=caps,
        universe_flag=universe_flag,
        universe_scored=universe_scored,
        universe_down=universe_down,
    )
