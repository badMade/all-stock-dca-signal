"""Tests for fail-closed same-day broker reconciliation and effective prior.

The headline regression is the 2026-07-23 incident: the artifacts showed a
symbol's prior allocation as $0 while a real same-day Robinhood fill existed, and
the top-up guard returned the FULL desired amount. These tests pin the fixed
behavior: the reconciler folds broker exposure into the effective prior, and the
decision fails closed when broker coverage cannot be proven complete.
"""

from __future__ import annotations

import pytest

from scripts import reason_codes as rc
from scripts.broker_reconciliation import (
    BrokerOrder,
    EffectivePrior,
    SnapshotResult,
    decide_effective_top_up,
    normalize_order,
    paginate_same_day_orders,
    reconcile_effective_prior,
    reconciliation_metrics,
    same_day_order_snapshot,
)
from scripts.run_classification import PriorAllocation

# --- helpers -----------------------------------------------------------------


def _raw(order_id, symbol, state, *, qty=None, price=None, notional=None, created_at=None):
    raw = {"id": order_id, "symbol": symbol, "state": state, "side": "buy"}
    if qty is not None:
        raw["cumulative_quantity"] = qty
    if price is not None:
        raw["average_price"] = price
    if notional is not None:
        raw["total_notional"] = notional
    if created_at is not None:
        raw["created_at"] = created_at
    return raw


def _paginator(pages):
    """Fake get_equity_orders. ``pages`` = list of (orders, next_cursor|None)."""
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        cursor = kwargs.get("cursor")
        idx = 0 if cursor is None else int(cursor)
        orders, nxt = pages[idx]
        resp = {"orders": orders}
        if nxt is not None:
            resp["next"] = f"https://api.robinhood.com/orders/?cursor={nxt}"
        return resp

    return fn, calls


# --- normalize_order ---------------------------------------------------------


def test_normalize_filled_order_uses_executed_dollars_and_reference_price():
    order = normalize_order(_raw("o1", "TXN", "filled", qty="0.5", price="2.00"))
    assert order is not None
    assert order.notional_usd == pytest.approx(1.0)
    assert order.is_filled and order.is_live
    assert order.reference_price == pytest.approx(2.0)


def test_normalize_live_unfilled_order_counts_planned_notional_no_reference():
    order = normalize_order(_raw("o2", "TXN", "confirmed", notional="1.00"))
    assert order is not None
    assert order.notional_usd == pytest.approx(1.0)
    assert order.is_live and not order.is_filled
    # A live-but-unfilled order's price is never trusted as a reference.
    assert order.reference_price is None


def test_normalize_terminal_state_has_zero_exposure():
    for state in ("cancelled", "rejected", "failed", "voided"):
        order = normalize_order(_raw("o3", "TXN", state, notional="1.00"))
        assert order is not None
        assert not order.is_live
        assert order.notional_usd == 0.0


def test_normalize_rejects_rows_missing_id_or_symbol():
    assert normalize_order({"symbol": "TXN", "state": "filled"}) is None
    assert normalize_order({"id": "o4", "state": "filled"}) is None


def test_normalize_symbol_is_uppercased():
    order = normalize_order(_raw("o5", "txn", "filled", qty="1", price="1"))
    assert order is not None and order.symbol == "TXN"


# --- pagination --------------------------------------------------------------


def test_paginate_single_page_is_complete():
    fn, calls = _paginator([([_raw("o1", "TXN", "filled", qty="1", price="1")], None)])
    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert result.complete
    assert result.pages_fetched == 1
    assert len(result.orders) == 1
    # First page must NOT send a cursor.
    assert "cursor" not in calls[0]


def test_paginate_follows_cursor_across_pages():
    fn, calls = _paginator(
        [
            ([_raw("o1", "AAA", "filled", qty="1", price="1")], "1"),
            ([_raw("o2", "BBB", "filled", qty="1", price="2")], None),
        ]
    )
    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert result.complete
    assert result.pages_fetched == 2
    assert {o.order_id for o in result.orders} == {"o1", "o2"}
    assert calls[1]["cursor"] == "1"


def test_paginate_error_page_fails_closed():
    def fn(**kwargs):
        return {"success": False, "error": "throttled"}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("page_error_response" in r for r in result.incomplete_reasons)


def test_paginate_exception_fails_closed():
    def fn(**kwargs):
        raise RuntimeError("network down")

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("page_fetch_exception" in r for r in result.incomplete_reasons)


def test_paginate_max_pages_fails_closed():
    # Every page points to a new cursor -> never terminates on its own.
    def fn(**kwargs):
        cur = kwargs.get("cursor")
        nxt = 1 if cur is None else int(cur) + 1
        return {"orders": [], "next": f"https://api/orders/?cursor={nxt}"}

    result = paginate_same_day_orders(
        fn, account_number="ACCT", created_at_gte="2026-07-23", max_pages=3
    )
    assert not result.complete
    assert result.pages_fetched == 3
    assert any("max_pages_exceeded" in r for r in result.incomplete_reasons)


