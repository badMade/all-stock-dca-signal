"""Tests for the not-yet-written ticker-advisor/daily_ranking.py (step 7a of docs/fundamentals_feed_scope.md).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

Step 7a is the daily ranking run in PROPOSE-ONLY form: it ties the feed pieces to the engine and returns a
proposal plus a report. It never writes ``config/tickers.json`` (the gated writer does that, later) and has no
scheduler or checkout logic (step 7b, blocked on the backup-planner decision). It is pure: files are written only
by ``write_daily_outputs``, which a test below exercises.

What it does, in order (all from the ADR and the scope document):

1. build the engine inputs from the snapshot and the price inputs, leaving out pinned names;
2. filter the pool, take hard fails from the statuses, score and rank;
3. run the engine in strict daily mode for ``size - len(pinned)`` ranked seats (``size`` is 100 in production);
4. add the pinned names back, then run the safeguards over the FULL ``size``;
5. report who enters and leaves, sector counts, who fails the pool floors (and which of the CURRENT names do, the
   evidence the operator needs to decide the floors), and who could not be scored.

Freshness: the snapshot's fetch date is the "SEC data refreshed" date, the latest price session is the quote date,
and the quote age in sessions counts weekdays only (no holiday calendar is available here, so a holiday makes the
age one session too high, which errs toward rejection).

All inputs are SYNTHETIC. No run of 6 or more digits appears below (the account-number guard).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from daily_ranking import run_daily_ranking, write_daily_outputs
from price_factors import PriceInputs
from universe import PoolThresholds

AS_OF = date(2026, 6, 30)
FETCHED = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
GENERATED = datetime(2026, 6, 30, 13, 0, tzinfo=timezone.utc)
NAMES = ["AAA", "AAB", "AAC", "AAD", "AAE", "AAF", "AAG", "AAH"]  # index i = better score as i rises
RELAXED = PoolThresholds(
    min_price=1.0,
    min_market_cap=1.0,
    min_median_dollar_volume_60d=1.0,
    min_history_sessions=1,
    require_fractional_eligible=False,
)


def stamp(document):
    content = {k: v for k, v in document.items() if k not in ("fetched_at", "content_sha256")}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    document["content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return document


def fact(val, start=None, end="2025-12-31"):
    return {"val": val, "start": start, "end": end, "filed": "2026-02-11", "form": "10-K", "accn": "A-1"}


def record(i, *, drop=()):
    facts = {
        "us-gaap:GrossProfit": fact(400 + 10 * i, start="2025-01-01"),
        "us-gaap:Revenues": fact(900, start="2025-01-01"),
        "us-gaap:Assets": fact(5000),
        "us-gaap:StockholdersEquity": fact(2000 + 100 * i),
        "dei:EntityCommonStockSharesOutstanding": fact(70, end="2026-02-05"),
    }
    for key in drop:
        facts.pop(key, None)
    return {
        "status": "ok",
        "cik": "c",
        "sic": "3571",
        "sic_description": "d",
        "sector": "35",
        "industry": "3571",
        "facts": facts,
    }


def snapshot(*, fetched=FETCHED, drop_profitability=()):
    symbols = {
        n: record(i, drop=("us-gaap:GrossProfit", "us-gaap:Revenues") if n in drop_profitability else ())
        for i, n in enumerate(NAMES)
    }
    symbols["PIN"] = {"status": "no_cik"}
    document = {
        "schema_version": 1,
        "kind": "fundamentals_snapshot",
        "as_of": AS_OF.isoformat(),
        "fetched_at": fetched.isoformat(),
        "coverage": {"symbols": len(symbols)},
        "symbols": symbols,
    }
    return stamp(document)


def prices(*, last_session=AS_OF, overrides=None):
    out = {}
    for i, n in enumerate(NAMES):
        values = dict(
            momentum_6m=0.1 * i,
            momentum_12m=0.2 * i,
            volatility_1y=0.10 - 0.005 * i,
            median_dollar_volume_60d=3.0e7,
            history_sessions=300,
            last_price=10.0,
            last_session=last_session,
            sessions_below_min_price=0,
            flags=(),
        )
        values.update((overrides or {}).get(n, {}))
        out[n] = PriceInputs(**values)
    return out


EXCHANGES = {n: "Nasdaq" for n in NAMES}
PRIOR = ("AAA", "AAB", "AAC", "AAD", "PIN")


def run(**overrides):
    kwargs = dict(
        snapshot=snapshot(),
        price_inputs=prices(),
        prior_active=PRIOR,
        pinned={"PIN"},
        exchange_by_symbol=EXCHANGES,
        as_of=AS_OF,
        generated_at=GENERATED,
        size=5,
        thresholds=RELAXED,
    )
    kwargs.update(overrides)
    return run_daily_ranking(**kwargs)


def codes(ranking):
    return {f.code for f in ranking.proposal.safeguard_failures}


# --- the proposal ----------------------------------------------------------------------------------------------


def test_the_top_ranked_names_fill_the_seats_the_pinned_list_leaves_and_pinned_names_stay():
    ranking = run()  # size 5, one pinned name: 4 ranked seats go to the 4 best scores
    assert ranking.proposal.active == ("AAE", "AAF", "AAG", "AAH", "PIN")
    assert ranking.proposal.adds == ("AAE", "AAF", "AAG", "AAH")
    assert ranking.proposal.drops == ("AAA", "AAB", "AAC", "AAD")
    assert ranking.proposal.status == "pending_approval" and codes(ranking) == set()


def test_the_best_score_is_rank_one_and_pinned_names_are_never_ranked():
    ranks = run().proposal.ranks
    assert [t for t, _ in sorted(ranks.items(), key=lambda kv: kv[1])][:4] == ["AAH", "AAG", "AAF", "AAE"]
    assert "PIN" not in ranks


def test_a_name_that_fails_a_pool_floor_is_dropped_even_if_it_is_a_current_holding():
    thresholds = PoolThresholds(
        min_market_cap=1.0, min_median_dollar_volume_60d=1.0, min_history_sessions=1, require_fractional_eligible=False
    )  # min_price keeps its default of 5.0
    cheap = {"AAD": {"last_price": 2.0}}
    ranking = run(price_inputs=prices(overrides=cheap), thresholds=thresholds)
    assert "AAD" not in ranking.proposal.active and "AAD" in ranking.proposal.drops
    assert ranking.report.ineligible == {"AAD": "price_at_or_below_min"}


# --- the safeguards over the full size ----------------------------------------------------------------------------


def test_snapshot_data_older_than_seven_days_rejects_the_proposal_and_keeps_the_prior_list():
    ranking = run(snapshot=snapshot(fetched=FETCHED - timedelta(days=10)))
    assert ranking.proposal.status == "rejected" and "stale_edgar" in codes(ranking)
    assert ranking.proposal.recommended_active == PRIOR


def test_stale_prices_reject_the_proposal():
    ranking = run(price_inputs=prices(last_session=date(2026, 6, 18)))
    assert ranking.proposal.status == "rejected"
    assert {"stale_quotes", "stale_quotes_calendar"} & codes(ranking)


def test_a_pool_far_smaller_than_the_prior_runs_pool_rejects_the_proposal():
    ranking = run(prior_pool_size=1000)
    assert "pool_coverage_drop" in codes(ranking)


def test_the_first_run_needs_no_prior_pool_size():
    assert run(prior_pool_size=None).proposal.status == "pending_approval"


def test_a_factor_missing_for_more_than_ten_percent_of_the_pool_rejects_the_proposal():
    ranking = run(snapshot=snapshot(drop_profitability={"AAA", "AAB"}))  # 2 of 8 lack profitability
    assert "factor_missing_too_high" in codes(ranking)


# --- the report -------------------------------------------------------------------------------------------------------


def test_the_report_lists_entries_exits_and_sector_counts():
    report = run().report
    assert report.entering == ("AAE", "AAF", "AAG", "AAH")
    assert report.leaving == ("AAA", "AAB", "AAC", "AAD")
    assert report.sector_counts == {"35": 4}  # ranked names only; pinned names have no sector here


def test_the_report_names_current_holdings_that_fail_the_floors():
    thresholds = PoolThresholds(
        min_market_cap=1.0, min_median_dollar_volume_60d=1.0, min_history_sessions=1, require_fractional_eligible=False
    )
    report = run(price_inputs=prices(overrides={"AAD": {"last_price": 2.0}}), thresholds=thresholds).report
    assert report.floor_failures_among_current == {"AAD": "price_at_or_below_min"}


def test_names_that_could_not_be_scored_are_reported_with_a_reason_and_pinned_names_are_not():
    report = run().report
    assert "PIN" not in report.unscored
    assert run().report.unscored == {}


# --- writing the outputs ---------------------------------------------------------------------------------


def test_the_outputs_are_a_hashed_proposal_file_and_a_report_file_with_no_temp_files_left(tmp_path):
    ranking = run()
    proposal_path, report_path = write_daily_outputs(tmp_path, ranking)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal["kind"] == "universe_proposal" and proposal["status"] == "pending_approval"
    assert proposal["active"] == list(ranking.proposal.active) and len(proposal["content_sha256"]) == 64
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert {"entering", "leaving", "sector_counts", "ineligible", "unscored", "floor_failures_among_current"} <= set(
        report
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([proposal_path.name, report_path.name])


def test_running_never_touches_a_tickers_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run()
    assert list(tmp_path.iterdir()) == []  # a pure run writes nothing anywhere


@pytest.mark.parametrize("size", [0, -1])
def test_a_size_that_leaves_no_ranked_seats_is_refused(size):
    with pytest.raises(ValueError):
        run(size=size)
