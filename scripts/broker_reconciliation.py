#!/usr/bin/env python3
"""Fail-closed same-day broker reconciliation and effective-prior allocation.

This module closes the P0 reliability gap the 2026-07-23 incident exposed: the
tracked runners computed a symbol's prior same-day allocation from persisted
artifacts/CSVs only, and when those showed nothing the top-up guard returned the
FULL desired amount (``no_prior_run``). A real Robinhood fill that was missing
from the artifacts therefore looked like ``prior_usd=0`` and a fresh full buy was
planned that collided with the live order. The fix is two things this module
provides, both as pure logic over *injected* broker callables (no network here):

  1. A paginated, account-scoped same-day order snapshot that pages through ALL
     same-day equity orders (no ``placed_agent`` pre-filter), persists every raw
     page for audit, and runs a bounded per-``order_id`` fallback for order ids
     an artifact recorded but the snapshot missed -- so an artifact fill is never
     silently dropped (:func:`same_day_order_snapshot`).
  2. A pure reconciler that folds the deduped artifact prior allocation together
     with the same-day broker exposure into an ``effective_prior_usd`` per symbol
     (:func:`reconcile_effective_prior`), and a decision function that FAILS
     CLOSED -- returning ``BROKER_HISTORY_INCOMPLETE`` with a $0 buy -- whenever
     same-day broker coverage could not be proven complete
     (:func:`decide_effective_top_up`).

The safety-critical strict-below top-up invariant is NOT re-implemented here: the
decision delegates to :func:`scripts.run_classification.incremental_top_up_amount`
(the existing, tested ``current_price < reference_price`` guard) and only adds the
effective-prior combination and the fail-closed gate on top.

Standard library only. No trading logic beyond the pure reconciliation math, no
network, no secrets. Every external broker interaction is an injected callable
that maps 1:1 to the Robinhood ``get_equity_orders`` MCP tool (list mode with a
``cursor`` for pagination; single-order mode via ``order_id`` for the fallback).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union
from urllib.parse import parse_qs, urlparse

from scripts.reason_codes import BROKER_HISTORY_INCOMPLETE, from_top_up_guard
from scripts.run_classification import PriorAllocation, incremental_top_up_amount

logger = logging.getLogger("dca.broker_reconciliation")

#: Default safety ceiling on pages fetched. Reaching it means we CANNOT prove we
#: saw every same-day order, so the snapshot is marked incomplete (fail closed)
#: rather than assuming the pages we did see are the whole history.
DEFAULT_MAX_PAGES = 50

# Order states that mean "this order consumed (or may still consume) same-day
# capacity" and so counts as exposure. Mirrors
# ``dca_live_adapters._LIVE_ORDER_STATES`` -- duplicated (not imported) to keep
# this reconciliation utility standard-library-only and free of the heavy
# near_close_dca import chain, matching the other scripts/ modules. Terminal
# failure states (cancelled/rejected/failed/voided) never consumed capacity.
_LIVE_ORDER_STATES = frozenset(
    {"new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"}
)

# States that definitively mean the order consumed no capacity and is finished.
# A state that is neither live nor a known terminal (or is missing) is UNKNOWN --
# it might be a live order under a broker-added state, so it is treated as
# unreconciled exposure and fails the snapshot closed rather than being assumed
# terminal.
_TERMINAL_ORDER_STATES = frozenset(
    {"cancelled", "canceled", "rejected", "failed", "voided", "expired"}
)


# --- Small MCP-response helpers (mirror dca_live_adapters, kept local) --------


def _unwrap(payload: Any) -> Dict[str, Any]:
    """Return the inner dict for an MCP response optionally wrapped in ``data``."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _response_failure(response: Any) -> Optional[str]:
    """Reason string when a wrapper reported failure (outer OR inner), else None.

    An outage surfaced as data (``{"error": ...}`` / ``{"success": false}``) must
    be treated as a failure -- fail closed -- not as "no orders found".
    """
    for candidate in (response, _unwrap(response)):
        if not isinstance(candidate, dict):
            continue
        if candidate.get("error"):
            return f"error: {candidate.get('error')}"
        for flag in ("success", "ok"):
            if candidate.get(flag) is False:
                return f"{flag}=false"
    return None


def _to_float(value: Any) -> Optional[float]:
    """Best-effort float parse; returns None for missing/non-finite/non-numeric."""
    if value is None or value == "" or value == "-":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return result


def _money(value: Any) -> Optional[float]:
    """Parse a Robinhood money value: a scalar, or a ``{"amount": ...}`` dict."""
    if isinstance(value, dict):
        return _to_float(value.get("amount"))
    return _to_float(value)


