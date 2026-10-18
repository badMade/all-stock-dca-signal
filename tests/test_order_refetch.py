"""``scripts.order_refetch``: the ``get_equity_orders`` -> ``BrokerOrder`` mapping an external caller passes to
``dca_runner.execute_plan(refetch_orders_fn=...)`` (simultaneous-start guard, DCA Issues keys ``duplicate_fills``
and ``competing_executor``). Fail closed: anything that cannot be proven a complete, readable history raises, and
``execute_plan`` then halts the remainder instead of reading the failure as "no orders"."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import dca_runner
from conftest import FIXTURE_ACCOUNT_NUMBER
from scripts import order_refetch

ET = ZoneInfo("America/New_York")


def _row(order_id, symbol="MSFT", state="filled", created="2026-07-28T13:44:00Z", amount="2.00"):
    return {
        "id": order_id, "symbol": symbol, "side": "buy", "state": state, "created_at": created,
        "placed_agent": "agentic", "dollar_based_amount": {"amount": amount, "currency": "USD"},
    }


def _pager(*pages):
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return pages[len(calls) - 1]

    fetch.calls = calls
    return fetch


def test_maps_rows_to_broker_orders():
    fetch = _pager({"data": {"orders": [_row("o1")], "next": None}})
    [order] = order_refetch.make_refetch_orders_fn(fetch)()
    assert (order.symbol, order.side, order.state, order.dollar_amount) == ("MSFT", "buy", "filled", 2.0)
    assert order.placed_agent == "agentic"


def test_follows_the_cursor_across_pages_and_dedups_by_id():
    fetch = _pager(
        {"orders": [_row("o1"), _row("o2", "AAPL")], "next": "CUR1"},
        {"orders": [_row("o2", "AAPL"), _row("o3", "NVDA")], "next": ""},
    )
    orders = order_refetch.make_refetch_orders_fn(fetch)()
    assert fetch.calls == [None, "CUR1"]
    assert sorted(o.symbol for o in orders) == ["AAPL", "MSFT", "NVDA"]


def test_empty_history_is_an_empty_list_when_the_page_is_a_real_response():
    assert order_refetch.make_refetch_orders_fn(_pager({"orders": [], "next": None}))() == []


@pytest.mark.parametrize("page", [
    {"error": "boom"},
    {"success": False, "data": {"orders": []}},
    {},
    None,
    "not json",
])
def test_failure_or_unreadable_page_raises_instead_of_reading_as_no_orders(page):
    with pytest.raises(order_refetch.OrderRefetchError):
        order_refetch.make_refetch_orders_fn(_pager(page))()


def test_unreadable_row_raises():
    bad = _row("o1")
    bad["created_at"] = "2026-07-28T13:44:00"  # naive
    with pytest.raises(order_refetch.OrderRefetchError):
        order_refetch.make_refetch_orders_fn(_pager({"orders": [bad], "next": None}))()


def test_non_string_cursor_and_runaway_pagination_raise():
    with pytest.raises(order_refetch.OrderRefetchError):
        order_refetch.make_refetch_orders_fn(_pager({"orders": [], "next": 7}))()
    endless = lambda cursor: {"orders": [], "next": "MORE"}  # noqa: E731
    with pytest.raises(order_refetch.OrderRefetchError):
        order_refetch.make_refetch_orders_fn(endless, max_pages=3)()


def test_fetch_exception_text_is_not_leaked():
    def fetch(cursor):
        raise RuntimeError(f"token abc for account {FIXTURE_ACCOUNT_NUMBER}")

    with pytest.raises(order_refetch.OrderRefetchError) as caught:
        order_refetch.make_refetch_orders_fn(fetch)()
    assert FIXTURE_ACCOUNT_NUMBER not in str(caught.value) and "abc" not in str(caught.value)


def test_plugs_into_execute_plan_and_skips_the_ticker_another_engine_ordered():
    now = datetime(2026, 7, 28, 9, 43, tzinfo=ET)
    day = date(2026, 7, 28)
    calls = []
    broker = dca_runner.build_runner_broker(
        place_fn=lambda **kw: calls.append(kw["symbol"]) or {"id": "ok"},
        enable_live_orders=True, dry_run=False, account_number=FIXTURE_ACCOUNT_NUMBER,
    )
    snapshot = dca_runner.budget_snapshot([], day)
    tier = dca_runner.BudgetTier("normal", 180.0, 900.0)
    candidates = [dca_runner.Candidate(t, 2.0) for t in ("AAPL", "MSFT")]
    plan = dca_runner.plan_orders(candidates=candidates, snapshot=snapshot, tier=tier)
    fetch = _pager(*[{"orders": [_row("o9")], "next": None}] * 2)  # MSFT filled 09:44 ET by the other engine
    result = dca_runner.execute_plan(
        plan, broker, run_id="test-run-2026-07-28", snapshot=snapshot, trading_day=day, orders=[], tier=tier,
        buying_power_usd=1000.0, orders_account_number=FIXTURE_ACCOUNT_NUMBER, now=now, now_fn=lambda: now,
        slot_id="0935", orders_fetched_at=now, candidates=candidates,
        refetch_orders_fn=order_refetch.make_refetch_orders_fn(fetch),
    )
    assert calls == ["AAPL"] and result.skipped_already_ordered == ["MSFT"]
