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
    """Return the inner dict for an MCP response that may be wrapped in ``{"data": ...}``."""
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float parse for Robinhood's stringified numbers; ``None`` -> default."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        quote = self._quote_for(ticker)
        if quote is None:
            return None
        if not quote.get("has_traded", True) or quote.get("state", "active") != "active":
            logger.warning("skipping quote for %s: has_traded=%s state=%s", ticker, quote.get("has_traded"), quote.get("state"))
            return None
        price = _to_float(quote.get("last_trade_price"))
        if price <= 0:
            return None
        return price, _parse_rfc3339(quote.get("venue_last_trade_time"))

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
                bars.append(
                    Bar(
                        timestamp=timestamp,
                        open=_to_float(raw.get("open_price")),
                        high=_to_float(raw.get("high_price")),
                        low=_to_float(raw.get("low_price")),
                        close=_to_float(raw.get("close_price")),
                        volume=_to_float(raw.get("volume")),
                    )
                )
        return bars

    def _quote_for(self, ticker: str) -> Optional[Dict[str, Any]]:
        data = _unwrap(self._quotes_fn(symbols=[ticker]))  # type: ignore[misc]
        for result in data.get("results") or []:
            quote = result.get("quote") or {}
            if quote.get("symbol") in (ticker, None):
                # Attach the official settled close so callers can reuse it.
                quote = dict(quote)
                quote["_close"] = result.get("close") or {}
                return quote
        return None

    def _previous_close_from_quote(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        quote = self._quote_for(ticker)
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
        return Bar(timestamp=timestamp, open=price, high=price, low=price, close=price, volume=0.0)


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
        if isinstance(result, dict):
            bar = _coerce_bar(result)
            if bar is not None:
                return bar
            # A price-only mapping (e.g. {"price": "100"} / {"close": "100"}) has no bar
            # timestamp; fall through to the scalar path rather than dropping it, so the
            # finance source still confirms and the reconciliation stays two-sided.
            price = _to_float(result.get("price") or result.get("close") or result.get("previous_close"))
        else:
            price = _to_float(result)
        if price <= 0:
            return None
        timestamp = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        return Bar(timestamp=timestamp, open=price, high=price, low=price, close=price, volume=0.0)


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
    """

    def __init__(
        self,
        account: str = ROBINHOOD_AGENTIC_ACCOUNT_NUMBER,
        *,
        buying_power_fn: Optional[Callable[[str], float]] = None,
        orders_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        tradability_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        review_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        place_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        order_fill_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        enable_live_orders: bool = False,
        time_in_force: str = "gfd",
        simulated_buying_power_usd: float = 300.0,
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

    def account_number(self) -> str:
        return self._account

    def available_buying_power(self) -> float:
        if self._buying_power_fn is None:
            return self._simulated_buying_power_usd
        try:
            return float(self._buying_power_fn(account_number=self._account))
        except Exception:  # noqa: BLE001 - never let buying-power lookup crash a scan
            logger.exception("buying-power lookup failed; treating as 0")
            return 0.0

    def same_day_strategy_order_exists(self, ticker: str, trading_day: date) -> bool:
        if self._orders_fn is None:
            return False
        response = self._orders_fn(
            account_number=self._account,
            symbol=ticker,
            created_at_gte=trading_day.isoformat(),
        )
        data = _unwrap(response)
        for order in data.get("orders") or []:
            if str(order.get("state", "")).lower() in _LIVE_ORDER_STATES:
                return True
        return False

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
            return f"RH-DRYRUN-{ticker}-{notional_usd:.2f}-{ref_id}"
        response = self._place_fn(
            account_number=self._account,
            symbol=ticker,
            side="buy",
            type="market",
            dollar_amount=f"{notional_usd:.2f}",
            time_in_force=self._time_in_force,
            ref_id=ref_id,
        )
        data = _unwrap(response)
        order = data.get("order") or data
        order_id = order.get("id") or order.get("order_id")
        if not order_id:
            # Fail fast: run_once treats any returned id as a submitted order and records
            # the notional against daily/weekly budgets. Returning "" would book a phantom
            # buy with no order to reconcile, so raise instead of reporting false success.
            logger.error("place_equity_order returned no order id for %s: %s", ticker, data)
            raise RuntimeError(f"live placement for {ticker} returned no order id; response={data}")
        return str(order_id)

    def get_order_fill(self, order_id: str) -> Optional[OrderFill]:
        if not order_id or self._order_fill_fn is None:
            return None
        data = _unwrap(self._order_fill_fn(account_number=self._account, order_id=order_id))
        orders = data.get("orders") or ([data] if (data.get("id") or data.get("order_id")) else [])
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
    close = raw.get("close", raw.get("close_price", raw.get("c")))
    if close is None:
        return None
    return Bar(
        timestamp=timestamp,
        open=_to_float(raw.get("open", raw.get("open_price", raw.get("o")))),
        high=_to_float(raw.get("high", raw.get("high_price", raw.get("h")))),
        low=_to_float(raw.get("low", raw.get("low_price", raw.get("l")))),
        close=_to_float(close),
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
    blocking: List[str] = []

    # Explicit failure signals: fail closed when the wrapper reports failure as data.
    if data.get("error"):
        blocking.append(f"error: {data.get('error')}")
    for flag in ("success", "ok", "approved"):
        if data.get(flag) is False:
            blocking.append(f"{flag}=false")
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