def test_paginate_repeated_cursor_fails_closed():
    def fn(**kwargs):
        return {"orders": [], "next": "https://api/orders/?cursor=SAME"}

    result = paginate_same_day_orders(
        fn, account_number="ACCT", created_at_gte="2026-07-23", max_pages=10
    )
    assert not result.complete
    assert any("repeated_pagination_cursor" in r for r in result.incomplete_reasons)


def test_paginate_requires_account_number():
    fn, _ = _paginator([([], None)])
    with pytest.raises(ValueError):
        paginate_same_day_orders(fn, account_number="", created_at_gte="2026-07-23")


def test_paginate_persists_raw_pages():
    fn, _ = _paginator([([_raw("o1", "TXN", "filled", qty="1", price="1")], None)])
    sink: list = []
    paginate_same_day_orders(
        fn, account_number="ACCT", created_at_gte="2026-07-23", raw_sink=sink
    )
    assert len(sink) == 1 and "orders" in sink[0]


# --- snapshot + order-id fallback --------------------------------------------


def test_snapshot_resolves_missing_expected_id_via_fallback():
    # The paged snapshot returns nothing (empty individual account, the 3:40
    # watchdog symptom), but the artifact recorded order o9.
    def orders_fn(**kwargs):
        if "order_id" in kwargs:
            return {"orders": [_raw(kwargs["order_id"], "TXN", "filled", qty="0.5", price="2.0")]}
        return {"orders": []}

    snap = same_day_order_snapshot(
        orders_fn,
        account_number="ACCT",
        created_at_gte="2026-07-23",
        expected_order_ids=["o9"],
    )
    assert snap.complete
    assert "o9" in snap.resolved_by_fallback
    assert not snap.unresolved_order_ids
    assert any(o.order_id == "o9" for o in snap.orders)


def test_snapshot_unresolved_expected_id_forces_incomplete():
    def orders_fn(**kwargs):
        if "order_id" in kwargs:
            return {"orders": []}  # fallback cannot find it
        return {"orders": []}

    snap = same_day_order_snapshot(
        orders_fn,
        account_number="ACCT",
        created_at_gte="2026-07-23",
        expected_order_ids=["missing"],
    )
    assert not snap.complete
    assert snap.unresolved_order_ids == ["missing"]


def test_snapshot_dedupes_paged_and_fallback_same_id():
    def orders_fn(**kwargs):
        row = _raw("o1", "TXN", "filled", qty="1", price="1")
        return {"orders": [row]}

    snap = same_day_order_snapshot(
        orders_fn,
        account_number="ACCT",
        created_at_gte="2026-07-23",
        expected_order_ids=["o1"],  # already present -> no fallback, no dupe
    )
    assert len([o for o in snap.orders if o.order_id == "o1"]) == 1
    assert snap.resolved_by_fallback == []


# --- reconcile_effective_prior ----------------------------------------------


def _snapshot(orders, *, complete=True):
    return SnapshotResult(
        orders=list(orders),
        pages_fetched=1,
        pagination_complete=complete,
        unresolved_order_ids=[] if complete else ["x"],
    )


def test_reconcile_counts_broker_only_missed_fill():
    # Artifacts empty; broker shows a same-day TXN fill -> effective prior = $1.
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0)])
    effective = reconcile_effective_prior({}, snap)
    assert "TXN" in effective
    ep = effective["TXN"]
    assert ep.effective_prior_usd == pytest.approx(1.0)
    assert ep.broker_only is True
    assert ep.reference_price == pytest.approx(2.0)


def test_reconcile_does_not_double_count_order_already_in_artifacts():
    alloc = PriorAllocation(ticker="TXN", prior_usd=1.0, reference_price=2.0, order_ids=["o1"])
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    # The order is in both sources; max(artifact=1, broker=1) avoids double-count.
    assert effective["TXN"].effective_prior_usd == pytest.approx(1.0)


def test_reconcile_adds_new_broker_order_to_artifact_prior():
    alloc = PriorAllocation(ticker="TXN", prior_usd=1.0, reference_price=2.0, order_ids=["o1"])
    snap = _snapshot(
        [
            BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0),
            BrokerOrder("o2", "TXN", "filled", 1.0, True, True, 1.5),
        ]
    )
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].effective_prior_usd == pytest.approx(2.0)
    # Reference is the lowest confirmed price across artifact + new broker order.
    assert effective["TXN"].reference_price == pytest.approx(1.5)


