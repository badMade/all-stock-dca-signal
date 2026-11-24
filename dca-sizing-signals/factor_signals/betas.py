"""F1: point-in-time, Blume-shrunk market and orthogonalized-sector betas.

Spec: docs/adr-factor-residual-dip-signals.md, component F1 and shared rules 1, 2, 4 and 6.

The model, per ticker i with sector ETF s, over the trailing window of daily log returns:

    e_s = r_s - (a_s + b_s|m * r_m)              (sector ETF orthogonalized to the market)
    r_i = a_i + b_m * r_m + b_s * e_s + eps_i    (two-factor OLS)

Orthogonalizing the sector first keeps b_m interpretable as market beta (the report's
recommendation): b_s then measures only the part of the sector's move the market does not
explain. A ticker mapped to ``None`` (a broad ETF, by design) gets the market-only model.

Point-in-time rules this module keeps, deliberately strict like
comovement_signals/returns.py (whose row contract it copies; it cannot import that package,
see tests/test_architecture_boundaries.py):

  * a row dated after ``as_of``, or on it unless marked ``final: True``, is dropped before
    anything reads it -- the lookahead guard. At an intraday slot today's bar is still
    forming, so betas use data through the prior close only (ADR rule 1);
  * a row flagged ``interpolated`` is dropped before its close is looked at (Robinhood fills
    pre-listing days with such bars; they are not prices);
  * a malformed row raises FactorInputError instead of being skipped: a silently skipped
    bad row would quietly change every beta (fail closed, ADR rule 2).

The session calendar is the market symbol's own close dates, so no exchange calendar is
needed and a stray vendor date on one ticker cannot become a "session" for everyone. A
return exists only between two adjacent market sessions on which the series has closes;
a gap yields no return rather than a silently multi-day one.

Whole-run failures (no market series, a stale one, too short a one) raise. Per-ticker
problems never raise: the ticker lands in ``FactorModel.skipped`` with a reason, and the
downstream suggesters publish nothing for it (the existing tier stands).

Every default below is frozen (ADR rule 6): changing one is a new row in the ledger's
``trials`` table, not a tweak. Pure: no I/O, no network, no clock. Stdlib only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

MODEL_VERSION = "factor_betas/v1"
DEFAULT_MARKET = "SPY"
# ADR F1: 252 trading days (one year) of daily data, refit nightly through the prior close.
DEFAULT_WINDOW_SESSIONS = 252
# Same 120-session floor as comovement_signals: fewer returns than this and a beta is noise.
DEFAULT_MIN_SESSIONS = 120
# Same staleness bound the co-movement suggesters use: a market series whose newest close is
# more than this many calendar days before as_of fails the whole run (covers a long weekend).
DEFAULT_MAX_STALENESS_DAYS = 4
# Blume-style shrinkage, as Collin-Dufresne and Daniel use it (per the source report; not
# re-verified): beta_shrunk = 0.23 + 0.77 * beta_hat. Applied to the sector beta too, toward
# 1, because a cap-weighted sector ETF's members load about 1 on it on average.
BLUME_INTERCEPT = 0.23
BLUME_SLOPE = 0.77
# Below this a regressor's variance (or the 2x2 normal-equation determinant) is treated as
# zero: the fit is "degenerate" rather than a division by a rounding error.
_DEGENERATE_EPS = 1e-18

SKIP_UNMAPPED = "unmapped"
SKIP_NO_PRICES = "no_prices"
SKIP_STALE = "stale"
SKIP_SECTOR_UNAVAILABLE = "sector_unavailable"
SKIP_INSUFFICIENT_HISTORY = "insufficient_history"
SKIP_DEGENERATE = "degenerate"


class FactorInputError(ValueError):
    """A caller-supplied input is malformed, or the run cannot proceed. Raised, never skipped."""


@dataclass(frozen=True)
class SectorMap:
    """The versioned ticker -> sector-ETF table (ADR rule 4).

    ``etf_by_ticker[t]`` is the sector ETF symbol, or ``None`` for a ticker that is
    market-only by design (a broad index ETF). A ticker absent from the map is "unmapped"
    and is skipped -- never silently given a market-only fit, because a missing mapping is
    a data problem, not a modelling choice. ``version`` is carried into every output so a
    reclassification can never leak backward unnoticed.
    """

    version: str
    etf_by_ticker: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise FactorInputError("sector map version must be a non-empty string")
        for ticker, etf in self.etf_by_ticker.items():
            if not isinstance(ticker, str) or not ticker:
                raise FactorInputError(f"sector map ticker must be a non-empty string: {ticker!r}")
            if etf is not None and (not isinstance(etf, str) or not etf):
                raise FactorInputError(f"sector ETF for {ticker} must be a non-empty string or None: {etf!r}")


@dataclass(frozen=True)
class SectorFit:
    """The sector ETF's regression on the market: e_s = r_s - (alpha + beta_on_market * r_m).

    F3 reuses alpha and beta_on_market to orthogonalize the sector ETF's intraday move.
    """

    etf: str
    alpha: float
    beta_on_market: float
    n_obs: int


@dataclass(frozen=True)
class TickerBetas:
    """One ticker's fit. ``beta_market``/``beta_sector`` are shrunk; ``*_raw`` are OLS.

    ``sigma_idio_daily`` is the residual standard deviation (degrees of freedom n - k - 1),
    the denominator F5's z-score will use. ``r_squared`` is the share of the ticker's daily
    variance the factors explain (unshrunk fit).
    """

    ticker: str
    sector_etf: str | None
    n_obs: int
    alpha: float
    beta_market_raw: float
    beta_sector_raw: float | None
    beta_market: float
    beta_sector: float | None
    sigma_idio_daily: float
    r_squared: float
    latest_close_date: date


@dataclass(frozen=True)
class FactorModel:
    """Everything fit as of one date.

    ``factor_returns`` holds the factor series over the window: the market's raw log returns
    under the market symbol, and each usable sector ETF's *orthogonalized* returns under its
    own symbol. F2 builds its factor covariance from these.
    """

    as_of: date
    market: str
    latest_session: date
    sessions: tuple[date, ...]
    window_sessions: int
    min_sessions: int
    sector_map_version: str
    model_version: str
    sector_fits: Mapping[str, SectorFit]
    factor_returns: Mapping[str, Mapping[date, float]]
    tickers: Mapping[str, TickerBetas]
    skipped: Mapping[str, str] = field(default_factory=dict)


# --- rows --------------------------------------------------------------------------------------


def _parse_date(value: object) -> date:
    if isinstance(value, date):
        # datetime is a date subclass; keep only the calendar day.
        return date(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise FactorInputError(f"unparseable date {value!r}") from exc
    raise FactorInputError(f"date must be a date or ISO string, got {type(value).__name__}")


def _bool_flag(row: Mapping[str, object], name: str) -> bool:
    value = row.get(name, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        # A string "false" is truthy; refusing non-bools avoids guessing what it meant.
        raise FactorInputError(f"{name} flag must be a bool: {row!r}")
    return value


def _clean_rows(close_rows: Iterable[Mapping[str, object]], as_of: date) -> dict[str, dict[date, float]]:
    """Validate every real row (future ones too: a malformed feed fails regardless of date),
    then keep only closes that were final on or before as_of."""
    closes: dict[str, dict[date, float]] = {}
    seen: set[tuple[str, date]] = set()
    for row in close_rows:
        if not isinstance(row, Mapping):
            raise FactorInputError(f"row must be a mapping, got {type(row).__name__}")
        if _bool_flag(row, "interpolated"):
            continue
        final = _bool_flag(row, "final")
        try:
            ticker, raw_date, raw_close = row["ticker"], row["date"], row["close"]
        except KeyError as exc:
            raise FactorInputError(f"row missing ticker/date/close: {row!r}") from exc
        if not isinstance(ticker, str) or not ticker:
            raise FactorInputError(f"ticker must be a non-empty string: {row!r}")
        d = _parse_date(raw_date)
        if (ticker, d) in seen:
            raise FactorInputError(f"duplicate row for {ticker} on {d.isoformat()}")
        seen.add((ticker, d))
        if isinstance(raw_close, bool) or not isinstance(raw_close, (int, float)):
            raise FactorInputError(f"close must be a number: {row!r}")
        close = float(raw_close)
        if not math.isfinite(close) or close <= 0.0:
            raise FactorInputError(f"close must be finite and positive: {row!r}")
        if d > as_of or (d == as_of and not final):
            continue
        closes.setdefault(ticker, {})[d] = close
    return closes


def _window_returns(by_date: Mapping[date, float], window_dates: Sequence[date]) -> dict[date, float]:
    out: dict[date, float] = {}
    for prev, cur in zip(window_dates, window_dates[1:], strict=False):
        if prev in by_date and cur in by_date:
            out[cur] = math.log(by_date[cur] / by_date[prev])
    return out


# --- least squares -----------------------------------------------------------------------------


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _ols1(y: Sequence[float], x: Sequence[float]) -> tuple[float, float] | None:
    """(alpha, beta) of y on x with intercept, or None when x has no variance."""
    xbar, ybar = _mean(x), _mean(y)
    sxx = sum((a - xbar) ** 2 for a in x)
    if sxx <= _DEGENERATE_EPS:
        return None
    sxy = sum((a - xbar) * (b - ybar) for a, b in zip(x, y, strict=True))
    beta = sxy / sxx
    return ybar - beta * xbar, beta


def _ols2(y: Sequence[float], x1: Sequence[float], x2: Sequence[float]) -> tuple[float, float, float] | None:
    """(alpha, b1, b2) of y on x1 and x2 with intercept, via centered normal equations.

    e_s is orthogonal to r_m only on the sector-fit sample, and a ticker's sample can be a
    subset of it (a later listing, a missing day), so the full 2x2 solve is used rather than
    two separate univariate slopes.
    """
    x1bar, x2bar, ybar = _mean(x1), _mean(x2), _mean(y)
    c1 = [a - x1bar for a in x1]
    c2 = [a - x2bar for a in x2]
    cy = [a - ybar for a in y]
    s11 = sum(a * a for a in c1)
    s22 = sum(a * a for a in c2)
    s12 = sum(a * b for a, b in zip(c1, c2, strict=True))
    s1y = sum(a * b for a, b in zip(c1, cy, strict=True))
    s2y = sum(a * b for a, b in zip(c2, cy, strict=True))
    det = s11 * s22 - s12 * s12
    if s11 <= _DEGENERATE_EPS or s22 <= _DEGENERATE_EPS or det <= _DEGENERATE_EPS * max(s11 * s22, 1.0):
        return None
    b1 = (s22 * s1y - s12 * s2y) / det
    b2 = (s11 * s2y - s12 * s1y) / det
    return ybar - b1 * x1bar - b2 * x2bar, b1, b2


def _shrink(beta: float) -> float:
    return BLUME_INTERCEPT + BLUME_SLOPE * beta


# --- the model ---------------------------------------------------------------------------------


def fit_factor_model(
    close_rows: Iterable[Mapping[str, object]],
    as_of: date,
    tickers: Iterable[str],
    sector_map: SectorMap,
    market: str = DEFAULT_MARKET,
    window_sessions: int = DEFAULT_WINDOW_SESSIONS,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    max_staleness_days: int = DEFAULT_MAX_STALENESS_DAYS,
) -> FactorModel:
    """Fit F1 for ``tickers`` as of ``as_of``.

    Args:
        close_rows: mappings with ``ticker``, ``date`` (date or ISO string), ``close``
            (positive number) and optionally ``interpolated`` / ``final`` (bools). Must
            include the market symbol and every sector ETF the map names, as ordinary rows.
        as_of: the decision date. Rows after it, and non-final rows on it, are ignored.
        tickers: the tickers to fit. Each one either gets a TickerBetas or a skip reason.
        sector_map: the versioned ticker -> sector ETF table.
        market: the market factor symbol.
        window_sessions: market return sessions kept (the newest ones).
        min_sessions: minimum overlapping returns for any fit.
        max_staleness_days: see DEFAULT_MAX_STALENESS_DAYS.

    Raises:
        FactorInputError: a malformed row or parameter, or a market series that is missing,
            stale or shorter than ``min_sessions`` (no ticker can be fit without it).
    """
    if not isinstance(sector_map, SectorMap):
        raise FactorInputError("sector_map must be a SectorMap")
    if window_sessions < 1 or min_sessions < 3 or max_staleness_days < 0:
        raise FactorInputError("window_sessions >= 1, min_sessions >= 3, max_staleness_days >= 0 required")
    closes = _clean_rows(close_rows, as_of)

    market_closes = closes.get(market)
    if not market_closes:
        raise FactorInputError(f"no usable closes for market symbol {market}")
    calendar = sorted(market_closes)
    latest_session = calendar[-1]
    if (as_of - latest_session).days > max_staleness_days:
        raise FactorInputError(
            f"stale market series: newest {market} close {latest_session.isoformat()} is more than "
            f"{max_staleness_days} days before as_of {as_of.isoformat()}"
        )
    # One extra leading date so the oldest kept session still has a prior close.
    window_dates = calendar[-(window_sessions + 1):]
    sessions = tuple(window_dates[1:])
    r_m = _window_returns(market_closes, window_dates)
    if len(r_m) < min_sessions:
        raise FactorInputError(f"market series has {len(r_m)} returns in the window, fewer than {min_sessions}")

    # Sector ETFs: fit each one the map names once, so every ticker in a sector shares the
    # same orthogonalized series (F2 needs one consistent factor set).
    sector_fits: dict[str, SectorFit] = {}
    factor_returns: dict[str, dict[date, float]] = {market: r_m}
    for etf in sorted({e for e in sector_map.etf_by_ticker.values() if e is not None}):
        by_date = closes.get(etf)
        if not by_date or max(by_date) != latest_session:
            continue  # missing or stale sector ETF: its tickers are sector_unavailable
        r_s = _window_returns(by_date, window_dates)
        common = [d for d in sessions if d in r_s and d in r_m]
        if len(common) < min_sessions:
            continue
        fit = _ols1([r_s[d] for d in common], [r_m[d] for d in common])
        if fit is None:
            continue
        alpha, beta = fit
        sector_fits[etf] = SectorFit(etf=etf, alpha=alpha, beta_on_market=beta, n_obs=len(common))
        factor_returns[etf] = {d: r_s[d] - (alpha + beta * r_m[d]) for d in common}

    fitted: dict[str, TickerBetas] = {}
    skipped: dict[str, str] = {}
    for ticker in sorted(set(tickers)):
        if ticker not in sector_map.etf_by_ticker:
            skipped[ticker] = SKIP_UNMAPPED
            continue
        by_date = closes.get(ticker)
        if not by_date:
            skipped[ticker] = SKIP_NO_PRICES
            continue
        latest_close = max(by_date)
        if latest_close != latest_session:
            skipped[ticker] = SKIP_STALE
            continue
        etf = sector_map.etf_by_ticker[ticker]
        if etf is not None and etf not in sector_fits:
            skipped[ticker] = SKIP_SECTOR_UNAVAILABLE
            continue
        r_i = _window_returns(by_date, window_dates)
        e_s = factor_returns[etf] if etf is not None else None
        sample = [d for d in sessions if d in r_i and d in r_m and (e_s is None or d in e_s)]
        if len(sample) < min_sessions:
            skipped[ticker] = SKIP_INSUFFICIENT_HISTORY
            continue
        y = [r_i[d] for d in sample]
        xm = [r_m[d] for d in sample]
        if e_s is None:
            fit1 = _ols1(y, xm)
            if fit1 is None:
                skipped[ticker] = SKIP_DEGENERATE
                continue
            alpha, b_m = fit1
            b_s = None
            fitted_values = [alpha + b_m * a for a in xm]
            k = 1
        else:
            xs = [e_s[d] for d in sample]
            fit2 = _ols2(y, xm, xs)
            if fit2 is None:
                skipped[ticker] = SKIP_DEGENERATE
                continue
            alpha, b_m, b_s = fit2
            fitted_values = [alpha + b_m * a + b_s * b for a, b in zip(xm, xs, strict=True)]
            k = 2
        sse = sum((a - b) ** 2 for a, b in zip(y, fitted_values, strict=True))
        ybar = _mean(y)
        sst = sum((a - ybar) ** 2 for a in y)
        if sst <= _DEGENERATE_EPS:
            skipped[ticker] = SKIP_DEGENERATE
            continue
        fitted[ticker] = TickerBetas(
            ticker=ticker,
            sector_etf=etf,
            n_obs=len(sample),
            alpha=alpha,
            beta_market_raw=b_m,
            beta_sector_raw=b_s,
            beta_market=_shrink(b_m),
            beta_sector=None if b_s is None else _shrink(b_s),
            sigma_idio_daily=math.sqrt(sse / (len(sample) - k - 1)),
            r_squared=max(0.0, 1.0 - sse / sst),
            latest_close_date=latest_close,
        )

    return FactorModel(
        as_of=as_of,
        market=market,
        latest_session=latest_session,
        sessions=sessions,
        window_sessions=window_sessions,
        min_sessions=min_sessions,
        sector_map_version=sector_map.version,
        model_version=MODEL_VERSION,
        sector_fits=sector_fits,
        factor_returns=factor_returns,
        tickers=fitted,
        skipped=skipped,
    )
