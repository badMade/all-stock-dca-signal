"""Research-only dynamic 100-ticker universe selection engine.

Turns a scored candidate pool into a *proposed* quarterly membership change
for the DCA universe -- Nasdaq-100-style hysteresis (enter in the top 75 two
reviews running, stay while in the top 150), sector/industry caps, a
minimum tenure, a per-review swap cap, and hard-fail exits -- then checks
that proposal against fail-closed safeguards.

What this module deliberately is NOT:

- It never edits ``config/tickers.json``, ``near_close_dca.ACTIVE_TICKERS``
  or ``config/scheduled_tasks.json``. Its only output is a
  ``universe_proposal.json``. Applying a passing proposal to the buy list is the job of
  ``universe_apply.apply_proposal`` alone (docs/adr-program-maintained-universe.md); executor
  wiring is still a later, separate step.
- It makes no network calls and reads no files. Every input is passed in
  already computed (prices, EDGAR facts, SIC sectors, prior-review ranks,
  tenure). The single side effect is the explicit ``write_proposal`` helper.
- It never talks to a broker and never sizes or places anything.

Pure stdlib, like ``scoring.py``, so its tests run without the heavy
signal stack installed.

Every threshold below is the design spec's *proposed* default (research
report "Dynamic 100-Ticker Universe for a Buy-Only DCA System",
2026-11-01) and is NOT operator-approved; each one is a named
``UniverseConfig`` / ``PoolThresholds`` field so a different value is a
constructor argument, not a code edit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from scoring import TICKER_SYMBOL_PATTERN

FACTORS: tuple[str, ...] = ("momentum", "profitability", "low_volatility", "value")

DEFAULT_FACTOR_WEIGHTS: Mapping[str, float] = {
    "momentum": 0.25,
    "profitability": 0.25,
    "low_volatility": 0.25,
    "value": 0.25,
}

# Below this many usable values a sector has no spread to normalize against.
# Mirrors scoring._zscore's "fewer than two -> exclude" rule: a lone name in
# its sector gets None (missing) for that factor rather than a fabricated
# 0.0 that would read as "exactly sector-average".
MIN_SECTOR_SIZE = 2

# ``max_swaps`` value meaning "no limit on names changed per review" (strict daily mode). Far above any
# real pool, so neither the exit deferral nor the ``too_many_swaps`` safeguard can ever bind.
UNLIMITED_SWAPS = 10**9

# Same floating-point-noise tolerance scoring._zscore uses: a sector whose
# values differ only by summation noise is treated as zero-dispersion
# instead of producing huge z-scores from a micro-epsilon stdev.
_STDEV_EPSILON = 1e-12

PROPOSAL_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# Value guards
# --------------------------------------------------------------------------


def _finite_number(value: object) -> float | None:
    # bool subclasses int, so True would otherwise slip through as 1.0 --
    # the most frequent malformed-input trap AGENTS.md records for this repo.
    # NaN/inf are rejected too: json.loads accepts both, and a single NaN
    # reaching statistics.pstdev() poisons the whole sector.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _strict_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolThresholds:
    """Candidate-pool eligibility thresholds (spec "Candidate pool").

    ``min_price``/``min_market_cap``/``min_median_dollar_volume_60d`` are
    strict lower bounds (the spec says "> $5", "> $2B", "> $20M");
    ``min_history_sessions`` is inclusive (">= 252 trading days").
    """

    min_price: float = 5.0
    min_market_cap: float = 2e9
    min_median_dollar_volume_60d: float = 20e6
    min_history_sessions: int = 252
    required_security_type: str = "common_stock"


@dataclass(frozen=True)
class UniverseConfig:
    """Every selection and safeguard threshold, defaulting to the spec value.

    Validated on construction: a config that could never be satisfied (a
    floor above the ceiling) or that would invert the hysteresis (an entry
    band wider than the keep band) is refused outright, since a silently
    nonsensical config would yield a proposal that *looks* rule-compliant.
    """

    max_active: int = 100
    min_active: int = 90
    entry_rank: int = 75
    incumbent_keep_rank: int = 150
    sector_cap: int = 25
    industry_cap: int = 10
    min_tenure_quarters: int = 2
    max_swaps: int = 8
    bench_min_rank: int = 101
    bench_max_rank: int = 250
    min_pool_coverage: float = 0.95
    max_quote_age_sessions: int = 3
    # Calendar-day ceiling on the quote date itself (PR #2100 review: a
    # caller-supplied session count of 0 on a months-old quote passed). 7
    # days covers 3 sessions across a weekend plus a holiday or two.
    max_quote_age_calendar_days: int = 7
    max_edgar_age_days: int = 7
    max_factor_missing_fraction: float = 0.10
    winsor_limit: float = 3.0
    # A ticker missing MORE than this many factors is unscored. The spec's
    # "missing >= 2 factors is unscored" is max_missing_factors=1.
    max_missing_factors: int = 1
    # True (the spec default): a non-member enters only if it ranked inside the entry band at the PRIOR
    # review too ("two reviews running"). False: it enters on the first review it ranks inside the band.
    # The strict daily top-N mode (``strict_daily_top``) sets this False.
    entry_requires_prior_review: bool = True
    hard_fail_min_price: float = 5.0
    hard_fail_low_price_sessions: int = 20
    factor_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_FACTOR_WEIGHTS))

    @classmethod
    def strict_daily_top(cls, size: int = 100) -> "UniverseConfig":
        """The operator's daily rule (docs/adr-program-maintained-universe.md, decision item 3).

        The buy-eligible set is each day's ranks 1..``size``: one band (no 75/150 hysteresis), a name
        enters on the first day it ranks inside it, leaves the day it falls out, nothing is
        tenure-protected, the sector and industry caps cannot bind a top-``size`` name, and the number
        of names changed per day is effectively unlimited. Every input safeguard (pool coverage, stale
        data, missing factors) and the active-size floor and cap stay as they are.
        """
        return cls(
            max_active=size,
            min_active=min(90, size),
            entry_rank=size,
            incumbent_keep_rank=size,
            sector_cap=size,
            industry_cap=size,
            min_tenure_quarters=0,
            max_swaps=UNLIMITED_SWAPS,
            entry_requires_prior_review=False,
        )

    def __post_init__(self) -> None:
        if not isinstance(self.entry_requires_prior_review, bool):
            raise ValueError("UniverseConfig.entry_requires_prior_review must be a bool")
        positive_ints = (
            "max_active", "min_active", "entry_rank", "incumbent_keep_rank", "sector_cap", "industry_cap",
            "bench_min_rank", "bench_max_rank", "max_quote_age_sessions", "max_quote_age_calendar_days",
            "max_edgar_age_days",
            "hard_fail_low_price_sessions",
        )
        for name in positive_ints:
            value = _strict_int(getattr(self, name))
            if value is None or value < 1:
                raise ValueError(f"UniverseConfig.{name} must be a positive int, got {getattr(self, name)!r}")
        for name in ("min_tenure_quarters", "max_swaps", "max_missing_factors"):
            value = _strict_int(getattr(self, name))
            if value is None or value < 0:
                raise ValueError(f"UniverseConfig.{name} must be a non-negative int, got {getattr(self, name)!r}")
        for name in ("min_pool_coverage", "max_factor_missing_fraction"):
            value = _finite_number(getattr(self, name))
            if value is None or not 0.0 <= value <= 1.0:
                raise ValueError(f"UniverseConfig.{name} must be a finite fraction in [0, 1]")
        for name in ("winsor_limit", "hard_fail_min_price"):
            value = _finite_number(getattr(self, name))
            if value is None or value <= 0:
                raise ValueError(f"UniverseConfig.{name} must be a finite positive number")
        if self.min_active > self.max_active:
            raise ValueError("UniverseConfig.min_active cannot exceed max_active")
        if self.entry_rank > self.incumbent_keep_rank:
            raise ValueError("UniverseConfig.entry_rank cannot exceed incumbent_keep_rank (inverts the hysteresis)")
        if self.bench_min_rank > self.bench_max_rank:
            raise ValueError("UniverseConfig.bench_min_rank cannot exceed bench_max_rank")
        if set(self.factor_weights) != set(FACTORS):
            raise ValueError(f"UniverseConfig.factor_weights keys must be exactly {FACTORS}")
        for name, weight in self.factor_weights.items():
            value = _finite_number(weight)
            if value is None or value <= 0:
                raise ValueError(f"UniverseConfig.factor_weights[{name!r}] must be a finite positive number")


# --------------------------------------------------------------------------
# Pool filter
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One scan-date candidate. ``None`` means the value was unavailable."""

    ticker: str
    nms_listed: bool | None
    security_type: str | None
    price: float | None
    market_cap: float | None
    median_dollar_volume_60d: float | None
    history_sessions: int | None
    fractional_eligible: bool | None


