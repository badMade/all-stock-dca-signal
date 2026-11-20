"""The daily ranking run, in PROPOSE-ONLY form (feed step 7a).

Specified first by tests/test_daily_ranking.py (docs/fundamentals_feed_scope.md). It ties the feed pieces to the
engine and returns a proposal plus a report. It never writes ``config/tickers.json`` (the gated writer
``universe_apply`` does that, later) and has no scheduler or checkout logic (step 7b). It is pure: files are
written only by :func:`write_daily_outputs`.

In order: build the engine inputs from the snapshot and price inputs (pinned names left out), filter the pool, take
hard fails, score and rank, run the engine in strict daily mode for ``size - len(pinned)`` ranked seats, add the
pinned names back, then run the safeguards over the FULL ``size``. The report lists who enters and leaves, sector
counts, who fails the pool floors (and which CURRENT holdings do), and who could not be scored.

Freshness: the snapshot's fetch date is the "SEC data refreshed" date, the latest price session is the quote date,
and the quote age in sessions counts weekdays only (no holiday calendar is available here, so a holiday makes the
age one session too high, which errs toward rejection).

A name enters the ranking only if it passes the pool filter, or is a current holding with an unknown flag (an
unknown value holds an incumbent, it never demotes it). Pure stdlib, no network.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from price_factors import PriceInputs
from universe import (
    Incumbent,
    InputFreshness,
    PoolThresholds,
    PriorRunStats,
    Proposal,
    UniverseConfig,
    apply_pinned,
    apply_safeguards,
    composite_scores,
    compute_pool_stats,
    detect_hard_fails,
    filter_pool,
    ineligible_tickers,
    select_universe,
    write_proposal,
)
from universe_inputs import build_ranking_inputs

PROPOSAL_FILENAME = "universe_proposal.json"
REPORT_FILENAME = "ranking_report.json"
DEFAULT_SIZE = 100


@dataclass(frozen=True)
class RankingReport:
    """What the run found, for a human to read. All name lists are sorted."""

    entering: tuple[str, ...]
    leaving: tuple[str, ...]
    sector_counts: Mapping[str, int]
    ineligible: Mapping[str, str]
    unscored: Mapping[str, str]
    floor_failures_among_current: Mapping[str, str]
    pool_size: int
    scored_count: int


@dataclass(frozen=True)
class DailyRanking:
    proposal: Proposal
    report: RankingReport


def _weekdays_between(start: date, end: date) -> int:
    """Weekdays d with ``start < d <= end``. Negative spans count as zero."""
    days = (end - start).days
    return sum(1 for i in range(1, days + 1) if (start + timedelta(days=i)).weekday() < 5)


def _freshness(snapshot_fetched_at: datetime, price_inputs: Mapping[str, PriceInputs], as_of: date):
    sessions = [px.last_session for px in price_inputs.values() if px.last_session is not None]
    if not sessions:
        return None  # no quote date at all: the safeguards report freshness_missing (fail closed)
    quote_date = max(sessions)
    return InputFreshness(
        quote_date=quote_date,
        quote_age_sessions=_weekdays_between(quote_date, as_of),
        edgar_refreshed_date=snapshot_fetched_at.date(),
    )


def run_daily_ranking(
    *,
    snapshot: Mapping[str, Any],
    price_inputs: Mapping[str, PriceInputs],
    prior_active: Iterable[str],
    pinned: Iterable[str] = (),
    exchange_by_symbol: Mapping[str, str | None] | None = None,
    as_of: date,
    generated_at: datetime,
    size: int = DEFAULT_SIZE,
    thresholds: PoolThresholds | None = None,
    prior_pool_size: int | None = None,
    review_id: str | None = None,
) -> DailyRanking:
    """Rank the pool as of ``as_of`` and return the proposal and report. Raises ``ValueError`` on a bad ``size``.

    Args:
        snapshot: a validated fundamentals snapshot document (see ``universe_inputs.load_snapshot``).
        price_inputs: price-derived inputs per symbol (``price_factors.price_inputs``).
        prior_active: the current ``active`` list (what the proposal is built on).
        pinned: hand-held names kept in ``active`` outside the ranking; they count toward ``size``.
        exchange_by_symbol: exchange per symbol for the ``nms_listed`` flag.
        size: the number of names in the active list (100 in production).
        thresholds: pool floors; defaults to the ADR floors without a fractional-eligibility check.
        prior_pool_size: the previous run's pool size, or None for the first run.
    """
    pinned_set = set(pinned)
    ranked_seats = size - len(pinned_set)
    if size < 1 or ranked_seats < 1:
        raise ValueError("size must leave at least one ranked seat after the pinned names")
    ranked_config = UniverseConfig.strict_daily_top(ranked_seats)
    full_config = UniverseConfig.strict_daily_top(size)
    thresholds = thresholds or PoolThresholds(require_fractional_eligible=False)
    prior = tuple(prior_active)

    inputs = build_ranking_inputs(snapshot, price_inputs, pinned=pinned_set, exchange_by_symbol=exchange_by_symbol)
    pool = filter_pool(inputs.candidates, thresholds)
    ineligible = ineligible_tickers(pool)
    hard_fails = detect_hard_fails(inputs.statuses, ranked_config)

    incumbent_names = {t for t in prior if t not in pinned_set}
    eligible_names = {c.ticker for c in pool.eligible}
    keep = eligible_names | incumbent_names
    scores = composite_scores([f for f in inputs.factor_inputs if f.ticker in keep], ranked_config)
    sector_of = {f.ticker: (f.sector, f.industry) for f in inputs.factor_inputs}
    incumbents = {t: Incumbent(tenure_quarters=0, sector=sector_of.get(t, (None, None))[0],
                               industry=sector_of.get(t, (None, None))[1]) for t in incumbent_names}

    proposal = select_universe(
        scores,
        {},
        incumbents,
        hard_fails,
        ranked_config,
        review_id=review_id or f"daily-{as_of.isoformat()}",
        as_of_date=as_of,
        generated_at=generated_at,
        pool_stats=compute_pool_stats(inputs.factor_inputs),
        ineligible=ineligible,
    )
    # The engine lists adds in rank order; the daily proposal lists every name list sorted, like ``drops``.
    proposal = dataclasses.replace(proposal, adds=tuple(sorted(proposal.adds)))
    proposal = apply_pinned(proposal, pinned_set)
    proposal = dataclasses.replace(proposal, config=full_config)  # the floor and cap apply to the FULL list
    prior_stats = PriorRunStats(pool_size=prior_pool_size, first_review=prior_pool_size is None)
    proposal = apply_safeguards(
        proposal, prior_stats, _freshness(inputs.snapshot_fetched_at, price_inputs, as_of), full_config
    )

    sector_counts = Counter(
        info["sector"] for t in proposal.active if (info := proposal.classification.get(t)) and info.get("sector")
    )
    report = RankingReport(
        entering=tuple(sorted(proposal.adds)),
        leaving=tuple(sorted(proposal.drops)),
        sector_counts=dict(sorted(sector_counts.items())),
        ineligible=dict(sorted(ineligible.items())),
        unscored=dict(sorted(inputs.unscored.items())),
        floor_failures_among_current={t: ineligible[t] for t in sorted(prior) if t in ineligible},
        pool_size=len(inputs.candidates),
        scored_count=sum(1 for s in scores.values() if s.composite is not None),
    )
    return DailyRanking(proposal=proposal, report=report)


def _write_text_atomically(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_daily_outputs(directory: str | Path, ranking: DailyRanking) -> tuple[Path, Path]:
    """Write ``universe_proposal.json`` and ``ranking_report.json`` atomically into ``directory``.

    Returns ``(proposal_path, report_path)``. Nothing else is written; ``config/tickers.json`` is never touched.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    proposal_path = target / PROPOSAL_FILENAME
    report_path = target / REPORT_FILENAME
    write_proposal(proposal_path, ranking.proposal)
    text = json.dumps(dataclasses.asdict(ranking.report), sort_keys=True, indent=2, allow_nan=False) + "\n"
    _write_text_atomically(report_path, text)
    return proposal_path, report_path
