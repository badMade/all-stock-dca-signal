"""The widest available per-ticker, per-day snapshot market_data_ingest can
produce, combining every data category the integration plan identified:
fundamentals, options/IV, index, and (new, this revision) earnings.

Two rules, both a direct consequence of "OHLCV is the backbone, not one
source among equals":

1. This module never creates a (ticker, day) row on its own. It only
   widens rows that ml_forecaster's or wondrous_octopus's own OHLCV-driven
   panel already contains -- callers look up a snapshot FOR a day the
   OHLCV archive already defines, never the reverse. There is no version
   of "expand beyond OHLCV" that produces more historical rows than OHLCV
   has, since the forward-return label is computed from the price series
   itself.
2. Every category carries its own *_data_available flag, independent of
   the others, because each source has a different history depth: local
   OHLCV has "a few hundred to ~1500 rows" per ml_forecaster's own README,
   DoltHub options coverage starts in 2019, SEC EDGAR fundamentals often
   reach further back than either, and Robinhood's own earnings data may
   have yet another depth (unverified, see suggester.py's note). A
   ticker/day can have OHLCV and fundamentals but no options data, or any
   other combination -- callers must check the flag, never assume every
   field is populated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    ticker: str
    asof: date

    # -- Fundamentals (SEC EDGAR, joined on `filed` date -- see the
    #    integration plan's point-in-time discipline) --
    fundamentals_data_available: bool
    fundamentals_asof: date | None  # the actual filing date used, for audit
    revenue_growth_ttm_pct: float | None
    gross_margin_pct: float | None
    net_margin_pct: float | None
    earnings_yield: float | None

    # -- Options / implied volatility (DoltHub, 2019-present) --
    options_data_available: bool
    iv_30d: float | None
    iv_minus_realized_vol_20d: float | None
    iv_rank_1y: float | None
    put_call_skew_25delta: float | None

    # -- Index / market context --
    index_data_available: bool
    index_relative_strength_5d_pct: float | None
    index_realized_vol_20d: float | None

    # -- Earnings (new this revision) --
    # days_until_next_earnings is schedule knowledge, not outcome
    # knowledge -- the date of an already-announced or estimated earnings
    # call is public on day t, so this is not lookahead the way a feature
    # derived from the earnings RESULT itself would be if dated wrong.
    earnings_data_available: bool
    days_since_last_earnings: int | None
    days_until_next_earnings: int | None
    last_earnings_surprise_pct: float | None  # (actual - estimate) / |estimate|


def load_asof(market_data_root: str, ticker: str, asof: date) -> MarketDataSnapshot:
    """Build the widest available snapshot for one (ticker, day).

    Each category is resolved independently against its own source's
    point-in-time rule: fundamentals use the most recent filing with
    filed <= asof; options use the DoltHub row dated asof if present;
    earnings use the most recent report with report_date <= asof, plus
    the next scheduled report date if that was already public knowledge
    on asof. A category with no qualifying row sets its *_data_available
    flag False and leaves its fields None -- it never raises, and it
    never backfills from a later date.

    Args:
        market_data_root: path to the versioned, point-in-time-stamped
            files market_data_ingest writes.
        ticker: the equity symbol.
        asof: the date to build the snapshot for.

    Returns:
        A MarketDataSnapshot with every category resolved independently.

    This function is a contract, not an implementation -- wiring it to
    the actual local files is Phase 1 of the integration plan, unchanged
    by this revision.
    """
    raise NotImplementedError