def test_reconcile_live_unfilled_broker_order_poisons_reference():
    snap = _snapshot([BrokerOrder("o1", "TXN", "confirmed", 1.0, True, False, None)])
    effective = reconcile_effective_prior({}, snap)
    assert effective["TXN"].effective_prior_usd == pytest.approx(1.0)
    assert effective["TXN"].reference_price is None


def test_reconcile_propagates_incomplete_coverage():
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0)], complete=False)
    effective = reconcile_effective_prior({}, snap)
    assert effective["TXN"].coverage_complete is False


# --- decide_effective_top_up (the fail-closed gate) --------------------------


def _ep(**kw):
    base = dict(
        symbol="TXN",
        artifact_prior_usd=0.0,
        broker_prior_usd=0.0,
        effective_prior_usd=0.0,
        reference_price=None,
        coverage_complete=True,
    )
    base.update(kw)
    return EffectivePrior(**base)


def test_decide_fails_closed_when_coverage_incomplete():
    # The 2026-07-23 scenario: artifacts empty, but coverage could not be proven,
    # so we must NOT buy the full desired amount.
    ep = _ep(effective_prior_usd=0.0, coverage_complete=False)
    decision = decide_effective_top_up("TXN", 2.0, ep, current_price=1.0)
    assert decision.incremental_usd == 0.0
    assert decision.reason == rc.BROKER_HISTORY_INCOMPLETE
    assert decision.coverage_complete is False


def test_decide_first_allocation_when_no_prior_and_complete():
    ep = _ep(effective_prior_usd=0.0, coverage_complete=True)
    decision = decide_effective_top_up("TXN", 2.0, ep, current_price=1.0)
    assert decision.incremental_usd == pytest.approx(2.0)
    assert decision.reason == rc.FIRST_ALLOCATION


def test_decide_effective_top_up_when_price_below_reference():
    ep = _ep(effective_prior_usd=1.0, reference_price=2.0)
    decision = decide_effective_top_up("TXN", 3.0, ep, current_price=1.5)
    assert decision.incremental_usd == pytest.approx(2.0)
    assert decision.reason == rc.EFFECTIVE_TOP_UP


def test_decide_no_lower_price_blocks_top_up():
    ep = _ep(effective_prior_usd=1.0, reference_price=2.0)
    decision = decide_effective_top_up("TXN", 3.0, ep, current_price=2.5)
    assert decision.incremental_usd == 0.0
    assert decision.reason == rc.NO_LOWER_PRICE


def test_decide_already_met_when_effective_prior_exceeds_desired():
    ep = _ep(effective_prior_usd=3.0, reference_price=1.0)
    decision = decide_effective_top_up("TXN", 2.0, ep, current_price=0.5)
    assert decision.incremental_usd == 0.0
    assert decision.reason == rc.ALREADY_MET_OR_OVERFILLED


def test_decide_missing_reference_price_blocks_top_up():
    ep = _ep(effective_prior_usd=1.0, reference_price=None)
    decision = decide_effective_top_up("TXN", 3.0, ep, current_price=1.5)
    assert decision.incremental_usd == 0.0
    assert decision.reason == rc.MISSING_PRIOR_PRICE


def test_regression_broker_fill_missing_from_artifacts_blocks_full_rebuy():
    """End-to-end: artifacts say $0 prior, broker has a same-day fill.

    Before the fix the guard saw prior=0 and returned the full $2. After the fix
    the reconciler folds the $1 broker fill into the effective prior, so with the
    price not below the fill price the top-up is blocked -- no full re-buy.
    """
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0)])
    effective = reconcile_effective_prior({}, snap)["TXN"]
    # Current price is NOT below the $2 fill -> no additional buy.
    decision = decide_effective_top_up("TXN", 2.0, effective, current_price=2.0)
    assert decision.incremental_usd == 0.0
    assert decision.reason in (rc.NO_LOWER_PRICE, rc.ALREADY_MET_OR_OVERFILLED)


# --- metrics -----------------------------------------------------------------


def test_reconciliation_metrics_flags_prior_allocation_discrepancy():
    snap = SnapshotResult(
        orders=[BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0)],
        pages_fetched=1,
        pagination_complete=True,
        resolved_by_fallback=["o1"],
    )
    effective = reconcile_effective_prior({}, snap)
    metrics = reconciliation_metrics(snap, effective, {})
    assert metrics["coverage_complete"] is True
    assert metrics["symbols_with_broker_exposure"] == 1
    assert "TXN" in metrics["prior_allocation_discrepancies"]
    assert metrics["resolved_by_fallback"] == ["o1"]


# --- review-fix regressions --------------------------------------------------


def test_paginate_next_url_without_cursor_fails_closed():
    # A next URL exists but has no cursor param: more may exist, cannot page it.
    def fn(**kwargs):
        return {"orders": [], "next": "https://api.robinhood.com/orders/"}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("next_url_without_usable_cursor" in r for r in result.incomplete_reasons)


