"""Alpaca Basic: intraday last trades, end-of-day failover bars, corporate actions, calendar.

* Last trades come from ``feed=iex`` (real time on Basic) or ``feed=delayed_sip`` (15-minute
  delayed consolidated tape). Real-time SIP is not in Basic, so any other feed is refused.
  Freshness is judged by ``intraday.check_fresh``, not here: this module reports what the
  feed said, with ``ts`` = trade time and ``as_of`` = fetch time.
* Daily bars are ``adjustment=raw`` from the SIP feed (allowed on Basic once the bar is more
  than 15 minutes old, which an end-of-day bar always is). They need a universe to ask for,
  normally the symbols of the last good Massive session; with none the call fails closed.
* The calendar lists open days only. A day absent from a successful response is closed;
  a failed response raises.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .errors import IncompleteBatchError, ProviderError
from .http import HttpClient
from .models import CorporateAction, DailyBar, Quote

ET = ZoneInfo("America/New_York")
SOURCE = "alpaca"
DATA_BASE_URL = "https://data.alpaca.markets"
TRADING_BASE_URL = "https://paper-api.alpaca.markets"
MIN_INTERVAL_S = 0.3  # Basic allows 200 requests/minute
FEEDS = frozenset({"iex", "delayed_sip"})
SYMBOLS_PER_REQUEST = 200
# The failover must answer for most of the universe, or the night is incomplete, not "ok".
MIN_UNIVERSE_COVERAGE = 0.90
MAX_PAGES = 500
_ACTION_GROUPS = {
    "forward_splits": "forward_split",
    "reverse_splits": "reverse_split",
    "stock_dividends": "stock_dividend",
    "cash_dividends": "cash_dividend",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _credentials(env: Mapping[str, str] | None) -> tuple[str, str]:
    env = os.environ if env is None else env
    key_id, secret = env.get("ALPACA_KEY_ID", ""), env.get("ALPACA_SECRET_KEY", "")
    missing = [name for name, value in (("ALPACA_KEY_ID", key_id), ("ALPACA_SECRET_KEY", secret)) if not value]
    if missing:
        raise ProviderError(f"{' and '.join(missing)} not set", code="missing_credentials")
    return key_id, secret


def _default_session(kwargs: dict[str, Any]) -> None:
    if kwargs.get("session") is None:
        import requests

        kwargs["session"] = requests.Session()


def _http(key_id: str, secret: str, session: Any, clock, sleep) -> HttpClient:
    if not key_id or not secret:
        raise ProviderError("ALPACA_KEY_ID and ALPACA_SECRET_KEY are required", code="missing_credentials")
    return HttpClient(
        session,
        vendor="alpaca",
        headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
        clock=clock,
        sleep=sleep,
        min_interval_s=MIN_INTERVAL_S,
    )


def parse_rfc3339(value: Any) -> datetime:
    """Alpaca timestamps: RFC 3339 in UTC, often with nanoseconds (truncated to microseconds)."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProviderError("alpaca timestamp is not RFC 3339 UTC", code="malformed_row")
    body = value[:-1]
    if "." in body:
        head, frac = body.split(".", 1)
        body = f"{head}.{(frac + '000000')[:6]}"
    try:
        return datetime.fromisoformat(body).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProviderError("alpaca timestamp is not RFC 3339 UTC", code="malformed_row") from exc


