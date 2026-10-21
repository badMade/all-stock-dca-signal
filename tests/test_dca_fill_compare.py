"""Tests for expected-vs-actual fill comparison (plan-first Rock 5)."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from dca_fill_compare import FillComparator, slippage_pct
from dca_rec_models import ExpectedPrice, ExpectedPriceSet, Recommendation, RecommendationSet
from dca_rec_store import RecommendationStore
from scripts.validate_native_placement import native_ref_id

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 13)
AS_OF = datetime(2026, 10, 13, 12, 25, tzinfo=ET)


def _handed(store, slot="1230", orders=(("AAPL", 3.0, 100.0), ("MSFT", 2.0, 50.0))):
    rec = RecommendationSet(
        DAY, AS_OF, slot, "robinhood_agent_fetch", "ladder-v1", "hash-v1",
        tuple(Recommendation(t, usd, native_ref_id(slot, DAY.isoformat(), t)) for t, usd, _ in orders),
    )
    expected = ExpectedPriceSet(
        DAY, AS_OF, slot, "run_state_artifacts",
        tuple(ExpectedPrice(t, price, price * 1.02, None, 1.5, 1.96, usd) for t, usd, price in orders),
        {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900},
    )
    store.write(rec, expected)
    store.record_handed(rec)
    return rec


def _placement(store, slot, ticker, order_id):
    path = store.placements_dir(DAY, slot) / f"{ticker}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": {"order": {"id": order_id, "symbol": ticker, "state": "queued"}}}),
                    encoding="utf-8")
    return path


def _order(order_id, symbol, state, *, qty=None, avg=None, notional=None):
    row = {"id": order_id, "symbol": symbol, "state": state, "side": "buy",
           "created_at": "2026-10-13T16:31:00Z", "dollar_based_amount": {"amount": "3.00"}}
    if qty is not None:
        row["cumulative_quantity"] = str(qty)
    if avg is not None:
        row["average_price"] = str(avg)
    if notional is not None:
        row["executed_notional"] = {"amount": str(notional)}
    return row


@pytest.fixture
def store(tmp_path):
    return RecommendationStore(tmp_path / "recommendations")


def test_filled_order_reports_expected_fill_shares_and_slippage(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    rows = FillComparator(store).compare(DAY, [_order("oid-aapl", "AAPL", "filled", qty=0.02985, avg=100.5,
                                                      notional=3.0)])
    (row,) = rows
    assert row.status == "filled"
    assert (row.expected_price, row.fill_price) == (100.0, 100.5)
    assert row.slippage_pct == 0.5
    assert row.filled_shares == pytest.approx(0.02985)
    assert row.filled_usd == pytest.approx(3.0)
    assert row.requested_usd == 3.0
    assert row.recommendation_as_of == AS_OF.isoformat()


def test_slippage_matches_dca_reporting_sign_convention():
    assert slippage_pct(99.0, 100.0) == -1.0  # filled cheaper than expected: favourable for a buy
    assert slippage_pct(101.0, 100.0) == 1.0


def test_live_order_is_pending_with_no_fill_numbers(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    (row,) = FillComparator(store).compare(DAY, [_order("oid-aapl", "AAPL", "queued")])
    assert row.status == "pending"
    assert (row.fill_price, row.filled_usd, row.slippage_pct) == (None, None, None)


def test_partially_filled_live_order_is_reported_as_such(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    (row,) = FillComparator(store).compare(DAY, [_order("oid-aapl", "AAPL", "partially_filled", qty=0.01,
                                                        avg=100.2, notional=1.0)])
    assert row.status == "partially_filled"
    assert row.slippage_pct is None


def test_cancelled_order_with_no_fill_is_no_fill(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    (row,) = FillComparator(store).compare(DAY, [_order("oid-aapl", "AAPL", "cancelled")])
    assert row.status == "no_fill"


def test_join_is_by_order_id_never_by_ticker(store):
    # A same-ticker order from another agent or slot must not be scored as this buy.
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    rows = FillComparator(store).compare(DAY, [_order("someone-else", "AAPL", "filled", qty=1, avg=90.0)])
    assert rows[0].status == "order_not_found"
    assert rows[0].fill_price is None


def test_newer_order_state_replaces_older(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    rows = FillComparator(store).compare(DAY, [
        _order("oid-aapl", "AAPL", "queued"),
        _order("oid-aapl", "AAPL", "filled", qty=0.03, avg=100.0, notional=3.0),
    ])
    assert rows[0].status == "filled" and rows[0].slippage_pct == 0.0


def test_unreadable_placement_is_reported_not_skipped(store):
    _handed(store)
    path = _placement(store, "1230", "AAPL", "oid-aapl")
    path.write_text("{broken", encoding="utf-8")
    (row,) = FillComparator(store).compare(DAY, [])
    assert row.status == "placement_unreadable" and row.order_id is None


def test_placement_without_order_id_is_unreadable(store):
    _handed(store)
    path = store.placements_dir(DAY, "1230") / "AAPL.json"
    path.write_text(json.dumps({"data": {"order": {"symbol": "AAPL"}}}), encoding="utf-8")
    assert FillComparator(store).compare(DAY, [])[0].status == "placement_unreadable"


def test_placement_with_no_handed_record_still_reports_the_fill(store):
    _placement(store, "1330", "MSFT", "oid-msft")
    (row,) = FillComparator(store).compare(DAY, [_order("oid-msft", "MSFT", "filled", qty=0.04, avg=50.0,
                                                        notional=2.0)])
    assert row.status == "filled"
    assert (row.expected_price, row.requested_usd, row.slippage_pct) == (None, None, None)


def test_ticker_not_in_expected_set_has_no_expected_price(store):
    _handed(store, orders=(("AAPL", 3.0, 100.0),))
    _placement(store, "1230", "MSFT", "oid-msft")
    (row,) = FillComparator(store).compare(DAY, [_order("oid-msft", "MSFT", "filled", qty=0.04, avg=50.0,
                                                        notional=2.0)])
    assert row.expected_price is None and row.slippage_pct is None


def test_non_ticker_files_and_rows_without_ids_are_ignored(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    (store.placements_dir(DAY, "1230") / "notes.txt").write_text("x", encoding="utf-8")
    (store.placements_dir(DAY, "1230") / "prior_ids.json").write_text("[]", encoding="utf-8")
    rows = FillComparator(store).compare(DAY, [{"symbol": "AAPL"}, _order("oid-aapl", "AAPL", "queued")])
    assert [row.ticker for row in rows] == ["AAPL"]


def test_no_placements_means_no_rows(store):
    assert FillComparator(store).compare(DAY, []) == []


def test_report_is_written_atomically_with_summary(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-aapl")
    _placement(store, "1230", "MSFT", "oid-msft")
    comparator = FillComparator(store)
    rows = comparator.compare(DAY, [
        _order("oid-aapl", "AAPL", "filled", qty=0.03, avg=101.0, notional=3.0),
        _order("oid-msft", "MSFT", "filled", qty=0.04, avg=49.0, notional=2.0),
    ])
    path = comparator.write_report(DAY, rows, generated_at=datetime(2026, 10, 13, 16, 5, tzinfo=ET))
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["summary"] == {"placed": 2, "filled_with_slippage": 2, "mean_slippage_pct": -0.5}  # (+1% + -2%) / 2
    assert {order["ticker"] for order in document["orders"]} == {"AAPL", "MSFT"}


def test_report_with_no_fills_has_null_mean(store):
    path = FillComparator(store).write_report(DAY, [], generated_at=datetime(2026, 10, 13, 16, 5, tzinfo=ET))
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["mean_slippage_pct"] is None


def test_report_requires_an_aware_timestamp(store):
    with pytest.raises(ValueError):
        FillComparator(store).write_report(DAY, [], generated_at=datetime(2026, 10, 13, 16, 5))


# --- PR #1815 review: execution is classified independently of price availability -------------------


def test_cancelled_order_with_a_partial_fill_keeps_its_executed_amounts(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-1")
    row = _order("oid-1", "AAPL", "cancelled", qty=0.01, avg=100.0, notional=1.0)
    (fill,) = [r for r in FillComparator(store).compare(DAY, [row]) if r.ticker == "AAPL"]
    assert fill.status == "partially_filled" and fill.broker_state == "cancelled"
    assert fill.filled_usd == 1.0 and fill.filled_shares == 0.01
    assert fill.fill_price is None and fill.slippage_pct is None  # no trusted reference price


def test_filled_order_without_a_recoverable_price_still_reports_what_executed(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-1")
    # Filled, with executed shares but neither an average price nor executed notional to value them.
    row = _order("oid-1", "AAPL", "filled", qty=0.03)
    (fill,) = [r for r in FillComparator(store).compare(DAY, [row]) if r.ticker == "AAPL"]
    assert fill.status == "filled" and fill.filled_shares == 0.03  # previously fell through to no_fill
    assert fill.fill_price is None and fill.slippage_pct is None


def test_live_partial_order_is_never_reported_with_committed_notional_as_executed(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-1")
    row = _order("oid-1", "AAPL", "confirmed", qty=0.01)
    (fill,) = [r for r in FillComparator(store).compare(DAY, [row]) if r.ticker == "AAPL"]
    assert fill.status == "partially_filled" and fill.filled_usd is None and fill.filled_shares is None


def test_cancelled_order_with_nothing_executed_is_no_fill(store):
    _handed(store)
    _placement(store, "1230", "AAPL", "oid-1")
    (fill,) = [r for r in FillComparator(store).compare(DAY, [_order("oid-1", "AAPL", "cancelled")])
               if r.ticker == "AAPL"]
    assert fill.status == "no_fill" and fill.filled_usd is None