def test_snapshot_fallback_accepts_single_order_envelope():
    # order_id_fn returns {"order": {...}} (a form get_order_fill accepts) -> resolves.
    def orders_fn(**kwargs):
        if "order_id" in kwargs:
            return {
                "order": {
                    "id": kwargs["order_id"],
                    "symbol": "TXN",
                    "state": "filled",
                    "cumulative_quantity": "0.5",
                    "average_price": "2.0",
                }
            }
        return {"orders": []}

    snap = same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23", expected_order_ids=["o9"]
    )
    assert snap.complete
    assert "o9" in snap.resolved_by_fallback


def test_snapshot_fallback_accepts_bare_order_object():
    def orders_fn(**kwargs):
        if "order_id" in kwargs:
            return {"id": kwargs["order_id"], "symbol": "TXN", "state": "filled",
                    "cumulative_quantity": "1", "average_price": "1"}
        return {"orders": []}

    snap = same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23", expected_order_ids=["z1"]
    )
    assert snap.complete and "z1" in snap.resolved_by_fallback


def test_reconcile_excludes_same_day_sell():
    sell = BrokerOrder("s1", "TXN", "filled", 5.0, True, True, 2.0, side="sell")
    effective = reconcile_effective_prior({}, _snapshot([sell]))
    assert effective["TXN"].broker_prior_usd == 0.0
    assert effective["TXN"].effective_prior_usd == 0.0


def test_reconcile_counts_buy_not_sell_for_same_symbol():
    buy = BrokerOrder("b1", "TXN", "filled", 1.0, True, True, 2.0, side="buy")
    sell = BrokerOrder("s1", "TXN", "filled", 5.0, True, True, 3.0, side="sell")
    effective = reconcile_effective_prior({}, _snapshot([buy, sell]))
    assert effective["TXN"].effective_prior_usd == pytest.approx(1.0)


def test_reconcile_symbol_and_order_ids_are_deterministic():
    alloc = PriorAllocation(ticker="TXN", prior_usd=1.0, reference_price=2.0, order_ids=["a", "b", "c"])
    snap = _snapshot([BrokerOrder("d", "TXN", "filled", 1.0, True, True, 1.5, side="buy")])
    r1 = reconcile_effective_prior({"TXN": alloc}, snap)
    r2 = reconcile_effective_prior({"TXN": alloc}, snap)
    assert r1["TXN"].order_ids == r2["TXN"].order_ids == ["a", "b", "c", "d"]


# --- review-fix regressions (round 2) ---------------------------------------


def test_partial_fill_counts_full_working_notional():
    # $0.5 x $2 = $1 executed, but $10 total is still working on the book.
    order = normalize_order(_raw("p1", "TXN", "partially_filled", qty="0.5", price="2.0", notional="10.0"))
    assert order is not None and order.is_live and not order.is_filled
    assert order.notional_usd == pytest.approx(10.0)