@dataclass(frozen=True)
class PoolResult:
    eligible: tuple[Candidate, ...]
    rejections: Mapping[str, tuple[str, ...]]


def _candidate_rejections(candidate: Candidate, thresholds: PoolThresholds) -> list[str]:
    reasons: list[str] = []
    if not isinstance(candidate.ticker, str) or not TICKER_SYMBOL_PATTERN.match(candidate.ticker):
        reasons.append("invalid_symbol")
    # An unknown (None) flag is never evidence of eligibility, but it is
    # reported as missing:* rather than as a failed check, so
    # ineligible_tickers() can tell "the data did not arrive" (an incumbent
    # holds) apart from "the ticker fails the floor" (an incumbent leaves the
    # pool) -- PR #2100 review round 1.
    if candidate.nms_listed is None:
        reasons.append("missing:nms_listed")
    elif candidate.nms_listed is not True:
        reasons.append("not_nms_listed")
    if candidate.security_type is None:
        reasons.append("missing:security_type")
    elif candidate.security_type != thresholds.required_security_type:
        reasons.append("not_common_stock")
    numeric_floors = (
        ("price", thresholds.min_price, "price_at_or_below_min"),
        ("market_cap", thresholds.min_market_cap, "market_cap_at_or_below_min"),
        ("median_dollar_volume_60d", thresholds.min_median_dollar_volume_60d, "dollar_volume_at_or_below_min"),
    )
    for attr, floor, code in numeric_floors:
        value = _finite_number(getattr(candidate, attr))
        if value is None:
            reasons.append(f"missing:{attr}")
        elif value <= floor:
            reasons.append(code)
    history = _strict_int(candidate.history_sessions)
    if history is None:
        reasons.append("missing:history_sessions")
    elif history < thresholds.min_history_sessions:
        reasons.append("history_too_short")
    if candidate.fractional_eligible is None:
        reasons.append("missing:fractional_eligible")
    elif candidate.fractional_eligible is not True:
        reasons.append("not_fractional_eligible")
    return reasons


def filter_pool(candidates: Iterable[Candidate], thresholds: PoolThresholds) -> PoolResult:
    """Split candidates into the eligible pool and per-ticker rejection reasons.

    Every failing check is recorded (not just the first) so a reviewer sees
    the whole picture. A symbol that appears more than once is rejected
    outright: two rows for one ticker means the upstream scan is ambiguous,
    and picking either silently would let row order decide eligibility.
    Eligible candidates come back sorted by ticker for determinism.
    """
    candidates = list(candidates)
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.ticker] = counts.get(candidate.ticker, 0) + 1
    eligible: list[Candidate] = []
    rejections: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        if counts[candidate.ticker] > 1:
            rejections[candidate.ticker] = ("duplicate_ticker",)
            continue
        reasons = _candidate_rejections(candidate, thresholds)
        if reasons:
            rejections[candidate.ticker] = tuple(reasons)
        else:
            eligible.append(candidate)
    return PoolResult(eligible=tuple(sorted(eligible, key=lambda c: c.ticker)), rejections=rejections)