def _next_cursor(response: Any) -> Optional[str]:
    """Extract the next-page cursor from an orders response, or None on last page.

    Accepts the MCP ``next`` URL (``https://.../orders/?cursor=ABC``) and pulls
    its ``cursor`` query param, or a direct ``next_cursor``/``cursor`` field. An
    absent/empty ``next`` means the last page was reached.
    """
    data = _unwrap(response)
    direct_next = data.get("next_cursor")
    if isinstance(direct_next, str) and direct_next.strip():
        return direct_next.strip()
    nxt = data.get("next")
    if isinstance(nxt, str) and nxt.strip():
        params = parse_qs(urlparse(nxt).query)
        cursor_values = params.get("cursor")
        if cursor_values:
            return cursor_values[0]
        # A next URL with no cursor param is still a "there is more" signal we
        # cannot follow deterministically; surface None so the caller stops and
        # (because more may exist) does not treat the run as provably complete.
    # A bare ``cursor`` field is checked LAST: some APIs echo the CURRENT page's
    # cursor, which would look like a repeated cursor and fail closed, so the
    # authoritative ``next``/``next_cursor`` pointer takes precedence over it.
    direct = data.get("cursor")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return None


def _has_nonstring_continuation(data: Mapping[str, Any]) -> bool:
    """True when ``next``/``next_cursor`` is present but is not a string.

    ``_next_cursor`` only understands string continuation pointers, so a
    non-string one (e.g. ``{"next": {"cursor": "C"}}`` or ``next_cursor: 1``)
    silently yields ``None`` and would otherwise be read as end-of-list. A caller
    that has already failed to extract a cursor uses this to fail closed instead.
    """
    for key in ("next", "next_cursor"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return True
    return False


def _has_continuation(data: Mapping[str, Any]) -> bool:
    """True when a response advertises MORE pages via ``next``/``next_cursor``.

    A non-empty string, or any non-string value, in either field means the
    response is one page of a larger list rather than a self-contained answer.
    """
    for key in ("next", "next_cursor"):
        value = data.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value is not None:
            return True
    return False


# --- Normalized broker order -------------------------------------------------


@dataclass
class BrokerOrder:
    """One same-day broker order normalized for reconciliation."""

    order_id: str
    symbol: str
    state: str
    notional_usd: float
    is_live: bool
    is_filled: bool
    reference_price: Optional[float]
    side: Optional[str] = None
    created_at: Optional[str] = None
    unknown_state: bool = False  # state is neither known-live nor known-terminal
    has_unvalued_fill: bool = False  # terminal order shows a fill we could not value


def _executed_dollars(raw: Mapping[str, Any], *, filled_qty: Optional[float], avg_price: Optional[float]) -> float:
    """Dollars this order actually executed (settled), or 0 if nothing filled.

    Prefers ``cumulative_quantity * average_price`` (the executed fill), falling
    back to a broker-reported ``executed_notional``. ``average_price`` is the
    *executed fill price*; the limit ``price`` field is deliberately NOT used here
    because on a limit order it is the limit, not the achieved fill.
    """
    if filled_qty and avg_price and filled_qty > 0 and avg_price > 0:
        return filled_qty * avg_price
    reported_executed = _money(raw.get("executed_notional"))
    if reported_executed and reported_executed > 0:
        return reported_executed
    return 0.0


def _live_commitment(raw: Mapping[str, Any], *, executed: float) -> float:
    """Dollars a *live* order commits, counted conservatively (never undercount).

    Returns ``max(executed, planned_total_notional)``. For a ``partially_filled``
    order the remaining quantity is still working on the book, so counting only
    the executed portion would undercount the live commitment and let a top-up
    over-allocate against the still-open remainder. A fully-filled order is
    complete: its executed cash is the exact commitment and the planned/target
    notional is no longer active, so inflating it to ``max(executed, planned)``
    would overstate the prior and could wrongly suppress a legitimate later
    top-up -- so a filled order returns exactly its executed dollars.
    """
    state = str(raw.get("state", "")).strip().lower()
    if state == "filled" and executed > 0:
        return executed
    planned = 0.0
    for key in ("total_notional", "dollar_based_amount", "amount"):
        value = _money(raw.get(key))
        if value and value > 0:
            planned = value
            break
    if planned <= 0 and state != "filled":
        # A share-based WORKING order can carry a quantity + limit price but no
        # explicit dollar notional. Estimate the commitment from quantity * price so
        # a sizable working order is counted as exposure instead of collapsing to
        # $0 (which would flag it unsizable and needlessly block the whole window).
        # For a buy, the limit ``price`` is a conservative upper bound on the
        # commitment; it is used ONLY to size a working order's exposure here, never
        # as a fill or reference price -- and never for a ``filled`` order, whose
        # executed value must come from ``average_price`` (see _executed_dollars).
        qty = _to_float(raw.get("quantity") or raw.get("cumulative_quantity"))
        est_price = _to_float(raw.get("price") or raw.get("average_price"))
        if qty and est_price and qty > 0 and est_price > 0:
            planned = qty * est_price
    return max(executed, planned)


def normalize_order(raw: Mapping[str, Any]) -> Optional[BrokerOrder]:
    """Normalize a raw Robinhood order dict into a :class:`BrokerOrder`.

    Returns None for a row with no usable ``order_id`` or ``symbol`` (nothing we
    can reconcile against an artifact).
    """
    if not isinstance(raw, Mapping):
        return None
    oid = raw.get("id") or raw.get("order_id")
    order_id = str(oid).strip() if oid is not None else ""
    symbol_raw = raw.get("symbol") or raw.get("chain_symbol") or raw.get("instrument_symbol")
    symbol = str(symbol_raw).strip().upper() if symbol_raw else ""
    if not order_id or not symbol:
        return None
    state = str(raw.get("state", "")).strip().lower()
    # Evaluate cumulative_quantity (the EXECUTED quantity) directly -- a Python
    # ``x or y`` would fall through to the order ``quantity`` when the fill is a
    # falsy numeric 0.0, misreading a 0-filled order as fully executed. Fall back
    # to ``quantity`` only when cumulative_quantity is genuinely absent.
    cumulative_qty = _to_float(raw.get("cumulative_quantity"))
    filled_qty = cumulative_qty if cumulative_qty is not None else _to_float(raw.get("quantity"))
    # ``average_price`` is the executed fill price. The ``price`` field is the
    # LIMIT price on a limit order, not the achieved fill, so it must not be used
    # as the fill price for notional/reference computations.
    avg_price = _to_float(raw.get("average_price"))
    is_filled = state == "filled"
    is_live = state in _LIVE_ORDER_STATES
    # A state that is neither known-live nor a known terminal (including a missing
    # state) is unknown: it might be an outstanding order under a broker-added
    # state, so it must not be assumed terminal/zero-exposure.
    unknown_state = (not is_live) and (state not in _TERMINAL_ORDER_STATES)
    # A reference price is trusted only from a confirmed full fill; a live but
    # unfilled order's logged price may still be rejected/repriced.
    reference_price = avg_price if (is_filled and avg_price and avg_price > 0) else None
    executed = _executed_dollars(raw, filled_qty=filled_qty, avg_price=avg_price)
    if is_live:
        notional_usd = _live_commitment(raw, executed=executed)
    elif executed > 0:
        # A terminal order (cancelled/expired/rejected/...) can still carry a
        # settled partial or full fill before it ended. That executed cash is real
        # prior exposure -- ignoring it would let a subsequent FIRST_ALLOCATION
        # re-buy the full desired amount on top of shares already bought. Only the
        # executed portion counts (any unfilled planned remainder is dead).
        notional_usd = executed
    else:
        notional_usd = 0.0
    # A terminal order that shows a fill (a positive cumulative_quantity) but that
    # we could NOT value (no average_price and no executed_notional -> executed 0)
    # is exposure we know happened but cannot size. The live-order unknown-notional
    # gate does not cover terminal orders, so flag it here so the snapshot fails
    # closed instead of treating a known-but-unvaluable partial fill as zero.
    has_unvalued_fill = (
        (not is_live)
        and cumulative_qty is not None
        and cumulative_qty > 0
        and notional_usd <= 0
    )
    return BrokerOrder(
        order_id=order_id,
        symbol=symbol,
        state=state,
        notional_usd=notional_usd,
        is_live=is_live,
        is_filled=is_filled,
        reference_price=reference_price,
        side=(str(raw.get("side")).strip().lower() if raw.get("side") else None),
        created_at=(str(raw.get("created_at")) if raw.get("created_at") else None),
        unknown_state=unknown_state,
        has_unvalued_fill=has_unvalued_fill,
    )


def _orders_from_response(response: Any) -> List[Any]:
    """Raw order dicts from any ``get_equity_orders`` envelope.

    Accepts list mode (``{"orders": [...]}``), a single-order envelope
    (``{"order": {...}}``), and a bare top-level order object. The per-``order_id``
    fallback uses single-order mode, so without this a healthy ``{"order": {...}}``
    response would look empty, leave the id unresolved, and wrongly force the whole
    snapshot incomplete.
    """
    data = _unwrap(response)
    orders = data.get("orders")
    if isinstance(orders, list):
        return orders
    single = data.get("order") if isinstance(data.get("order"), dict) else None
    if single is None and (data.get("id") or data.get("order_id")):
        single = data
    return [single] if single else []


def _is_wellformed_orders_page(response: Any) -> bool:
    """True when a page response is a recognizable orders envelope.

    A ``None``, a bare list, or a dict with no ``orders`` list, no single-order
    envelope, and no pagination field is unparseable -- it must NOT be read as a
    valid *empty* history (which would mark the snapshot complete and let a
    decision issue a full first allocation). Such a page fails closed instead.
    An explicit ``{"orders": []}`` is well-formed (a genuine empty page).
    """
    if not isinstance(response, dict):
        return False
    data = _unwrap(response)
    if isinstance(data.get("orders"), list):
        return True
    if isinstance(data.get("order"), dict):
        return True
    if data.get("id") or data.get("order_id"):
        return True
    return any(data.get(key) for key in ("next", "next_cursor", "cursor"))


def _normalize_orders(raw_orders: Iterable[Any]) -> List[BrokerOrder]:
    out: List[BrokerOrder] = []
    for raw in raw_orders or []:
        order = normalize_order(raw) if isinstance(raw, Mapping) else None
        if order is not None:
            out.append(order)
    return out


def _dedupe_orders_by_id(orders: Iterable[BrokerOrder]) -> List[BrokerOrder]:
    """Deduplicate by ``order_id``, preferring a filled row over a non-filled one.

    A paged order and a fallback-resolved order can be the same id; a filled
    snapshot of an order supersedes an earlier live-but-unfilled snapshot.
    """
    by_id: Dict[str, BrokerOrder] = {}
    for order in orders:
        existing = by_id.get(order.order_id)
        if existing is None or (order.is_filled and not existing.is_filled):
            by_id[order.order_id] = order
    return list(by_id.values())


def _clean_order_ids(order_ids: Iterable[str]) -> List[str]:
    if isinstance(order_ids, str):
        # A bare string is iterable char-by-char; a single id passed as a string
        # would otherwise be split into individual characters. Treat it as one id.
        order_ids = [order_ids]
    seen: Dict[str, None] = {}
    for oid in order_ids or []:
        text = str(oid).strip()
        if text and text != "-":
            seen.setdefault(text, None)
    return list(seen)


# --- Pagination + per-order-id fallback --------------------------------------

# A raw-response sink is either an in-memory list (collected for the returned
# ``SnapshotResult`` / tests) OR a DURABLE append callback the caller supplies to
# persist each raw broker response before any derived reconciliation output is
# produced. The list form is in-memory only and therefore not crash-durable; a
# caller that must satisfy the raw-input contract passes a callable that writes
# durably (e.g. append+fsync). A callable sink's write failure PROPAGATES, so the
# snapshot fails closed rather than deriving output over un-persisted input.
RawSink = Union[List[Dict[str, Any]], Callable[[Dict[str, Any]], None]]


def _emit_raw(raw_sink: "Optional[RawSink]", response: Dict[str, Any]) -> None:
    """Persist/collect one raw broker response to ``raw_sink``.

    A callable sink is invoked (a durable write; any exception propagates so the
    caller fails closed). A list sink is appended in memory. ``None`` is a no-op.
    """
    if raw_sink is None:
        return
    if callable(raw_sink):
        raw_sink(response)
    else:
        raw_sink.append(response)


@dataclass
class PagedOrders:
    """Outcome of paging all same-day orders for an account."""

    orders: List[BrokerOrder]
    pages_fetched: int
    complete: bool
    incomplete_reasons: List[str] = field(default_factory=list)
    raw_pages: List[Dict[str, Any]] = field(default_factory=list)


def paginate_same_day_orders(
    orders_fn: Callable[..., Any],
    *,
    account_number: str,
    created_at_gte: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    raw_sink: "Optional[RawSink]" = None,
) -> PagedOrders:
    """Page through ALL same-day equity orders for ``account_number``.

    ``orders_fn`` is an injected callable mapping 1:1 to the Robinhood
    ``get_equity_orders`` MCP tool; it is called with ``account_number``,
    ``created_at_gte`` and (from the second page) ``cursor``. No ``symbol`` or
    ``placed_agent`` filter is applied -- the plan requires paging every same-day
    order first, then classifying. Every raw page is appended to ``raw_sink``
    (when provided) and to :attr:`PagedOrders.raw_pages` for the raw-input
    contract. The loop fails closed: any error-shaped page, an exception, a
    repeated cursor (loop), or hitting ``max_pages`` marks ``complete=False``.
    """
    if not account_number:
        raise ValueError("account_number is required (account pinning) to page orders")
    orders: List[BrokerOrder] = []
    raw_pages: List[Dict[str, Any]] = []
    reasons: List[str] = []
    seen_cursors: set[str] = set()
    cursor: Optional[str] = None
    complete = False
    pages = 0
    while pages < max_pages:
        kwargs: Dict[str, Any] = {"account_number": account_number, "created_at_gte": created_at_gte}
        if cursor is not None:
            kwargs["cursor"] = cursor
        try:
            response = orders_fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - a paging outage must fail closed, not crash
            logger.warning("same-day order paging failed on page %d: %s", pages + 1, exc)
            reasons.append(f"page_fetch_exception: {exc}")
            break
        pages += 1
        if isinstance(response, dict):
            raw_pages.append(response)
            _emit_raw(raw_sink, response)  # durable-persist before deriving output
        failure = _response_failure(response)
        if failure is not None:
            reasons.append(f"page_error_response: {failure}")
            break
        if not _is_wellformed_orders_page(response):
            # Unparseable page (None/list/dict with no order envelope): fail
            # closed rather than treating it as a valid empty history.
            reasons.append("malformed_page_response")
            break
        if not isinstance(_unwrap(response).get("orders"), list):
            # A pagination (list-mode) call must return an ``orders`` LIST. A
            # single-order envelope (``{"order": {...}}`` or a bare order object) is
            # only evidence about ONE order -- it is the shape the per-id fallback
            # uses and does NOT prove the account list has ended. Treating it as a
            # complete page would ignore every remaining same-day order, so fail
            # closed instead.
            reasons.append("single_order_envelope_in_pagination")
            break
        raw_order_rows = _orders_from_response(response)
        normalized = _normalize_orders(raw_order_rows)
        orders.extend(normalized)
        if len(normalized) < len(raw_order_rows):
            # A recognizable page whose order row(s) could not be normalized -- a
            # dict missing an id/symbol, OR a non-mapping element such as null --
            # contains an order we cannot reconcile. ALL raw elements are counted
            # (non-mappings are not filtered away first), so a null row is treated
            # as unnormalizable rather than silently dropped. Failing closed here
            # stops the snapshot from proving an empty history and authorizing a buy.
            reasons.append("unnormalizable_order_row")
            break
        cursor = _next_cursor(response)
        if cursor is None:
            # Distinguish a clean last page (no continuation) from a continuation
            # pointer we could not follow. Either means more orders may exist that
            # we cannot fetch -> fail closed (leave complete=False) rather than
            # declaring the history proven-complete.
            data = _unwrap(response)
            nxt = data.get("next")
            if isinstance(nxt, str) and nxt.strip():
                # A next URL with no extractable cursor param.
                reasons.append("next_url_without_usable_cursor")
                break
            if _has_nonstring_continuation(data):
                # A present but non-string next/next_cursor (a dict, number, ...):
                # an unparseable continuation pointer, not an end-of-list signal.
                reasons.append("unparseable_pagination_pointer")
                break
            complete = True
            break
        if cursor in seen_cursors:
            reasons.append("repeated_pagination_cursor")
            break
        seen_cursors.add(cursor)
    else:
        # while-loop exhausted without break: we hit max_pages with more to fetch.
        reasons.append(f"max_pages_exceeded: {max_pages}")
    return PagedOrders(
        orders=orders,
        pages_fetched=pages,
        complete=complete,
        incomplete_reasons=reasons,
        raw_pages=raw_pages,
    )


@dataclass
class SnapshotResult:
    """Complete same-day broker snapshot: paged orders + resolved fallbacks."""

    orders: List[BrokerOrder]
    pages_fetched: int
    pagination_complete: bool
    resolved_by_fallback: List[str] = field(default_factory=list)
    unresolved_order_ids: List[str] = field(default_factory=list)
    unknown_exposure_order_ids: List[str] = field(default_factory=list)
    incomplete_reasons: List[str] = field(default_factory=list)
    raw_responses: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True only when paging terminated cleanly, every expected id resolved,
        AND no live order has unknown (unsizable) exposure."""
        return (
            self.pagination_complete
            and not self.unresolved_order_ids
            and not self.unknown_exposure_order_ids
        )

    def orders_by_symbol(self) -> Dict[str, List[BrokerOrder]]:
        grouped: Dict[str, List[BrokerOrder]] = {}
        for order in self.orders:
            grouped.setdefault(order.symbol, []).append(order)
        return grouped


def same_day_order_snapshot(
    orders_fn: Callable[..., Any],
    *,
    account_number: str,
    created_at_gte: str,
    expected_order_ids: Sequence[str] = (),
    order_id_fn: Optional[Callable[..., Any]] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    raw_sink: "Optional[RawSink]" = None,
) -> SnapshotResult:
    """Build the account-scoped same-day order snapshot with an id fallback.

    Pages all same-day orders (:func:`paginate_same_day_orders`), then for every
    id in ``expected_order_ids`` (typically the order ids recorded in the same-day
    ``submitted_orders.csv`` / artifact prior) that the paged set did not surface,
    runs one bounded single-order lookup via ``order_id_fn`` (defaults to
    ``orders_fn``) so an artifact fill is never lost. An expected id that still
    cannot be resolved is recorded in ``unresolved_order_ids``, which forces
    :attr:`SnapshotResult.complete` to False (fail closed).
    """
    if not account_number:
        raise ValueError("account_number is required (account pinning) for the snapshot")
    # Persist every raw page/response to the caller's raw_sink AS IT IS FETCHED
    # (the raw-input contract: raw broker input must be durable before any derived
    # output), not batched at the end where a crash mid-fallback would lose it.
    # paginate appends each page to raw_sink directly; the fallback loop appends
    # each response immediately below. raw_responses keeps the full local copy for
    # the returned SnapshotResult.
    paged = paginate_same_day_orders(
        orders_fn,
        account_number=account_number,
        created_at_gte=created_at_gte,
        max_pages=max_pages,
        raw_sink=raw_sink,
    )
    raw_responses: List[Dict[str, Any]] = list(paged.raw_pages)
    orders = list(paged.orders)
    present_ids = {o.order_id for o in orders}
    reasons = list(paged.incomplete_reasons)
    resolved: List[str] = []
    unresolved: List[str] = []

    missing = [oid for oid in _clean_order_ids(expected_order_ids) if oid not in present_ids]
    fetch_by_id = order_id_fn or orders_fn
    for oid in missing:
        try:
            response = fetch_by_id(account_number=account_number, order_id=oid)
        except Exception as exc:  # noqa: BLE001 - fallback outage fails closed for that id
            logger.warning("order-id fallback lookup failed for %s: %s", oid, exc)
            unresolved.append(oid)
            reasons.append(f"fallback_exception:{oid}")
            continue
        if isinstance(response, dict):
            raw_responses.append(response)
            _emit_raw(raw_sink, response)  # durable-persist the raw input before deriving output
        if _response_failure(response) is not None:
            unresolved.append(oid)
            reasons.append(f"fallback_error_response:{oid}")
            continue
        raw_rows = _orders_from_response(response)
        resolved_orders = _normalize_orders(raw_rows)
        if len(resolved_orders) < len(raw_rows):
            # The fallback response carried a row that could not be normalized (a
            # non-mapping such as null, or a dict missing an id/symbol). That row
            # can be another unreconciled live order, so -- exactly as the
            # pagination path does -- treat the response as unusable and fail closed
            # for this id rather than silently dropping the bad row.
            unresolved.append(oid)
            reasons.append(f"fallback_unnormalizable_row:{oid}")
            continue
        match = next((o for o in resolved_orders if o.order_id == oid), None)
        if match is None:
            unresolved.append(oid)
            reasons.append(f"fallback_unresolved:{oid}")
            continue
        if _has_continuation(_unwrap(response)):
            # A single-order fallback should be self-contained. A continuation
            # pointer means the broker returned one page of a larger list (it
            # ignored/broadened the id filter), so later pages may hold additional
            # same-day buys we did not fetch. Fail closed for this id rather than
            # trusting only the first page.
            unresolved.append(oid)
            reasons.append(f"fallback_response_paginated:{oid}")
            continue
        # Retain EVERY normalized order the fallback returned, not just the matched
        # one. A single-order fallback that also surfaces another valid same-day
        # order is real exposure that must be reconciled -- dropping it could let a
        # later decision take FIRST_ALLOCATION despite an outstanding buy. Dedup by
        # id at the end removes any overlap with the paged set.
        orders.extend(resolved_orders)
        resolved.append(oid)

    final_orders = _dedupe_orders_by_id(orders)
    # Exposure we know exists but cannot size forces the snapshot incomplete (fail
    # closed) rather than being treated as $0 (which would let the reconciler report
    # effective_prior=0 and authorize a full first allocation despite the order).
    # Three shapes qualify:
    #   * an unknown-state order (a broker-added state we can't classify),
    #   * a LIVE order with no parseable notional, and
    #   * a TERMINAL order that shows a fill (cumulative_quantity) we could not value.
    # Explicit sells are excluded from buy exposure by reconcile_effective_prior, so
    # an unsized/unknown SELL must not fail the whole snapshot closed and block every
    # DCA buy. Only buy/ambiguous-side orders gate completeness.
    unknown_exposure = [
        o.order_id
        for o in final_orders
        if o.side != "sell"
        and (o.unknown_state or (o.is_live and o.notional_usd <= 0) or o.has_unvalued_fill)
    ]
    if unknown_exposure:
        reasons.append(f"unsized_exposure_order_ids:{unknown_exposure}")

    return SnapshotResult(
        orders=final_orders,
        pages_fetched=paged.pages_fetched,
        pagination_complete=paged.complete,
        resolved_by_fallback=resolved,
        unresolved_order_ids=unresolved,
        unknown_exposure_order_ids=unknown_exposure,
        incomplete_reasons=reasons,
        raw_responses=raw_responses,
    )


# --- Effective-prior reconciliation ------------------------------------------


@dataclass
class EffectivePrior:
    """Per-symbol effective prior allocation after broker reconciliation."""

    symbol: str
    artifact_prior_usd: float
    broker_prior_usd: float
    effective_prior_usd: float
    reference_price: Optional[float]
    coverage_complete: bool
    order_ids: List[str] = field(default_factory=list)
    broker_only: bool = False  # broker exposure the artifacts never recorded


def _artifact_alloc(
    artifact_priors: Mapping[str, PriorAllocation], symbol: str
) -> Optional[PriorAllocation]:
    alloc = artifact_priors.get(symbol)
    return alloc if isinstance(alloc, PriorAllocation) else None


def reconcile_effective_prior(
    artifact_priors: Mapping[str, PriorAllocation],
    snapshot: SnapshotResult,
) -> Dict[str, EffectivePrior]:
    """Fold artifact prior allocation together with same-day broker exposure.

    For every symbol in the union of the artifact priors and the broker snapshot,
    the effective prior is ``max(artifact prior_usd, total live buy-side broker
    notional)``. A complete account-scoped snapshot is the authoritative same-day
    exposure and already contains every order the artifact recorded, so the max
    both avoids double-counting an order present in both sources and never
    undercounts when an artifact dollar amount was unparseable. Explicit same-day
    sells are excluded from buy exposure. A symbol with broker exposure but no
    artifact record -- the 2026-07-23 missed-fill case -- appears with
    ``broker_only=True``.

    The reference price combines the artifact reference with any newly-seen
    *confirmed-fill* broker price via ``min``; any contributing order without a
    confirmed price poisons the reference to None (the same "unknown poisons the
    aggregate" rule the artifact loader uses), so the top-up guard cannot compare
    against a price that isn't real. ``coverage_complete`` is the snapshot's
    global completeness -- if the broker history could not be proven complete, no
    symbol's prior can be trusted.
    """
    orders_by_symbol = snapshot.orders_by_symbol()
    # Deterministic symbol order (artifacts first, then broker-only symbols in
    # snapshot order) so metrics/output are stable for identical inputs rather
    # than depending on set iteration order.
    symbols = list(dict.fromkeys([*artifact_priors, *orders_by_symbol]))
    coverage = snapshot.complete
    result: Dict[str, EffectivePrior] = {}
    for symbol in symbols:
        alloc = _artifact_alloc(artifact_priors, symbol)
        artifact_prior = float(alloc.prior_usd) if alloc else 0.0
        # Preserve insertion order for order_ids (matches _clean_order_ids).
        artifact_ids = _clean_order_ids(alloc.order_ids) if alloc else []
        order_ids = list(artifact_ids)
        seen_order_ids = set(artifact_ids)
        artifact_id_set = set(artifact_ids)

        # The artifact's confirmed reference price (a recorded fill price) always
        # contributes to the reference minimum; a prior with dollars but no
        # confirmed price flags the reference unknown (poisons).
        known_prices: List[float] = []
        poisoned = False
        if alloc is not None:
            if alloc.reference_price is not None:
                known_prices.append(float(alloc.reference_price))
            elif artifact_prior > 0:
                poisoned = True

        # Sum buy-side broker exposure for the symbol (the snapshot already deduped
        # by order id). A same-day sell is excluded from buy exposure; an
        # unknown/None side is counted conservatively (the dominant risk is a
        # duplicate buy, so an ambiguous same-day order still offsets the top-up).
        broker_prior = 0.0
        for order in orders_by_symbol.get(symbol, []):
            if order.side == "sell":
                continue
            # Count live orders (their working commitment) AND terminal orders that
            # still carry a settled fill (executed dollars > 0, e.g. a partially
            # filled buy that was later cancelled). A terminal order with no fill
            # has no prior exposure and is skipped.
            if not order.is_live and order.notional_usd <= 0:
                continue
            if order.order_id not in seen_order_ids:
                order_ids.append(order.order_id)
                seen_order_ids.add(order.order_id)
            broker_prior += order.notional_usd
            if order.reference_price is not None:
                # A confirmed full-fill price contributes to the reference minimum.
                known_prices.append(order.reference_price)
            elif order.is_live and not order.is_filled:
                # A live, not-yet-filled order's eventual fill price is unknowable,
                # so it poisons the reference (the top-up must never compare against
                # a price that isn't real).
                poisoned = True
            elif order.notional_usd > 0 and order.order_id not in artifact_id_set:
                # A settled BROKER-ONLY order that contributes exposure but has no
                # confirmed fill price leaves the true minimum prior price unknown.
                # Trusting another order's price as the reference could overstate it
                # and authorize a buy that isn't actually below every prior fill, so
                # fail closed by poisoning. An order the artifact already accounts
                # for keeps the artifact's confirmed reference (it is not re-priced
                # here) -- that settled-fill case is deliberately not poisoned.
                poisoned = True

        # Fold the artifact prior together with the same-day broker exposure.
        # A KEYED artifact prior (its order_ids are present) describes the SAME
        # orders as the broker snapshot -- a complete account-scoped snapshot is
        # guaranteed to include them, since an unresolved artifact id forces the
        # snapshot incomplete -- so max(artifact, broker) dedupes the overlap and
        # still never undercounts an unparseable artifact amount.
        #
        # An UNKEYED artifact prior (positive dollars but NO order_ids, which
        # unkeyed CSV rows permit) cannot be matched against the snapshot, so its
        # overlap with the broker exposure is UNPROVABLE. max() would then assume an
        # overlap that may not exist -- an unkeyed $2 prior plus a distinct $2
        # broker buy would report $2 instead of $4 and let a top-up over-allocate.
        # Treat it as independent exposure and ADD it. Adding is conservative: if
        # the two happen to overlap it merely over-counts and suppresses a top-up
        # (fail-safe), never under-counts and over-buys.
        if artifact_prior > 0 and not artifact_ids:
            effective_prior = artifact_prior + broker_prior
        else:
            effective_prior = max(artifact_prior, broker_prior)

        reference = None if (poisoned or not known_prices) else min(known_prices)
        result[symbol] = EffectivePrior(
            symbol=symbol,
            artifact_prior_usd=artifact_prior,
            broker_prior_usd=broker_prior,
            effective_prior_usd=effective_prior,
            reference_price=reference,
            coverage_complete=coverage,
            order_ids=order_ids,
            broker_only=alloc is None and broker_prior > 0,
        )
    return result


@dataclass
class TopUpDecision:
    """Per-symbol top-up decision derived from the reconciled effective prior."""

    symbol: str
    desired_usd: float
    effective_prior_usd: float
    incremental_usd: float
    reason: str
    coverage_complete: bool


def decide_effective_top_up(
    symbol: str,
    desired_usd: float,
    effective: EffectivePrior,
    current_price: Optional[float],
) -> TopUpDecision:
    """Decide the incremental buy for one symbol, failing closed on incomplete history.

    The core rule: if same-day broker coverage could not be proven complete,
    return ``BROKER_HISTORY_INCOMPLETE`` with a $0 buy -- never fall back to the
    full desired amount on an unproven-empty history. Otherwise the strict-below
    top-up invariant is delegated to
    :func:`scripts.run_classification.incremental_top_up_amount` (buy
    ``max(0, desired - effective_prior)`` only when ``current_price`` is strictly
    below the reference price), and its ``top_up_guard_result`` is mapped onto the
    canonical reason vocabulary.
    """
    if not effective.coverage_complete:
        return TopUpDecision(
            symbol=symbol,
            desired_usd=desired_usd,
            effective_prior_usd=effective.effective_prior_usd,
            incremental_usd=0.0,
            reason=BROKER_HISTORY_INCOMPLETE,
            coverage_complete=False,
        )
    incremental, guard_result = incremental_top_up_amount(
        desired_usd,
        effective.effective_prior_usd,
        current_price,
        effective.reference_price,
    )
    # from_top_up_guard maps the on-disk top_up_guard_result codes onto the
    # canonical reason vocabulary (no_prior_run -> FIRST_ALLOCATION, etc.).
    reason = from_top_up_guard(guard_result)
    return TopUpDecision(
        symbol=symbol,
        desired_usd=desired_usd,
        effective_prior_usd=effective.effective_prior_usd,
        incremental_usd=incremental,
        reason=reason,
        coverage_complete=True,
    )


def reconciliation_metrics(
    snapshot: SnapshotResult,
    effective: Mapping[str, EffectivePrior],
    artifact_priors: Mapping[str, PriorAllocation],
) -> Dict[str, Any]:
    """Structured reconciliation metrics for the observability panel.

    Surfaces the plan's reconciliation metrics: pagination coverage, unresolved
    broker ids, submitted-vs-snapshot counts, and per-symbol prior-allocation
    discrepancies (where the broker same-day exposure differs from the artifact
    prior -- the discrepancy the 2026-07-23 incident would have flagged).
    """
    discrepancies = {
        symbol: {
            "artifact_prior_usd": ep.artifact_prior_usd,
            "broker_prior_usd": ep.broker_prior_usd,
            "effective_prior_usd": ep.effective_prior_usd,
            "broker_only": ep.broker_only,
        }
        for symbol, ep in effective.items()
        if ep.broker_prior_usd > 0
    }
    return {
        "coverage_complete": snapshot.complete,
        "pagination_complete": snapshot.pagination_complete,
        "pages_fetched": snapshot.pages_fetched,
        "orders_in_snapshot": len(snapshot.orders),
        "resolved_by_fallback": list(snapshot.resolved_by_fallback),
        "unresolved_order_ids": list(snapshot.unresolved_order_ids),
        "incomplete_reasons": list(snapshot.incomplete_reasons),
        "artifact_symbols": len(artifact_priors),
        "symbols_with_broker_exposure": len(discrepancies),
        "prior_allocation_discrepancies": discrepancies,
    }


__all__ = [
    "DEFAULT_MAX_PAGES",
    "BrokerOrder",
    "normalize_order",
    "PagedOrders",
    "paginate_same_day_orders",
    "SnapshotResult",
    "same_day_order_snapshot",
    "EffectivePrior",
    "reconcile_effective_prior",
    "TopUpDecision",
    "decide_effective_top_up",
    "reconciliation_metrics",
]
