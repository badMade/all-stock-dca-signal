"""Shared synthetic OHLCV fixtures for ml-forecaster's test suite.

Every test in this suite runs against small, deterministic, in-memory
data -- never the real (gitignored, possibly-absent) ticker_data/ archive.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
import pytest


def make_synthetic_ohlcv(
    *,
    n: int = 260,
    seed: int = 0,
    start: str = "2021-01-04",
    drift: float = 0.0003,
    vol: float = 0.018,
    with_volume: bool = True,
) -> pd.DataFrame:
    """A deterministic, realistic-shaped daily OHLCV frame for one ticker."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n)
    daily_returns = rng.normal(drift, vol, size=n)
    close = 50.0 * np.cumprod(1.0 + daily_returns)
    high = close * (1.0 + rng.uniform(0.0, 0.01, size=n))
    low = close * (1.0 - rng.uniform(0.0, 0.01, size=n))
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1.0 + rng.normal(0.0, 0.002, size=n))
    frame = pd.DataFrame(
        {"Date": dates, "Open": open_, "High": high, "Low": low, "Close": close, "Adj Close": close}
    )
    if with_volume:
        frame["Volume"] = rng.integers(500_000, 5_000_000, size=n)
    return frame


def write_ticker_csv(history_dir: Path, ticker: str, frame: pd.DataFrame) -> Path:
    path = history_dir / f"{ticker}.csv"
    frame.to_csv(path, index=False)
    return path


@pytest.fixture
def synthetic_history_dir(tmp_path: Path) -> Path:
    """A small three-ticker archive: two with Volume, one without."""
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=1, drift=0.0006))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=2, drift=-0.0002))
    write_ticker_csv(history_dir, "CCC", make_synthetic_ohlcv(seed=3, drift=0.0001, with_volume=False))
    return history_dir


@pytest.fixture
def dip_dataset(tmp_path: Path) -> pd.DataFrame:
    from features import build_dataset

    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    write_ticker_csv(history_dir, "AAA", make_synthetic_ohlcv(seed=31, n=260, drift=0.0004))
    write_ticker_csv(history_dir, "BBB", make_synthetic_ohlcv(seed=32, n=260, drift=-0.0002))
    write_ticker_csv(history_dir, "CCC", make_synthetic_ohlcv(seed=33, n=260, drift=0.0001))
    dataset = build_dataset(history_dir, ["AAA", "BBB", "CCC"])
    return cast(pd.DataFrame, dataset[dataset["dip_day"]].reset_index(drop=True))
