"""Tests for the not-yet-written ticker-advisor/price_factors.py (step 5 of docs/fundamentals_feed_scope.md).

Written BEFORE the implementation (TDD). Do not implement until these are reviewed and approved.

Step 5 turns a daily price archive into the price-derived inputs the ranking needs:

* ``load_price_history`` reads the archive's CSV (the shape of the repo's sample ``historical_stock_data.csv``:
  ``ticker,date,open,high,low,close,volume,vwap,n``) into one ascending list of bars per ticker. The sample
  EXACTLY duplicates every row of five tickers, so exact duplicates collapse, while a conflicting duplicate is
  refused. It fails closed: a malformed row raises with its line number and never echoes the row.
* ``price_inputs`` computes, as of a date and using only bars on or before it (no lookahead): 6- and 12-month
  momentum that skips the latest month (a session is a bar; 21 sessions is a month, 126 six months, 252 a
  year), one-year daily-return volatility, the 60-session median dollar volume, history length, the last price
  and session, and the count of trailing sessions below the minimum price. A figure it cannot compute is None
  with a flag saying why, never a guess (missing data holds a name, it never demotes it).
* A one-day move that looks like an UNADJUSTED stock split (a ratio within 1% of 2, 3, 4, 5, 10, 3/2 or their
  reciprocals) in the last 300 sessions makes momentum and volatility None and raises
  ``possible_unadjusted_split``, because a split read as a crash or surge would corrupt the ranking. Whether the
  real archive is split-adjusted is NOT known here.

All inputs are SYNTHETIC except ``test_the_repo_sample_file_loads_and_scores``, which reads the repo's sample CSV
read-only. No run of 6 or more digits appears below (the account-number guard in tests/test_config_invariants.py).
"""

from __future__ import annotations

import io
import statistics
from datetime import date, timedelta
from pathlib import Path

import pytest

from price_factors import Bar, load_price_history, price_inputs

START = date(2025, 1, 1)
REPO_ROOT = Path(__file__).resolve().parents[2]


def series(closes, volumes=None, start=START):
    """One bar per close on consecutive calendar days (sessions are counted by bar, not by weekday)."""
    volumes = volumes or [1000] * len(closes)
    return [
        Bar(date=start + timedelta(days=i), close=float(c), volume=float(v))
        for i, (c, v) in enumerate(zip(closes, volumes, strict=True))
    ]


def linear(n):
    return series([100 + i for i in range(n)])


def as_of_last(bars):
    return bars[-1].date


# --- momentum ------------------------------------------------------------------------------------------------------


def test_momentum_skips_the_latest_month_and_looks_back_6_and_12_months():
    # close = 100 + i over 300 bars. Last index 299; skipping 21 sessions gives index 278 (price 378).
    # 6m base is index 278-126 = 152 (price 252): 378/252 - 1 = 0.5. 12m base is index 26 (price 126): 3.0 - 1 = 2.0.
    bars = linear(300)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.momentum_6m == pytest.approx(0.5)
    assert out.momentum_12m == pytest.approx(2.0)


def test_a_history_too_short_for_12m_still_gives_6m_and_flags_the_gap():
    bars = linear(200)  # index 199 -> 178 after the skip; 6m base 52. 12m needs 274 bars.
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.momentum_6m == pytest.approx((100 + 178) / (100 + 52) - 1)
    assert out.momentum_12m is None
    assert "short_history:momentum_12m" in out.flags and "short_history:momentum_6m" not in out.flags


def test_a_very_short_history_has_no_momentum():
    bars = linear(100)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.momentum_6m is None and out.momentum_12m is None
    assert {"short_history:momentum_6m", "short_history:momentum_12m"} <= set(out.flags)


def test_bars_after_the_as_of_date_are_ignored():
    bars = linear(300)
    cut = bars[250].date  # 251 sessions on or before this date
    out = price_inputs(bars, as_of=cut)
    assert out.history_sessions == 251
    assert out.last_session == cut and out.last_price == pytest.approx(100 + 250)


