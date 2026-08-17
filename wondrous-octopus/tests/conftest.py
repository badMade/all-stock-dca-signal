"""Shared synthetic fixtures for wondrous-octopus's test suite.

Every test in this suite runs against small, deterministic, in-memory or
tmp_path data -- never the real (gitignored, possibly-absent) ticker_data/
archive. Adapted from ml-forecaster/tests/conftest.py (siblings deliberately
do not import each other -- see the root CLAUDE.md's sibling-tree
conventions), extended with what THIS subproject's data layer must handle:

- ``Dividends`` / ``Stock Splits`` columns (the archive's Yahoo layout);
- an optional mid-series 2:1 split, so the raw-vs-adjusted OHLC math in
  data.py is exercised against a genuinely discontinuous raw series;
- a shared market factor across tickers, so cross-ticker learning (the
  market encoder, mask-ticker reconstruction) has real signal to find;
- a late-IPO ticker, a mid-series gap ticker, and underscore-prefixed
  archive decoys (``_delisted.txt``, ``_intraday/``), because the harness
  must treat all of those as non-blockers.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

#: Default row count for a synthetic ticker -- long enough for the 60-row
#: eligibility warm-up plus a couple of hundred predictable days, short
#: enough that a full walk-forward over the fixture archive stays fast.
DEFAULT_ROWS = 260

#: The calendar start shared by every full-history fixture ticker. Tests that
#: exercise the "training reaches back before --start" rule pass a --start
#: after this date and assert the earlier rows still reached the initial fit.
DEFAULT_START = "2021-01-04"


def make_market_returns(n: int = DEFAULT_ROWS, seed: int = 100) -> np.ndarray:
    """A deterministic daily market-factor return series shared across tickers.

    Tickers built with a nonzero ``market_beta`` mix this into their own
    idiosyncratic returns, giving the fixture archive genuine cross-ticker
    correlation for the market encoder tests to detect.
    """
    rng = np.random.default_rng(seed)
    return rng.normal(0.0002, 0.012, size=n)


def make_synthetic_ohlcv(
    *,
    n: int = DEFAULT_ROWS,
    seed: int = 0,
    start: str = DEFAULT_START,
    drift: float = 0.0003,
    vol: float = 0.018,
    with_volume: bool = True,
    market_returns: np.ndarray | None = None,
    market_beta: float = 0.0,
    split_at: int | None = None,
    gap: tuple[int, int] | None = None,
) -> pd.DataFrame:
    """A deterministic, realistic-shaped daily OHLCV frame for one ticker.

    The ADJUSTED price path is generated first (continuous, split-free), and
    raw O/H/L/C are derived from it: for a 2:1 split at row ``split_at``,
    every raw price BEFORE that row is doubled -- exactly the discontinuity a
    real Yahoo archive shows -- while ``Adj Close`` stays continuous and the
    ``Stock Splits`` column carries 2.0 on the split row. Bars are built
    consistent by construction (High >= max(Open, Close), Low <= min(Open,
    Close)) so the data layer's impossible-bar repair only triggers in tests
    that corrupt rows deliberately.

    ``gap=(start_row, length)`` drops ``length`` consecutive rows, simulating
    a halt/illiquid stretch the eligibility logic must survive.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n)
    daily_returns = rng.normal(drift, vol, size=n)
    if market_returns is not None:
        if len(market_returns) < n:
            raise ValueError("market_returns must cover at least n rows")
        # Align to the TAIL of the market series so a late-IPO ticker (small
        # n, later start) shares the same market days as the full-history
        # tickers it overlaps with.
        daily_returns = daily_returns + market_beta * np.asarray(market_returns)[-n:]
    adj_close = 50.0 * np.cumprod(1.0 + daily_returns)
    adj_open = np.concatenate([[adj_close[0]], adj_close[:-1]]) * (1.0 + rng.normal(0.0, 0.002, size=n))
    body_high = np.maximum(adj_open, adj_close)
    body_low = np.minimum(adj_open, adj_close)
    adj_high = body_high * (1.0 + rng.uniform(0.0, 0.008, size=n))
    adj_low = body_low * (1.0 - rng.uniform(0.0, 0.008, size=n))

    split_factor = np.ones(n)
    splits_column = np.zeros(n)
    if split_at is not None:
        if not 0 < split_at < n:
            raise ValueError("split_at must fall strictly inside the series")
        split_factor[:split_at] = 2.0
        splits_column[split_at] = 2.0

    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": adj_open * split_factor,
            "High": adj_high * split_factor,
            "Low": adj_low * split_factor,
            "Close": adj_close * split_factor,
            "Adj Close": adj_close,
            "Dividends": np.zeros(n),
            "Stock Splits": splits_column,
        }
    )
    if with_volume:
        frame["Volume"] = rng.integers(500_000, 5_000_000, size=n)
    if gap is not None:
        gap_start, gap_len = gap
        keep = np.ones(n, dtype=bool)
        keep[gap_start : gap_start + gap_len] = False
        frame = frame.loc[keep].reset_index(drop=True)
    return frame


def write_ticker_csv(history_dir: Path, ticker: str, frame: pd.DataFrame) -> Path:
    path = history_dir / f"{ticker}.csv"
    frame.to_csv(path, index=False)
    return path


@pytest.fixture
def market_factor() -> np.ndarray:
    return make_market_returns()


@pytest.fixture
def synthetic_history_dir(tmp_path: Path, market_factor: np.ndarray) -> Path:
    """The standard fixture archive.

    - AAA, BBB: full-history, strongly market-correlated (beta 1.0) -- the
      pair the cross-ticker tests lean on; BBB carries a mid-series 2:1
      split so adjustment logic is always exercised.
    - CCC: late IPO (starts ~40% in) -- joins the universe mid-replay.
    - DDD: mid-series 10-row gap, no Volume column -- graceful degradation.
    - ZZZ: uncorrelated (beta 0.0) -- the control for "does the market
      encoder actually use cross-ticker signal?".
    - _delisted.txt, _intraday/, AAA-backup.csv: archive debris the
      discovery glob must skip.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(
        history_dir, "AAA", make_synthetic_ohlcv(seed=1, drift=0.0006, market_returns=market_factor, market_beta=1.0)
    )
    write_ticker_csv(
        history_dir,
        "BBB",
        make_synthetic_ohlcv(seed=2, drift=-0.0002, market_returns=market_factor, market_beta=1.0, split_at=130),
    )
    late_start = str(pd.bdate_range(start=DEFAULT_START, periods=DEFAULT_ROWS)[104].date())
    write_ticker_csv(
        history_dir,
        "CCC",
        make_synthetic_ohlcv(
            n=DEFAULT_ROWS - 104, seed=3, start=late_start, market_returns=market_factor, market_beta=0.7
        ),
    )
    write_ticker_csv(
        history_dir,
        "DDD",
        make_synthetic_ohlcv(seed=4, with_volume=False, gap=(120, 10)),
    )
    write_ticker_csv(history_dir, "ZZZ", make_synthetic_ohlcv(seed=5, market_beta=0.0))
    # Archive debris: none of these may surface as tickers.
    (history_dir / "_delisted.txt").write_text("XYZ delisted 2020-01-01\n")
    intraday = history_dir / "_intraday"
    intraday.mkdir()
    (intraday / "AAA.csv").write_text("trading_day,price\n2021-01-04,50.0\n")
    (history_dir / "AAA-backup.csv").write_text("Date,Open,High,Low,Close\n2021-01-04,1,1,1,1\n")
    return history_dir