def ineligible_tickers(pool: PoolResult) -> dict[str, str]:
    """``{ticker: reasons}`` for tickers that genuinely FAIL a pool floor.

    Only non-``missing:*`` reasons count, and ``duplicate_ticker`` does not
    count either (ambiguous rows are a data problem, not a verdict). The
    result is what ``select_universe(..., ineligible=...)`` expects: an
    incumbent listed here exits, while an incumbent that is merely absent or
    unscored holds. Reasons are joined with "," in filter order.
    """
    out: dict[str, str] = {}
    for ticker, reasons in pool.rejections.items():
        failed = [r for r in reasons if not r.startswith("missing:") and r != "duplicate_ticker"]
        if failed:
            out[ticker] = ",".join(failed)
    return out


# --------------------------------------------------------------------------
# Hard fails
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerStatus:
    """Per-ticker lifecycle facts used for hard-fail exits."""

    ticker: str
    delisted: bool = False
    fractional_eligible: bool | None = True
    sessions_below_min_price: int = 0
    pending_acquisition: bool = False


def detect_hard_fails(statuses: Iterable[TickerStatus], config: UniverseConfig) -> dict[str, str]:
    """Return ``{ticker: reason}`` for every hard-fail exit (spec "Hard-fail exits").

    Only an explicit ``fractional_eligible is False`` is a hard fail: an
    unknown (None) eligibility is missing data, and missing data means HOLD,
    never demote. Multiple reasons are joined with "," in a fixed order.
    """
    out: dict[str, str] = {}
    for status in statuses:
        reasons = []
        if status.delisted is True:
            reasons.append("delisted")
        if status.fractional_eligible is False:
            reasons.append("fractional_ineligible")
        sessions = _strict_int(status.sessions_below_min_price)
        if sessions is not None and sessions >= config.hard_fail_low_price_sessions:
            reasons.append("price_below_min_sustained")
        if status.pending_acquisition is True:
            reasons.append("pending_acquisition")
        if reasons:
            out[status.ticker] = ",".join(reasons)
    return out


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorInputs:
    """Raw point-in-time factor inputs for one eligible ticker.

    The caller is responsible for point-in-time correctness: momentum
    returns already skip the most recent month (12-1 style), and the EDGAR
    facts behind ``profitability`` (gross profits / total assets) and
    ``value`` (earnings yield or book-to-price) are first-filed values with
    ``filed_date <= as_of``, never filtered on period end. ``volatility_1y``
    is the raw 1-year daily-return volatility; it is negated here so that
    lower volatility scores higher.
    """

    ticker: str
    sector: str
    industry: str
    momentum_6m: float | None = None
    momentum_12m: float | None = None
    profitability: float | None = None
    volatility_1y: float | None = None
    value: float | None = None


@dataclass(frozen=True)
class ScoredTicker:
    """One ticker's composite. ``composite is None`` means unscored."""

    ticker: str
    sector: str
    industry: str
    composite: float | None
    factors: Mapping[str, float | None] = field(default_factory=dict)
    missing_factors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PoolStats:
    pool_size: int
    factor_missing_fraction: Mapping[str, float]


def sector_zscores(values: Mapping[str, object], sectors: Mapping[str, str]) -> dict[str, float | None]:
    """Population z-score of each ticker's value WITHIN its own sector.

    Missing, bool and non-finite values come back None and are excluded
    from their sector's mean/stdev. A sector with fewer than
    MIN_SECTOR_SIZE usable values yields None for its names; a sector with
    zero dispersion yields 0.0 (those names genuinely sit at the mean).
    """
    by_sector: dict[str, dict[str, float]] = {}
    for ticker, raw in values.items():
        value = _finite_number(raw)
        if value is not None:
            by_sector.setdefault(sectors[ticker], {})[ticker] = value
    out: dict[str, float | None] = dict.fromkeys(values)
    for members in by_sector.values():
        if len(members) < MIN_SECTOR_SIZE:
            continue
        mean = statistics.fmean(members.values())
        stdev = statistics.pstdev(members.values())
        for ticker, value in members.items():
            out[ticker] = 0.0 if stdev < _STDEV_EPSILON else (value - mean) / stdev
    return out


def winsorize(zscores: Mapping[str, float | None], limit: float) -> dict[str, float | None]:
    """Clip each z-score to ``[-limit, +limit]``; None passes through."""
    return {t: (None if z is None else max(-limit, min(limit, z))) for t, z in zscores.items()}


def _mean_available(values: Iterable[float | None]) -> float | None:
    usable = [v for v in values if v is not None]
    return statistics.fmean(usable) if usable else None


def _negate(value: float | None) -> float | None:
    number = _finite_number(value)
    return None if number is None else -number


def _index_inputs(inputs: Sequence[FactorInputs]) -> dict[str, FactorInputs]:
    indexed: dict[str, FactorInputs] = {}
    for item in inputs:
        # Two rows for one ticker would make the sector statistics depend on
        # which row "wins"; refuse rather than guess.
        if item.ticker in indexed:
            raise ValueError(f"duplicate ticker in factor inputs: {item.ticker!r}")
        indexed[item.ticker] = item
    return indexed


