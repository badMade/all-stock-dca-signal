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
from typing import Any, Callable, Dict, List, Optional, Tuple

from near_close_dca import (
    EASTERN,
    ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
    Bar,
    OrderFill,
    SourceName,
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

    def _same_day_orders(self, ticker: str, trading_day: date) -> List[Dict[str, Any]]:
        if self._orders_fn is None:
            return []
        response = self._orders_fn(
            account_number=self._account,
            symbol=ticker,
            created_at_gte=trading_day.isoformat(),
        )
        error = _response_failure(response)
        if error is not None:
            # Fail closed: an error-shaped lookup ("no orders" by omission) must not let
            # the one-order-per-ticker-per-day guard pass and allow a duplicate buy.
            raise RuntimeError(f"same-day order lookup for {ticker} failed: {error}")
        data = _unwrap(response)
        return list(data.get("orders") or [])

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        for order in self._same_day_orders(ticker, trading_day):
            if str(order.get("state", "")).lower() in _LIVE_ORDER_STATES:
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
        for order in self._same_day_orders(ticker, trading_day):
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
        response = self._place_fn(
            account_number=self._account,
            symbol=ticker,
            side="buy",
            type="market",
            dollar_amount=f"{notional_usd:.2f}",
            time_in_force=self._time_in_force,
            ref_id=ref_id,
        )
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
            if str(oid) != str(order_id):
                continue
            filled_qty = _to_float(order.get("cumulative_quantity") or order.get("quantity"))
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
