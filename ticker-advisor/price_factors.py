"""Price-derived inputs for the daily universe ranking (feed step 5).

Specified first by tests/test_price_factors.py (docs/fundamentals_feed_scope.md). Pure stdlib, no network:

* :func:`load_price_history` reads the daily price archive's CSV (the shape of the repo's sample
  ``historical_stock_data.csv``: ``ticker,date,open,high,low,close,volume,vwap,n``) into one ascending list of
  :class:`Bar` per ticker. The sample exactly duplicates every row of five tickers, so exact duplicates collapse,
  while a conflicting duplicate is refused. It fails closed: a malformed row raises with its line number and never
  echoes the row.
* :func:`price_inputs` computes, as of a date and from bars on or before it only (no lookahead): 6- and 12-month
  momentum that skips the latest month, one-year daily-return volatility, the 60-session median dollar volume,
  history length, the last price and session, and the count of trailing sessions below the minimum price. A figure
  it cannot compute is ``None`` with a flag saying why, never a guess: missing data holds a name, it never
  demotes it.

Conventions: a session is a bar, 21 sessions is a month, 126 six months, 252 a year. Momentum is
``close[t-21] / close[t-21-N] - 1`` (the 12-1 style the engine documents). Volatility is the sample standard
deviation of the last 252 simple daily returns, not annualized (annualizing is a monotone change that z-scoring
ignores). Dollar volume is ``close * volume``.

UNADJUSTED SPLITS. A one-day close ratio within 1% of 2, 3, 4, 5, 10, 3/2 or a reciprocal in the last 300 sessions
makes momentum and volatility ``None`` and raises ``possible_unadjusted_split``: a split read as a crash or surge
would corrupt the ranking. Whether the real archive is split-adjusted is not known here.
"""

from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO

SESSIONS_PER_MONTH = 21
MOMENTUM_6M_SESSIONS = 126
MOMENTUM_12M_SESSIONS = 252
VOLATILITY_RETURNS = 252
DOLLAR_VOLUME_SESSIONS = 60
SPLIT_LOOKBACK_SESSIONS = 300
SPLIT_TOLERANCE = 0.01
_SPLIT_RATIOS = tuple(r for base in (2.0, 3.0, 4.0, 5.0, 10.0, 1.5) for r in (base, 1.0 / base))
REQUIRED_COLUMNS = ("ticker", "date", "close", "volume")


@dataclass(frozen=True)
class Bar:
    """One trading session of one ticker."""

    date: date
    close: float
    volume: float


@dataclass(frozen=True)
class PriceInputs:
    """Price-derived inputs as of a date. ``flags`` explains every ``None`` and any data-quality warning."""

    momentum_6m: float | None
    momentum_12m: float | None
    volatility_1y: float | None
    median_dollar_volume_60d: float | None
    history_sessions: int
    last_price: float | None
    last_session: date | None
    sessions_below_min_price: int
    flags: tuple[str, ...]


def _positive_finite(raw: str | None) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _row_to_bar(row: Mapping[str, str | None], line: int) -> Bar:
    try:
        day = date.fromisoformat((row["date"] or "").strip())
    except ValueError:
        raise ValueError(f"line {line}: date is not an ISO date") from None
    close = _positive_finite(row["close"])
    if close is None:
        raise ValueError(f"line {line}: close is not a positive finite number")
    try:
        volume = float(row["volume"])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"line {line}: volume is not a number") from None
    if not math.isfinite(volume) or volume < 0:
        raise ValueError(f"line {line}: volume is not a non-negative finite number")
    return Bar(date=day, close=close, volume=volume)


def _read_rows(handle: TextIO, wanted: set[str] | None) -> dict[str, dict[date, Bar]]:
    reader = csv.DictReader(handle)
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"price archive is missing required column(s): {', '.join(missing)}")
    by_ticker: dict[str, dict[date, Bar]] = {}
    for row in reader:
        ticker = (row["ticker"] or "").strip().upper()
        if not ticker:
            raise ValueError(f"line {reader.line_num}: blank ticker")
        if wanted is not None and ticker not in wanted:
            continue
        bar = _row_to_bar(row, reader.line_num)
        days = by_ticker.setdefault(ticker, {})
        existing = days.get(bar.date)
        if existing is not None and existing != bar:
            raise ValueError(f"conflicting rows for {ticker} on {bar.date.isoformat()}")
        days[bar.date] = bar
    return by_ticker


