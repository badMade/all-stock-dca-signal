"""``get_equity_orders`` pages -> ``BrokerOrder`` list: the ``refetch_orders_fn`` for ``execute_plan``.

Simultaneous-start guard (DCA Issues keys ``duplicate_fills`` and ``competing_executor``,
``docs/adr-simultaneous-start-guard.md``): ``dca_runner.execute_plan`` calls ``refetch_orders_fn()`` before every
order. An external caller that holds the Robinhood MCP tools passes::

    refetch = make_refetch_orders_fn(lambda cursor: get_equity_orders(account_number=..., created_at_gte=<today>,
                                                                      **({"cursor": cursor} if cursor else {})))

``fetch_page_fn(cursor)`` returns one raw response (``cursor`` is ``None`` for the first page). The row mapping is
the backup planner's own (``scripts.backup_planner._to_broker_order``), so the guard and the backup read the same
orders. Fail closed: a failure envelope, an empty or non-object page, a non-string or endless cursor, or an
unreadable row raises :class:`OrderRefetchError`, which ``execute_plan`` turns into a halt of the remainder; it is
never read as "no orders". The message carries only a reason code, never connector text. Standard library only.
"""

from __future__ import annotations

from typing import Any, Callable

from dca_runner import BrokerOrder
from scripts.assemble_order_pages import _order_rows
from scripts.backup_planner import _to_broker_order
from scripts.broker_reconciliation import _has_continuation, _response_failure, _unwrap

DEFAULT_MAX_PAGES = 20


class OrderRefetchError(RuntimeError):
    """The order history could not be proven complete and readable."""


def make_refetch_orders_fn(
    fetch_page_fn: Callable[[str | None], Any], *, max_pages: int = DEFAULT_MAX_PAGES
) -> Callable[[], list[BrokerOrder]]:
    """Build the zero-argument pull ``execute_plan`` calls before each order."""

    def refetch() -> list[BrokerOrder]:
        orders: list[BrokerOrder] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(max_pages):
            try:
                page = fetch_page_fn(cursor)
            except Exception as exc:  # noqa: BLE001 - injected callable; the text may hold secrets
                raise OrderRefetchError(f"page fetch failed ({type(exc).__name__})") from None
            if _response_failure(page) is not None:
                raise OrderRefetchError("page reported failure")
            inner = _unwrap(page)
            if not isinstance(page, dict) or not inner:
                raise OrderRefetchError("page is empty or not an object")
            try:
                for row in _order_rows(page):
                    order_id = str(row.get("id")) if isinstance(row, dict) else ""
                    if order_id and order_id in seen:
                        continue  # an order repeated across pages must not count twice
                    seen.add(order_id)
                    orders.append(_to_broker_order(row))
            except (ValueError, TypeError) as exc:
                raise OrderRefetchError(f"unreadable order row ({type(exc).__name__})") from None
            if not _has_continuation(inner):
                return orders
            cursor = inner.get("next_cursor") or inner.get("next")
            if not isinstance(cursor, str) or not cursor.strip():
                raise OrderRefetchError("continuation cursor is not a string")
        raise OrderRefetchError(f"history not complete within {max_pages} pages")

    return refetch