# --- volatility ------------------------------------------------------------------------------------------


def test_a_constant_daily_return_has_zero_volatility():
    bars = series([100 * 1.01**i for i in range(300)])
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.volatility_1y == pytest.approx(0.0, abs=1e-12)


def test_volatility_is_the_sample_stdev_of_the_last_252_daily_returns():
    closes = [100 + (2 if i % 2 else 0) for i in range(300)]  # alternates 100, 102, 100, 102 ...
    bars = series(closes)
    window = closes[-253:]
    returns = [b / a - 1 for a, b in zip(window, window[1:], strict=False)]
    assert len(returns) == 252
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.volatility_1y == pytest.approx(statistics.stdev(returns))


def test_volatility_needs_a_full_year_of_returns():
    bars = linear(200)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.volatility_1y is None and "short_history:volatility_1y" in out.flags


# --- unadjusted splits -----------------------------------------------------------------------------------


def test_a_split_like_one_day_move_blanks_momentum_and_volatility_and_raises_the_flag():
    closes = [200.0] * 200 + [100.0] * 100  # the close halves exactly at index 200, inside the 300-session window
    bars = series(closes)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert "possible_unadjusted_split" in out.flags
    assert out.momentum_6m is None and out.momentum_12m is None and out.volatility_1y is None
    assert out.median_dollar_volume_60d is not None  # volume times price is consistent across a split


def test_a_big_but_not_split_like_move_is_not_flagged():
    closes = [100.0] * 200 + [70.0] * 100  # a 30% drop: big, but not a split ratio
    bars = series(closes)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert "possible_unadjusted_split" not in out.flags
    assert out.momentum_12m is not None


def test_a_split_older_than_the_300_session_lookback_is_not_flagged():
    old = series([200.0] * 20 + [100.0] * 380)      # halves at index 20; the last 300 sessions are indices 100..399
    recent = series([200.0] * 320 + [100.0] * 80)   # halves at index 320, inside that window
    assert "possible_unadjusted_split" not in price_inputs(old, as_of=old[-1].date).flags
    assert "possible_unadjusted_split" in price_inputs(recent, as_of=recent[-1].date).flags


# --- dollar volume, history, price -----------------------------------------------------------------------


def test_median_dollar_volume_is_the_median_of_close_times_volume_over_60_sessions():
    bars = series([10.0] * 60, volumes=[i + 1 for i in range(60)])  # dollars 10 .. 600
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.median_dollar_volume_60d == pytest.approx(305.0)  # (300 + 310) / 2


def test_dollar_volume_uses_only_the_latest_60_sessions():
    volumes = [1] * 40 + [10] * 60  # the early 40 sessions must not count
    bars = series([10.0] * 100, volumes=volumes)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.median_dollar_volume_60d == pytest.approx(100.0)


def test_fewer_than_60_sessions_has_no_dollar_volume():
    bars = series([10.0] * 59)
    out = price_inputs(bars, as_of=as_of_last(bars))
    assert out.median_dollar_volume_60d is None and "short_history:dollar_volume_60d" in out.flags


def test_history_sessions_counts_bars_on_or_before_as_of():
    bars = linear(120)
    assert price_inputs(bars, as_of=as_of_last(bars)).history_sessions == 120


def test_trailing_sessions_below_the_minimum_price_are_counted():
    bars = series([20.0] * 10 + [4.0] * 25)
    assert price_inputs(bars, as_of=as_of_last(bars)).sessions_below_min_price == 25


def test_one_session_at_or_above_the_minimum_resets_the_below_price_count():
    bars = series([4.0] * 25 + [5.0] + [4.0] * 3)  # exactly 5.0 is not below the minimum
    assert price_inputs(bars, as_of=as_of_last(bars)).sessions_below_min_price == 3


def test_no_bars_gives_no_figures():
    out = price_inputs([], as_of=START)
    assert out.history_sessions == 0 and out.last_price is None and out.last_session is None
    assert out.momentum_12m is None and out.volatility_1y is None and out.median_dollar_volume_60d is None
    assert out.sessions_below_min_price == 0


