"""Loss-hedge SHADOW LOG helpers: research only, suggest only, never trades.

Implements the pricing and scoring rules of docs/adr-goals-and-loss-hedge-shadow.md (D4), beside
the cash-secured put shadow log (csp_shadow.py) and under the same rules:

* It names no broker order tool and makes no network call; a caller reads option and crypto
  quotes from the READ-ONLY broker tools and hands the rows in as plain dicts.
  tests/ledger/test_hedge_shadow_no_orders.py greps this file and fails the build otherwise.
* It never changes DCA sizing and nothing in the live paths imports it.
* Point in time. A quote is usable only if its OWN timestamp is at or before the decision time.
  A later quote gives no price (None), never an estimate (tests/ledger/test_hedge_shadow_pit.py).
* The scorecard judges the days the equity portfolio fell, not only the full-period correlation,
  because in 2026 data a hedge can look uncorrelated and still fall on the same down days
  (tests/ledger/test_hedge_shadow_scorecard.py).

Pure functions only in this first cut. The append-only DuckDB tables for daily shadow records
follow csp_shadow.py's store rules when the daily capture task is added (ADR phase 3).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DECISION_TIME = time(16, 0)
CONTRACT_MULTIPLIER = Decimal(100)
# Mirrors dca_goals.CRYPTO_ASSETS (core module; this package does not import core modules).
CRYPTO_ASSETS: tuple[str, ...] = ("BTC", "ETH")
MIN_SCORECARD_DAYS = 3



def decision_time(day: date) -> datetime:
    """4:00 PM America/New_York on ``day``, timezone-aware (DST handled by zoneinfo)."""
    return datetime.combine(day, DECISION_TIME, tzinfo=ET)


def _require_aware(moment: datetime) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("decision time must be a timezone-aware datetime")


def parse_timestamp(text: Any) -> datetime | None:
    """Parse a broker timestamp, rounding sub-microsecond precision UP. None if unparseable.

    Python datetimes stop at microseconds. Truncating ``.920988052`` to ``.920988`` would let a
    decision at ``.920988`` see a quote published 52 ns later; rounding up to ``.920989`` keeps
    every comparison on the safe side of the lookahead line.
    """
    if not isinstance(text, str):
        return None
    stamp = text.strip()
    # Shape: YYYY-MM-DDTHH:MM:SS[.f{1,9}](Z|+HH:MM|-HH:MM). Parsed by hand: this module's import
    # allowlist (test_hedge_shadow_no_orders.py) is the CSP shadow log's, which has no regex module.
    if stamp.endswith("Z"):
        body, offset = stamp[:-1], "Z"
    else:
        body, offset = stamp[:-6], stamp[-6:]
        if len(offset) != 6 or offset[0] not in "+-" or offset[3] != ":":
            return None
    base, _, fraction = body.partition(".")
    if len(base) != 19 or base[10] != "T" or (fraction and not (fraction.isdigit() and len(fraction) <= 9)):
        return None
    if "." in body and not fraction:
        return None
    try:
        moment = datetime.fromisoformat(base + ("+00:00" if offset == "Z" else offset))
    except ValueError:
        return None
    if fraction:
        nanoseconds = int(fraction.ljust(9, "0"))
        moment += timedelta(microseconds=-(-nanoseconds // 1000))  # ceiling division
    return moment


def _quote_rows(rows: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    # Accept both the read-only tool's {"quote": {...}} envelope and a bare quote dict.
    for row in rows:
        if isinstance(row, Mapping):
            inner = row.get("quote")
            yield inner if isinstance(inner, Mapping) else row


def option_quote_at(option_quotes: Iterable[Any], instrument_id: str, decision_at: datetime
                    ) -> Mapping[str, Any] | None:
    """The latest quote for ``instrument_id`` stamped at or before ``decision_at``, else None."""
    _require_aware(decision_at)
    best: tuple[datetime, Mapping[str, Any]] | None = None
    for quote in _quote_rows(option_quotes):
        if quote.get("instrument_id") != instrument_id:
            continue
        stamp = parse_timestamp(quote.get("updated_at"))
        if stamp is None or stamp > decision_at:
            continue
        if best is None or stamp > best[0]:
            best = (stamp, quote)
    return None if best is None else best[1]


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def put_entry_cost(quote: Mapping[str, Any], contracts: int = 1) -> Decimal | None:
    """Cost of BUYING ``contracts`` puts at the ask (we pay the offer), to the cent. None if no ask."""
    if isinstance(contracts, bool) or not isinstance(contracts, int) or contracts < 1:
        raise ValueError("contracts must be a positive integer")
    ask = _decimal(quote.get("ask_price"))
    if ask is None:
        return None
    return (ask * CONTRACT_MULTIPLIER * contracts).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def crypto_price_at(quotes: Iterable[Any], asset: str, decision_at: datetime) -> Decimal | None:
    """Mark price of the latest ``asset``/USD quote stamped at or before ``decision_at``, else None.

    Crypto trades 24/7, so "the price at 4 PM" is the last quote at or before 4 PM, never one after.
    Assets outside ``CRYPTO_ASSETS`` have no price; a zero or missing mark is skipped.
    """
    _require_aware(decision_at)
    if asset not in CRYPTO_ASSETS:
        return None
    symbols = {f"{asset}USD", f"{asset}-USD"}
    best: tuple[datetime, Decimal] | None = None
    for quote in _quote_rows(quotes):
        if quote.get("symbol") not in symbols:
            continue
        stamp = parse_timestamp(quote.get("updated_at"))
        mark = _decimal(quote.get("mark_price"))
        if stamp is None or mark is None or stamp > decision_at:
            continue
        if best is None or stamp > best[0]:
            best = (stamp, mark)
    return None if best is None else best[1]


def down_day_scorecard(equity_returns: Mapping[date, float], hedge_returns: Mapping[date, float]
                       ) -> dict[str, Any] | None:
    """How the hedge behaved on the days the equity portfolio fell, beside full-period correlation.

    Only dates present in both series count. Fewer than ``MIN_SCORECARD_DAYS`` common days, or
    any non-finite return among them, gives no card (None) rather than a misleading one. With no
    equity down days the down-day fields are None: "no evidence" must not read as "never fell".
    """
    days = sorted(set(equity_returns) & set(hedge_returns))
    if len(days) < MIN_SCORECARD_DAYS:
        return None
    equity = [equity_returns[d] for d in days]
    hedge = [hedge_returns[d] for d in days]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in equity + hedge):
        return None
    try:
        correlation: float | None = statistics.correlation(equity, hedge)
    except statistics.StatisticsError:
        correlation = None  # a constant series has no correlation
    down = [hedge_returns[d] for d in days if equity_returns[d] < 0]
    return {
        "days": len(days),
        "correlation": correlation,
        "equity_down_days": len(down),
        "hedge_fell_with_equity_share": (sum(1 for r in down if r < 0) / len(down)) if down else None,
        "hedge_mean_on_down_days": statistics.fmean(down) if down else None,
    }