def test_paginate_none_response_fails_closed():
    result = paginate_same_day_orders(lambda **k: None, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("malformed_page_response" in r for r in result.incomplete_reasons)


def test_paginate_empty_dict_response_fails_closed():
    result = paginate_same_day_orders(lambda **k: {}, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("malformed_page_response" in r for r in result.incomplete_reasons)


def test_paginate_list_response_fails_closed():
    result = paginate_same_day_orders(lambda **k: [], account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete


def test_paginate_empty_but_valid_page_is_complete():
    result = paginate_same_day_orders(
        lambda **k: {"orders": []}, account_number="ACCT", created_at_gte="2026-07-23"
    )
    assert result.complete


def test_reconcile_counts_broker_order_when_artifact_prior_is_zero():
    # Artifact retained order_id but prior_usd=0 (unparseable amount): the broker
    # snapshot must still supply the notional, not be suppressed by the id.
    alloc = PriorAllocation(ticker="TXN", prior_usd=0.0, reference_price=None, order_ids=["o1"])
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, 2.0, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].effective_prior_usd == pytest.approx(1.0)
    assert effective["TXN"].order_ids == ["o1"]  # no duplicate


# --- review-fix regressions (round 3) ---------------------------------------


def test_reconcile_mixed_unparseable_artifact_counts_broker():
    # Artifact prior=$5 from order A only; order B's amount was unparseable but
    # its id was retained. A complete broker snapshot has A=$5 and B=$3.
    alloc = PriorAllocation(ticker="TXN", prior_usd=5.0, reference_price=2.0, order_ids=["A", "B"])
    snap = _snapshot(
        [
            BrokerOrder("A", "TXN", "filled", 5.0, True, True, 2.0, side="buy"),
            BrokerOrder("B", "TXN", "filled", 3.0, True, True, 1.5, side="buy"),
        ]
    )
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    # max(artifact 5, broker 8) = 8 -> B's live exposure is not undercounted.
    assert effective["TXN"].effective_prior_usd == pytest.approx(8.0)


def test_paginate_unnormalizable_order_row_fails_closed():
    # A well-formed page whose only order row has no symbol -> cannot reconcile.
    def fn(**kwargs):
        return {"orders": [{"id": "x1", "state": "filled", "cumulative_quantity": "1", "average_price": "1"}]}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("unnormalizable_order_row" in r for r in result.incomplete_reasons)


# --- review-fix regressions (round 4) ---------------------------------------


def test_order_notional_filled_uses_executed_not_planned():
    # A filled order for $95 with a $100 target must count $95 (exact), not $100.
    order = normalize_order(_raw("f1", "TXN", "filled", qty="95", price="1.0", notional="100"))
    assert order is not None and order.is_filled
    assert order.notional_usd == pytest.approx(95.0)


def test_next_cursor_prefers_next_url_over_echoed_current_cursor():
    from scripts.broker_reconciliation import _next_cursor

    resp = {"orders": [], "cursor": "CURRENT", "next": "https://api/orders/?cursor=NEXTPAGE"}
    assert _next_cursor(resp) == "NEXTPAGE"


def test_next_cursor_uses_bare_cursor_as_last_resort():
    from scripts.broker_reconciliation import _next_cursor

    assert _next_cursor({"orders": [], "cursor": "ONLY"}) == "ONLY"
    assert _next_cursor({"orders": []}) is None


# --- review-fix regressions (round 5) ---------------------------------------


def test_paginate_null_order_row_fails_closed():
    result = paginate_same_day_orders(
        lambda **k: {"orders": [None]}, account_number="ACCT", created_at_gte="2026-07-23"
    )
    assert not result.complete
    assert any("unnormalizable_order_row" in r for r in result.incomplete_reasons)


def test_paginate_mixed_valid_and_null_rows_fails_closed():
    page = {"orders": [_raw("o1", "TXN", "filled", qty="1", price="1"), None]}
    result = paginate_same_day_orders(
        lambda **k: page, account_number="ACCT", created_at_gte="2026-07-23"
    )
    assert not result.complete


# --- review-fix regressions (round 6) ---------------------------------------


def test_reconcile_retains_artifact_reference_when_broker_fill_lacks_price():
    # A confirmed artifact reference must survive a broker filled order that has
    # notional but no reported price (a settled fill, not a working order).
    alloc = PriorAllocation(ticker="TXN", prior_usd=1.0, reference_price=2.0, order_ids=["o1"])
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, None, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].reference_price == pytest.approx(2.0)


def test_reconcile_live_unconfirmed_still_poisons_even_with_artifact_reference():
    alloc = PriorAllocation(ticker="TXN", prior_usd=1.0, reference_price=2.0, order_ids=["o1"])
    # A still-working (not filled) broker order poisons the reference.
    snap = _snapshot([BrokerOrder("o2", "TXN", "confirmed", 1.0, True, False, None, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].reference_price is None


# --- review-fix regressions (round 7) ---------------------------------------


def test_snapshot_live_order_unknown_notional_fails_closed():
    # A live confirmed buy with NO parseable amount is unsizable exposure.
    def orders_fn(**kwargs):
        return {"orders": [{"id": "u1", "symbol": "TXN", "state": "confirmed", "side": "buy"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not snap.complete
    assert "u1" in snap.unknown_exposure_order_ids


def test_snapshot_live_order_with_notional_is_complete():
    def orders_fn(**kwargs):
        return {"orders": [{"id": "k1", "symbol": "TXN", "state": "confirmed", "side": "buy",
                            "total_notional": "1.00"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert snap.complete
    assert not snap.unknown_exposure_order_ids


# --- review-fix regressions (round 8) ---------------------------------------


def test_snapshot_unknown_broker_state_fails_closed():
    def orders_fn(**kwargs):
        # A broker-added state that is neither known-live nor known-terminal.
        return {"orders": [{"id": "p1", "symbol": "TXN", "state": "held", "side": "buy",
                            "total_notional": "1.00"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not snap.complete
    assert "p1" in snap.unknown_exposure_order_ids


def test_snapshot_missing_state_fails_closed():
    def orders_fn(**kwargs):
        return {"orders": [{"id": "m1", "symbol": "TXN", "side": "buy", "total_notional": "1.00"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not snap.complete


def test_snapshot_known_terminal_order_is_not_unknown():
    def orders_fn(**kwargs):
        return {"orders": [{"id": "c1", "symbol": "TXN", "state": "cancelled", "side": "buy"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert snap.complete


# --- review-fix regressions (round 9) ---------------------------------------


def test_snapshot_unsized_sell_does_not_block_buys():
    # A live sell with quantity but no price/notional -> notional 0, but a sell
    # is not buy exposure, so it must NOT fail the whole snapshot closed.
    def orders_fn(**kwargs):
        return {"orders": [{"id": "sell1", "symbol": "TXN", "state": "confirmed",
                            "side": "sell", "quantity": "3"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert snap.complete
    assert "sell1" not in snap.unknown_exposure_order_ids


def test_snapshot_unsized_buy_still_blocks():
    def orders_fn(**kwargs):
        return {"orders": [{"id": "buy1", "symbol": "TXN", "state": "confirmed", "side": "buy"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not snap.complete


# --- review-fix regressions (round 10) --------------------------------------


def test_terminal_partial_fill_counts_executed_notional():
    # A buy that partially filled ($0.5 x $2 = $1) then ended cancelled: the
    # executed cash is real prior exposure even though the order is no longer live.
    # Only the executed portion counts, not the $10 planned (unfilled) remainder.
    order = normalize_order(_raw("t1", "TXN", "cancelled", qty="0.5", price="2.0", notional="10.0"))
    assert order is not None and not order.is_live
    assert order.notional_usd == pytest.approx(1.0)


def test_reconcile_counts_terminal_partial_fill_exposure():
    # A cancelled-after-partial-fill buy must offset the top-up so a full
    # FIRST_ALLOCATION is not issued on top of already-executed shares.
    snap = _snapshot([BrokerOrder("t1", "TXN", "cancelled", 1.0, False, False, None, side="buy")])
    effective = reconcile_effective_prior({}, snap)
    assert effective["TXN"].broker_prior_usd == pytest.approx(1.0)
    assert effective["TXN"].effective_prior_usd == pytest.approx(1.0)


def test_terminal_order_without_fill_still_zero_exposure():
    # A cancelled order that never filled (only a planned amount) is NOT exposure.
    order = normalize_order(_raw("t2", "TXN", "cancelled", notional="10.0"))
    assert order is not None and order.notional_usd == 0.0


def test_normalize_ignores_limit_price_as_fill_price():
    # `price` is the LIMIT price, not the executed fill. A row that only reports
    # `price` (no average_price) must not treat the limit as the fill price for
    # the reference or the executed notional.
    raw = {"id": "l1", "symbol": "TXN", "state": "filled", "side": "buy",
           "cumulative_quantity": "1", "price": "5.0"}
    order = normalize_order(raw)
    assert order is not None
    assert order.reference_price is None
    assert order.notional_usd == 0.0


# --- review-fix regressions (round 11) --------------------------------------


def test_share_based_live_order_sizes_from_quantity_times_price():
    # A live buy with quantity + limit price but no dollar-notional field must be
    # sized as qty*price (not $0), so it counts as exposure instead of being
    # flagged unsizable and blocking the whole window.
    raw = {"id": "s1", "symbol": "TXN", "state": "confirmed", "side": "buy",
           "quantity": "3", "price": "2.0"}
    order = normalize_order(raw)
    assert order is not None and order.is_live
    assert order.notional_usd == pytest.approx(6.0)


def test_share_based_live_order_does_not_block_snapshot():
    def orders_fn(**kwargs):
        return {"orders": [{"id": "s1", "symbol": "TXN", "state": "confirmed",
                            "side": "buy", "quantity": "3", "price": "2.0"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert snap.complete
    assert "s1" not in snap.unknown_exposure_order_ids


def test_reconcile_broker_only_priceless_fill_poisons_reference():
    # A broker-only settled order with exposure but no confirmed fill price leaves
    # the true minimum prior price unknown -> the reference must poison (fail
    # closed), not silently adopt another order's price.
    snap = _snapshot([
        BrokerOrder("a", "TXN", "filled", 5.0, True, True, 2.0, side="buy"),
        BrokerOrder("b", "TXN", "filled", 5.0, True, True, None, side="buy"),
    ])
    effective = reconcile_effective_prior({}, snap)
    assert effective["TXN"].reference_price is None


def test_reconcile_artifact_covered_priceless_fill_keeps_reference():
    # But an order the artifact already priced keeps that confirmed reference: a
    # broker copy of the same order lacking a price does not re-poison it.
    alloc = PriorAllocation(ticker="TXN", prior_usd=1.0, reference_price=2.0, order_ids=["o1"])
    snap = _snapshot([BrokerOrder("o1", "TXN", "filled", 1.0, True, True, None, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].reference_price == pytest.approx(2.0)


def test_fallback_malformed_row_fails_closed():
    # The per-order-id fallback response carries the matching order AND a null row;
    # the null could be another unreconciled order, so the id must fail closed
    # rather than silently dropping it (matching the pagination path).
    def orders_fn(**kwargs):
        return {"orders": []}  # pagination surfaces nothing

    def order_id_fn(**kwargs):
        return {"orders": [{"id": "m1", "symbol": "TXN", "state": "filled",
                            "cumulative_quantity": "1", "average_price": "2.0", "side": "buy"}, None]}

    snap = same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23",
        expected_order_ids=["m1"], order_id_fn=order_id_fn,
    )
    assert not snap.complete
    assert "m1" in snap.unresolved_order_ids


def test_paginate_nonstring_next_pointer_fails_closed():
    # A recognizable page whose `next` is a non-string (a dict) is an unparseable
    # continuation pointer -> fail closed rather than treat it as end-of-list.
    def fn(**kwargs):
        return {"orders": [], "next": {"cursor": "C"}}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("unparseable_pagination_pointer" in r for r in result.incomplete_reasons)


def test_paginate_nonstring_next_cursor_fails_closed():
    def fn(**kwargs):
        return {"orders": [], "next_cursor": 1}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete


# --- review-fix regressions (round 12) --------------------------------------


def test_terminal_partial_fill_without_price_fails_closed():
    # A cancelled buy that shows a fill (cumulative_quantity) but no price/notional
    # is a fill we KNOW happened yet cannot value -> unknown exposure -> the whole
    # snapshot must fail closed, not treat the partial fill as zero and rebuy.
    def orders_fn(**kwargs):
        return {"orders": [{"id": "tp", "symbol": "TXN", "state": "cancelled",
                            "side": "buy", "cumulative_quantity": "0.5"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not snap.complete
    assert "tp" in snap.unknown_exposure_order_ids


def test_terminal_no_fill_is_not_unvalued_exposure():
    # A cancelled order that never filled (no cumulative_quantity) is genuinely
    # zero exposure and must NOT be flagged as an unvaluable fill.
    order = normalize_order(_raw("t3", "TXN", "cancelled"))
    assert order is not None
    assert not order.has_unvalued_fill
    assert order.notional_usd == 0.0


def test_terminal_partial_fill_with_price_is_valued_not_flagged():
    # A cancelled buy with a settled fill we CAN value counts its executed dollars
    # and is not flagged as unvaluable exposure.
    order = normalize_order(_raw("tv", "TXN", "cancelled", qty="0.5", price="2.0"))
    assert order is not None
    assert not order.has_unvalued_fill
    assert order.notional_usd == pytest.approx(1.0)


def test_zero_filled_quantity_is_not_read_from_order_quantity():
    # A numeric cumulative_quantity of 0 must NOT short-circuit to the order
    # `quantity`: a cancelled order that filled nothing is zero exposure, not
    # quantity * price misread as a full execution.
    raw = {"id": "z1", "symbol": "TXN", "state": "cancelled", "side": "buy",
           "cumulative_quantity": 0.0, "quantity": 10.0, "average_price": 2.0}
    order = normalize_order(raw)
    assert order is not None
    assert order.notional_usd == pytest.approx(0.0)
    assert not order.has_unvalued_fill


def test_paginate_bare_token_next_fails_closed():
    # A `next` that is a bare token (not a URL with ?cursor=) is not something we
    # can confidently follow; fail closed rather than guess it is a cursor.
    def fn(**kwargs):
        return {"orders": [], "next": "cursor_abc123"}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("next_url_without_usable_cursor" in r for r in result.incomplete_reasons)


def test_raw_sink_persists_each_response_as_fetched(tmp_path):
    # The raw-input contract requires persisting each raw broker response before
    # deriving output. raw_sink must be populated incrementally (pagination pages
    # and each fallback response), not batched at the end where a crash loses it.
    sink = []
    sink_sizes_at_fetch = []

    def orders_fn(**kwargs):
        return {"orders": []}  # one (empty) pagination page

    def order_id_fn(*, order_id, **kwargs):
        sink_sizes_at_fetch.append(len(sink))  # sink state BEFORE this response lands
        return {"orders": [{"id": order_id, "symbol": "TXN", "state": "filled",
                            "side": "buy", "cumulative_quantity": "1", "average_price": "2.0"}]}

    same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23",
        expected_order_ids=["A", "B"], order_id_fn=order_id_fn, raw_sink=sink,
    )
    # The pagination page and the first fallback response are already in the sink
    # by the time the second fallback is fetched (strictly growing, not end-batched).
    assert sink_sizes_at_fetch[0] < sink_sizes_at_fetch[1]
    assert len(sink) == 3  # 1 pagination page + 2 fallback responses


def test_raw_sink_callable_receives_each_response():
    # A callable raw_sink is a durable-write hook: it must be invoked with each raw
    # broker response (here, the pagination page) before derived output is produced.
    persisted = []

    def durable_write(resp):
        persisted.append(resp)

    def orders_fn(**kwargs):
        return {"orders": [{"id": "o1", "symbol": "TXN", "state": "filled",
                            "side": "buy", "cumulative_quantity": "1", "average_price": "2.0"}]}

    same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23", raw_sink=durable_write,
    )
    assert persisted and persisted[0]["orders"][0]["id"] == "o1"


def test_raw_sink_callable_write_failure_fails_closed():
    # If the durable-write hook fails, the snapshot must NOT proceed to derive
    # output over un-persisted raw input -- the write failure propagates (fail closed).
    def failing_write(resp):
        raise OSError("cannot persist raw input")

    def orders_fn(**kwargs):
        return {"orders": []}

    with pytest.raises(OSError):
        same_day_order_snapshot(
            orders_fn, account_number="ACCT", created_at_gte="2026-07-23", raw_sink=failing_write,
        )


def test_fallback_retains_additional_valid_orders(tmp_path):
    # A single-order fallback that returns the requested order PLUS another valid
    # same-day order must retain BOTH -- dropping the extra order would hide its
    # exposure and could authorize a FIRST_ALLOCATION over-buy.
    def orders_fn(**kwargs):
        return {"orders": []}  # pagination surfaces nothing

    def order_id_fn(**kwargs):
        return {"orders": [
            {"id": "X", "symbol": "TXN", "state": "filled", "side": "buy",
             "cumulative_quantity": "1", "average_price": "2.0"},
            {"id": "Y", "symbol": "TXN", "state": "filled", "side": "buy",
             "cumulative_quantity": "1", "average_price": "3.0"},
        ]}

    snap = same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23",
        expected_order_ids=["X"], order_id_fn=order_id_fn,
    )
    assert {o.order_id for o in snap.orders} == {"X", "Y"}


def test_unkeyed_artifact_prior_adds_to_distinct_broker_exposure():
    # An unkeyed artifact prior ($2, no order_ids) cannot be proven to overlap a
    # distinct $2 broker buy, so effective prior must be $4 (add), not $2 (max) --
    # otherwise a $3 desired allocation could over-allocate against $4 of exposure.
    alloc = PriorAllocation(ticker="TXN", prior_usd=2.0, reference_price=None, order_ids=[])
    snap = _snapshot([BrokerOrder("B", "TXN", "filled", 2.0, True, True, 1.5, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].effective_prior_usd == pytest.approx(4.0)


def test_keyed_artifact_prior_still_dedupes_via_max():
    # A KEYED artifact prior overlaps the broker snapshot (its id is present), so
    # it still dedupes with max rather than double-counting.
    alloc = PriorAllocation(ticker="TXN", prior_usd=2.0, reference_price=1.5, order_ids=["B"])
    snap = _snapshot([BrokerOrder("B", "TXN", "filled", 2.0, True, True, 1.5, side="buy")])
    effective = reconcile_effective_prior({"TXN": alloc}, snap)
    assert effective["TXN"].effective_prior_usd == pytest.approx(2.0)


def test_fallback_paginated_response_fails_closed():
    # A single-order fallback that returns a paginated page (a next cursor) means
    # the broker ignored the id filter -- later pages may hold more same-day buys,
    # so the id must fail closed rather than trusting only the first page.
    def orders_fn(**kwargs):
        return {"orders": []}  # pagination surfaces nothing

    def order_id_fn(**kwargs):
        return {
            "orders": [{"id": "X", "symbol": "TXN", "state": "filled", "side": "buy",
                        "cumulative_quantity": "1", "average_price": "2.0"}],
            "next": "https://api.robinhood.com/orders/?cursor=MORE",
        }

    snap = same_day_order_snapshot(
        orders_fn, account_number="ACCT", created_at_gte="2026-07-23",
        expected_order_ids=["X"], order_id_fn=order_id_fn,
    )
    assert not snap.complete
    assert "X" in snap.unresolved_order_ids


def test_paginate_single_order_envelope_fails_closed():
    # A list-mode pagination call that returns a single-order envelope only proves
    # one order; it must NOT be treated as a complete account history.
    def fn(**kwargs):
        return {"order": {"id": "o1", "symbol": "TXN", "state": "filled",
                          "cumulative_quantity": "1", "average_price": "2.0", "side": "buy"}}

    result = paginate_same_day_orders(fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not result.complete
    assert any("single_order_envelope_in_pagination" in r for r in result.incomplete_reasons)


def test_filled_order_without_price_or_notional_fails_closed():
    # A filled buy with a fill quantity but no average_price and no notional cannot
    # be valued -> notional 0 -> flagged unsized exposure (via the live-order gate)
    # -> snapshot incomplete. The limit `price` is never used to value a fill.
    def orders_fn(**kwargs):
        return {"orders": [{"id": "f0", "symbol": "TXN", "state": "filled",
                            "side": "buy", "cumulative_quantity": "1"}]}

    snap = same_day_order_snapshot(orders_fn, account_number="ACCT", created_at_gte="2026-07-23")
    assert not snap.complete
    assert "f0" in snap.unknown_exposure_order_ids
