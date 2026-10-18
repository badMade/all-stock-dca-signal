"""MassiveProvider: Massive (ex-Polygon) Stocks Basic, the primary nightly source.

One grouped-daily call returns the whole US market's RAW bars for a session
(``adjusted=false``, OTC excluded). Completeness rules, in the order they are checked:

1. ``status`` must be OK/DELAYED; ``NOT_AUTHORIZED`` (a date beyond the plan's 2-year window)
   is a ``ProviderError`` even on HTTP 200, never an empty day.
2. ``resultsCount`` must equal ``len(results)``.
3. Zero rows are accepted only when the calendar says the market was closed.
4. Row count must not be under 90% of the trailing-20-session median (from the store). With
   fewer than 20 stored sessions the provider refuses unless an explicit ``bootstrap_floor``
   was given, which then acts as an absolute minimum.

Endpoint paths and field names follow Massive's published docs as of 2026-10; the synthetic
fixtures under tests/prices/fixtures/ carry that provenance, and the opt-in PoC re-records
live payloads to confirm them.
"""

from __future__ import annotations

import logging
import os
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .errors import IncompleteBatchError, ProviderError
from .http import HttpClient
from .models import CorporateAction, DailyBar, Quote
from .provider import SessionCalendar

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SOURCE = "massive"
DEFAULT_BASE_URL = "https://api.massive.com"
FREE_TIER_MIN_INTERVAL_S = 12.0  # 5 calls/minute
BASELINE_SESSIONS = 20
MIN_FRACTION_OF_MEDIAN = 0.90
MAX_PAGES = 200
_OK_STATUSES = frozenset({"OK", "DELAYED"})
_SPLIT_TYPES = frozenset({"forward_split", "reverse_split", "stock_dividend"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _number(row: Mapping[str, Any], key: str, *, required: bool = True) -> float | None:
    value = row.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(f"massive row field {key!r} is missing or not a number", code="malformed_row")
    return float(value)


def _iso_date(value: Any, field_name: str, *, required: bool = True) -> date | None:
    if value in (None, "") and not required:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ProviderError(f"massive field {field_name!r} is not an ISO date", code="malformed_row") from exc


class MassiveProvider:
    """``PriceProvider`` over Massive Stocks Basic. Construct with ``from_env`` in production."""

    def __init__(
        self,
        api_key: str,
        *,
        session: Any,
        calendar: SessionCalendar,
        trailing_counts: Callable[[date], Sequence[int]],
        bootstrap_floor: int | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = DEFAULT_BASE_URL,
        min_interval_s: float = FREE_TIER_MIN_INTERVAL_S,
    ) -> None:
        if not api_key:
            raise ProviderError("MASSIVE_API_KEY is not set", code="missing_credentials")
        self._http = HttpClient(
            session,
            vendor="massive",
            headers={"Authorization": f"Bearer {api_key}"},
            clock=clock,
            sleep=sleep,
            min_interval_s=min_interval_s,
        )
        self._calendar = calendar
        self._trailing_counts = trailing_counts
        self._bootstrap_floor = bootstrap_floor
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"MassiveProvider(base_url={self._base_url!r})"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: Any) -> MassiveProvider:
        env = os.environ if env is None else env
        key = env.get("MASSIVE_API_KEY", "")
        if not key:
            raise ProviderError("MASSIVE_API_KEY is not set", code="missing_credentials")
        if kwargs.get("session") is None:
            import requests

            kwargs["session"] = requests.Session()
        return cls(key, **kwargs)

    # --- daily bars ------------------------------------------------------------------------

    def daily_bars(self, session: date) -> list[DailyBar]:
        body = self._http.get_json(
            f"{self._base_url}/v2/aggs/grouped/locale/us/market/stocks/{session.isoformat()}",
            params={"adjusted": "false", "include_otc": "false"},
        )
        self._check_status(body)
        results = body.get("results", [])
        if not isinstance(results, list):
            raise ProviderError("massive grouped 'results' is not a list", code="malformed")
        count = body.get("resultsCount")
        if isinstance(count, bool) or not isinstance(count, int):
            raise IncompleteBatchError("massive grouped response has no integer resultsCount", code="count_mismatch")
        if count != len(results):
            raise IncompleteBatchError(
                f"massive grouped resultsCount={count} but {len(results)} rows arrived", code="count_mismatch"
            )
        if not results:
            if self._calendar.is_session(session):
                raise IncompleteBatchError(f"massive returned no bars for open session {session}",
                                           code="empty_open_session")
            return []
        self._check_row_count(session, len(results))
        return [self._parse_bar(row, session) for row in results]

    def _check_status(self, body: Mapping[str, Any]) -> None:
        status = body.get("status")
        if status == "NOT_AUTHORIZED":
            raise ProviderError("massive: the plan does not cover this request (NOT_AUTHORIZED)",
                                code="not_authorized")
        if status not in _OK_STATUSES:
            raise ProviderError(f"massive returned status {str(status)[:32]!r}", code="vendor_status")

    def _check_row_count(self, session: date, n_rows: int) -> None:
        counts = list(self._trailing_counts(session))[:BASELINE_SESSIONS]
        if len(counts) < BASELINE_SESSIONS:
            if self._bootstrap_floor is None:
                raise IncompleteBatchError(
                    f"only {len(counts)} stored sessions for the {BASELINE_SESSIONS}-session baseline; "
                    "rerun with an explicit bootstrap floor",
                    code="no_baseline",
                )
            if n_rows < self._bootstrap_floor:
                raise IncompleteBatchError(f"{n_rows} rows is under the bootstrap floor {self._bootstrap_floor}",
                                           code="below_floor")
            return
        median = statistics.median(counts)
        if n_rows < MIN_FRACTION_OF_MEDIAN * median:
            raise IncompleteBatchError(
                f"{n_rows} rows is under 90% of the trailing {BASELINE_SESSIONS}-session median ({median:g})",
                code="below_median",
            )

    @staticmethod
    def _parse_bar(row: Mapping[str, Any], session: date) -> DailyBar:
        symbol = row.get("T")
        if not isinstance(symbol, str) or not symbol:
            raise ProviderError("massive row has no ticker", code="malformed_row")
        stamp = _number(row, "t")
        assert stamp is not None
        if datetime.fromtimestamp(stamp / 1000, ET).date() != session:
            raise ProviderError(f"massive row for {symbol} is stamped for another session", code="session_mismatch")
        trades = _number(row, "n", required=False)
        return DailyBar(
            source=SOURCE,
            symbol=symbol,
            session=session,
            open=_number(row, "o"),  # type: ignore[arg-type]
            high=_number(row, "h"),  # type: ignore[arg-type]
            low=_number(row, "l"),  # type: ignore[arg-type]
            close=_number(row, "c"),  # type: ignore[arg-type]
            volume=_number(row, "v"),  # type: ignore[arg-type]
            vwap=_number(row, "vw", required=False),
            trades=None if trades is None else int(trades),
        )

    # --- corporate actions -----------------------------------------------------------------

    def corporate_actions(self, since: date) -> list[CorporateAction]:
        splits = self._paged(f"{self._base_url}/stocks/v1/splits",
                             {"execution_date.gte": since.isoformat(), "limit": 1000})
        dividends = self._paged(f"{self._base_url}/stocks/v1/dividends",
                                {"ex_dividend_date.gte": since.isoformat(), "limit": 1000})
        return [self._parse_split(r) for r in splits] + [self._parse_dividend(r) for r in dividends]

    def _paged(self, url: str, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        next_url: str | None = url
        next_params: Mapping[str, Any] | None = params
        for _ in range(MAX_PAGES):
            if next_url is None:
                return rows
            body = self._http.get_json(next_url, params=next_params)
            self._check_status(body)
            page = body.get("results", [])
            if not isinstance(page, list) or not all(isinstance(r, dict) for r in page):
                raise ProviderError("massive 'results' is not a list of objects", code="malformed")
            rows.extend(page)
            raw_next = body.get("next_url")
            if raw_next is not None and not (isinstance(raw_next, str) and raw_next.startswith(self._base_url)):
                raise ProviderError("massive next_url points somewhere unexpected", code="malformed")
            # next_url already carries the cursor and the original filters; the key stays in the header.
            next_url, next_params = raw_next, None
        raise ProviderError(f"massive pagination exceeded {MAX_PAGES} pages", code="pagination_limit")

    @staticmethod
    def _parse_split(row: Mapping[str, Any]) -> CorporateAction:
        action_type = row.get("adjustment_type")
        if action_type not in _SPLIT_TYPES:
            raise ProviderError(f"massive split has unknown adjustment_type {str(action_type)[:32]!r}",
                                code="malformed_row")
        symbol = row.get("ticker")
        if not isinstance(symbol, str) or not symbol:
            raise ProviderError("massive split has no ticker", code="malformed_row")
        return CorporateAction(
            source=SOURCE,
            action_type=action_type,
            symbol=symbol,
            ex_date=_iso_date(row.get("execution_date"), "execution_date"),  # type: ignore[arg-type]
            split_from=_number(row, "split_from"),
            split_to=_number(row, "split_to"),
            historical_adjustment_factor=_number(row, "historical_adjustment_factor", required=False),
            vendor_id=str(row["id"]) if row.get("id") is not None else None,
        )

    @staticmethod
    def _parse_dividend(row: Mapping[str, Any]) -> CorporateAction:
        symbol = row.get("ticker")
        if not isinstance(symbol, str) or not symbol:
            raise ProviderError("massive dividend has no ticker", code="malformed_row")
        currency = row.get("currency")
        return CorporateAction(
            source=SOURCE,
            action_type="cash_dividend",
            symbol=symbol,
            ex_date=_iso_date(row.get("ex_dividend_date"), "ex_dividend_date"),  # type: ignore[arg-type]
            cash_amount=_number(row, "cash_amount"),
            currency=currency if isinstance(currency, str) else None,
            declaration_date=_iso_date(row.get("declaration_date"), "declaration_date", required=False),
            record_date=_iso_date(row.get("record_date"), "record_date", required=False),
            pay_date=_iso_date(row.get("pay_date"), "pay_date", required=False),
            historical_adjustment_factor=_number(row, "historical_adjustment_factor", required=False),
            vendor_id=str(row["id"]) if row.get("id") is not None else None,
        )

    # --- last trade --------------------------------------------------------------------------

    def last_trade(self, symbols: Sequence[str]) -> dict[str, Quote]:
        raise ProviderError("massive Stocks Basic has no real-time trades", code="not_in_plan")
