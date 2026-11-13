"""Daily option-chain snapshots from DoltHub's public ``post-no-preference/options`` database.

Specified first by tests/test_dolthub_options.py (TDD-first, fixtures pulled live
2026-08-22). The two payload facts those fixtures pinned, and that this module must not
paper over:

* Every numeric column comes back as a JSON string ("125.00") even though the response's
  own ``schema`` block says ``decimal``. Values are coerced from the payload, never trusted
  from the schema, and a value that will not parse as a finite float fails the whole call.
* ``call_put`` is "Call" / "Put", capitalized. It is kept exactly as sent, and any other
  value fails closed rather than being normalized into something a filter might miss.

``vol`` is the table's implied volatility as a decimal (0.6993 = 69.93% IV), not traded
volume, so it is exposed as ``implied_vol``.

Research ingest only: nothing on the live DCA side imports this
(tests/test_architecture_boundaries.py).
"""

from __future__ import annotations

import math
import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import Any

from ._json_get import get_json_object

API_URL = "https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master"
TABLE = "option_chain"
_SOURCE = "DoltHub options"
_CALL_PUT_VALUES = frozenset({"Call", "Put"})
_COLUMNS = (
    "date", "act_symbol", "expiration", "strike", "call_put", "bid", "ask",
    "vol", "delta", "gamma", "theta", "vega", "rho",
)


@dataclass(frozen=True)
class OptionQuote:
    """One end-of-day option quote. All numeric fields are floats, all dates are dates."""

    date: date
    act_symbol: str
    expiration: date
    strike: float
    call_put: str
    bid: float
    ask: float
    implied_vol: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def _sql_string_literal(value: str) -> str:
    # Defence in depth (test_query_string_escapes_ticker): the endpoint is read-only, but a
    # ticker still lands inside raw SQL, so a quote is doubled per the SQL standard.
    return "'" + value.replace("'", "''") + "'"


def _build_query(*, ticker: str, on_date: date) -> str:
    """The SQL text sent as the ``q`` parameter for one ticker's chain on one date."""
    columns = ", ".join(f"`{c}`" for c in _COLUMNS)
    return (
        f"SELECT {columns} FROM `{TABLE}` "
        f"WHERE act_symbol={_sql_string_literal(ticker)} "
        f"AND date={_sql_string_literal(on_date.isoformat())} "
        "ORDER BY expiration, strike, call_put"
    )


def _run_query(query: str) -> dict[str, Any]:
    """Send ``query`` to the DoltHub SQL API and return the parsed response envelope."""
    url = f"{API_URL}?{urllib.parse.urlencode({'q': query})}"
    return get_json_object(url, source=_SOURCE)


def _finite_float(row: dict[str, Any], column: str) -> float:
    raw = row.get(column)
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"{_SOURCE} column {column!r} is not numeric")
    value = float(raw)  # ValueError on a non-numeric string propagates: fail closed
    if not math.isfinite(value):
        raise ValueError(f"{_SOURCE} column {column!r} is not finite")
    return value


def _iso_date(row: dict[str, Any], column: str) -> date:
    raw = row.get(column)
    if not isinstance(raw, str):
        raise ValueError(f"{_SOURCE} column {column!r} is not a date string")
    return date.fromisoformat(raw)


def _parse_row(row: Any) -> OptionQuote:
    if not isinstance(row, dict):
        raise ValueError(f"{_SOURCE} row is not an object")
    call_put = row.get("call_put")
    if call_put not in _CALL_PUT_VALUES:
        raise ValueError(f"{_SOURCE} call_put is not 'Call' or 'Put'")
    symbol = row.get("act_symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError(f"{_SOURCE} act_symbol is missing")
    return OptionQuote(
        date=_iso_date(row, "date"),
        act_symbol=symbol,
        expiration=_iso_date(row, "expiration"),
        strike=_finite_float(row, "strike"),
        call_put=call_put,
        bid=_finite_float(row, "bid"),
        ask=_finite_float(row, "ask"),
        implied_vol=_finite_float(row, "vol"),
        delta=_finite_float(row, "delta"),
        gamma=_finite_float(row, "gamma"),
        theta=_finite_float(row, "theta"),
        vega=_finite_float(row, "vega"),
        rho=_finite_float(row, "rho"),
    )


def fetch_option_chain(*, ticker: str, on_date: date) -> list[OptionQuote]:
    """Every option quote DoltHub recorded for ``ticker`` on ``on_date``.

    An empty list means DoltHub has no rows for that ticker and date, which is normal:
    its ~2,098 symbols do not cover this repo's whole universe.

    Raises:
        ValueError: the query did not succeed, or any row is malformed. One bad row fails
            the whole chain rather than returning a silently partial one.
        OSError / urllib.error.HTTPError: transport failures, from ``_run_query``.
    """
    response = _run_query(_build_query(ticker=ticker, on_date=on_date))
    if response.get("query_execution_status") != "Success":
        raise ValueError(f"{_SOURCE} query did not succeed")
    rows = response.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(f"{_SOURCE} rows is not a list")
    return [_parse_row(row) for row in rows]