def composite_scores(inputs: Sequence[FactorInputs], config: UniverseConfig) -> dict[str, ScoredTicker]:
    """Four-factor composite: sector-neutral, winsorized, weight-averaged.

    Each raw input is z-scored within sector and winsorized at
    ``config.winsor_limit``. Momentum is the mean of whichever of the 6m/12m
    z-scores exist. The composite is the weighted mean of the available
    factors (weights renormalized over what is present -- the spec's
    "missing factor -> average of available factors"); a ticker missing
    more than ``config.max_missing_factors`` factors is unscored, never
    given a neutral 0 that would rank it as "average".
    """
    indexed = _index_inputs(inputs)
    sectors = {t: item.sector for t, item in indexed.items()}

    def zw(raw: Mapping[str, object]) -> dict[str, float | None]:
        return winsorize(sector_zscores(raw, sectors), config.winsor_limit)

    m6 = zw({t: i.momentum_6m for t, i in indexed.items()})
    m12 = zw({t: i.momentum_12m for t, i in indexed.items()})
    factor_z = {
        "momentum": {t: _mean_available((m6[t], m12[t])) for t in indexed},
        "profitability": zw({t: i.profitability for t, i in indexed.items()}),
        "low_volatility": zw({t: _negate(i.volatility_1y) for t, i in indexed.items()}),
        "value": zw({t: i.value for t, i in indexed.items()}),
    }
    out: dict[str, ScoredTicker] = {}
    for ticker, item in indexed.items():
        factors = {name: factor_z[name][ticker] for name in FACTORS}
        missing = tuple(name for name in FACTORS if factors[name] is None)
        composite: float | None = None
        if len(missing) <= config.max_missing_factors:
            present = [name for name in FACTORS if factors[name] is not None]
            total_weight = sum(config.factor_weights[name] for name in present)
            composite = sum(config.factor_weights[name] * factors[name] for name in present) / total_weight
        out[ticker] = ScoredTicker(
            ticker=ticker,
            sector=item.sector,
            industry=item.industry,
            composite=composite,
            factors=factors,
            missing_factors=missing,
        )
    return out


def compute_pool_stats(inputs: Sequence[FactorInputs]) -> PoolStats:
    """Pool size and the RAW missing fraction per factor (data availability).

    Measured on raw inputs, not z-scores, so the ">10% missing" safeguard
    reflects whether the data feed delivered, not whether a sector happened
    to be too small to normalize. Momentum counts as missing only when both
    horizons are absent, matching how composite_scores uses it.
    """
    indexed = _index_inputs(inputs)
    size = len(indexed)
    if size == 0:
        return PoolStats(pool_size=0, factor_missing_fraction=dict.fromkeys(FACTORS, 1.0))
    missing = dict.fromkeys(FACTORS, 0)
    for item in indexed.values():
        if _finite_number(item.momentum_6m) is None and _finite_number(item.momentum_12m) is None:
            missing["momentum"] += 1
        if _finite_number(item.profitability) is None:
            missing["profitability"] += 1
        if _finite_number(item.volatility_1y) is None:
            missing["low_volatility"] += 1
        if _finite_number(item.value) is None:
            missing["value"] += 1
    return PoolStats(pool_size=size, factor_missing_fraction={name: missing[name] / size for name in FACTORS})


def rank_tickers(scores: Mapping[str, ScoredTicker]) -> dict[str, int]:
    """1-based ranks by composite descending, ties broken by ticker symbol.

    Unscored tickers (composite None or non-finite) get no rank at all.
    Sorting on (-composite, ticker) makes the order independent of mapping
    insertion order, which the proposal hash depends on.
    """
    scored = [(t, _finite_number(s.composite)) for t, s in scores.items()]
    ordered = sorted(((t, c) for t, c in scored if c is not None), key=lambda tc: (-tc[1], tc[0]))
    return {ticker: index + 1 for index, (ticker, _) in enumerate(ordered)}


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Incumbent:
    """A current member. ``tenure_quarters`` = completed quarterly reviews held.

    ``sector``/``industry`` are only a fallback for counting caps when the
    ticker has no score this review (a missing-data hold).
    """

    tenure_quarters: int
    sector: str | None = None
    industry: str | None = None


@dataclass(frozen=True)
class SafeguardFailure:
    code: str
    detail: str


@dataclass(frozen=True)
class Proposal:
    """A proposed membership change. Never an approval.

    ``prior_active`` is the last approved list the proposal was computed
    from; on a safeguard rejection ``recommended_active`` falls back to it
    (spec: "reject whole proposal, keep last approved list").
    """

    review_id: str
    as_of_date: date
    generated_at: datetime
    prior_active: tuple[str, ...]
    active: tuple[str, ...]
    adds: tuple[str, ...]
    drops: tuple[str, ...]
    hard_fail_drops: tuple[str, ...]
    holds: tuple[str, ...]
    bench: tuple[str, ...]
    ranks: Mapping[str, int]
    reasons: Mapping[str, str]
    pool_stats: PoolStats | None
    config: UniverseConfig
    # Evidence validate_proposal re-checks per add (PR #2100 review: a
    # tampered add of a never-ranked symbol passed). ``prior_ranks`` holds
    # each add's prior-review rank; ``classification`` holds sector/industry
    # for every active ticker whose classification is known.
    ineligible_drops: tuple[str, ...] = ()
    prior_ranks: Mapping[str, int] = field(default_factory=dict)
    classification: Mapping[str, Mapping[str, str | None]] = field(default_factory=dict)
    status: str = "unvalidated"
    safeguard_failures: tuple[SafeguardFailure, ...] = ()
    recommended_active: tuple[str, ...] = ()


