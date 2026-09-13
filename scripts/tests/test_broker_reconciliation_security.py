"""Broker failures retain incomplete coverage without disclosing credentials."""

import pytest

from scripts.broker_reconciliation import paginate_same_day_orders, same_day_order_snapshot


@pytest.mark.parametrize("lookup", ["paging", "fallback"])
def test_lookup_exception_keeps_fail_closed_result_without_credentials(lookup, caplog):
    private_value = "synthetic-reconciliation-credential"
    calls = []

    def orders_fn(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            if lookup == "fallback":
                return {"orders": []}
            return {
                "orders": [
                    {
                        "id": "prior-order",
                        "symbol": "AAPL",
                        "state": "filled",
                        "side": "buy",
                        "cumulative_quantity": "1",
                        "average_price": "2",
                    }
                ],
                "next": "https://example.invalid/orders?cursor=next-page",
            }
        raise RuntimeError(f"Authorization: Bearer {private_value}")

    if lookup == "paging":
        result = paginate_same_day_orders(orders_fn, account_number="ACCT", created_at_gte="2026-09-11")
        assert result.pages_fetched == 1
        assert [order.order_id for order in result.orders] == ["prior-order"]
        assert any(reason.startswith("page_fetch_exception") for reason in result.incomplete_reasons)
    else:
        result = same_day_order_snapshot(
            orders_fn,
            account_number="ACCT",
            created_at_gte="2026-09-11",
            expected_order_ids=["missing-order"],
        )
        assert result.unresolved_order_ids == ["missing-order"]
        assert not result.resolved_by_fallback
        assert any(reason.startswith("fallback_exception:missing-order") for reason in result.incomplete_reasons)
    assert not result.complete
    assert len(calls) == 2
    assert private_value not in "\n".join(result.incomplete_reasons)
    assert private_value not in caplog.text
    assert "RuntimeError" in caplog.text
