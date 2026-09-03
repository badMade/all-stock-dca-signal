"""Live market-data providers and broker adapter for the near-close graded DCA strategy.

These implementations conform to the ``MarketDataProvider`` and ``BrokerAdapter``
protocols in :mod:`near_close_dca`. They hold **no credentials and make no network
calls of their own**: every external interaction goes through *injected callables*
that the scheduler/agent supplies at runtime — typically thin wrappers around the
Robinhood MCP tools (``get_equity_historicals``, ``get_equity_quotes``,
``review_equity_order``, ``place_equity_order`` …).

This mirrors the dependency-injection pattern already used by ``AlpacaBroker`` and
keeps the module import-safe, unit-testable, and secret-free. The callables receive
plain keyword arguments that map 1:1 to the MCP tool inputs and return the raw MCP
response dicts; all parsing lives here.

Safety posture
--------------
* ``RobinhoodBroker.place_market_buy`` refuses to place a real order unless
  ``enable_live_orders=True`` *and* a ``place_fn`` is wired in. With the defaults it
  returns a dry-run sentinel id and never touches the network.
* ``near_close_dca.run_once`` additionally never calls ``place_market_buy`` for a
  non-``DryRunBroker`` while ``StrategyConfig.dry_run`` is True, so live placement
  requires flipping **two** independent gates.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from near_close_dca import (
    EASTERN,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    Bar,
    OrderFill,
    OrderPlacementAmbiguousError,
    SourceName,
    WholeSharePlacementTerminalError,
    _is_blank_broker_field,
)

logger = logging.getLogger("near_close_dca.live_adapters")

# Sentinel distinguishing "not yet fetched" from a cached ``None`` (quote genuinely
# unavailable) in the signal-time quote memo, so a legitimately-absent quote is
# cached rather than re-fetched on the paired signal-time call.
_UNSET = object()

_UTC = timezone.utc

# Robinhood ``get_equity_historicals`` accepts a fixed set of interval strings; the
# server does not aggregate intermediate bars. Map the strategy's interval-in-minutes
# to the matching Robinhood interval name. 15-minute bars are intentionally absent —
# Robinhood does not offer them, so callers must use a supported granularity.
RH_INTERVAL_BY_MINUTES: Dict[int, str] = {
    1: "minute",
    5: "5minute",
    10: "10minute",
    30: "30minute",
    60: "hour",
}

# Order states that mean "an order already exists for this ticker today" when scanning
# for the one-order-per-ticker-per-day rule. Cancelled/rejected/failed orders never
# consumed capacity and must not block a fresh attempt.
_LIVE_ORDER_STATES = frozenset(
    {"new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"}
)

# Ceiling on how many ``get_equity_orders`` pages one same-day lookup will follow.
# A single ticker's same-day history is a handful of orders in practice, so this is
# a runaway/cursor-loop backstop, not a real limit; hitting it raises rather than
# returning a truncated history (see ``_iter_same_day_orders``).
_MAX_ORDER_PAGES = 20


def _next_order_cursor(data: Dict[str, Any], current_cursor: Optional[str] = None) -> Optional[str]:
    """Extract the next-page cursor from an unwrapped orders page, or None on the last page.

    Mirrors ``scripts/broker_reconciliation._next_cursor``: an explicit
    ``next_cursor``, or the ``cursor`` query param of the MCP ``next`` URL
    (``https://.../orders/?cursor=ABC``). A bare ``cursor`` field is read LAST
    because some APIs echo the CURRENT page's cursor there, and a bare cursor
    EQUAL to ``current_cursor`` (the cursor this very page was requested with)
    is exactly that echo -- the end of the list, not a continuation. Reading it
    as a continuation made the caller detect a repeated cursor and raise, so an
    absence check over a paginated history failed closed on a perfectly good
    final page, freezing crash leases and blocking valid buys (Codex review:
    "Treat an echoed cursor as the end of pagination").
    """
    direct_next = data.get("next_cursor")
    if isinstance(direct_next, str) and direct_next.strip():
        return direct_next.strip()
    next_url = data.get("next")
    if isinstance(next_url, str) and next_url.strip():
        cursor_values = parse_qs(urlparse(next_url).query).get("cursor")
        if cursor_values and cursor_values[0].strip():
            return cursor_values[0].strip()
        # A next URL with no cursor param -- or an empty/whitespace one, the
        # same end-of-list convention checked for next_cursor/cursor above --
        # is still a "there is more" signal we cannot follow deterministically;
        # _has_continuation_pointer below turns it into a raise rather than
        # letting a blank cursor be treated as a real continuation.
        return None
    direct = data.get("cursor")
    if isinstance(direct, str):
        stripped_direct = direct.strip()
        # Compare the STRIPPED value against current_cursor: current_cursor
        # was itself normalized by this same function on the prior call, so
        # a response that echoes it back with incidental surrounding
        # whitespace (e.g. " ABC " for a page requested with "ABC") is still
        # the same echo -- the end of the list, not a continuation. Comparing
        # the raw value made that echo look like a fresh cursor, which
        # _iter_same_day_orders then rejected as a repeated cursor instead of
        # ending pagination cleanly, failing closed on a perfectly good final
        # page (Codex review: "Normalize the cursor before checking for an
        # echo").
        if stripped_direct and stripped_direct != current_cursor:
            return stripped_direct
    return None


def _is_provable_orders_page(response: Any) -> bool:
    """True when ``response`` is a LIST-mode orders page a caller may reason about.

    Requires an actual ``orders`` list. A ``None``, a bare list, or a dict with
    no ``orders`` list is unparseable and must never be read as a valid *empty*
    history by a caller whose next step depends on absence being proven. An
    explicit ``{"orders": []}`` is fine -- a genuine empty page.

    A SINGLE-ORDER envelope (``{"order": {...}}``, or a bare order object) is
    rejected too, matching ``scripts/broker_reconciliation``'s own
    ``single_order_envelope_in_pagination`` rule. It is evidence about ONE order
    and says nothing about whether the account list has ended -- and
    ``_iter_same_day_orders`` only ever yields ``data["orders"]``, so such an
    envelope would pass a laxer check while silently yielding nothing, which
    crash recovery would read as "the claimed order was never submitted" and act
    on by releasing the balance (Codex review: "Reject single-order envelopes
    when proving history").
    """
    if not isinstance(response, dict):
        return False
    return isinstance(_unwrap(response).get("orders"), list)


def _has_continuation_pointer(data: Dict[str, Any]) -> bool:
    """True when a page advertises MORE results via ``next``/``next_cursor``.

    Used only after :func:`_next_order_cursor` already failed to produce a usable
    cursor, to distinguish "this was the last page" from "there is more history we
    cannot reach" -- the latter must never be reported to callers as a complete
    (and therefore absence-proving) history.

    Mirrors ``scripts/broker_reconciliation._has_continuation`` exactly rather
    than testing truthiness. A present but FALSEY non-string pointer --
    ``next_cursor: 0``, ``next: []`` -- is one ``_next_order_cursor`` cannot
    parse, so a truthiness test reported "no continuation" and the iterator
    declared a truncated history complete. Crash recovery reads that as proof
    the claimed order was never submitted, releases the lease and authorizes a
    duplicate purchase (Codex review: "Reject falsey non-string continuation
    pointers"). Any non-string value is an unusable continuation and fails
    closed; only an absent pointer, or an empty/whitespace string (the
    conventional end-of-list), means there is genuinely nothing more.
    """
    for key in ("next", "next_cursor"):
        value = data.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value is not None:
            return True
    return False


def _unwrap(payload: Any) -> Dict[str, Any]:
    """Extract the inner data dict from an MCP resource/tool payload.

    Returns ``payload['data']`` when present and a dict; otherwise returns the
    original payload dict. Non-dict payloads return an empty dict.
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    return data


def _to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float parse for Robinhood's stringified numbers; ``None`` -> default.

    Non-finite values (``NaN``/``inf`` from e.g. ``"NaN"``) collapse to ``default`` so they
    can never slip through a numeric comparison — a NaN buying power would make
    ``buying_power < amount`` false and bypass the gate.
    """
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _first_positive_float(mapping: Dict[str, Any], *keys: str) -> float:
    """Return the first strictly-positive float found across ``keys`` in ``mapping``.

    Returns ``0.0`` if no key is present or every candidate is non-positive/non-numeric,
    so callers can treat that the same as "field not present" rather than special-casing
    each of several possible key names individually.
    """
    for key in keys:
        value = _to_float(mapping.get(key))
        if value > 0:
            return value
    return 0.0


def _is_error_response(data: Any) -> Optional[str]:
    """Check if an un-wrapped response dictionary represents an MCP-level error.

    Returns the error message if one is found, otherwise None.
    """
    if not isinstance(data, dict):
        return None
    # 1. The explicit isError envelope returned by mcp-bridge for network failures.
    if data.get("isError") is True:
        msg = data.get("error")
        return str(msg) if msg else "Unknown network/bridge error"

    # 2. A truthy top-level "error" is always a failure signal on its own, whatever
    # its shape (string, dict, list, ...) — only its rendering differs, so detection
    # never depends on it happening to be a string and empty values stay non-errors.
    error_val = data.get("error")
    if error_val:
        return f"error: {error_val}"

    # 3. "message"/"detail" are informational field names, not intrinsic error
    # indicators — only trust them as a failure reason once an explicit
    # success=false/ok=false flag has already established that this is a failure.
    for flag in ("success", "ok"):
        if data.get(flag) is False:
            for key in ("message", "detail"):
                val = data.get(key)
                if val:
                    return str(val)
            return f"{flag}=false"
    return None

def _response_failure(response: Any) -> Optional[str]:
    """Extract the error message from a raw MCP response, if any.

    Checks for failure markers at two levels: the outer payload and the first
    unwrapped inner ``data`` value (via :func:`_unwrap`). Does not recursively
    unwrap nested envelopes.
    Returns None if the response is valid and contains no error markers.
    """
    data = _unwrap(response)
    if not data and not isinstance(response, dict):
        return "Response is not a valid JSON object"
    # Empty unwrapped dict is usually just empty data, not necessarily an error,
    # unless the raw payload itself was an error envelope.
    return _is_error_response(data) or _is_error_response(response if isinstance(response, dict) else {})


def _parse_rfc3339(value: Any) -> Optional[datetime]:
    """Parse an RFC3339/ISO-8601 timestamp (incl. ``Z`` and nanosecond precision).

    Robinhood returns timestamps like ``2026-07-06T18:31:29.201412148Z`` (9 fractional
    digits). ``datetime.fromisoformat`` accepts at most microsecond precision, so trim
    the fractional part to 6 digits and normalise the trailing ``Z`` first.

    Returns a timezone-*aware* datetime, or ``None`` when the value is missing/unparseable.
    The instant is preserved exactly: an explicit offset (``Z`` or ``±HH:MM``) is kept as
    given; a naive string (no zone suffix) is assumed to be Eastern, since that is the
    market timezone the strategy operates in. Downstream callers convert via
    ``astimezone(EASTERN)``, so any correctly-zoned input yields the right wall-clock time.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        frac = tail
        offset = ""
        for sep in ("+", "-"):
            idx = tail.find(sep)
            if idx != -1:
                frac, offset = tail[:idx], tail[idx:]
                break
        frac = frac[:6]
        text = f"{head}.{frac}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EASTERN)
    return parsed


def _rh_interval(interval_minutes: int) -> str:
    try:
        return RH_INTERVAL_BY_MINUTES[interval_minutes]
    except KeyError as exc:
        supported = ", ".join(str(m) for m in sorted(RH_INTERVAL_BY_MINUTES))
        raise ValueError(
            f"Robinhood has no {interval_minutes}-minute bar; supported minutes: {supported}"
        ) from exc


# Callable signatures (all keyword-only in practice) that mirror the Robinhood MCP tools.
HistoricalsFn = Callable[..., Dict[str, Any]]  # (symbols, start_time, end_time, interval, bounds) -> dict
QuotesFn = Callable[..., Dict[str, Any]]        # (symbols) -> dict
BarsFn = Callable[..., Any]                     # generic finance bars -> iterable of bar-like mappings
PrevCloseFn = Callable[..., Any]                # generic finance previous close -> price or mapping


class RobinhoodDataProvider:
    """``MarketDataProvider`` backed by the Robinhood ``get_equity_historicals`` /
    ``get_equity_quotes`` MCP tools via injected callables.

    Parameters
    ----------
    historicals_fn:
        ``fn(symbols=[...], start_time=iso, end_time=iso, interval=str, bounds=str,
        adjustment_type=str) -> dict`` returning the raw ``get_equity_historicals``
        response. All six keywords are always passed (they mirror the MCP tool inputs),
        so a wrapper must accept them — declare ``**kwargs`` if it ignores some.
    quotes_fn:
        Optional ``fn(symbols=[...]) -> dict`` returning the raw ``get_equity_quotes``
        response. Required for the previous-close and signal-time quote; without it
        ``get_previous_close_bar`` falls back to a daily historicals lookup and
        ``get_quote`` returns ``None`` (the strategy then uses the decision-bar close).
    """

    source = SourceName.ROBINHOOD

    def __init__(
        self,
        historicals_fn: HistoricalsFn,
        quotes_fn: Optional[QuotesFn] = None,
        *,
        bounds: str = "regular",
        adjustment_type: str = "none",
    ) -> None:
        self._historicals_fn = historicals_fn
        self._quotes_fn = quotes_fn
        self._bounds = bounds
        self._adjustment_type = adjustment_type
        # ⚡ Bolt: signal-time quote memo, scoped to a single decision moment. Both
        # get_previous_close_bar (via _previous_close_from_quote) and get_quote are
        # invoked from near_close_dca.evaluate_source_signal for the SAME ticker at the
        # SAME decision moment, each previously issuing an identical
        # quotes_fn(symbols=[ticker]) call -- a duplicate live MCP/network round-trip
        # (or a duplicate O(N) scan of the frozen quote snapshot) per ticker every run.
        # Memoizing the signal-time snapshot halves signal-time quote fetches
        # (~44 redundant fetches -> 0 for the live universe) with byte-identical
        # behavior: within one run each ticker is evaluated once at one decision moment.
        # The cache is cleared whenever the decision moment (``as_of``) changes, so a
        # reused provider stays bounded to the current moment's universe (~44 entries)
        # and never grows without bound or stale-serves a later run.
        # Deliberately NOT consulted by get_bid_ask -- that is the pre-placement spread
        # circuit breaker, which must always read a fresh quote (it keeps using the
        # uncached _quote_for), so the memo can never stale-serve the breaker.
        self._signal_quote_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._signal_quote_as_of: Optional[datetime] = None

    def get_intraday_bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> List[Bar]:
        interval = _rh_interval(interval_minutes)
        response = self._historicals_fn(
            symbols=[ticker],
            start_time=_to_utc_iso(start),
            end_time=_to_utc_iso(end),
            interval=interval,
            bounds=self._bounds,
            adjustment_type=self._adjustment_type,
        )
        return self._parse_bars(response, ticker)

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        if self._quotes_fn is not None:
            bar = self._previous_close_from_quote(ticker, as_of)
            if bar is not None:
                return bar
        # Fallback: pull daily bars over the trailing two weeks and take the last
        # completed regular-session day strictly before the decision date.
        as_of_date = as_of.astimezone(EASTERN).date()
        response = self._historicals_fn(
            symbols=[ticker],
            start_time=_to_utc_iso(datetime.combine(as_of_date - timedelta(days=14), time(0, 0), tzinfo=EASTERN)),
            end_time=_to_utc_iso(as_of),
            interval="day",
            bounds=self._bounds,
            adjustment_type=self._adjustment_type,
        )
        daily = [bar for bar in self._parse_bars(response, ticker) if bar.timestamp.astimezone(EASTERN).date() < as_of_date]
        if not daily:
            return None
        return max(daily, key=lambda bar: bar.timestamp)

    def get_quote(self, ticker: str, as_of: datetime) -> Optional[Tuple[float, Optional[datetime]]]:
        if self._quotes_fn is None:
            return None
        quote = self._signal_quote_for(ticker, as_of)
        if quote is None:
            return None
        if not quote.get("has_traded", True) or quote.get("state", "active") != "active":
            logger.warning("skipping quote for %s: has_traded=%s state=%s", ticker, quote.get("has_traded"), quote.get("state"))
            return None
        price = _to_float(quote.get("last_trade_price"))
        if price <= 0:
            return None
        return price, _parse_rfc3339(quote.get("venue_last_trade_time"))

    def get_bid_ask(self, ticker: str, as_of: datetime) -> Optional[Tuple[float, float]]:
        """Return the current bid/ask quote for the spread circuit breaker."""
        if self._quotes_fn is None:
            return None
        quote = self._quote_for(ticker)
        if quote is None:
            return None
        # Check activity from both the nested quote and the raw sibling result —
        # some payloads expose has_traded/state at the top-level result alongside
        # the nested "quote" object. Fail closed if either source says inactive.
        siblings = quote.get("_siblings") or {}
        has_traded = quote.get("has_traded", True) and siblings.get("has_traded", True)
        state = quote.get("state", "active")
        sibling_state = siblings.get("state", "active")
        if not has_traded or state != "active" or sibling_state != "active":
            raise ValueError(
                f"no spread for {ticker}: inactive quote "
                f"(has_traded={quote.get('has_traded')}/{siblings.get('has_traded')}, "
                f"state={quote.get('state')}/{siblings.get('state')})"
            )
        # Some Robinhood MCP payloads expose bid/ask under alternate key names
        # (best_bid/best_ask) and/or as siblings of the nested "quote" object
        # rather than inside it. Check the nested quote first, then the raw
        # top-level result, so a genuinely positive value in either location
        # wins rather than an empty/zero nested placeholder unconditionally
        # shadowing a usable sibling value.
        bid_keys = ("bid_price", "bid", "best_bid", "best_bid_price")
        ask_keys = ("ask_price", "ask", "best_ask", "best_ask_price")
        bid = _first_positive_float(quote, *bid_keys) or _first_positive_float(siblings, *bid_keys)
        ask = _first_positive_float(quote, *ask_keys) or _first_positive_float(siblings, *ask_keys)
        # Deliberately no last-trade-price fallback here: a spread breaker
        # that treats a missing/one-sided NBBO as a synthetic 0% spread would
        # fail open exactly when it has the least information. Any quote
        # without a genuine two-sided bid/ask stays unavailable.
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError(
                f"no usable bid/ask in {ticker} quote (bid={bid!r}, ask={ask!r})"
            )
        return bid, ask

    # -- internals ---------------------------------------------------------------

    def _parse_bars(self, response: Any, ticker: str) -> List[Bar]:
        data = _unwrap(response)
        results = data.get("results") or []
        bars: List[Bar] = []
        for result in results:
            if result.get("symbol") and result.get("symbol") != ticker:
                continue
            for raw in result.get("bars") or []:
                if raw.get("interpolated"):
                    continue  # gap-fill bar carries no new information
                timestamp = _parse_rfc3339(raw.get("begins_at"))
                if timestamp is None:
                    continue
                close = _to_float(raw.get("close_price"), default=-1.0)
                if close <= 0:
                    # A missing/non-numeric close during an API gap or partial bar would
                    # otherwise become 0.0 and read as a ~100% drop -> spurious max BUY.
                    # Drop it so the strategy degrades to NO_DATA instead.
                    logger.warning("dropping %s bar with invalid close_price=%r", ticker, raw.get("close_price"))
                    continue
                bars.append(
                    Bar(
                        timestamp=timestamp,
                        open=_to_float(raw.get("open_price")),
                        high=_to_float(raw.get("high_price")),
                        low=_to_float(raw.get("low_price")),
                        close=close,
                        volume=_to_float(raw.get("volume")),
                    )
                )
        return bars

    def _signal_quote_for(self, ticker: str, as_of: datetime) -> Optional[Dict[str, Any]]:
        """Signal-time quote for ``ticker``, memoized per (ticker, decision moment).

        ⚡ Bolt: dedupes the two signal-time quote fetches that
        get_previous_close_bar and get_quote each make for the same ticker at the
        same decision moment. get_bid_ask intentionally bypasses this (it must read
        a fresh quote for the pre-placement spread breaker). The returned dict is
        only ever read by its consumers, so sharing the cached object is safe.

        The memo is scoped to a single decision moment: when ``as_of`` changes it is
        dropped, keeping it bounded to the current moment's universe rather than
        accumulating an entry per (ticker, moment) forever on a reused provider.
        """
        if as_of != self._signal_quote_as_of:
            self._signal_quote_cache.clear()
            self._signal_quote_as_of = as_of
        cached = self._signal_quote_cache.get(ticker, _UNSET)
        if cached is not _UNSET:
            return cached  # type: ignore[return-value]
        quote = self._quote_for(ticker)
        self._signal_quote_cache[ticker] = quote
        return quote

    def _quote_for(self, ticker: str) -> Optional[Dict[str, Any]]:
        data = _unwrap(self._quotes_fn(symbols=[ticker]))  # type: ignore[misc]
        for result in data.get("results") or []:
            quote = result.get("quote") or {}
            if quote.get("symbol") in (ticker, None):
                # Attach the official settled close so callers can reuse it.
                quote = dict(quote)
                quote["_close"] = result.get("close") or {}
                # Some MCP payloads expose bid/ask (and possibly other quote-side
                # fields) as top-level siblings of the nested "quote" object rather
                # than inside it. Keep the raw top-level result available so callers
                # like get_bid_ask can fall back to sibling fields without an
                # unconditional merge silently discarding usable nested data (or
                # vice versa).
                quote["_siblings"] = result
                return quote
        return None

    def _previous_close_from_quote(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        quote = self._signal_quote_for(ticker, as_of)
        if quote is None:
            return None
        official = quote.get("_close") or {}
        price = _to_float(official.get("price")) or _to_float(quote.get("previous_close"))
        if price <= 0:
            return None
        close_date_str = official.get("date") or quote.get("previous_close_date")
        close_date = _parse_iso_date(close_date_str) or (as_of.astimezone(EASTERN).date() - timedelta(days=1))
        # Stamp the prior-session close bar at the 4pm ET regular-session close.
        timestamp = datetime.combine(close_date, time(16, 0), tzinfo=EASTERN)
        # low=0.0 (not price): this quote-derived bar has no real prior-session low,
        # only a close. select_first_allocation_base_usd treats a low <= 0 as
        # unknown/missing (conservative $1 base) -- fabricating low=price here would
        # make every below-close decision price also read as "below the low" and
        # always select the $2 base.
        return Bar(timestamp=timestamp, open=price, high=price, low=0.0, close=price, volume=0.0)


class FinanceDataProvider:
    """Source-agnostic ``MarketDataProvider`` for the independent "finance" cross-check.

    The reconciliation logic in :mod:`near_close_dca` compares a finance-side and a
    Robinhood-side signal before ordering, so the finance provider should be a genuinely
    independent source (e.g. yfinance/Yahoo, a vendor OHLCV API, or a second Robinhood
    query at a different granularity). Wire it with two callables:

    * ``bars_fn(ticker, start, end, interval_minutes) -> iterable`` yielding bar-like
      mappings with ``timestamp`` (datetime or ISO string) and ``open/high/low/close/volume``.
    * ``previous_close_fn(ticker, as_of) -> float | mapping`` returning the prior close.

    Keys are matched leniently (``close``/``close_price``, ``timestamp``/``begins_at``/``t``)
    so most vendor payloads work without a bespoke adapter.
    """

    source = SourceName.FINANCE

    def __init__(self, bars_fn: BarsFn, previous_close_fn: PrevCloseFn) -> None:
        self._bars_fn = bars_fn
        self._previous_close_fn = previous_close_fn

    def get_intraday_bars(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        interval_minutes: int,
    ) -> List[Bar]:
        raw_bars = self._bars_fn(ticker, start, end, interval_minutes) or []
        bars: List[Bar] = []
        for raw in raw_bars:
            bar = _coerce_bar(raw)
            if bar is not None:
                bars.append(bar)
        return bars

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        result = self._previous_close_fn(ticker, as_of)
        if result is None:
            return None
        if isinstance(result, Bar):
            return result
        price = 0.0
        low = 0.0
        if isinstance(result, dict):
            bar = _coerce_bar(result)
            if bar is not None:
                return bar
            # A price-only mapping (e.g. {"price": "100"} / {"close": "100"}) has no bar
            # timestamp; fall through to the scalar path rather than dropping it, so the
            # finance source still confirms and the reconciliation stays two-sided.
            price = _to_float(result.get("price") or result.get("close") or result.get("previous_close"))
            # Preserve a real low the mapping supplied even though it lacks a bar
            # timestamp (Codex review: "Preserve an available low in price-only
            # finance mappings") -- e.g. {"close": 100, "low": 95} would otherwise
            # fall through here and lose its genuine low to the low=0.0 default
            # below, wrongly downgrading a below-$95 decision price to the $1 base.
            # select_first_allocation_base_usd separately validates this against
            # previous_close, so an invalid/missing low still safely falls back to
            # 0.0 (unknown).
            low = _to_float(result.get("low"))
        else:
            price = _to_float(result)
        if price <= 0:
            return None
        timestamp = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        # low=0.0 (not price) when no real low was supplied: a scalar previous-close
        # carries no real prior-session low. See the matching comment in
        # _previous_close_from_quote above -- fabricating low=price here would make
        # select_first_allocation_base_usd treat every below-close price as also
        # below the low and always pick the $2 base.
        return Bar(timestamp=timestamp, open=price, high=price, low=low, close=price, volume=0.0)


class RobinhoodBroker:
    """``BrokerAdapter`` for Robinhood equities via injected MCP callables.

    All callables are optional; a missing callable degrades that check to a safe
    default (permissive review/tradability, no same-day order, simulated buying power)
    so the broker is usable in progressively-wired environments. Real placement is
    gated behind ``enable_live_orders`` **and** a ``place_fn``.

    Callable signatures (keyword args mirror the Robinhood MCP tools):

    * ``buying_power_fn(account_number) -> float``
    * ``orders_fn(account_number, symbol, created_at_gte) -> dict`` (get_equity_orders)
    * ``tradability_fn(account_number, symbols) -> dict`` (get_equity_tradability)
    * ``review_fn(account_number, symbol, side, type, dollar_amount) -> dict`` (review_equity_order)
    * ``place_fn(account_number, symbol, side, type, dollar_amount, ref_id, time_in_force) -> dict``
    * ``order_fill_fn(account_number, order_id) -> dict`` (get_equity_orders single-order mode)

    ``supports_concurrent_fill_lookup`` defaults to False: ``order_fill_fn`` is typically a
    thin wrapper around a scheduler-owned MCP callable, and MCP client sessions are commonly
    bound to a single connection/event loop that is not safe to invoke from multiple threads
    at once. Only pass ``True`` once the wired ``order_fill_fn`` has been verified safe for
    concurrent calls (e.g. it opens an independent connection per call).

    ``review_fn``/``place_fn`` are reused (not duplicated) for whole-share,
    quantity-based orders -- see :meth:`review_whole_share_buy` and
    :meth:`place_whole_share_buy`, built for
    ``dca_whole_share_accumulator.py`` -- by passing ``quantity=`` instead of
    ``dollar_amount=``. This assumes the wired Robinhood
    ``review_equity_order``/``place_equity_order`` MCP tools accept a
    ``quantity`` argument as an alternative order-sizing method to
    ``dollar_amount``, matching how Robinhood's real order API supports both.
    **Unverified in this repo**: no session that has written this module has
    had live Robinhood MCP access to confirm it. A live agent session with
    real broker access should confirm the tool accepts ``quantity`` (and
    what it returns for a non-fractional instrument) before the whole-share
    path is trusted with a real order -- see
    ``docs/whole_share_accumulation.md``.
    """

    def __init__(
        self,
        account: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
        *,
        buying_power_fn: Optional[Callable[..., Any]] = None,
        orders_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        tradability_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        review_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        place_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        order_fill_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        enable_live_orders: bool = False,
        time_in_force: str = "gfd",
        simulated_buying_power_usd: float = 300.0,
        supports_concurrent_fill_lookup: bool = False,
    ) -> None:
        self._account = account
        self._buying_power_fn = buying_power_fn
        self._orders_fn = orders_fn
        self._tradability_fn = tradability_fn
        self._review_fn = review_fn
        self._place_fn = place_fn
        self._order_fill_fn = order_fill_fn
        self.enable_live_orders = enable_live_orders
        self._time_in_force = time_in_force
        self._simulated_buying_power_usd = simulated_buying_power_usd
        self._raw_placement_responses: List[Dict[str, Any]] = []
        self.supports_concurrent_fill_lookup = supports_concurrent_fill_lookup

    def account_number(self) -> str:
        return self._account

    def available_buying_power(self) -> float:
        if self._buying_power_fn is None:
            return self._simulated_buying_power_usd
        try:
            result = self._buying_power_fn(account_number=self._account)
        except Exception:  # noqa: BLE001 - never let buying-power lookup crash a scan
            logger.exception("buying-power lookup failed; treating as 0")
            return 0.0
        # Accept either a scalar or a raw MCP dict (per the module-level callable contract).
        if isinstance(result, dict):
            data = _unwrap(result)
            for key in ("buying_power", "cash", "unallocated_buying_power", "portfolio_cash", "withdrawable_amount"):
                if key in data:
                    return _to_float(data.get(key))
            logger.warning("buying-power response had no recognizable amount field: %s", data)
            return 0.0
        return _to_float(result)

    def _iter_same_day_orders(
        self, ticker: str, trading_day: date, *, require_provable_history: bool = False
    ) -> Iterator[Dict[str, Any]]:
        """Yield every order for ``ticker`` from ``trading_day`` onward, page by page.

        Lazy on purpose. ``get_equity_orders`` has a lower time bound
        (``created_at_gte``) but no upper one, so a lease recovered several
        trading days after its claim scans every order since -- and the
        newest-first ordering puts the one we want last. Materializing the whole
        result before searching it meant a long-enough history tripped the page
        ceiling and raised even when the matching ref had already gone past,
        which recovery then read as "cannot reconcile" and preserved the lease
        indefinitely (Codex review: "Bound historical recovery queries to the
        claim day"). Yielding lets a caller looking for ONE order stop the
        moment it finds it, so a found ref never depends on how much history
        sits behind it.

        The ceiling still binds for callers that must consume everything --
        proving absence, or summing notional -- which is the fail-closed
        direction: those callers raise rather than conclude from a truncated
        history.

        ``require_provable_history`` raises, rather than yielding nothing, for
        the two shapes that are indistinguishable from a genuinely empty history
        but prove nothing: an adapter with no ``orders_fn`` wired at all, and a
        response that is not a recognizable orders envelope. Silence from either
        one let crash recovery conclude that a claimed order was never submitted
        and release the balance for a duplicate purchase (Codex review:
        "Preserve leases when order history is unavailable").

        It is opt-in rather than the default because the ordinary duplicate-order
        gate has the OPPOSITE established contract -- an unwired adapter reports
        "no order today" and lets a buy proceed (see
        ``test_robinhood_broker_same_day_lookup_without_orders_fn``, and
        ``morning_dca.py``'s use of it as its sole duplicate gate). Flipping that
        is a real improvement but a separate, broader change than this one.
        """
        if self._orders_fn is None:
            if require_provable_history:
                raise RuntimeError(
                    f"same-day order lookup for {ticker} has no orders_fn wired; "
                    "an unqueryable history cannot prove an order is absent"
                )
            return
        seen_cursors: set[str] = set()
        seen_order_ids: set[str] = set()
        cursor: Optional[str] = None
        for _ in range(_MAX_ORDER_PAGES):
            kwargs: Dict[str, Any] = {
                "account_number": self._account,
                "symbol": ticker,
                "created_at_gte": trading_day.isoformat(),
            }
            if cursor is not None:
                kwargs["cursor"] = cursor
            response = self._orders_fn(**kwargs)
            error = _response_failure(response)
            if error is not None:
                # Fail closed: an error-shaped lookup ("no orders" by omission) must not let
                # the one-order-per-ticker-per-day guard pass and allow a duplicate buy.
                raise RuntimeError(f"same-day order lookup for {ticker} failed: {error}")
            data = _unwrap(response)
            if require_provable_history and not _is_provable_orders_page(response):
                # A None, a bare list, a dict carrying no orders list, or a
                # single-order envelope is not a list-mode page -- NOT a valid
                # empty history.
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned an unrecognizable page"
                )
            orders = data.get("orders")
            if orders is None:
                orders = []
            if not isinstance(orders, list):
                raise RuntimeError(
                    f"same-day order lookup for {ticker} returned orders in an unexpected shape"
                )
            for order in orders:
                # Robinhood can repeat an order at a page boundary (an insert
                # ahead of the cursor shifts every row behind it by one). An
                # id-less order cannot be deduplicated and is always yielded --
                # every observed shape carries one, so this only widens the
                # fail-open surface for a response shape that would already be
                # unusable elsewhere. Summing every returned row verbatim
                # overstates same_day_ticker_notional_usd's broker-reported
                # notional and can wrongly shrink or skip a guarded top-up
                # (Codex review: "Deduplicate orders across pagination
                # boundaries").
                order_id = order.get("id") if isinstance(order, dict) else None
                # Use the shared blank-field helper (rather than a bespoke isinstance
                # check) so this pagination-dedup path treats None/""/whitespace-only
                # order ids the same way the rest of this module does, and normalize
                # a non-string-but-truthy id (e.g. an int) to str before hashing it
                # into seen_order_ids so the same order can't slip through as two
                # "different" keys.
                if _is_blank_broker_field(order_id):
                    order_id = order.get("order_id") if isinstance(order, dict) else None
                if not _is_blank_broker_field(order_id):
                    order_id = str(order_id).strip()
                    if order_id in seen_order_ids:
                        continue
                    seen_order_ids.add(order_id)
                yield order
            cursor = _next_order_cursor(data, cursor)
            if cursor is None:
                if _has_continuation_pointer(data):
                    # A next/next_cursor pointer we could not turn into a usable
                    # cursor is "there is more we cannot reach", not "the list
                    # ended" -- letting a caller prove absence from a truncated
                    # history.
                    raise RuntimeError(
                        f"same-day order lookup for {ticker} returned an unusable pagination pointer"
                    )
                return
            if cursor in seen_cursors:
                raise RuntimeError(f"same-day order lookup for {ticker} repeated a pagination cursor")
            seen_cursors.add(cursor)
        raise RuntimeError(
            f"same-day order lookup for {ticker} exceeded {_MAX_ORDER_PAGES} pages"
        )

    def _same_day_orders(self, ticker: str, trading_day: date) -> List[Dict[str, Any]]:
        """Return EVERY same-day order for ``ticker``, following pagination.

        ``get_equity_orders`` is a paginated tool: a busy account's same-day
        history spans several pages and the first one alone is not the whole
        story. Reading only page one made every caller here fail OPEN --
        ``same_day_strategy_order_exists`` would report "no order today" and
        authorize a duplicate buy, and the crash-recovery lookup
        ``strategy_order_with_ref_id`` would report the claimed fire order as
        never submitted and release the lease for a second real purchase --
        purely because the matching order sat on page two (Codex review:
        "Page broker history before releasing the fire lease").
        ``scripts/broker_reconciliation.paginate_same_day_orders`` is the
        reference implementation of the cursor protocol; this is the same
        contract narrowed to one symbol, and it fails CLOSED the same way:
        an unfollowable continuation pointer, a cursor loop, or hitting the
        page ceiling raises instead of returning a partial history that
        callers would read as proof of absence.

        The eager form, for callers that genuinely need the complete list.
        Callers searching for ONE order should iterate ``_iter_same_day_orders``
        and stop on the match instead.
        """
        return list(self._iter_same_day_orders(ticker, trading_day))

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        for order in self._iter_same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() in _LIVE_ORDER_STATES:
                # Proving PRESENCE needs no further pages; only the negative
                # answer below depends on having seen the whole history.
                return True
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        """Return the first live same-day order whose client ref id exactly matches one of
        ``ref_id_prefixes``, or ``None`` if no such order exists.

        Exact matching avoids false positives when one ticker symbol is a prefix of another
        (e.g. ``GOOG`` vs ``GOOGL``). The idempotency keys generated by ``_place_broker_orders``
        are fully-qualified exact strings, so a raw ``startswith`` check is unnecessary and
        unsafe.
        """
        for order in self._iter_same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() not in _LIVE_ORDER_STATES:
                continue
            ref_id = str(
                order.get("ref_id")
                or order.get("client_order_id")
                or order.get("client_order_uuid")
                or order.get("client_id")
                or ""
            )
            if ref_id and ref_id in ref_id_prefixes:
                return order
        return None

    def same_day_order_with_ref_id_prefix_any_state(
        self, ticker: str, trading_day: date, ref_id_prefixes: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        """Same exact-ref-id match as
        :meth:`same_day_strategy_order_exists_with_ref_id_prefix`, but WITHOUT
        the ``_LIVE_ORDER_STATES`` filter -- also returns a genuinely
        terminal (``cancelled``/``rejected``/``expired``/etc.) order.

        The live-only method exists to answer "does an order still occupy
        this idempotency slot" and deliberately excludes a cancelled/
        rejected order (it "never consumed capacity", see
        ``_LIVE_ORDER_STATES``'s own comment). That is the wrong question
        for whole-share reconciliation of an AMBIGUOUS placement: an order
        that partially filled and was THEN cancelled is real, already-
        executed capital the ledger must still record, and the live-only
        method would report it as a clean "no match" indistinguishable from
        one that never reached the broker at all -- silently releasing
        credit for shares that already executed (Codex review: "Reconcile
        partial fills before releasing canceled claims"). Exposes the raw
        order (any state) so the caller can inspect its own
        ``cumulative_quantity``/``state`` directly.

        Iterates with ``require_provable_history=True``, the same posture
        :meth:`strategy_order_with_ref_id` already uses for the same
        recovery-sensitive lookup: an unwired ``orders_fn`` or an
        unrecognizable page is indistinguishable from a genuinely empty
        history, but proves nothing. Every caller of this method is
        whole-share reconciliation, which reads a clean ``None`` as
        permission to restore a stale or placement-failure claim -- if that
        "found nothing" were actually "could not prove anything", an order
        that genuinely reached the broker could have its already-spent
        credit released and reused (Codex review: "Require provable order
        history before releasing claims").
        """
        for order in self._iter_same_day_orders(ticker, trading_day, require_provable_history=True):
            ref_id = str(
                order.get("ref_id")
                or order.get("client_order_id")
                or order.get("client_order_uuid")
                or order.get("client_id")
                or ""
            )
            if ref_id and ref_id in ref_id_prefixes:
                return order
        return None

    def strategy_order_with_ref_id(
        self, ticker: str, trading_day: date, ref_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the matching historical order regardless of its current state.

        Iterates lazily and returns on the first match, so a ref found anywhere
        in the history never trips the page ceiling -- important for a crash
        lease recovered days after its claim, where the query has no upper time
        bound and the newest-first ordering puts the wanted order last.

        Requires a PROVABLE history: this is crash reconciliation, where an
        empty answer is taken as evidence the claimed order was never submitted
        and the reserved balance is released. An unwired ``orders_fn`` or an
        unrecognizable page therefore raises instead of returning ``None``, so
        the caller preserves the lease rather than authorizing a duplicate
        purchase on the strength of a lookup that never happened.
        """
        for order in self._iter_same_day_orders(
            ticker, trading_day, require_provable_history=True
        ):
            candidate_ref = str(
                order.get("ref_id")
                or order.get("client_order_id")
                or order.get("client_order_uuid")
                or order.get("client_id")
                or ""
            )
            if candidate_ref == ref_id:
                return order
        return None

    def same_day_ticker_notional_usd(self, ticker: str, trading_day: date) -> float:
        """Sum the dollar notional of every live same-day order for ``ticker``.

        Unlike the two methods above, this is NOT scoped to any particular
        ref-id convention -- it sums every live-state order the broker itself
        reports for this ticker/day, regardless of which stream or executor
        (this roster, a legacy morning_dca.py/midday_dca.py run, or a manual
        fill) placed it. Ground truth for a guarded top-up's incremental
        delta, which must never trust an artifact-derived prior allocation
        that is lower than what the broker actually shows (see
        near_close_dca.evaluate_top_up_guard's KNOWN GAP note).
        """
        total = 0.0
        for order in self._same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() not in _LIVE_ORDER_STATES:
                continue
            try:
                total += float(order.get("dollar_amount") or order.get("notional") or 0)
            except (TypeError, ValueError):
                continue
        return total

    def is_tradable_for_fractional_buy(self, ticker: str) -> bool:
        if self._tradability_fn is None:
            return True
        data = _unwrap(self._tradability_fn(account_number=self._account, symbols=[ticker]))
        for result in data.get("results") or []:
            if result.get("symbol") != ticker:
                continue
            if not result.get("tradeable", False) or result.get("state") != "active":
                return False
            return str(result.get("fractional_tradability", "")) == "tradable"
        return False

    def is_tradable_for_whole_share_buy(self, ticker: str) -> bool:
        """Same tradeable/active check as :meth:`is_tradable_for_fractional_buy`,
        WITHOUT the ``fractional_tradability`` requirement -- a whole-share
        ticker (e.g. BYDDY) is in that category precisely because it fails
        that requirement, so reusing the fractional check verbatim would
        block every whole-share order outright. Checked unconditionally
        (both scheduled and manual whole-share candidates), unlike
        :meth:`review_whole_share_buy`, which mirrors :meth:`review_market_buy`
        in only running on a manual run (Codex review: "Verify whole-share
        tradability before scheduled placement" -- without this, a scheduled
        run could reach ``place_whole_share_buy`` for an instrument that
        went halted/inactive/non-tradeable while quote data remained
        available, with no pre-placement tradability check at all).
        """
        if self._tradability_fn is None:
            return True
        data = _unwrap(self._tradability_fn(account_number=self._account, symbols=[ticker]))
        # Compared case-insensitively: unlike "active" (schema-enforced
        # uppercase), "whole_share" has no such schema constraint, so a
        # config-authored lowercase entry (e.g. "byddy") reaches this call
        # verbatim. The broker's real API response uses its own canonical
        # (typically uppercase) casing regardless of what was requested, so
        # an exact-match comparison would silently treat every real,
        # tradeable response as "symbol not found" -> not tradable,
        # restoring every fire's deduction instead of ever placing an order
        # (Codex review: "Require canonical symbols for whole-share
        # tickers").
        for result in data.get("results") or []:
            if str(result.get("symbol", "")).upper() != ticker.upper():
                continue
            return bool(result.get("tradeable", False)) and result.get("state") == "active"
        return False

    def review_market_buy(self, ticker: str, notional_usd: float) -> Tuple[bool, str]:
        if self._review_fn is None:
            # Fail closed on the live path: a real order must never be placed without a
            # broker-side review. Only the dry-run/simulated path may skip it.
            if self.enable_live_orders:
                return False, "live orders enabled but no review callable wired; refusing to place unreviewed order"
            return True, "no review callable wired; skipping broker-side review (dry-run)"
        response = self._review_fn(
            account_number=self._account,
            symbol=ticker,
            side="buy",
            type="market",
            dollar_amount=f"{notional_usd:.2f}",
        )
        return _interpret_review(response)

    def place_market_buy(self, ticker: str, notional_usd: float, ref_id: str) -> str:
        if not (self.enable_live_orders and self._place_fn is not None):
            # run_once only calls a live broker's place_market_buy on the non-dry-run path
            # (dry-run uses a DRY-RUN-SIM sentinel without touching the broker). Reaching
            # here with the live gate disabled is a misconfiguration; returning a
            # success-like id would make run_once record a phantom buy and consume budget.
            raise RuntimeError(
                f"place_market_buy called for {ticker} with the live gate disabled "
                f"(enable_live_orders={self.enable_live_orders}, "
                f"place_fn={'set' if self._place_fn else 'None'}); use dry_run=True for simulated runs"
            )
        try:
            response = self._place_fn(
                account_number=self._account,
                symbol=ticker,
                side="buy",
                type="market",
                dollar_amount=f"{notional_usd:.2f}",
                time_in_force=self._time_in_force,
                ref_id=ref_id,
            )
        except Exception as exc:  # noqa: BLE001 - the connector call itself
            # (network timeout, connection reset, MCP transport error, ...)
            # gave no broker response at all, unlike every raise below this
            # point, which reacts to a definitive response the broker DID
            # return (a failure envelope, no order id, or a terminal state).
            # Whether this order actually reached and was accepted by the
            # broker is genuinely unknown here -- re-raise as the distinct
            # ambiguous-placement error so the caller records
            # "potentially_submitted" and conservatively reserves this
            # notional instead of treating the ticker as unbought (Codex
            # review, PR #1300 discussion_r3920155064: "Emit the new
            # ambiguous-placement status").
            raise OrderPlacementAmbiguousError(
                f"placement connector call for {ticker} failed with no broker response: {exc}"
            ) from exc
        outer_failure = _response_failure(response)
        if outer_failure is not None:
            # A failure envelope that still carries an inner order id (e.g.
            # {"success": false, "data": {"order": {...}}}) must not be read as success.
            logger.error("place_equity_order for %s reported failure: %s", ticker, response)
            raise RuntimeError(f"live placement for {ticker} failed: {outer_failure}")
        data = _unwrap(response)
        order = data.get("order") or data
        order_id = order.get("id") or order.get("order_id")
        if not order_id:
            # Fail fast: run_once treats any returned id as a submitted order and records
            # the notional against daily/weekly budgets. Returning "" would book a phantom
            # buy with no order to reconcile, so raise instead of reporting false success.
            logger.error("place_equity_order returned no order id for %s: %s", ticker, data)
            raise RuntimeError(f"live placement for {ticker} returned no order id; response={data}")
        state = str(order.get("state", "")).lower()
        if state in {"rejected", "failed", "cancelled", "canceled", "voided"}:
            # An id in a terminal-failure state means no live order exists; reporting it as
            # submitted would consume budget and block a retry, so raise instead.
            logger.error("place_equity_order for %s returned terminal state %s: %s", ticker, state, data)
            raise RuntimeError(f"live placement for {ticker} came back in terminal state {state}; response={data}")
        # Accumulate raw broker response for orders_after_place.json so fill data
        # is replayable from durable storage without a live broker re-query.
        self._raw_placement_responses.append({
            "ticker": ticker,
            "order_id": str(order_id),
            **order,
        })
        return str(order_id)

    def review_whole_share_buy(self, ticker: str, quantity: int) -> Tuple[bool, str]:
        """Quantity-based counterpart to :meth:`review_market_buy`.

        For :mod:`dca_whole_share_accumulator`'s fire decisions: ``ticker``
        is not tradable for fractional/notional buys, so a dollar-amount
        review would be reviewing the wrong kind of order entirely. See the
        class docstring's note on the ``quantity=`` assumption.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive for a whole-share buy review")
        if self._review_fn is None:
            if self.enable_live_orders:
                return False, "live orders enabled but no review callable wired; refusing to place unreviewed order"
            return True, "no review callable wired; skipping broker-side review (dry-run)"
        response = self._review_fn(
            account_number=self._account,
            symbol=ticker,
            side="buy",
            type="market",
            quantity=str(quantity),
        )
        return _interpret_review(response)

    def place_whole_share_buy(self, ticker: str, quantity: int, ref_id: str) -> str:
        """Quantity-based counterpart to :meth:`place_market_buy`.

        Places a whole-share (non-fractional) market buy for exactly
        ``quantity`` shares, for a ticker not tradable for fractional/notional
        buys -- see :mod:`dca_whole_share_accumulator`. Every fail-closed
        check :meth:`place_market_buy` applies (live gate + place_fn wired,
        failure envelope, missing order id, terminal rejection state) applies
        identically here; only the order-sizing argument differs.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive for a whole-share buy")
        if not (self.enable_live_orders and self._place_fn is not None):
            raise RuntimeError(
                f"place_whole_share_buy called for {ticker} with the live gate disabled "
                f"(enable_live_orders={self.enable_live_orders}, "
                f"place_fn={'set' if self._place_fn else 'None'}); use dry_run=True for simulated runs"
            )
        try:
            response = self._place_fn(
                account_number=self._account,
                symbol=ticker,
                side="buy",
                type="market",
                quantity=str(quantity),
                time_in_force=self._time_in_force,
                ref_id=ref_id,
            )
        except Exception as exc:  # noqa: BLE001 - see place_market_buy's
            # identical wrapping just above: the connector call itself
            # (network timeout, connection reset, MCP transport error, ...)
            # gave no broker response at all, unlike every raise below this
            # point, which reacts to a definitive response the broker DID
            # return. Whether this whole-share order actually reached and
            # was accepted by the broker is genuinely unknown here (Codex
            # review, PR #1300 discussion_r3920546926: "Preserve ambiguity
            # for whole-share placement timeouts").
            raise OrderPlacementAmbiguousError(
                f"whole-share placement connector call for {ticker} failed with no broker response: {exc}"
            ) from exc
        outer_failure = _response_failure(response)
        if outer_failure is not None:
            logger.error("place_equity_order (whole-share) for %s reported failure: %s", ticker, response)
            # An outer failure envelope (e.g. {"success": false, "data":
            # {"order": {...}}}) can still carry an inner order that reached
            # the broker and reports its own terminal state and
            # cumulative_quantity -- for example, a partially filled order
            # that was then cancelled, with the cancellation itself surfaced
            # as the outer "success: false". Discarding that inner payload
            # and raising a bare RuntimeError forces the caller to rely
            # exclusively on a SEPARATE, later order-history lookup that can
            # itself be eventually consistent and report a clean "no match"
            # for an order this very response already proved landed --
            # restoring the full reservation and making already-spent
            # credit reusable (Codex review: "Preserve inner orders from
            # failure envelopes"). Mirrors the terminal-state branch below:
            # an inner order in a genuinely terminal state is authoritative
            # evidence regardless of the outer envelope's own verdict.
            outer_failure_data = _unwrap(response)
            # Mirrors the normal success path's own `data.get("order") or
            # data` fallback just below: _unwrap(response) can itself BE the
            # bare order object (no nested "order" key at all), a shape the
            # success path already treats as equally valid. Checking only
            # `.get("order")` missed that shape entirely, so an outer
            # failure like {"success": false, "data": {"id": ..., "state":
            # "cancelled", "cumulative_quantity": ...}} still fell through to
            # a bare RuntimeError, discarding the very evidence this branch
            # exists to preserve (Codex review: "Preserve bare orders in
            # outer failure envelopes").
            inner_order = None
            if isinstance(outer_failure_data, dict):
                nested = outer_failure_data.get("order")
                inner_order = nested if isinstance(nested, dict) else outer_failure_data
            if isinstance(inner_order, dict):
                inner_order_id = inner_order.get("id") or inner_order.get("order_id")
                inner_state = str(inner_order.get("state", "")).lower()
                # Not gated on inner_state being in a TERMINAL set -- an
                # inner order still LIVE/working (e.g. "new"/"confirmed"/
                # "partially_filled") is just as real a broker acceptance as
                # a terminal one, and discarding it here (an earlier version
                # of this fix did) forced the caller to rely exclusively on
                # its own separate live/terminal-inclusive lookups, which can
                # BOTH independently miss the very order this response
                # already proved was accepted (an eventually-consistent
                # order-history endpoint) -- restoring the full reservation
                # and making already-committed credit reusable while the
                # order remains live at the broker (Codex review: "Preserve
                # accepted nonterminal orders in failure envelopes"). The
                # caller's own exc.order handling already discriminates
                # terminal vs. still-live states correctly (see its "Verify
                # terminal state after the any-state fallback" branch), so
                # passing ANY inner order with a usable id through this same
                # exception -- regardless of state -- is safe: it is treated
                # exactly like the any-state fallback's own result.
                if inner_order_id:
                    raise WholeSharePlacementTerminalError(
                        f"live whole-share placement for {ticker} reported an outer failure "
                        f"({outer_failure}), but its inner order carries evidence of a real "
                        f"broker attempt (state={inner_state or 'unknown'}); response={outer_failure_data}",
                        order=inner_order,
                    )
            raise RuntimeError(f"live whole-share placement for {ticker} failed: {outer_failure}")
        data = _unwrap(response)
        order = data.get("order") or data
        order_id = order.get("id") or order.get("order_id")
        if not order_id:
            logger.error("place_equity_order (whole-share) returned no order id for %s: %s", ticker, data)
            raise RuntimeError(f"live whole-share placement for {ticker} returned no order id; response={data}")
        state = str(order.get("state", "")).lower()
        # "expired" is a terminal, non-live state exactly like the others
        # here (see _LIVE_ORDER_STATES's own comment above, which already
        # excludes it) -- without rejecting it too, the caller would treat
        # this response as a successful full submission and settle the
        # claim for the ENTIRE requested quantity even if no shares
        # actually executed (Codex review: "Reject expired whole-share
        # placement responses"). Raising here (rather than returning the
        # order id) routes this through the SAME ambiguous-placement
        # reconciliation the caller already applies to any other raised
        # exception, which inspects the real cumulative fill via the
        # terminal-inclusive lookup before restoring or settling credit.
        if state in {"rejected", "failed", "cancelled", "canceled", "voided", "expired"}:
            logger.error(
                "place_equity_order (whole-share) for %s returned terminal state %s: %s", ticker, state, data
            )
            # Carries the order payload rather than a bare RuntimeError --
            # this response is ALREADY authoritative terminal evidence
            # (state + cumulative_quantity + average_price), and discarding
            # it before raising would force the caller to depend
            # exclusively on a SEPARATE, later order-history lookup that can
            # itself be eventually consistent and report a clean "no match"
            # for an order this very response already proved landed (Codex
            # review: "Reconcile terminal fills from the placement
            # response").
            raise WholeSharePlacementTerminalError(
                f"live whole-share placement for {ticker} came back in terminal state {state}; response={data}",
                order=order,
            )
        self._raw_placement_responses.append({
            "ticker": ticker,
            "order_id": str(order_id),
            **order,
        })
        return str(order_id)

    def get_raw_placement_responses(self) -> List[Dict[str, Any]]:
        """Return and clear all raw broker placement responses for the current run.

        Drains the internal buffer so a reused broker instance does not carry
        responses from a prior pipeline call into a subsequent one.
        """
        responses = list(self._raw_placement_responses)
        self._raw_placement_responses.clear()
        return responses

    def get_order_fill(self, order_id: str) -> Optional[OrderFill]:
        if not order_id or self._order_fill_fn is None:
            return None
        try:
            data = _unwrap(self._order_fill_fn(account_number=self._account, order_id=order_id))
        except Exception:  # noqa: BLE001 - a fill-lookup outage must not abort the run
            # The order is already placed by the time we look up its fill; a transient
            # get_equity_orders failure should leave it recorded as submitted/unfilled,
            # not crash run_once before the durable artifacts are written.
            logger.exception("order-fill lookup failed for %s; recording as unfilled", order_id)
            return None
        # Accept list mode ({"orders": [...]}), a single-order envelope ({"order": {...}},
        # the same nesting place_market_buy accepts), or a bare top-level order object.
        single = data.get("order") if isinstance(data.get("order"), dict) else None
        if single is None and (data.get("id") or data.get("order_id")):
            single = data
        orders = data.get("orders") or ([single] if single else [])
        for order in orders:
            # place_market_buy accepts either "id" or "order_id"; match on both here so a
            # history wrapper that keys fills by "order_id" is not misread as an unfilled gap.
            oid = order.get("id") or order.get("order_id")
            if str(oid) != order_id:
                continue
            # Plain "quantity" is the REQUESTED amount, not what executed --
            # scoped to state == "filled" for the same reason
            # near_close_dca._recovered_broker_notional's own quantity_field
            # selection is: a cancelled/rejected/expired order that omits
            # cumulative_quantity but carries its requested quantity and an
            # average_price would otherwise combine into a fabricated fill
            # for an order that moved nothing, marking a correctly-$0
            # recovery as "filled" in build_execution_record() (Codex
            # review: "Do not turn confirmed zero-fill orders into fills").
            order_state = str(order.get("state", "")).strip().lower()
            if not _is_blank_broker_field(order.get("cumulative_quantity")):
                quantity_source = order.get("cumulative_quantity")
            elif order_state == "filled":
                quantity_source = order.get("quantity")
            else:
                quantity_source = None
            filled_qty = _to_float(quantity_source) if quantity_source is not None else 0.0
            avg_price = _to_float(order.get("average_price") or order.get("price"))
            if filled_qty <= 0 or avg_price <= 0:
                return None
            return OrderFill(
                avg_fill_price=avg_price,
                quantity_filled=filled_qty,
                order_submitted_at=_parse_rfc3339(order.get("created_at")),
                order_filled_at=_parse_rfc3339(order.get("last_transaction_at") or order.get("updated_at")),
            )
        return None


# -- module helpers --------------------------------------------------------------


def _to_utc_iso(moment: datetime) -> str:
    """Render an aware datetime as a UTC RFC3339 string (naive assumed Eastern)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=EASTERN)
    return moment.astimezone(tz=_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _coerce_bar(raw: Any) -> Optional[Bar]:
    """Coerce a vendor bar mapping (or a ready ``Bar``) into a ``Bar``."""
    if isinstance(raw, Bar):
        return raw
    if not isinstance(raw, dict):
        return None
    ts_value = raw.get("timestamp") or raw.get("begins_at") or raw.get("t") or raw.get("date")
    if isinstance(ts_value, datetime):
        timestamp: Optional[datetime] = ts_value if ts_value.tzinfo else ts_value.replace(tzinfo=EASTERN)
    elif isinstance(ts_value, date):
        timestamp = datetime.combine(ts_value, time(16, 0), tzinfo=EASTERN)
    else:
        timestamp = _parse_rfc3339(ts_value)
    if timestamp is None:
        return None
    close = _to_float(raw.get("close", raw.get("close_price", raw.get("c"))), default=-1.0)
    if close <= 0:
        # Reject placeholder/non-numeric closes (e.g. "N/A") like the Robinhood parser: a
        # 0.0 close would read as a ~100% drop and trigger a spurious max BUY.
        return None
    return Bar(
        timestamp=timestamp,
        open=_to_float(raw.get("open", raw.get("open_price", raw.get("o")))),
        high=_to_float(raw.get("high", raw.get("high_price", raw.get("h")))),
        low=_to_float(raw.get("low", raw.get("low_price", raw.get("l")))),
        close=close,
        volume=_to_float(raw.get("volume", raw.get("v"))),
    )


def _interpret_review(response: Any) -> Tuple[bool, str]:
    """Interpret a ``review_equity_order`` response into ``(approved, reason)``.

    Robinhood's review returns a live quote plus pre-trade *alerts*. Approval requires no
    blocking signal. Blocking comes from **structured** evidence only, so that neither a
    harmless warning nor a happy-path quote flips the result the wrong way:

    * explicit failure fields — ``error`` truthy, or ``success``/``ok``/``approved`` is
      ``False`` — fail closed (a wrapper that reports failure as data, not an exception);
    * ``state`` in ``{rejected, failed}`` or ``tradeable is False``;
    * a dict alert (in any container) flagged error/blocking/reject/critical;
    * a non-empty string in an explicitly error-named container (``errors`` …).

    Plain-string entries under ``warnings``/``messages``/``alerts`` are treated as
    informational and never block on their own (they carry no severity). An empty
    response approves — it is genuinely empty, not a reported failure.
    """
    data = _unwrap(response)
    if not data:
        return True, "empty review response; no blocking alerts reported"

    # Shape sanity-check: a well-formed review payload must contain at least one recognised
    # review field.  A plain quote response (e.g. {"quote": {...}}) with no review/alert keys
    # does not constitute approval — fail closed so unexpected broker shapes don't silently
    # green-light a live order.
    _REVIEW_FIELDS = frozenset(
        {
            "approved",
            "state",
            "tradeable",
            "alerts",
            "warnings",
            "pre_trade_alerts",
            "messages",
            "checks",
            "errors",
            "error_messages",
            "rejections",
            "success",
            "ok",
            "error",
        }
    )
    if not (data.keys() & _REVIEW_FIELDS):
        return False, f"unexpected review response shape; no recognised review fields (keys: {sorted(data.keys())})"

    blocking: List[str] = []

    # Explicit failure signals, at the outer envelope or inside data: fail closed when the
    # wrapper reports failure as data (e.g. {"success": false, "data": {...}}).
    outer_failure = _response_failure(response)
    if outer_failure is not None:
        blocking.append(outer_failure)
    if data.get("approved") is False:
        blocking.append("approved=false")
    if str(data.get("state", "")).lower() in {"rejected", "failed"}:
        blocking.append(f"review state={data.get('state')}")
    if data.get("tradeable") is False:
        blocking.append("instrument not tradeable")

    # Structured alerts: block only on error/blocking severity, regardless of container.
    for key in ("alerts", "warnings", "pre_trade_alerts", "messages", "checks"):
        for alert in _as_list(data.get(key)):
            if not isinstance(alert, dict):
                continue  # plain strings here are informational, not blockers
            severity = str(alert.get("severity") or alert.get("level") or alert.get("type") or "").lower()
            is_blocking = (
                alert.get("blocking") is True
                or bool(alert.get("is_error"))
                or severity in {"error", "blocking", "reject", "rejected", "critical"}
            )
            if is_blocking:
                blocking.append(str(alert.get("message") or alert.get("reason") or alert.get("code") or severity))

    # Non-empty strings in explicitly error-named containers do block.
    for key in ("errors", "error_messages", "rejections"):
        for item in _as_list(data.get(key)):
            if item:
                blocking.append(str(item))

    if blocking:
        return False, "; ".join(dict.fromkeys(blocking))
    return True, "review passed with no blocking alerts"


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