class _CapCounter:
    def __init__(self, config: UniverseConfig) -> None:
        self._config = config
        self.sectors: dict[str, int] = {}
        self.industries: dict[str, int] = {}

    def blocker(self, sector: str | None, industry: str | None) -> str | None:
        # Sector first: it is the coarser bucket, so when both are full the
        # reason names the constraint that would still bind after an
        # industry-level change.
        if sector is not None and self.sectors.get(sector, 0) >= self._config.sector_cap:
            return "sector"
        if industry is not None and self.industries.get(industry, 0) >= self._config.industry_cap:
            return "industry"
        return None

    def add(self, sector: str | None, industry: str | None) -> None:
        # An unknown sector/industry (a missing-data hold with no fallback)
        # cannot be counted; it still occupies a seat, which the size check
        # covers.
        if sector is not None:
            self.sectors[sector] = self.sectors.get(sector, 0) + 1
        if industry is not None:
            self.industries[industry] = self.industries.get(industry, 0) + 1


def select_universe(
    scores_now: Mapping[str, ScoredTicker],
    ranks_prior_review: Mapping[str, int],
    incumbents_with_tenure: Mapping[str, Incumbent],
    hard_fails: Mapping[str, str],
    config: UniverseConfig,
    *,
    review_id: str,
    as_of_date: date,
    generated_at: datetime,
    pool_stats: PoolStats | None = None,
    ineligible: Mapping[str, str] | None = None,
) -> Proposal:
    """Apply the spec's selection rule and return an unvalidated Proposal.

    ``ineligible`` maps tickers that genuinely fail a pool floor to the
    reason (see ``ineligible_tickers``). It is deliberately separate from
    "absent from ``scores_now``": absent means missing data (hold), listed
    here means the ticker left the pool (exit).

    Order of operations (each step's reason code lands in ``reasons``):

    0. Hard-failed and pool-ineligible tickers are removed BEFORE ranking,
       so they never occupy a rank slot and shift the 75/150 edges.
    1. Hard-fail incumbents exit unconditionally -- rank and tenure do not
       protect them, and these exits do not use the swap budget.
    2. Pool-ineligible incumbents become exit candidates ahead of any
       rank-based one. Leaving the pool is a rule exit, not a rank-based
       one, so the min-tenure protection does not apply; it is not a hard
       fail either, so it does use the swap budget.
    3. Incumbents with no score this review (and not ineligible) are held
       (missing data never demotes).
    3. Remaining incumbents are walked best rank first: inside the keep band
       and within caps -> hold; outside the band or over a cap -> exit
       candidate, unless tenure is below ``min_tenure_quarters`` (hold).
    4. Exit candidates leave (pool exits in ticker order, then worst rank
       first), at most ``max_swaps``; the rest are deferred to a later review.
    5. Non-members ranked inside the entry band now AND at the prior review
       enter in rank order while there is room, swap budget (hard-fail
       vacancies + ``max_swaps``) and sector/industry headroom.
    6. Bench = scored non-members ranked ``bench_min_rank..bench_max_rank``.
    """
    ineligible = dict(ineligible or {})
    # Excluded before ranking (PR #2100 review round 1: a hard-failed #1 kept
    # the true #2 out of a 2-wide entry band and pushed an incumbent out of
    # the keep band, because it still occupied rank 1).
    ranks = rank_tickers({t: s for t, s in scores_now.items() if t not in hard_fails and t not in ineligible})
    reasons: dict[str, str] = {}
    caps = _CapCounter(config)
    prior_active = tuple(sorted(incumbents_with_tenure))

    def sector_of(ticker: str) -> tuple[str | None, str | None]:
        scored = scores_now.get(ticker)
        if scored is not None:
            return scored.sector, scored.industry
        incumbent = incumbents_with_tenure.get(ticker)
        return (incumbent.sector, incumbent.industry) if incumbent else (None, None)

    hard_fail_drops = sorted(t for t in incumbents_with_tenure if t in hard_fails)
    for ticker in hard_fail_drops:
        reasons[ticker] = f"drop:hard_fail:{hard_fails[ticker]}"

    for ticker in sorted(scores_now):
        if ticker in hard_fails and ticker not in incumbents_with_tenure:
            reasons[ticker] = "skip:hard_fail"

    pool_exit_candidates = [
        (t, f"drop:left_pool:{ineligible[t]}") for t in prior_active if t not in hard_fails and t in ineligible
    ]

    holds: list[str] = []
    for ticker in prior_active:
        if ticker not in hard_fails and ticker not in ineligible and ticker not in ranks:
            holds.append(ticker)
            reasons[ticker] = "hold:missing_data"
            caps.add(*sector_of(ticker))

    ranked_incumbents = sorted(
        (t for t in prior_active if t not in hard_fails and t in ranks), key=lambda t: ranks[t]
    )
    exit_candidates: list[tuple[str, str]] = []
    for ticker in ranked_incumbents:
        tenure = _strict_int(incumbents_with_tenure[ticker].tenure_quarters)
        # A malformed tenure is treated as "unknown" -> protected, the same
        # way missing data holds: demoting on a bad record is the failure
        # mode the spec forbids.
        protected = tenure is None or tenure < config.min_tenure_quarters
        sector, industry = sector_of(ticker)
        if ranks[ticker] > config.incumbent_keep_rank:
            exit_reason = "drop:rank_outside_keep_band"
        elif caps.blocker(sector, industry) is not None:
            exit_reason = "drop:cap_excess"
        else:
            holds.append(ticker)
            reasons[ticker] = "hold:rank_within_keep_band"
            caps.add(sector, industry)
            continue
        if protected:
            holds.append(ticker)
            reasons[ticker] = "hold:min_tenure"
            caps.add(sector, industry)
        else:
            exit_candidates.append((ticker, exit_reason))

    # Worst rank leaves first; the swap cap defers the rest. Deferred names
    # are counted toward caps AFTER the in-band holds, so a deferred
    # cap-excess name can only block entrants, never a better incumbent.
    exit_candidates.sort(key=lambda tr: -ranks[tr[0]])
    rank_drops: list[str] = []
    for ticker, exit_reason in [*pool_exit_candidates, *exit_candidates]:
        if len(rank_drops) < config.max_swaps:
            rank_drops.append(ticker)
            reasons[ticker] = exit_reason
        else:
            holds.append(ticker)
            reasons[ticker] = "hold:exit_deferred_swap_cap"
            caps.add(*sector_of(ticker))

    # Refilling a hard-fail vacancy is not a rank-based swap, so it does not
    # spend the budget (spec: hard-fail exits do not count toward the 8).
    entry_budget = len(hard_fail_drops) + config.max_swaps
    adds: list[str] = []
    seats = len(holds)
    for ticker in sorted(ranks, key=lambda t: ranks[t]):
        if ranks[ticker] > config.entry_rank:
            break
        if ticker in incumbents_with_tenure:
            continue
        if config.entry_requires_prior_review:
            prior_rank = _strict_int(ranks_prior_review.get(ticker))
            if prior_rank is None or prior_rank < 1 or prior_rank > config.entry_rank:
                reasons[ticker] = "skip:not_in_entry_band_prior_review"
                continue
        if seats + len(adds) >= config.max_active:
            reasons[ticker] = "skip:no_room"
            continue
        if len(adds) >= entry_budget:
            reasons[ticker] = "skip:swap_cap"
            continue
        sector, industry = sector_of(ticker)
        blocked = caps.blocker(sector, industry)
        if blocked is not None:
            reasons[ticker] = f"skip:{blocked}_cap"
            continue
        adds.append(ticker)
        reasons[ticker] = "add:entry_band_two_reviews" if config.entry_requires_prior_review else "add:entry_band"
        caps.add(sector, industry)

    active = tuple(sorted({*holds, *adds}))
    active_set = set(active)
    bench = tuple(
        t
        for t in sorted(ranks, key=lambda t: ranks[t])
        if config.bench_min_rank <= ranks[t] <= config.bench_max_rank and t not in active_set and t not in hard_fails
    )
    classification = {}
    for ticker in active:
        sector, industry = sector_of(ticker)
        if sector is not None or industry is not None:
            classification[ticker] = {"sector": sector, "industry": industry}
    return Proposal(
        review_id=review_id,
        as_of_date=as_of_date,
        generated_at=generated_at,
        prior_active=prior_active,
        active=active,
        adds=tuple(adds),
        drops=tuple(sorted({*hard_fail_drops, *rank_drops})),
        hard_fail_drops=tuple(hard_fail_drops),
        holds=tuple(sorted(holds)),
        bench=bench,
        ranks=dict(ranks),
        reasons=reasons,
        pool_stats=pool_stats,
        config=config,
        ineligible_drops=tuple(sorted(t for t in rank_drops if t in ineligible)),
        # Only adds that HAD a prior rank carry one: strict daily mode admits a first-day entrant that was
        # not ranked at the prior review at all.
        prior_ranks={t: ranks_prior_review[t] for t in adds if t in ranks_prior_review},
        classification=classification,
    )


