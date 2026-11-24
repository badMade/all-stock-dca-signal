"""Backtest protocol item 6: anomalous inputs are quarantined, never interpolated, and give no price.

Phase A guard, written BEFORE the implementation (docs/backtest-gap-audit.md row 6,
docs/adr-backtest-protocol.md). Every test here is xfail(strict=True) until the quarantine exists, and
turns into a strict XPASS failure once it passes, so the marker goes in the change that makes the guard
real.

Today ``validate_bars`` checks each bar on its own (prices/validate.py:32-56): positive prices, high >=
low and so on. A raw close that falls 90% overnight passes, because nothing compares a bar with the
session before it or with the corporate actions known at the time. The contract:

* ``validate.bar_anomalies(bars, actions, *, previous_close=None, max_abs_move=0.5)`` returns one
  ``BarAnomaly(symbol, session, prior_close, close, move)`` per raw close-to-close move beyond
  ``max_abs_move``, after allowing for any split-type action with that ex-date. ``previous_close``
  maps symbol -> the stored close that precedes the batch, so a jump across two batches is caught.
* ``PriceStore.ingest_daily_bars`` runs that check against ``actions_known_at(as_of)`` and the stored
  bars. A batch with an anomaly writes NOTHING to ``daily_bars_raw`` (the existing strict
  one-bad-row-rejects-the-batch rule, operator PLAN Q4). It writes the anomalous rows to a new
  append-only ``quarantined_bars`` table with the reason, then raises ``AnomalousBarsError``. A
  rejected batch leaves a queryable record instead of only an exception message.
* No interpolation: ``adjusted_bars`` never shows a session the store did not accept.

Fixture: REAL raw NVDA daily bars around the 10-for-1 split that went ex on 2024-06-10
(tests/prices/fixtures/robinhood_nvda_raw_2024-06-05_13.json, Robinhood get_equity_historicals with
adjustment_type=none, recorded 2026-11-23). Without the split known, 1208.88 -> 121.79 is a -89.9% move:
exactly the row a vendor glitch would also produce. With the split known it is +0.75%.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timezone

import pytest

from .conftest import load_fixture

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=(ImportError, AttributeError, AssertionError, TypeError),
    reason="phase A guard: bar quarantine not implemented yet (backtest protocol item 6)",
)

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


def _errors():
    return importlib.import_module("market_data_ingest.prices.errors")


def _quarantine(store, sql: str):
    # Assert the table exists first, so a missing table is an assertion failure, not a DuckDB error.
    tables = store._rows("SELECT table_name FROM information_schema.tables WHERE table_name = 'quarantined_bars'", [])
    assert tables, "quarantined_bars table does not exist"
    return store._rows(sql, [])


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


def test_an_anomalous_batch_lands_in_quarantine_and_nowhere_else(store, staging_dir):
    store.ingest_daily_bars(_bars(last=date(2024, 6, 7)), run_id="clean", as_of=AS_OF, staging_dir=staging_dir)
    with pytest.raises(_errors().AnomalousBarsError):
        store.ingest_daily_bars(_bars(first=SPLIT_EX), run_id="glitch", as_of=AS_OF, staging_dir=staging_dir)

    accepted = {b.session for b in store.adjusted_bars("NVDA", as_of_date=date(2024, 6, 13))}
    assert accepted == {date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)}  # nothing filled in after 06-07

    rows = _quarantine(store, "SELECT symbol, session, close, reason, run_id FROM quarantined_bars ORDER BY session")
    assert [(r["symbol"], r["session"], r["close"], r["run_id"]) for r in rows] == [
        ("NVDA", SPLIT_EX, 121.79, "glitch")
    ]
    assert "1208.88" in rows[0]["reason"]


def test_the_same_batch_is_accepted_once_the_split_is_known(store, staging_dir):
    store.ingest_corporate_actions([_split()], run_id="actions", as_of=AS_OF, staging_dir=staging_dir)
    store.ingest_daily_bars(_bars(last=date(2024, 6, 7)), run_id="clean", as_of=AS_OF, staging_dir=staging_dir)
    store.ingest_daily_bars(_bars(first=SPLIT_EX), run_id="after-split", as_of=AS_OF, staging_dir=staging_dir)

    bars = {b.session: b for b in store.adjusted_bars("NVDA", as_of_date=date(2024, 6, 13))}
    assert len(bars) == 7
    assert bars[date(2024, 6, 7)].close_split_adjusted == pytest.approx(120.888)
    assert _quarantine(store, "SELECT count(*) AS n FROM quarantined_bars")[0]["n"] == 0


def test_a_quarantined_session_gives_no_price_rather_than_a_neighbours(store, staging_dir):
    store.ingest_daily_bars(_bars(last=date(2024, 6, 7)), run_id="clean", as_of=AS_OF, staging_dir=staging_dir)
    with pytest.raises(_errors().AnomalousBarsError):
        store.ingest_daily_bars(_bars(first=SPLIT_EX), run_id="glitch", as_of=AS_OF, staging_dir=staging_dir)
    # The latest bar on or before 06-10 is 06-07's. A caller asking for 06-10 specifically gets nothing.
    assert [b for b in store.adjusted_bars("NVDA", as_of_date=SPLIT_EX) if b.session == SPLIT_EX] == []