# --- load_price_history ----------------------------------------------------------------------------------


HEADER = "ticker,date,open,high,low,close,volume,vwap,n\n"


def csv_text(*rows):
    return HEADER + "".join(rows)


def row(ticker="AAA", day="2025-01-02", close="10.5", volume="1000"):
    return f"{ticker},{day},10,11,9,{close},{volume},10.4,30\n"


def load(text, **kwargs):
    return load_price_history(io.StringIO(text), **kwargs)


def test_rows_become_ascending_bars_per_uppercased_ticker():
    text = csv_text(row("aaa", "2025-01-03", "11"), row("AAA", "2025-01-02", "10"), row("BBB", "2025-01-02", "5"))
    loaded = load(text)
    assert sorted(loaded) == ["AAA", "BBB"]
    assert [b.date.isoformat() for b in loaded["AAA"]] == ["2025-01-02", "2025-01-03"]  # sorted, though given reversed
    assert loaded["AAA"][0] == Bar(date=date(2025, 1, 2), close=10.0, volume=1000.0)


def test_exact_duplicate_rows_collapse_like_the_sample_files_doubled_rows():
    loaded = load(csv_text(row(), row()))
    assert len(loaded["AAA"]) == 1


def test_a_conflicting_duplicate_is_refused_and_named():
    with pytest.raises(ValueError, match="AAA.*2025-01-02"):
        load(csv_text(row(close="10.5"), row(close="10.6")))


def test_only_the_requested_tickers_are_returned():
    loaded = load(csv_text(row("AAA"), row("BBB")), tickers=["bbb"])
    assert list(loaded) == ["BBB"]


def test_a_header_only_file_is_an_empty_archive():
    assert load(HEADER) == {}


@pytest.mark.parametrize("missing", ["ticker", "date", "close", "volume"])
def test_a_missing_required_column_is_refused(missing):
    header = ",".join(c for c in HEADER.strip().split(",") if c != missing) + "\n"
    with pytest.raises(ValueError, match=missing):
        load(header)


@pytest.mark.parametrize(
    "bad_row",
    [
        pytest.param(row(close="abc"), id="close_not_a_number"),
        pytest.param(row(close="0"), id="close_not_positive"),
        pytest.param(row(close="-3"), id="close_negative"),
        pytest.param(row(close="nan"), id="close_nan"),
        pytest.param(row(close="inf"), id="close_infinite"),
        pytest.param(row(volume="-1"), id="volume_negative"),
        pytest.param(row(volume="x"), id="volume_not_a_number"),
        pytest.param(row(day="not-a-date"), id="bad_date"),
        pytest.param(row(ticker=""), id="blank_ticker"),
    ],
)
def test_a_malformed_row_is_refused_with_its_line_number_and_never_echoed(bad_row):
    with pytest.raises(ValueError, match=r"line 3") as excinfo:
        load(csv_text(row(day="2025-01-01"), bad_row))
    assert "not-a-date" not in str(excinfo.value) and "abc" not in str(excinfo.value)


# --- against the repo's sample file ----------------------------------------------------------------------


def test_the_repo_sample_file_loads_and_scores():
    sample = REPO_ROOT / "historical_stock_data.csv"
    if not sample.exists():
        pytest.skip("the sample archive is not in this checkout")
    loaded = load_price_history(sample)
    assert len(loaded) == 10 and len(loaded["AAPL"]) == 500  # 5 tickers carry doubled rows, collapsed on load
    bars = loaded["AAPL"]
    out = price_inputs(bars, as_of=bars[-1].date)
    assert out.history_sessions == 500
    assert out.last_session == bars[-1].date
    assert out.median_dollar_volume_60d is not None and out.median_dollar_volume_60d > 0
    assert out.momentum_12m is not None or "possible_unadjusted_split" in out.flags
    assert price_inputs(loaded["AA"], as_of=loaded["AA"][-1].date).history_sessions == 1  # a single-row ticker