# --------------------------------------------------------------------------
# Safeguards
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorRunStats:
    """Stats from the last run. ``first_review=True`` is the only way to skip
    the coverage comparison -- passing no stats at all fails closed."""

    pool_size: int | None
    first_review: bool = False


@dataclass(frozen=True)
class InputFreshness:
    """Input dates. ``quote_age_sessions`` is supplied by the caller because a
    pure function cannot know the exchange holiday calendar."""

    quote_date: date
    quote_age_sessions: int
    edgar_refreshed_date: date


def rank_based_swap_count(proposal: Proposal) -> int:
    """Swaps as the spec defines them (1 exit + 1 entry), recomputed from
    membership rather than trusted from a stored counter: the larger of
    rank-based exits and entries beyond hard-fail refills."""
    rank_drops = len(set(proposal.drops) - set(proposal.hard_fail_drops))
    extra_entries = max(0, len(proposal.adds) - len(proposal.hard_fail_drops))
    return max(rank_drops, extra_entries)


def _membership_failures(proposal: Proposal) -> list[SafeguardFailure]:
    prior, active = set(proposal.prior_active), set(proposal.active)
    adds, drops, holds = set(proposal.adds), set(proposal.drops), set(proposal.holds)
    problems = []
    if len(active) != len(proposal.active) or len(adds) != len(proposal.adds):
        problems.append("duplicate symbols")
    if adds & prior:
        problems.append("adds overlap prior members")
    if not drops <= prior or not set(proposal.hard_fail_drops) <= drops:
        problems.append("drops not drawn from prior members")
    # A pool exit is exempt from the missing-data check below, so it must be
    # a real drop and never double as a hard fail.
    if not set(proposal.ineligible_drops) <= drops - set(proposal.hard_fail_drops):
        problems.append("ineligible_drops not a subset of non-hard-fail drops")
    if holds != prior - drops:
        problems.append("holds != prior - drops")
    if active != holds | adds:
        problems.append("active != holds + adds")
    if problems:
        return [SafeguardFailure("membership_inconsistent", "; ".join(problems))]
    return []


