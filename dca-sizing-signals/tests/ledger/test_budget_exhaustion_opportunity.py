"""Opportunity sizing for the MDP cash-pacing and absorbing-tier-chain ideas (read-only).

Question this answers before anything gets built: how often did a LATER scheduled slot see a
Tier B-or-deeper price (price <= the prior session's low) when the day's budget was already
too spent to fund a Tier B buy? If that almost never happens, an MDP that reserves cash for
later slots has nothing to save, and both ideas drop down the roadmap.

Proposed API (does not exist yet; this file is the spec, written test-first):

    from market_data_ingest.ledger.opportunity import budget_exhaustion_opportunity
    report = budget_exhaustion_opportunity(
        ledger, known_at=..., daily_cap_usd=180.0, tier_b_cost_usd=4.0)

* ``daily_cap_usd`` is a parameter, not read from config: the live cap moves between
  $180/$240/$300/$360 with the day's 3x-signal count (config/budgets.json, budget_tiers),
  and that tier is not stored in the ledger. The caller runs it once per cap of interest.
* ``tier_b_cost_usd`` defaults to 4.0 = $2.00 base (below close AND below prior low) x 2x.
* "Exhausted at a slot" means ``daily_cap_usd - spent_before < tier_b_cost_usd``, where
  ``spent_before`` sums SCHEDULED fills for that session with ``filled_at`` strictly before
  the quote's ``ts``. Manual fills never count (docs/run_classification.md).
* Tier C needs the forecast and is not reconstructed here; B-or-deeper is the measurable floor.
* Point in time: only fills, quotes and bars with ``as_of <= known_at`` are visible.
* Fail closed: a quote with no prior-session bar is skipped and counted, never guessed.
  Bad parameters raise ``ValueError``. The function never writes.

Fixture (synthetic, pencil-checkable; same conventions as conftest.py): ZZZA's prior-session
bar (SESSIONS[0], close 10.00) has low 9.90 (conftest ``bar`` sets low = close * 0.99).
The quote session is SESSIONS[1] = 2026-03-03, a Tuesday in EST (UTC-5), so the 13:30 ET
slot is 18:30 UTC and the 09:35 ET slot is 14:35 UTC.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from market_data_ingest.prices.models import Quote

from .conftest import FIRST_SEEN, LATER, SESSIONS, bar, fill, ingest_bars, ingest_calendar, utc

D1 = SESSIONS[1]
KNOW_ALL = utc(2026, 6, 1)
CAP = 180.0
SLOT_1330 = datetime(2026, 3, 3, 18, 30, tzinfo=timezone.utc)
FILL_0935 = datetime(2026, 3, 3, 14, 35, tzinfo=timezone.utc)
FILL_1530 = datetime(2026, 3, 3, 20, 30, tzinfo=timezone.utc)


def _run(ledger, **kwargs):
    from market_data_ingest.ledger.opportunity import budget_exhaustion_opportunity

    kwargs.setdefault("known_at", KNOW_ALL)
    kwargs.setdefault("daily_cap_usd", CAP)
    return budget_exhaustion_opportunity(ledger, **kwargs)


def _fill(order_id, *, notional, filled_at=FILL_0935, run_kind="scheduled", slot="09:35", symbol="ZZZA"):
    row = fill(order_id, run_kind=run_kind, symbol=symbol, session=D1, notional=notional, slot=slot)
    row["filled_at"] = filled_at
    return row


def _setup(ledger, staging_dir, *, fills, quote_price=9.50, quote_as_of=FIRST_SEEN, with_prior_bar=True):
    ingest_calendar(ledger)
    if with_prior_bar:
        ingest_bars(ledger, staging_dir, [bar("ZZZA", SESSIONS[0], 10.00)])
    if fills:
        ledger.ingest_fills(fills, source="run_artifact", run_id="f1", as_of=FIRST_SEEN)
    quote = Quote(symbol="ZZZA", price=quote_price, ts=SLOT_1330, feed="iex", as_of=quote_as_of)
    ledger.prices.insert_intraday_quote(quote, slot="13:30", delayed=False, run_id="q1")


# --- the core count ---------------------------------------------------------------------------------


def test_deep_tier_after_budget_spent_is_one_event(ledger, staging_dir):
    # Spent $180 at 09:35; remaining 180 - 180 = 0 < 4. At 13:30 price 9.50 <= low 9.90 -> event.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    report = _run(ledger)
    assert report["sessions_with_event"] == 1
    assert report["events"] == [
        {"session": D1, "symbol": "ZZZA", "slot": "13:30", "price": 9.50, "prior_low": pytest.approx(9.90),
         "spent_before_usd": 180.0, "remaining_usd": 0.0}
    ]


def test_remaining_below_tier_b_cost_counts_as_exhausted(ledger, staging_dir):
    # Spent $177; remaining 3.00 < 4.00 (the $2 base x 2x), so a Tier B buy could not be funded.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=177.0)])
    assert _run(ledger)["sessions_with_event"] == 1


def test_remaining_equal_to_tier_b_cost_is_not_exhausted(ledger, staging_dir):
    # Spent $176; remaining 4.00 is NOT < 4.00 -> the buy was fundable -> no event.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=176.0)])
    report = _run(ledger)
    assert report["sessions_with_event"] == 0
    assert report["events"] == []


def test_budget_left_means_no_event(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=100.0)])
    assert _run(ledger)["sessions_with_event"] == 0


def test_tier_a_price_is_not_an_event_even_when_exhausted(ledger, staging_dir):
    # 9.95 is below close 10.00 (Tier A) but above low 9.90, so it is not B-or-deeper.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_price=9.95)
    assert _run(ledger)["sessions_with_event"] == 0


def test_price_exactly_at_prior_low_is_tier_b(ledger, staging_dir):
    # The ladder's Tier B is "price <= previous day low", so equality counts.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_price=9.90)
    assert _run(ledger)["sessions_with_event"] == 1


# --- what counts as spent ---------------------------------------------------------------------------


def test_manual_fills_never_count_toward_spent(ledger, staging_dir):
    # $180 manual + $0 scheduled: scheduled spend is 0, so the day was not exhausted.
    _setup(ledger, staging_dir, fills=[_fill("m1", notional=180.0, run_kind="manual")])
    assert _run(ledger)["sessions_with_event"] == 0


def test_fills_after_the_slot_do_not_count(ledger, staging_dir):
    # The $180 filled at 15:30 ET, after the 13:30 quote: at 13:30 the budget was untouched.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0, filled_at=FILL_1530, slot="15:30")])
    assert _run(ledger)["sessions_with_event"] == 0


def test_spend_sums_across_tickers(ledger, staging_dir):
    # The daily cap is account-wide: $100 ZZZA + $80 ZZZB = $180 spent before 13:30 -> event on ZZZA.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=100.0), _fill("o2", notional=80.0, symbol="ZZZB")])
    assert _run(ledger)["sessions_with_event"] == 1


# --- point in time and fail closed ------------------------------------------------------------------


def test_rows_not_yet_known_are_invisible(ledger, staging_dir):
    # Everything was ingested at FIRST_SEEN; a reader at an earlier moment sees nothing.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    report = _run(ledger, known_at=utc(2026, 4, 1))
    assert report["sessions_with_event"] == 0
    assert report["quotes_considered"] == 0


def test_quote_fetched_after_known_at_is_invisible(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], quote_as_of=LATER)
    assert _run(ledger, known_at=FIRST_SEEN)["quotes_considered"] == 0


def test_missing_prior_bar_is_skipped_and_counted_not_guessed(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)], with_prior_bar=False)
    report = _run(ledger)
    assert report["sessions_with_event"] == 0
    assert report["skipped_no_prior_bar"] == 1


@pytest.mark.parametrize("cap", [0.0, -1.0, math.nan, math.inf])
def test_bad_daily_cap_raises(ledger, cap):
    with pytest.raises(ValueError, match="daily_cap_usd"):
        _run(ledger, daily_cap_usd=cap)


@pytest.mark.parametrize("cost", [0.0, -4.0, math.nan])
def test_bad_tier_b_cost_raises(ledger, cost):
    with pytest.raises(ValueError, match="tier_b_cost_usd"):
        _run(ledger, tier_b_cost_usd=cost)


def test_naive_known_at_is_refused(ledger):
    with pytest.raises(ValueError, match="known_at"):
        _run(ledger, known_at=datetime(2026, 6, 1))


# --- read-only and reporting shape ------------------------------------------------------------------


def test_function_never_writes(ledger, staging_dir):
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    before = {t: ledger.row_count(t) for t in ledger.tables()}
    _run(ledger)
    assert {t: ledger.row_count(t) for t in ledger.tables()} == before


def test_report_echoes_its_parameters(ledger, staging_dir):
    # The cap is a parameter, so the report must say which cap produced it.
    _setup(ledger, staging_dir, fills=[_fill("o1", notional=180.0)])
    report = _run(ledger, tier_b_cost_usd=6.0)
    assert report["daily_cap_usd"] == CAP
    assert report["tier_b_cost_usd"] == 6.0
    assert report["known_at"] == KNOW_ALL
    assert report["sessions_observed"] == 1
    assert report["quotes_considered"] == 1