def load_price_history(
    source: str | Path | TextIO, *, tickers: Iterable[str] | None = None
) -> dict[str, list[Bar]]:
    """Read the archive CSV into ascending bars per (uppercased) ticker, optionally only for ``tickers``.

    Exact duplicate rows collapse; a duplicate whose close or volume differs is refused. Raises ``ValueError`` for
    a missing required column or a malformed row (naming only the line number), and ``OSError`` if the file cannot
    be opened.
    """
    wanted = None if tickers is None else {t.strip().upper() for t in tickers}
    if hasattr(source, "read"):
        by_ticker = _read_rows(source, wanted)  # type: ignore[arg-type]
    else:
        with open(source, newline="", encoding="utf-8") as handle:
            by_ticker = _read_rows(handle, wanted)
    return {t: [days[d] for d in sorted(days)] for t, days in by_ticker.items()}


def _split_like(closes: Sequence[float]) -> bool:
    for previous, current in zip(closes, closes[1:], strict=False):
        ratio = current / previous
        if any(abs(ratio - s) / s <= SPLIT_TOLERANCE for s in _SPLIT_RATIOS):
            return True
    return False


def _momentum(closes: Sequence[float], sessions: int) -> float | None:
    reference = len(closes) - 1 - SESSIONS_PER_MONTH
    base = reference - sessions
    return None if base < 0 else closes[reference] / closes[base] - 1.0


def _volatility(closes: Sequence[float]) -> float | None:
    if len(closes) < VOLATILITY_RETURNS + 1:
        return None
    window = closes[-(VOLATILITY_RETURNS + 1):]
    return statistics.stdev(b / a - 1.0 for a, b in zip(window, window[1:], strict=False))


def _trailing_below(closes: Sequence[float], minimum: float) -> int:
    count = 0
    for close in reversed(closes):
        if close >= minimum:
            break
        count += 1
    return count


def price_inputs(bars: Sequence[Bar], *, as_of: date, min_price: float = 5.0) -> PriceInputs:
    """The price-derived inputs from ``bars`` on or before ``as_of`` (see the module docstring)."""
    eligible = sorted((b for b in bars if b.date <= as_of), key=lambda b: b.date)
    closes = [b.close for b in eligible]
    flags: list[str] = []
    momentum_6m = momentum_12m = volatility = None
    if _split_like(closes[-(SPLIT_LOOKBACK_SESSIONS + 1):]):
        flags.append("possible_unadjusted_split")
    else:
        momentum_6m = _momentum(closes, MOMENTUM_6M_SESSIONS)
        momentum_12m = _momentum(closes, MOMENTUM_12M_SESSIONS)
        volatility = _volatility(closes)
        for name, value in (("momentum_6m", momentum_6m), ("momentum_12m", momentum_12m),
                            ("volatility_1y", volatility)):
            if value is None:
                flags.append(f"short_history:{name}")
    dollar_volume = None
    if len(eligible) >= DOLLAR_VOLUME_SESSIONS:
        dollar_volume = statistics.median(b.close * b.volume for b in eligible[-DOLLAR_VOLUME_SESSIONS:])
    else:
        flags.append("short_history:dollar_volume_60d")
    return PriceInputs(
        momentum_6m=momentum_6m,
        momentum_12m=momentum_12m,
        volatility_1y=volatility,
        median_dollar_volume_60d=dollar_volume,
        history_sessions=len(eligible),
        last_price=closes[-1] if closes else None,
        last_session=eligible[-1].date if eligible else None,
        sessions_below_min_price=_trailing_below(closes, min_price),
        flags=tuple(flags),
    )