def validate_proposal(
    proposal: Proposal,
    prior_run_stats: PriorRunStats | None,
    input_freshness: InputFreshness | None,
    config: UniverseConfig,
) -> list[SafeguardFailure]:
    """Every fail-closed safeguard; an empty list is the only passing result.

    Any failure means the whole proposal is rejected and the last approved
    list stays (``apply_safeguards`` records that). Absent evidence --
    no prior stats, no pool stats, no freshness record, a factor with no
    missing-rate reported -- is itself a failure, never a pass.
    """
    failures = _membership_failures(proposal)

    swaps = rank_based_swap_count(proposal)
    if swaps > config.max_swaps:
        failures.append(SafeguardFailure("too_many_swaps", f"{swaps} rank-based swaps > {config.max_swaps}"))

    size = len(proposal.active)
    if size < config.min_active:
        failures.append(SafeguardFailure("active_too_small", f"{size} active < {config.min_active}"))
    if size > config.max_active:
        failures.append(SafeguardFailure("active_too_large", f"{size} active > {config.max_active}"))

    # Missing data must hold, never demote: a rank-based drop of a ticker
    # with no rank this review can only come from a tampered or buggy
    # proposal.
    rule_drops = set(proposal.hard_fail_drops) | set(proposal.ineligible_drops)
    unranked = sorted(t for t in set(proposal.drops) - rule_drops if t not in proposal.ranks)
    if unranked:
        failures.append(SafeguardFailure("demoted_on_missing_data", ",".join(unranked)))

    failures.extend(_add_failures(proposal, config))

    pool = proposal.pool_stats
    pool_size = None if pool is None else _strict_int(pool.pool_size)
    if pool is None or pool_size is None:
        failures.append(SafeguardFailure("pool_stats_missing", "no pool stats attached to the proposal"))
    else:
        for name in FACTORS:
            rate = _finite_number(pool.factor_missing_fraction.get(name))
            if rate is None or rate > config.max_factor_missing_fraction:
                failures.append(
                    SafeguardFailure(
                        "factor_missing_too_high",
                        f"{name}: missing fraction {rate!r} > {config.max_factor_missing_fraction}",
                    )
                )

    if prior_run_stats is None:
        failures.append(SafeguardFailure("prior_run_stats_missing", "no prior run stats; pass first_review=True"))
    elif prior_run_stats.first_review is not True and pool_size is not None:
        prior_size = _strict_int(prior_run_stats.pool_size)
        if prior_size is None or prior_size <= 0:
            failures.append(SafeguardFailure("prior_run_stats_missing", f"prior pool size {prior_size!r}"))
        elif pool_size / prior_size < config.min_pool_coverage:
            failures.append(
                SafeguardFailure(
                    "pool_coverage_drop",
                    f"pool {pool_size} is {pool_size / prior_size:.1%} of prior {prior_size} "
                    f"(< {config.min_pool_coverage:.0%})",
                )
            )

    failures.extend(_freshness_failures(proposal.as_of_date, input_freshness, config))
    return failures


def _add_failures(proposal: Proposal, config: UniverseConfig) -> list[SafeguardFailure]:
    """Re-check every add against the entry rule instead of trusting it.

    PR #2100 review round 1: membership consistency alone let a tampered
    proposal add an unscored, never-ranked symbol. Each add must be ranked
    inside the entry band now, have been inside it at the prior review,
    carry a known sector and industry, and leave both within their caps.
    Caps are counted over the whole active list: select_universe only adds
    into a bucket below its cap, so a genuine add never sits in an over-cap
    bucket even when tenure-protected incumbents push that bucket over.
    """
    sector_counts: dict[str, int] = {}
    industry_counts: dict[str, int] = {}
    for ticker in proposal.active:
        info = proposal.classification.get(ticker) or {}
        if info.get("sector") is not None:
            sector_counts[info["sector"]] = sector_counts.get(info["sector"], 0) + 1
        if info.get("industry") is not None:
            industry_counts[info["industry"]] = industry_counts.get(info["industry"], 0) + 1
    problems = []
    for ticker in proposal.adds:
        rank = _strict_int(proposal.ranks.get(ticker))
        prior = _strict_int(proposal.prior_ranks.get(ticker))
        info = proposal.classification.get(ticker) or {}
        sector, industry = info.get("sector"), info.get("industry")
        if rank is None or not 1 <= rank <= config.entry_rank:
            problems.append(f"{ticker}: rank {rank!r} outside entry band {config.entry_rank}")
        elif config.entry_requires_prior_review and (prior is None or not 1 <= prior <= config.entry_rank):
            problems.append(f"{ticker}: prior-review rank {prior!r} outside entry band {config.entry_rank}")
        elif sector is None or industry is None:
            problems.append(f"{ticker}: sector/industry unknown")
        elif sector_counts.get(sector, 0) > config.sector_cap:
            problems.append(f"{ticker}: sector {sector} has {sector_counts[sector]} > cap {config.sector_cap}")
        elif industry_counts.get(industry, 0) > config.industry_cap:
            problems.append(
                f"{ticker}: industry {industry} has {industry_counts[industry]} > cap {config.industry_cap}"
            )
    return [SafeguardFailure("add_not_qualified", "; ".join(problems))] if problems else []


