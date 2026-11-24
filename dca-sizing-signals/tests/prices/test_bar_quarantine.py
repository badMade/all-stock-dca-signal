"""Backtest protocol item 6: anomalous inputs are quarantined, never interpolated, and give no price.

Phase A guard, written BEFORE the implementation (docs/backtest-gap-audit.md row 6,
docs/adr-backtest-protocol.md). The store half was revised on 2026-11-24 from an ingest-time reject to
a read-time quarantine (operator decision): a reject conflicted with the store's contract that a split
may be learned AFTER the bars crossing it (test_store_point_in_time.py's
test_known_at_hides_a_split_we_learned_about_later).

Today ``validate_bars`` checks each bar on its own (prices/validate.py:32-56): positive prices, high >=
low and so on. A raw close that falls 90% overnight passes, because nothing compares a bar with the
session before it or with the corporate actions known at the time. The contract:

* ``validate.bar_anomalies(bars, actions, *, previous_close=None, max_abs_move=0.5)`` returns one
  ``BarAnomaly(symbol, session, prior_close, close, move)`` per raw close-to-close move beyond
  ``max_abs_move``, after allowing for any split-type action between the two sessions.
  ``previous_close`` maps symbol -> the stored close that precedes the batch, so a jump across two
  batches is caught.
* Ingest is unchanged: every validated bar is stored as delivered.
* ``PriceStore.bar_anomalies(as_of_date=, known_at=)`` (macro ``bar_anomalies_known_at``) is the
  queryable quarantine record, with each anomalous bar's run_id and reason, judged with only the
  actions known at ``known_at``. It agrees with the pure check.
* ``PriceStore.checked_bars`` (macro ``checked_bars``) is ``adjusted_bars`` without every session of a
  symbol from its first unexplained jump onward: no price, never an interpolated or mis-adjusted one.
  Learning the split later explains the jump and brings the sessions back for that later ``known_at``.
  ``adjusted_bars`` itself is unchanged.

Fixture: REAL raw NVDA daily bars around the 10-for-1 split that went ex on 2024-06-10
(tests/prices/fixtures/robinhood_nvda_raw_2024-06-05_13.json, Robinhood get_equity_historicals with
adjustment_type=none, recorded 2026-11-23). Without the split known, 1208.88 -> 121.79 is a -89.9% move:
exactly the row a vendor glitch would also produce. With the split known it is +0.75%.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone

import pytest

from .conftest import load_fixture

RAW = load_fixture("robinhood_nvda_raw_2024-06-05_13.json")
SPLIT_EX = date(2024, 6, 10)
AS_OF = datetime(2024, 6, 14, 2, 0, tzinfo=timezone.utc)
# Tagged with an accepted source only because daily_bars_raw's CHECK admits two; see the fixture's
# _provenance. The values are Robinhood's.
SOURCE = "massive"


def _bars(first: date | None = None, last: date | None = None):
    from market_data_ingest.prices.models import DailyBar

    out = []
    for row in RAW["bars"]:
        session = date.fromisoformat(row["session"])
        if (first and session < first) or (last and session > last):
            continue
        out.append(
            DailyBar(
                source=SOURCE, symbol=RAW["symbol"], session=session, open=row["open"], high=row["high"],
                low=row["low"], close=row["close"], volume=row["volume"],
            )
        )
    return out


def _split():
    from market_data_ingest.prices.models import CorporateAction

    return CorporateAction(
        source=SOURCE, action_type="forward_split", symbol="NVDA", ex_date=SPLIT_EX, split_from=1, split_to=10
    )


def _validate():
    return importlib.import_module("market_data_ingest.prices.validate")


# --- the pure check ---------------------------------------------------------------------------------------


def test_the_unexplained_split_day_is_the_one_anomaly():
    anomalies = _validate().bar_anomalies(_bars(), [])
    assert [(a.symbol, a.session) for a in anomalies] == [("NVDA", SPLIT_EX)]
    (a,) = anomalies
    assert a.prior_close == 1208.88 and a.close == 121.79
    assert a.move == pytest.approx(121.79 / 1208.88 - 1, abs=1e-9)  # about -0.899


def test_a_known_split_explains_the_move():
    assert _validate().bar_anomalies(_bars(), [_split()]) == []


def test_a_jump_across_batches_is_caught_with_the_stored_previous_close():
    later = _bars(first=SPLIT_EX)
    assert _validate().bar_anomalies(later, []) == []  # inside this batch alone nothing jumps
    anomalies = _validate().bar_anomalies(later, [], previous_close={"NVDA": 1208.88})
    assert [(a.symbol, a.session) for a in anomalies] == [("NVDA", SPLIT_EX)]


def test_ordinary_real_sessions_are_not_flagged():
    # 2024-06-12 -> 06-13 is +3.5% on real data: well inside the bound.
    assert _validate().bar_anomalies(_bars(first=date(2024, 6, 11)), []) == []


# --- the store ------------------------------------------------------------------------------------------


def _ingest(store, staging_dir, bars, run_id, as_of=AS_OF):
    store.ingest_daily_bars(bars, run_id=run_id, as_of=as_of, staging_dir=staging_dir)


def test_an_unexplained_jump_is_stored_but_gives_no_price_from_there_on(store, staging_dir):
    _ingest(store, staging_dir, _bars(last=date(2024, 6, 7)), "clean")
    _ingest(store, staging_dir, _bars(first=SPLIT_EX), "glitch")  # no raise: ingest is unchanged

    assert store.row_count("daily_bars_raw") == 7  # stored as delivered
    checked = {b.session for b in store.checked_bars("NVDA", as_of_date=date(2024, 6, 13))}
    assert checked == {date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)}  # nothing after 06-07
    # The unchecked view still shows all seven, so the existing point-in-time contract holds.
    assert len(store.adjusted_bars("NVDA", as_of_date=date(2024, 6, 13))) == 7


def test_the_quarantine_record_names_the_bar_its_batch_and_why(store, staging_dir):
    _ingest(store, staging_dir, _bars(last=date(2024, 6, 7)), "clean")
    _ingest(store, staging_dir, _bars(first=SPLIT_EX), "glitch")

    rows = store.bar_anomalies(as_of_date=date(2024, 6, 13))
    assert [(r["symbol"], r["session"], r["close"], r["run_id"]) for r in rows] == [
        ("NVDA", SPLIT_EX, 121.79, "glitch")
    ]
    assert rows[0]["prior_close"] == 1208.88 and "1208.88" in rows[0]["reason"]
    assert rows[0]["move"] == pytest.approx(121.79 / 1208.88 - 1, abs=1e-9)


def test_a_known_split_explains_the_jump(store, staging_dir):
    store.ingest_corporate_actions([_split()], run_id="actions", as_of=AS_OF, staging_dir=staging_dir)
    _ingest(store, staging_dir, _bars(last=date(2024, 6, 7)), "clean")
    _ingest(store, staging_dir, _bars(first=SPLIT_EX), "after-split")

    bars = {b.session: b for b in store.checked_bars("NVDA", as_of_date=date(2024, 6, 13))}
    assert len(bars) == 7
    assert bars[date(2024, 6, 7)].close_split_adjusted == pytest.approx(120.888)
    assert store.bar_anomalies(as_of_date=date(2024, 6, 13)) == []


def test_a_split_learned_later_clears_the_quarantine_only_from_then_on(store, staging_dir):
    _ingest(store, staging_dir, _bars(), "all-bars")
    learned = AS_OF + timedelta(days=1)
    store.ingest_corporate_actions([_split()], run_id="late-split", as_of=learned, staging_dir=staging_dir)

    before = store.checked_bars("NVDA", as_of_date=date(2024, 6, 13), known_at=AS_OF)
    after = store.checked_bars("NVDA", as_of_date=date(2024, 6, 13), known_at=learned)
    assert [b.session for b in before] == [date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)]
    assert len(after) == 7
    assert len(store.bar_anomalies(as_of_date=date(2024, 6, 13), known_at=AS_OF)) == 1
    assert store.bar_anomalies(as_of_date=date(2024, 6, 13), known_at=learned) == []


def test_a_quarantined_session_gives_no_price_rather_than_a_neighbours(store, staging_dir):
    _ingest(store, staging_dir, _bars(), "all-bars")
    # The latest checked bar on or before 06-10 is 06-07's. A caller asking for 06-10 specifically gets nothing.
    assert [b for b in store.checked_bars("NVDA", as_of_date=SPLIT_EX) if b.session == SPLIT_EX] == []


def test_an_as_of_date_before_the_jump_is_untouched(store, staging_dir):
    _ingest(store, staging_dir, _bars(), "all-bars")
    assert len(store.checked_bars("NVDA", as_of_date=date(2024, 6, 7))) == 3
    assert store.bar_anomalies(as_of_date=date(2024, 6, 7)) == []


@pytest.mark.parametrize("with_split", [False, True], ids=["split-unknown", "split-known"])
def test_the_sql_quarantine_agrees_with_the_pure_check(store, staging_dir, with_split):
    actions = [_split()] if with_split else []
    if actions:
        store.ingest_corporate_actions(actions, run_id="actions", as_of=AS_OF, staging_dir=staging_dir)
    _ingest(store, staging_dir, _bars(), "all-bars")
    rows = store.bar_anomalies(as_of_date=date(2024, 6, 13))
    sql = [(r["symbol"], r["session"], r["prior_close"], r["close"]) for r in rows]
    pure = [(a.symbol, a.session, a.prior_close, a.close) for a in _validate().bar_anomalies(_bars(), actions)]
    assert sql == pure