def _number(row: Mapping[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(f"alpaca field {key!r} is missing or not a number", code="malformed_row")
    return float(value)


def _iso_date(value: Any, field_name: str, *, required: bool = True) -> date | None:
    if value in (None, "") and not required:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProviderError(f"alpaca field {field_name!r} is not an ISO date", code="malformed_row") from exc


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


class AlpacaProvider:
    """``PriceProvider`` over Alpaca Basic market data."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        session: Any,
        universe: Callable[[], Sequence[str]],
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        data_base_url: str = DATA_BASE_URL,
    ) -> None:
        self._http = _http(key_id, secret_key, session, clock, sleep)
        self._universe = universe
        self._clock = clock
        self._base = data_base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"AlpacaProvider(data_base_url={self._base!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> AlpacaProvider:
        key_id, secret = _credentials(env)
        _default_session(kwargs)
        return cls(key_id, secret, **kwargs)

    def last_trade(self, symbols: Sequence[str], *, feed: str = "iex") -> dict[str, Quote]:
        if feed not in FEEDS:
            raise ValueError(f"feed must be one of {sorted(FEEDS)}, got {feed!r}")
        wanted = list(dict.fromkeys(symbols))
        quotes: dict[str, Quote] = {}
        for chunk in _chunks(wanted, SYMBOLS_PER_REQUEST):
            body = self._http.get_json(
                f"{self._base}/v2/stocks/trades/latest", params={"symbols": ",".join(chunk), "feed": feed}
            )
            fetched_at = self._clock()
            trades = body.get("trades")
            if not isinstance(trades, dict):
                raise ProviderError("alpaca latest-trades response has no 'trades' object", code="malformed")
            for symbol, trade in trades.items():
                if symbol not in chunk:
                    continue
                if not isinstance(trade, dict):
                    raise ProviderError(f"alpaca trade for {symbol} is not an object", code="malformed_row")
                quotes[symbol] = Quote(
                    symbol=symbol,
                    price=_number(trade, "p"),
                    ts=parse_rfc3339(trade.get("t")),
                    feed=feed,
                    as_of=fetched_at,
                )
        return quotes

    def daily_bars(self, session: date) -> list[DailyBar]:
        symbols = list(dict.fromkeys(self._universe()))
        if not symbols:
            raise ProviderError("alpaca daily-bar failover has no universe to request", code="empty_universe")
        bars: list[DailyBar] = []
        for chunk in _chunks(symbols, SYMBOLS_PER_REQUEST):
            params: dict[str, Any] = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": session.isoformat(),
                "end": session.isoformat(),
                "adjustment": "raw",
                "feed": "sip",
                "limit": 10000,
            }
            for page in self._pages(f"{self._base}/v2/stocks/bars", params):
                by_symbol = page.get("bars") or {}
                if not isinstance(by_symbol, dict):
                    raise ProviderError("alpaca 'bars' is not an object", code="malformed")
                for symbol, rows in by_symbol.items():
                    if not isinstance(rows, list):
                        raise ProviderError(f"alpaca bars for {symbol} are not a list", code="malformed_row")
                    bars.extend(self._parse_bar(symbol, row, session) for row in rows)
        covered = len({b.symbol for b in bars})
        if covered < MIN_UNIVERSE_COVERAGE * len(symbols):
            raise IncompleteBatchError(
                f"alpaca returned bars for {covered} of {len(symbols)} universe symbols", code="below_universe"
            )
        return bars

    def _pages(self, url: str, params: dict[str, Any]) -> Iterator[Mapping[str, Any]]:
        token: str | None = None
        for _ in range(MAX_PAGES):
            page_params = dict(params, page_token=token) if token else params
            body = self._http.get_json(url, params=page_params)
            yield body
            token = body.get("next_page_token")
            if not token:
                return
            if not isinstance(token, str):
                raise ProviderError("alpaca next_page_token is not a string", code="malformed")
        raise ProviderError(f"alpaca pagination exceeded {MAX_PAGES} pages", code="pagination_limit")

    @staticmethod
    def _parse_bar(symbol: str, row: Any, session: date) -> DailyBar:
        if not isinstance(row, dict):
            raise ProviderError(f"alpaca bar for {symbol} is not an object", code="malformed_row")
        if parse_rfc3339(row.get("t")).astimezone(ET).date() != session:
            raise ProviderError(f"alpaca bar for {symbol} is stamped for another session", code="session_mismatch")
        trades = row.get("n")
        vwap = row.get("vw")
        return DailyBar(
            source=SOURCE,
            symbol=symbol,
            session=session,
            open=_number(row, "o"),
            high=_number(row, "h"),
            low=_number(row, "l"),
            close=_number(row, "c"),
            volume=_number(row, "v"),
            vwap=float(vwap) if isinstance(vwap, (int, float)) and not isinstance(vwap, bool) else None,
            trades=int(trades) if isinstance(trades, int) and not isinstance(trades, bool) else None,
        )

    def corporate_actions(self, since: date) -> list[CorporateAction]:
        # Look ahead as well as back: an announced split with a future ex-date is already
        # public, and the adjusted views only apply it once its ex-date has passed.
        end = self._clock().astimezone(ET).date() + timedelta(days=90)
        params = {
            "types": ",".join(_ACTION_GROUPS.values()),
            "start": since.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
        }
        actions: list[CorporateAction] = []
        for page in self._pages(f"{self._base}/v1/corporate-actions", params):
            groups = page.get("corporate_actions") or {}
            if not isinstance(groups, dict):
                raise ProviderError("alpaca 'corporate_actions' is not an object", code="malformed")
            for group, rows in groups.items():
                action_type = _ACTION_GROUPS.get(group)
                if action_type is None:
                    continue  # mergers, spin-offs, name changes: not price adjustments this store applies
                if not isinstance(rows, list):
                    raise ProviderError(f"alpaca {group} is not a list", code="malformed")
                actions.extend(self._parse_action(action_type, row) for row in rows)
        return actions

    @staticmethod
    def _parse_action(action_type: str, row: Any) -> CorporateAction:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            raise ProviderError("alpaca corporate action has no symbol", code="malformed_row")
        common: dict[str, Any] = {
            "source": SOURCE,
            "action_type": action_type,
            "symbol": row["symbol"],
            "ex_date": _iso_date(row.get("ex_date"), "ex_date"),
            "record_date": _iso_date(row.get("record_date"), "record_date", required=False),
            "pay_date": _iso_date(row.get("payable_date"), "payable_date", required=False),
            "vendor_id": str(row["id"]) if row.get("id") is not None else None,
        }
        if action_type in ("forward_split", "reverse_split"):
            return CorporateAction(**common, split_from=_number(row, "old_rate"), split_to=_number(row, "new_rate"))
        if action_type == "stock_dividend":
            # ``rate`` is new shares per share held: 0.05 is a 5% stock dividend, 1 -> 1.05.
            return CorporateAction(**common, split_from=1.0, split_to=1.0 + _number(row, "rate"))
        return CorporateAction(
            **common, cash_amount=_number(row, "rate"), currency=None if row.get("foreign") else "USD"
        )


class AlpacaCalendar:
    """``SessionCalendar`` over Alpaca's ``/v2/calendar`` (open days only)."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        session: Any,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = TRADING_BASE_URL,
    ) -> None:
        self._http = _http(key_id, secret_key, session, clock, sleep)
        self._base = base_url.rstrip("/")
        self._known: dict[date, bool] = {}
        self._covered: list[tuple[date, date]] = []

    def __repr__(self) -> str:
        return f"AlpacaCalendar(base_url={self._base!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> AlpacaCalendar:
        key_id, secret = _credentials(env)
        _default_session(kwargs)
        return cls(key_id, secret, **kwargs)

    def _load(self, start: date, end: date) -> None:
        if any(lo <= start and end <= hi for lo, hi in self._covered):
            return
        days = self._http.get_json(
            f"{self._base}/v2/calendar", params={"start": start.isoformat(), "end": end.isoformat()}, expect=list
        )
        open_days = set()
        for item in days:
            if not isinstance(item, dict):
                raise ProviderError("alpaca calendar entry is not an object", code="malformed_row")
            open_days.add(_iso_date(item.get("date"), "date"))
        d = start
        while d <= end:
            self._known[d] = d in open_days
            d += timedelta(days=1)
        self._covered.append((start, end))

    def is_session(self, d: date) -> bool:
        self._load(d, d)
        return self._known[d]

    def previous_sessions(self, d: date, n: int) -> list[date]:
        if n <= 0:
            return []
        span = timedelta(days=n * 2 + 10)
        for _ in range(4):
            start = d - span
            self._load(start, d - timedelta(days=1))
            found = sorted((day for day, is_open in self._known.items() if is_open and start <= day < d), reverse=True)
            if len(found) >= n:
                return found[:n]
            span *= 2
        raise ProviderError(f"alpaca calendar returned fewer than {n} sessions before {d}", code="calendar_short")