def _freshness_failures(
    as_of: date, freshness: InputFreshness | None, config: UniverseConfig
) -> list[SafeguardFailure]:
    if freshness is None:
        return [SafeguardFailure("freshness_missing", "no input freshness record")]
    age = _strict_int(freshness.quote_age_sessions)
    if (
        age is None
        or age < 0
        or not isinstance(freshness.quote_date, date)
        or not isinstance(freshness.edgar_refreshed_date, date)
    ):
        return [SafeguardFailure("freshness_malformed", repr(freshness))]
    failures = []
    # A quote or filing dated after as_of is lookahead, not freshness.
    if freshness.quote_date > as_of:
        failures.append(SafeguardFailure("quote_date_after_as_of", f"{freshness.quote_date} > {as_of}"))
    if freshness.edgar_refreshed_date > as_of:
        failures.append(SafeguardFailure("edgar_date_after_as_of", f"{freshness.edgar_refreshed_date} > {as_of}"))
    if age > config.max_quote_age_sessions:
        failures.append(SafeguardFailure("stale_quotes", f"{age} sessions > {config.max_quote_age_sessions}"))
    quote_days = (as_of - freshness.quote_date).days
    # The session count is caller-supplied (no holiday calendar here), so it
    # is cross-checked against the dates it claims to describe: more
    # sessions than calendar days is impossible, and the quote date itself
    # has a calendar ceiling (PR #2100 review: age 0 on a 10-month-old quote
    # passed).
    if 0 <= quote_days < age:
        failures.append(SafeguardFailure("freshness_inconsistent", f"{age} sessions in {quote_days} calendar days"))
    if quote_days > config.max_quote_age_calendar_days:
        failures.append(
            SafeguardFailure(
                "stale_quotes_calendar", f"quote {quote_days} days old > {config.max_quote_age_calendar_days}"
            )
        )
    edgar_age = (as_of - freshness.edgar_refreshed_date).days
    if edgar_age > config.max_edgar_age_days:
        failures.append(SafeguardFailure("stale_edgar", f"{edgar_age} days > {config.max_edgar_age_days}"))
    return failures


def apply_safeguards(
    proposal: Proposal,
    prior_run_stats: PriorRunStats | None,
    input_freshness: InputFreshness | None,
    config: UniverseConfig,
) -> Proposal:
    """Validate and stamp the outcome.

    Pass -> ``status="pending_approval"`` (still only a proposal; the
    operator approves or nothing changes). Any failure ->
    ``status="rejected"`` with ``recommended_active`` = the last approved
    list MINUS its hard-fail drops, so a consumer that reads only that field
    cannot pick up a rejected membership by accident -- and cannot keep
    buying a delisted, fractional-ineligible, sub-$5 or being-acquired name
    either (PR #2100 review round 1: hard fails pushing the list below
    ``min_active`` made the fallback restore them). Pool-floor exits are NOT
    applied on rejection: they are rule exits that the rejected proposal
    did not get approval for, unlike hard fails, which are never safe to
    keep buying.
    """
    failures = tuple(validate_proposal(proposal, prior_run_stats, input_freshness, config))
    if failures:
        hard = set(proposal.hard_fail_drops)
        fallback = tuple(t for t in proposal.prior_active if t not in hard)
        return dataclasses.replace(
            proposal, status="rejected", safeguard_failures=failures, recommended_active=fallback
        )
    return dataclasses.replace(
        proposal, status="pending_approval", safeguard_failures=(), recommended_active=proposal.active
    )


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def _canonical(payload: object) -> str:
    # allow_nan=False: a NaN would serialize as a bare token strict parsers
    # reject, and would make the hash depend on a non-JSON value.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def proposal_to_dict(proposal: Proposal) -> dict:
    """JSON-safe dict for ``universe_proposal.json``, including ``content_sha256``.

    The hash covers everything except ``generated_at`` (and itself), so the
    same review regenerated later hashes identically, while any change to
    membership, reasons, config or safeguard results changes it. An
    approval record can pin this hash to prove what was approved.
    """
    pool = proposal.pool_stats
    passed = None if proposal.status == "unvalidated" else not proposal.safeguard_failures
    content = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "kind": "universe_proposal",
        "research_only": True,
        "review_id": proposal.review_id,
        "as_of_date": proposal.as_of_date.isoformat(),
        "status": proposal.status,
        "prior_active": list(proposal.prior_active),
        "active": list(proposal.active),
        "recommended_active": list(proposal.recommended_active),
        "adds": list(proposal.adds),
        "drops": list(proposal.drops),
        "hard_fail_drops": list(proposal.hard_fail_drops),
        "ineligible_drops": list(proposal.ineligible_drops),
        "prior_ranks": dict(proposal.prior_ranks),
        "classification": {t: dict(c) for t, c in proposal.classification.items()},
        "holds": list(proposal.holds),
        "bench": list(proposal.bench),
        "rank_based_swaps": rank_based_swap_count(proposal),
        "ranks": dict(proposal.ranks),
        "reasons": dict(proposal.reasons),
        "pool_stats": None
        if pool is None
        else {"pool_size": pool.pool_size, "factor_missing_fraction": dict(pool.factor_missing_fraction)},
        "config": {**dataclasses.asdict(proposal.config), "factor_weights": dict(proposal.config.factor_weights)},
        "safeguards": {
            "passed": passed,
            "failures": [{"code": f.code, "detail": f.detail} for f in proposal.safeguard_failures],
        },
    }
    digest = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    return {**content, "generated_at": proposal.generated_at.isoformat(), "content_sha256": digest}


def proposal_hash_matches(document: Mapping) -> bool:
    """True when a stored proposal's ``content_sha256`` matches its own content.

    Recomputes the digest the way ``proposal_to_dict`` builds it (everything except
    ``generated_at`` and the hash itself), so a proposal edited after it was written no longer
    matches. ``universe_apply`` refuses a proposal for which this is False.
    """
    stored = document.get("content_sha256")
    if not isinstance(stored, str):
        return False
    content = {k: v for k, v in document.items() if k not in ("generated_at", "content_sha256")}
    try:
        digest = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return False
    return digest == stored


def write_proposal(path: str | os.PathLike[str], proposal: Proposal) -> str:
    """Write ``universe_proposal.json`` atomically and return its content hash.

    The only file I/O in this module. Temp-file + ``os.replace`` so a
    reader never sees a half-written proposal. The caller chooses the path;
    nothing here knows about ``config/`` and it must never be pointed there.
    """
    doc = proposal_to_dict(proposal)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return doc["content_sha256"]
