"""Tests for the not-yet-written ticker-advisor/universe_inputs.py (step 6 of docs/fundamentals_feed_scope.md).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

Step 6 is the adapter between the two feeds and the engine. It reads the fundamentals SNAPSHOT file (the
contract written by dca-sizing-signals' ``fundamentals_snapshot``; ``ticker-advisor`` does not import that
package, so the hash rule is restated here and pinned by these tests) and the price-derived inputs from
``price_factors``, and produces what ``universe.py`` consumes: pool ``Candidate`` records, ``FactorInputs`` and
``TickerStatus`` records, plus a reason for every name left out of the factor inputs.

Definitions pinned here (all stated in the scope document, none taken from code):

* profitability = gross profit / total assets, where gross profit is the ``GrossProfit`` fact, or revenue minus
  cost of revenue when ``GrossProfit`` is absent and both facts cover the same period. Assets must be positive.
* value = book-to-price = stockholders' equity / market cap, market cap = shares outstanding times last price
  (the operator chose book-to-price first). Negative equity gives a negative value, not None.
* sector and industry come from the SIC fields; a name with none is left out of the factor inputs ("unscored"),
  never given a made-up sector. Missing data is None, never zero.
* pinned names are removed from every output (they are never ranked).
* the fractional-eligibility flag is NOT checked in the ranking (operator decision 2026-11-20), so every candidate
  carries ``fractional_eligible=None`` and ``PoolThresholds.require_fractional_eligible=False`` stops the pool
  filter from rejecting it as missing. The default stays True, so the engine's existing behaviour is unchanged.
* ``security_type`` is ``common_stock`` only for a name with SEC facts AND a SIC code; unknown stays None (a
  missing value holds an incumbent, it never demotes it). ``nms_listed`` comes from a caller-supplied exchange
  map; an unknown exchange is None.

All inputs are SYNTHETIC. No run of 6 or more digits appears below (the account-number guard in
tests/test_config_invariants.py), so CIKs are small and the snapshot hash is computed, never written out.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from price_factors import PriceInputs
from universe import PoolThresholds, UniverseConfig, composite_scores, filter_pool, rank_tickers
from universe_inputs import RankingInputs, build_ranking_inputs, load_snapshot

FETCHED = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def stamp(document: dict) -> dict:
    """Add the content hash the way the snapshot writer does (everything except fetched_at and the hash)."""
    content = {k: v for k, v in document.items() if k not in ("fetched_at", "content_sha256")}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    document["content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return document


def fact(val, end="2025-12-31", start=None):
    return {"val": val, "start": start, "end": end, "filed": "2026-02-11", "form": "10-K", "accn": "A-1"}


def record(*, facts, sic="3571", sector="35", industry="3571", status="ok"):
    return {
        "status": status,
        "cik": "c",
        "sic": sic,
        "sic_description": "d",
        "sector": sector,
        "industry": industry,
        "facts": facts,
    }


FULL = {
    "us-gaap:GrossProfit": fact(400, start="2025-01-01"),
    "us-gaap:Assets": fact(5000),
    "us-gaap:StockholdersEquity": fact(2000),
    "dei:EntityCommonStockSharesOutstanding": fact(70, end="2026-02-05"),
}


def snapshot(symbols: dict) -> dict:
    return stamp(
        {
            "schema_version": 1,
            "kind": "fundamentals_snapshot",
            "as_of": "2026-06-30",
            "fetched_at": FETCHED.isoformat(),
            "coverage": {"symbols": len(symbols)},
            "symbols": symbols,
        }
    )


def px(**overrides) -> PriceInputs:
    values = dict(
        momentum_6m=0.5,
        momentum_12m=2.0,
        volatility_1y=0.02,
        median_dollar_volume_60d=3.0e7,
        history_sessions=300,
        last_price=10.0,
        last_session=date(2026, 6, 30),
        sessions_below_min_price=0,
        flags=(),
    )
    values.update(overrides)
    return PriceInputs(**values)


def build(symbols, prices=None, **kwargs):
    prices = {s: px() for s in symbols} if prices is None else prices
    return build_ranking_inputs(snapshot(symbols), prices, **kwargs)


def one(inputs: RankingInputs, ticker: str):
    return next(f for f in inputs.factor_inputs if f.ticker == ticker)


# --- load_snapshot --------------------------------------------------------------------------------------------


def test_a_valid_snapshot_file_loads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(snapshot({"AAA": record(facts=FULL)})), encoding="utf-8")
    assert load_snapshot(path)["symbols"]["AAA"]["status"] == "ok"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d["symbols"]["AAA"].update(status="no_facts"), id="content_edited_after_hashing"),
        pytest.param(lambda d: d.update(kind="something_else"), id="wrong_kind"),
        pytest.param(lambda d: d.update(schema_version=2), id="unknown_schema_version"),
        pytest.param(lambda d: d.pop("content_sha256"), id="hash_missing"),
    ],
)
def test_a_snapshot_that_fails_validation_is_refused(tmp_path, mutate):
    document = snapshot({"AAA": record(facts=FULL)})
    mutate(document)
    path = tmp_path / "s.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot(path)


def test_a_snapshot_that_is_not_json_or_has_duplicate_keys_is_refused(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot(path)
    path.write_text('{"kind": "a", "kind": "b"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot(path)


def test_the_fetch_time_is_carried_as_a_timezone_aware_datetime():
    inputs = build({"AAA": record(facts=FULL)})
    assert inputs.snapshot_fetched_at == FETCHED and inputs.snapshot_fetched_at.tzinfo is not None


# --- profitability ---------------------------------------------------------------------------------------------------


def test_profitability_is_gross_profit_over_assets():
    assert one(build({"AAA": record(facts=FULL)}), "AAA").profitability == pytest.approx(400 / 5000)


def test_without_gross_profit_it_falls_back_to_revenue_minus_cost_of_revenue_for_the_same_period():
    facts = {k: v for k, v in FULL.items() if k != "us-gaap:GrossProfit"}
    facts["us-gaap:Revenues"] = fact(900, start="2025-01-01")
    facts["us-gaap:CostOfRevenue"] = fact(600, start="2025-01-01")
    assert one(build({"AAA": record(facts=facts)}), "AAA").profitability == pytest.approx(300 / 5000)


def test_revenue_and_cost_from_different_periods_give_no_profitability():
    facts = {k: v for k, v in FULL.items() if k != "us-gaap:GrossProfit"}
    facts["us-gaap:Revenues"] = fact(900, start="2025-01-01")
    facts["us-gaap:CostOfRevenue"] = fact(600, start="2024-01-01", end="2024-12-31")
    assert one(build({"AAA": record(facts=facts)}), "AAA").profitability is None


@pytest.mark.parametrize("assets", [None, 0, -5])
def test_missing_or_non_positive_assets_give_no_profitability(assets):
    facts = dict(FULL)
    if assets is None:
        del facts["us-gaap:Assets"]
    else:
        facts["us-gaap:Assets"] = fact(assets)
    assert one(build({"AAA": record(facts=facts)}), "AAA").profitability is None


def test_no_gross_profit_and_no_revenue_gives_no_profitability():
    facts = {k: v for k, v in FULL.items() if k != "us-gaap:GrossProfit"}
    assert one(build({"AAA": record(facts=facts)}), "AAA").profitability is None


# --- value (book-to-price) -------------------------------------------------------------------------------


def test_value_is_equity_over_shares_times_price():
    # market cap = 70 * 10 = 700; equity 2000.
    assert one(build({"AAA": record(facts=FULL)}), "AAA").value == pytest.approx(2000 / 700)


def test_negative_equity_gives_a_negative_value_not_none():
    facts = dict(FULL, **{"us-gaap:StockholdersEquity": fact(-300)})
    assert one(build({"AAA": record(facts=facts)}), "AAA").value == pytest.approx(-300 / 700)


@pytest.mark.parametrize("drop", ["us-gaap:StockholdersEquity", "dei:EntityCommonStockSharesOutstanding"])
def test_missing_equity_or_shares_gives_no_value(drop):
    facts = {k: v for k, v in FULL.items() if k != drop}
    assert one(build({"AAA": record(facts=facts)}), "AAA").value is None


def test_no_last_price_gives_no_value():
    inputs = build({"AAA": record(facts=FULL)}, prices={"AAA": px(last_price=None)})
    assert one(inputs, "AAA").value is None


# --- price-derived factors and sector --------------------------------------------------------------------


def test_momentum_and_volatility_come_from_the_price_inputs_and_sector_from_the_sic():
    factor = one(build({"AAA": record(facts=FULL)}), "AAA")
    assert (factor.momentum_6m, factor.momentum_12m, factor.volatility_1y) == (0.5, 2.0, 0.02)
    assert (factor.sector, factor.industry) == ("35", "3571")


def test_a_name_with_no_price_inputs_still_gets_factor_inputs_with_none_price_factors():
    factor = one(build({"AAA": record(facts=FULL)}, prices={}), "AAA")
    assert factor.momentum_12m is None and factor.volatility_1y is None
    assert factor.profitability == pytest.approx(0.08)  # the fundamentals still count


# --- who is left out, and why ----------------------------------------------------------------------------


def test_names_without_usable_snapshot_data_are_left_out_of_the_factor_inputs_with_a_reason():
    symbols = {
        "AAA": record(facts=FULL),
        "BBB": {"status": "no_cik"},
        "CCC": {"status": "no_facts", "cik": "c"},
        "DDD": record(facts=FULL, sector=None, industry=None, sic=None),  # facts but no SIC: no sector to score in
    }
    inputs = build(symbols)
    assert [f.ticker for f in inputs.factor_inputs] == ["AAA"]
    assert dict(inputs.unscored) == {"BBB": "snapshot:no_cik", "CCC": "snapshot:no_facts", "DDD": "no_sector"}


def test_pinned_names_are_removed_from_every_output():
    symbols = {"AAA": record(facts=FULL), "QQQ": {"status": "no_cik"}}
    inputs = build(symbols, pinned={"QQQ"})
    assert [c.ticker for c in inputs.candidates] == ["AAA"]
    assert [s.ticker for s in inputs.statuses] == ["AAA"]
    assert "QQQ" not in inputs.unscored and all(f.ticker != "QQQ" for f in inputs.factor_inputs)


def test_outputs_are_sorted_by_ticker_whatever_the_input_order():
    symbols = {"CCC": record(facts=FULL), "AAA": record(facts=FULL), "BBB": record(facts=FULL)}
    inputs = build(symbols)
    assert [c.ticker for c in inputs.candidates] == ["AAA", "BBB", "CCC"]
    assert [f.ticker for f in inputs.factor_inputs] == ["AAA", "BBB", "CCC"]


# --- pool candidates -------------------------------------------------------------------------------------


def test_a_candidate_carries_price_market_cap_dollar_volume_and_history():
    candidate = build({"AAA": record(facts=FULL)}).candidates[0]
    assert (candidate.price, candidate.market_cap, candidate.median_dollar_volume_60d, candidate.history_sessions) == (
        10.0,
        700.0,
        3.0e7,
        300,
    )
    assert candidate.fractional_eligible is None  # not checked in the ranking


def test_security_type_is_common_stock_only_with_sec_facts_and_a_sic_code():
    symbols = {
        "AAA": record(facts=FULL),
        "BBB": {"status": "no_cik"},
        "CCC": record(facts=FULL, sic=None, sector=None, industry=None),
    }
    by_ticker = {c.ticker: c for c in build(symbols).candidates}
    assert by_ticker["AAA"].security_type == "common_stock"
    assert by_ticker["BBB"].security_type is None and by_ticker["CCC"].security_type is None  # unknown, not "not stock"


@pytest.mark.parametrize(
    "exchange, expected", [("Nasdaq", True), ("NYSE", True), ("nasdaq", True), ("OTC", False), (None, None)]
)
def test_nms_listing_comes_from_the_caller_supplied_exchange_map(exchange, expected):
    inputs = build({"AAA": record(facts=FULL)}, exchange_by_symbol={"AAA": exchange})
    assert inputs.candidates[0].nms_listed is expected


def test_an_unlisted_symbol_in_the_exchange_map_is_none():
    assert build({"AAA": record(facts=FULL)}, exchange_by_symbol={}).candidates[0].nms_listed is None


def test_sessions_below_the_minimum_price_reach_the_ticker_status():
    inputs = build({"AAA": record(facts=FULL)}, prices={"AAA": px(sessions_below_min_price=25)})
    assert inputs.statuses[0].sessions_below_min_price == 25


# --- the engine, end to end ------------------------------------------------------------------------------


def good_symbols():
    """Six names in two sectors whose factors all differ, so no sector z-score is degenerate."""
    out = {}
    for i, (ticker, sector) in enumerate(
        [("AAA", "35"), ("BBB", "35"), ("CCC", "35"), ("DDD", "73"), ("EEE", "73"), ("FFF", "73")]
    ):
        facts = dict(
            FULL,
            **{
                "us-gaap:GrossProfit": fact(400 + 50 * i, start="2025-01-01"),
                "us-gaap:StockholdersEquity": fact(2000 + 100 * i),
            },
        )
        out[ticker] = record(facts=facts, sector=sector, industry=sector + "00")
    return out


def good_prices():
    return {
        t: px(momentum_6m=0.1 * i, momentum_12m=0.2 * i, volatility_1y=0.01 * (i + 1))
        for i, t in enumerate(good_symbols())
    }


def test_the_pool_filter_passes_a_good_candidate_only_when_fractional_is_not_required():
    inputs = build(good_symbols(), good_prices(), exchange_by_symbol={t: "Nasdaq" for t in good_symbols()})
    thresholds = PoolThresholds(min_market_cap=1.0, min_median_dollar_volume_60d=1.0, require_fractional_eligible=False)
    assert len(filter_pool(inputs.candidates, thresholds).eligible) == 6
    strict = PoolThresholds(min_market_cap=1.0, min_median_dollar_volume_60d=1.0)  # default: fractional required
    assert filter_pool(inputs.candidates, strict).eligible == ()


def test_the_factor_inputs_feed_the_scorer_and_the_ranking():
    inputs = build(good_symbols(), good_prices())
    scores = composite_scores(inputs.factor_inputs, UniverseConfig.strict_daily_top(6))
    assert all(s.composite is not None for s in scores.values())
    assert sorted(rank_tickers(scores).values()) == [1, 2, 3, 4, 5, 6]
