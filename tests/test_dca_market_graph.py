"""Tests for dca_market_graph -- loaders, normalization, seriation, and HTML emission.

Two environmental facts shape almost every fixture here: the per-ticker archive
(``ticker_data/``) and the artifact tree (``cron_tracking/``) are gitignored and do not
exist in a checkout, so anything touching them must synthesize its own directory under
``tmp_path`` and point the relevant environment variable at it. The one real data file
that IS committed -- ``historical_stock_data.csv`` -- gets a smoke test, because it is
the CLI's default source and a silent schema change there would break the default path.
"""

from __future__ import annotations

import json
import math
import random
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

import dca_market_graph as mg
from near_close_dca import Bar

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_bars(count=40, start=100.0, drift=0.001, seed=1, volume=1_000_000.0):
    """A deterministic synthetic price path, one bar per weekday."""
    rng = random.Random(seed)
    day = datetime(2024, 1, 1)
    price = start
    bars = []
    while len(bars) < count:
        if day.weekday() < 5:
            move = drift + rng.gauss(0, 0.012)
            open_ = price
            close = max(1.0, price * (1 + move))
            high = max(open_, close) * 1.004
            low = min(open_, close) * 0.996
            bars.append(Bar(timestamp=day, open=open_, high=high, low=low,
                            close=close, volume=volume * (1 + abs(move) * 8)))
            price = close
        day += timedelta(days=1)
    return tuple(bars)


def series(ticker="TEST", **kwargs):
    return mg.BarSeries(ticker=ticker, bars=make_bars(**kwargs), source="synthetic")


def factor_series(names_by_group, days=90, seed=11):
    """Series with a KNOWN cluster structure: each group shares a driving factor.

    Used to assert that seriation actually recovers structure rather than merely
    producing *some* deterministic order.
    """
    rng = random.Random(seed)
    calendar = []
    day = datetime(2024, 1, 1)
    while len(calendar) < days:
        if day.weekday() < 5:
            calendar.append(day)
        day += timedelta(days=1)

    factors = {group: [rng.gauss(0, 0.02) for _ in calendar] for group in names_by_group}
    out = []
    for group, names in names_by_group.items():
        for name in names:
            price = 100.0
            bars = []
            for i, when in enumerate(calendar):
                # Dominated by the shared factor, so within-group correlation is high
                # and cross-group correlation is near zero.
                move = factors[group][i] + rng.gauss(0, 0.003)
                open_ = price
                close = max(1.0, price * (1 + move))
                bars.append(Bar(timestamp=when, open=open_,
                                high=max(open_, close) * 1.003,
                                low=min(open_, close) * 0.997,
                                close=close, volume=1_000_000.0))
                price = close
            out.append(mg.BarSeries(ticker=name, bars=tuple(bars), source="synthetic"))
    return out


def write_archive(directory: Path, ticker: str, rows):
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits"]
    for row in rows:
        lines.append(",".join(str(v) for v in row) + ",0,0")
    (directory / f"{ticker}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2024-04-01", datetime(2024, 4, 1)),                              # date-only: midnight, untouched
        ("2024-04-01T15:30:00", datetime(2024, 4, 1, 19, 30)),             # naive+time: read as Eastern, converted
        ("2024-04-01T15:30:00Z", datetime(2024, 4, 1, 15, 30)),            # aware UTC: untouched
        ("2024-04-01T11:30:00-04:00", datetime(2024, 4, 1, 15, 30)),       # aware offset: converted to UTC
        (date(2024, 4, 1), datetime(2024, 4, 1)),
        ("", None),
        ("not-a-date", None),
        (None, None),
    ],
)
def test_parse_timestamp_accepts_every_spelling_the_archives_contain(value, expected):
    assert mg._parse_timestamp(value) == expected


def test_parse_timestamp_reads_a_naive_instant_as_eastern_not_as_utc():
    """A bare wall clock means the same thing everywhere in this repo.

    dca_live_adapters._to_utc_iso reads a naive datetime as Eastern; _parse_timestamp
    used to leave it untouched instead, which meant an intraday frame label could be
    printed with a "Z" that was not true of the value underneath it. Established here
    (not left ambiguous) so the "Z" the payload emits is a claim the data supports.
    """
    # January: EST, UTC-5. 14:00 Eastern -> 19:00 UTC.
    assert mg._parse_timestamp("2024-01-02T14:00:00") == datetime(2024, 1, 2, 19, 0)
    assert mg._parse_timestamp(datetime(2024, 1, 2, 14, 0)) == datetime(2024, 1, 2, 19, 0)
    # A naive datetime object at midnight is a DATE marker, not an instant -- shifting it
    # would invent a time nobody recorded and flip the payload's `intraday` flag.
    assert mg._parse_timestamp(datetime(2024, 1, 2, 0, 0)) == datetime(2024, 1, 2, 0, 0)
    # Already aware -- regardless of naive-Eastern being the convention for BARE
    # values -- is converted by its own offset, never reinterpreted as Eastern.
    assert mg._parse_timestamp("2024-01-02T14:00:00Z") == datetime(2024, 1, 2, 14, 0)


def test_parse_timestamp_returns_naive_utc_so_sorting_never_raises():
    aware = mg._parse_timestamp("2024-04-01T11:30:00-04:00")
    naive = mg._parse_timestamp("2024-04-01")
    assert aware is not None and naive is not None
    assert aware.tzinfo is None and naive.tzinfo is None
    assert sorted([aware, naive])  # would raise if one were tz-aware


@pytest.mark.parametrize("value", ["", "nope", None, float("nan"), float("inf"), True])
def test_parse_float_rejects_non_finite_and_non_numeric(value):
    assert mg._parse_float(value) is None


def test_bar_from_values_treats_zero_low_as_unknown_not_as_a_crash_to_zero():
    # dca_live_adapters stamps quote-derived prior-close bars with low=0.0 meaning
    # "unknown". Taking that literally would draw a wick down to -100%.
    bar = mg._bar_from_values("2024-04-01", 100, 0, 0, 101, 5)
    assert bar is not None
    assert bar.low == 100 and bar.high == 101


def test_bar_from_values_repairs_inverted_high_low():
    bar = mg._bar_from_values("2024-04-01", 100, 90, 110, 105, 5)
    assert bar is not None
    assert bar.low <= min(bar.open, bar.close)
    assert bar.high >= max(bar.open, bar.close)


@pytest.mark.parametrize("close", [0, -5, None, "x"])
def test_bar_from_values_rejects_rows_with_no_usable_close(close):
    assert mg._bar_from_values("2024-04-01", 1, 2, 0.5, close, 1) is None


def test_finalize_rejects_single_bar_series_because_there_is_no_prior_close():
    one = [Bar(timestamp=datetime(2024, 1, 1), open=1, high=1, low=1, close=1, volume=1)]
    assert mg._finalize("STUB", one, "test") is None


def test_finalize_sorts_and_dedupes_by_timestamp_last_write_wins():
    when = datetime(2024, 1, 2)
    bars = [
        Bar(timestamp=datetime(2024, 1, 3), open=3, high=3, low=3, close=3, volume=1),
        Bar(timestamp=when, open=1, high=1, low=1, close=1, volume=1),
        Bar(timestamp=when, open=2, high=2, low=2, close=2, volume=1),
    ]
    result = mg._finalize("T", bars, "test")
    assert result is not None
    assert [b.timestamp for b in result.bars] == [when, datetime(2024, 1, 3)]
    assert result.bars[0].close == 2


def test_bar_series_requires_a_ticker():
    with pytest.raises(ValueError):
        mg.BarSeries(ticker="", bars=make_bars(3))


# ---------------------------------------------------------------------------
# CombinedCsvLoader -- including against the real committed file
# ---------------------------------------------------------------------------


def test_combined_csv_loader_reads_the_committed_repository_file():
    loader = mg.CombinedCsvLoader()
    available = loader.available_tickers()
    assert "AAPL" in available, "the committed CSV's schema changed; the CLI default breaks"
    loaded = loader.load(["AAPL", "MSFT"])
    assert {s.ticker for s in loaded} == {"AAPL", "MSFT"}
    assert all(len(s.bars) > 100 for s in loaded)


def test_combined_csv_loader_drops_the_single_row_stub_tickers():
    # AA / AACB / AACG / AACI / AACT carry exactly one row each, so they have no prior
    # close and no computable channel. They must not reach the payload.
    loaded = mg.CombinedCsvLoader().load(["AA", "AACB", "AACG", "AACI", "AACT"])
    assert loaded == []


def test_combined_csv_loader_honours_a_date_window():
    loaded = mg.CombinedCsvLoader().load(
        ["AAPL"], start=date(2025, 1, 1), end=date(2025, 3, 31)
    )
    days = [b.timestamp.date() for b in loaded[0].bars]
    assert max(days) <= date(2025, 3, 31)
    # Exactly ONE bar before the window survives, as the prior-close baseline.
    assert len([d for d in days if d < date(2025, 1, 1)]) == 1


def test_window_keeps_one_pre_start_bar_as_the_prior_close_baseline():
    bars = make_bars(30)
    cut = bars[10].timestamp.date()
    kept = mg._window(bars, cut, None)
    assert kept[0].timestamp.date() < cut
    assert kept[1].timestamp.date() == cut
    assert len([b for b in kept if b.timestamp.date() < cut]) == 1


def test_window_without_a_start_keeps_everything_up_to_end():
    bars = make_bars(20)
    assert mg._window(bars, None, None) == list(bars)
    end = bars[5].timestamp.date()
    assert all(b.timestamp.date() <= end for b in mg._window(bars, None, end))


def test_the_baseline_bar_is_not_drawn_as_a_frame():
    """It is a calculation input, not a session anyone asked to see."""
    loaded = mg.CombinedCsvLoader().load(
        ["AAPL"], start=date(2025, 1, 2), end=date(2025, 3, 31)
    )
    payload = mg.build_payload(loaded, frame_start=date(2025, 1, 2))
    assert min(payload.dates) >= "2025-01-02"
    # ...and because the baseline supplied a prior close, the FIRST rendered session
    # carries real values instead of the null it would otherwise be stuck with.
    assert payload.series[0]["zc"][0] is not None


def _loud(bars, index, factor=10_000):
    """Same bars, with one bar's volume made absurd."""
    out = list(bars)
    bar = out[index]
    out[index] = Bar(timestamp=bar.timestamp, open=bar.open, high=bar.high,
                     low=bar.low, close=bar.close, volume=bar.volume * factor)
    return tuple(out)


@pytest.mark.parametrize("whole_market", [True, False])
@pytest.mark.parametrize("reason", ["pre-window-baseline", "no-prior-close"])
def test_an_undrawn_bar_never_moves_the_volume_axis(whole_market, reason):
    """Volume normalization is a function of the DRAWN bars alone.

    Stated as the property rather than as one exclusion reason, because fixing it one
    reason at a time is what let it recur. There are exactly two ways a bar can be
    present but never drawn, and both used to feed the per-ticker log min/max and the
    whole-market mean:

    ``pre-window-baseline``
        the bar ``--start`` retains solely to give the first rendered session a prior
        close. Fixed once already, by excluding bars outside ``frame_index``.

    ``no-prior-close``
        every series' FIRST bar, which is inside the window and so survived that fix,
        yet has no preceding close and is therefore always null. One bar per ticker on
        every render -- not an edge case -- so a first-day volume outlier skewed the
        whole scene by default.

    The assertion is the same either way: make the undrawn bar's volume absurd and the
    drawn glyphs must not move at all.
    """
    bars = make_bars(30)
    if reason == "pre-window-baseline":
        cut = bars[10].timestamp.date()
        quiet = mg.BarSeries("T", tuple(mg._window(bars, cut, None)), source="s")
        shouted = mg.BarSeries("T", tuple(mg._window(_loud(bars, 9), cut, None)), source="s")
    else:
        cut = None
        quiet = mg.BarSeries("T", bars, source="s")
        shouted = mg.BarSeries("T", _loud(bars, 0), source="s")

    left = mg.build_payload([quiet], whole_market=whole_market, frame_start=cut)
    right = mg.build_payload([shouted], whole_market=whole_market, frame_start=cut)

    assert left.dates == right.dates
    assert left.series[0]["x"] == right.series[0]["x"]
    # And the undrawn bar really is undrawn, so this is not passing vacuously: it is
    # absent from the axis entirely, in both shapes. (It used to sit there as a null
    # frame in the no-prior-close case -- see
    # test_a_frame_no_series_can_draw_is_not_on_the_axis.)
    assert len(left.dates) == len(quiet.bars) - 1
    assert None not in left.series[0]["x"]


def test_a_baseline_supplies_a_prior_close_the_first_session_would_otherwise_lack():
    """The other half of the baseline's job, unchanged by the volume rule above."""
    bars = make_bars(30)
    cut = bars[10].timestamp.date()
    with_baseline = mg.BarSeries("T", tuple(mg._window(bars, cut, None)), source="s")
    without = mg.BarSeries("T", tuple(b for b in bars if b.timestamp.date() >= cut), source="s")

    left = mg.build_payload([with_baseline], frame_start=cut)
    right = mg.build_payload([without], frame_start=cut)

    # With the baseline the window's opening session has a prior close and renders. It
    # is the first frame on the axis, and it is not null.
    assert left.dates[0] == cut.isoformat()
    assert left.series[0]["x"][0] is not None

    # Without it, that session cannot be drawn -- and is now simply absent from the axis
    # rather than present as a blank frame. One session shorter, starting a day later.
    assert right.dates[0] != cut.isoformat()
    assert len(right.dates) == len(left.dates) - 1
    assert None not in right.series[0]["x"]


def test_a_single_session_window_still_resolves():
    """Without the baseline bar this returned nothing at all."""
    loaded = mg.CombinedCsvLoader().load(
        ["AAPL"], start=date(2025, 3, 3), end=date(2025, 3, 3)
    )
    assert loaded, "a one-session window must still resolve using the preceding close"
    payload = mg.build_payload(loaded, frame_start=date(2025, 3, 3))
    assert len(payload.dates) == 1
    assert payload.series[0]["zc"][0] is not None


def test_combined_csv_loader_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        mg.CombinedCsvLoader(tmp_path / "nope.csv").load()
    assert mg.CombinedCsvLoader(tmp_path / "nope.csv").available_tickers() == []


def test_combined_csv_loader_skips_unparseable_rows_rather_than_raising(tmp_path):
    path = tmp_path / "combined.csv"
    path.write_text(
        "ticker,date,open,high,low,close,volume,vwap,n\n"
        "GOOD,2024-01-01,10,11,9,10.5,100,10,1\n"
        "GOOD,2024-01-02,10.5,12,10,11.5,100,11,1\n"
        "GOOD,nonsense,1,1,1,1,1,1,1\n"
        ",2024-01-03,1,1,1,1,1,1,1\n",
        encoding="utf-8",
    )
    loaded = mg.CombinedCsvLoader(path).load()
    assert len(loaded) == 1 and len(loaded[0].bars) == 2


# ---------------------------------------------------------------------------
# TickerArchiveLoader
# ---------------------------------------------------------------------------


def test_ticker_archive_loader_reads_yahoo_flavoured_csvs(tmp_path):
    write_archive(tmp_path, "AMD", [
        ("2024-01-01", 10, 11, 9, 10.5, 10.5, 1000),
        ("2024-01-02", 10.5, 12, 10, 11.5, 11.5, 2000),
    ])
    loaded = mg.TickerArchiveLoader(tmp_path).load()
    assert len(loaded) == 1 and loaded[0].ticker == "AMD"
    assert loaded[0].bars[1].volume == 2000


def test_ticker_archive_loader_applies_the_split_adjustment():
    """A 2-for-1 split must not read as a 50% overnight collapse.

    The archive stores RAW OHLC beside an ``Adj Close``. Taken literally, the session
    after a split shows prints at half the prior close -- a fabricated market-wide
    plunge that would set the shared percentile scale for the entire scene.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        # Flat 100 throughout in adjusted terms; a 2:1 split halves the raw prices
        # partway through, and Adj Close carries the correction for the pre-split rows.
        rows = []
        for day in range(1, 6):          # pre-split: raw 100, adjusted 50
            rows.append((f"2024-01-0{day}", 100, 100, 100, 100, 50, 1000))
        for day in range(6, 10):         # post-split: raw 50, adjusted 50
            rows.append((f"2024-01-0{day}", 50, 50, 50, 50, 50, 1000))
        write_archive(directory, "SPLT", rows)

        loaded = mg.TickerArchiveLoader(directory).load()
        closes = [b.close for b in loaded[0].bars]
        assert all(c == pytest.approx(50.0) for c in closes), closes

        # ...and therefore nothing moves in the rendered payload.
        payload = mg.build_payload(loaded)
        moves = [v for v in payload.series[0]["zc"] if v is not None]
        assert all(abs(v) < 1e-6 for v in moves), moves


def test_split_adjustment_is_skipped_when_the_ratio_is_unusable():
    bar = Bar(timestamp=datetime(2024, 1, 1), open=10, high=11, low=9, close=10, volume=5)
    for raw, adjusted in (("0", "5"), ("10", "0"), ("", "5"), ("10", ""), ("x", "y")):
        assert mg._split_adjusted(bar, raw, adjusted) == bar
    # A real ratio rescales all four prices and leaves volume (a share count) alone.
    adjusted_bar = mg._split_adjusted(bar, "10", "5")
    assert (adjusted_bar.open, adjusted_bar.close) == (5.0, 5.0)
    assert adjusted_bar.volume == 5


def test_ticker_archive_loader_reads_a_lowercase_archive_file(tmp_path):
    # Discovery uppercases the stem; load() must still open the real filename.
    write_archive(tmp_path, "amd", [
        ("2024-01-01", 10, 11, 9, 10.5, 10.5, 1000),
        ("2024-01-02", 10.5, 12, 10, 11.5, 11.5, 2000),
    ])
    loader = mg.TickerArchiveLoader(tmp_path)
    assert loader.available_tickers() == ["AMD"]
    assert [s.ticker for s in loader.load()] == ["AMD"]


def test_ticker_archive_loader_skips_underscore_prefixed_archive_metadata(tmp_path):
    write_archive(tmp_path, "AMD", [("2024-01-01", 1, 1, 1, 1, 1, 1), ("2024-01-02", 1, 1, 1, 1, 1, 1)])
    write_archive(tmp_path, "_delisted", [("2024-01-01", 1, 1, 1, 1, 1, 1)])
    assert mg.TickerArchiveLoader(tmp_path).available_tickers() == ["AMD"]


def test_ticker_archive_loader_ignores_non_ticker_csv_stems(tmp_path):
    """An archive accumulates auxiliary files beside the real per-ticker history.

    `AAPL-backup.csv` used to load as a ticker literally named AAPL-BACKUP, duplicating
    AAPL's history into the market centroid and handing the correlation grouping a pair
    with a meaningless perfect 1.0. Filtered through the same ^[A-Z.]{1,6}$ pattern
    ml-forecaster/features.py::discover_available_tickers already uses for its
    archive-wide branch, so the archive means the same thing to both readers of it.
    """
    write_archive(tmp_path, "AMD", [("2024-01-01", 1, 1, 1, 1, 1, 1), ("2024-01-02", 1, 1, 1, 1, 1, 1)])
    write_archive(tmp_path, "AAPL-backup", [("2024-01-01", 1, 1, 1, 1, 1, 1)])
    write_archive(tmp_path, "portfolio-summary", [("2024-01-01", 1, 1, 1, 1, 1, 1)])
    write_archive(tmp_path, "BRK.B", [("2024-01-01", 1, 1, 1, 1, 1, 1), ("2024-01-02", 1, 1, 1, 1, 1, 1)])
    assert mg.TickerArchiveLoader(tmp_path).available_tickers() == ["AMD", "BRK.B"]


def test_ticker_archive_loader_uses_the_configured_history_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(tmp_path))
    write_archive(tmp_path, "NVDA", [("2024-01-01", 1, 2, 1, 1.5, 1.5, 10), ("2024-01-02", 1.5, 2, 1, 1.8, 1.8, 10)])
    assert mg.TickerArchiveLoader().available_tickers() == ["NVDA"]


def test_ticker_archive_loader_missing_dir_names_the_env_var(tmp_path):
    loader = mg.TickerArchiveLoader(tmp_path / "absent")
    assert loader.available_tickers() == []
    with pytest.raises(FileNotFoundError, match="DCA_TICKER_HISTORY_DIR"):
        loader.load()


# ---------------------------------------------------------------------------
# IntradayArchiveLoader
# ---------------------------------------------------------------------------


def _write_intraday(directory: Path, ticker: str, rows):
    import csv as _csv

    from dca_intraday_archive import OBSERVATION_FIELDS

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{ticker}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = _csv.DictWriter(handle, fieldnames=list(OBSERVATION_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in OBSERVATION_FIELDS})


def test_intraday_loader_reads_bar_columns(tmp_path):
    _write_intraday(tmp_path, "AMD", [
        {"bar_timestamp": "2024-01-02T15:30:00Z", "bar_open": "10", "bar_high": "11",
         "bar_low": "9", "bar_close": "10.5", "bar_volume": "500"},
        {"bar_timestamp": "2024-01-02T16:30:00Z", "bar_open": "10.5", "bar_high": "12",
         "bar_low": "10", "bar_close": "11.5", "bar_volume": "700"},
    ])
    loaded = mg.IntradayArchiveLoader(tmp_path).load()
    assert len(loaded) == 1 and len(loaded[0].bars) == 2
    assert loaded[0].bars[0].volume == 500


def test_intraday_loader_falls_back_to_price_when_bar_columns_are_absent(tmp_path):
    # Rows archived before the bar_* columns existed still carry the decided price.
    _write_intraday(tmp_path, "AMD", [
        {"observed_at_utc": "2024-01-02T15:30:00Z", "price": "10"},
        {"observed_at_utc": "2024-01-02T16:30:00Z", "price": "11"},
    ])
    loaded = mg.IntradayArchiveLoader(tmp_path).load()
    assert [b.close for b in loaded[0].bars] == [10.0, 11.0]


def test_intraday_loader_handles_a_missing_directory(tmp_path):
    loader = mg.IntradayArchiveLoader(tmp_path / "absent")
    assert loader.available_tickers() == []
    assert loader.load() == []


def test_intraday_loader_uses_the_configured_archive_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(tmp_path))
    _write_intraday(tmp_path, "MU", [{"bar_timestamp": "2024-01-02T15:30:00Z", "bar_close": "10"}])
    assert mg.IntradayArchiveLoader().available_tickers() == ["MU"]


# ---------------------------------------------------------------------------
# ProviderLoader and the frozen replay path
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal MarketDataProvider stand-in."""

    def __init__(self, bars_by_ticker):
        self._bars = bars_by_ticker
        self.calls = []

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        self.calls.append((ticker, interval_minutes))
        return self._bars.get(ticker, [])


def test_provider_loader_wraps_any_market_data_provider():
    provider = _StubProvider({"AMD": list(make_bars(10))})
    loaded = mg.ProviderLoader(provider, tickers=["AMD"], interval_minutes=30).load()
    assert len(loaded) == 1 and loaded[0].ticker == "AMD"
    assert provider.calls == [("AMD", 30)]
    assert mg.ProviderLoader(provider, tickers=["AMD"]).available_tickers() == ["AMD"]


def test_provider_bars_are_sanitized_not_trusted():
    """A provider's own parsing is not a substitute for ours.

    RobinhoodDataProvider legitimately emits bars with zero open/high/low -- a
    quote-derived prior close stamps low=0.0 to mean "unknown". Passed through, that
    reads as a ~-100% move and sets the shared percentile scale for the whole scene.
    """
    dirty = [
        Bar(timestamp=datetime(2024, 1, 1) + timedelta(days=i), open=0.0, high=0.0,
            low=0.0, close=100.0 + i, volume=10.0)
        for i in range(6)
    ]
    loaded = mg.ProviderLoader(_StubProvider({"AMD": dirty}), tickers=["AMD"]).load()
    assert all(b.low > 0 and b.open > 0 and b.high > 0 for b in loaded[0].bars)

    payload = mg.build_payload(loaded)
    moves = [v for v in payload.series[0]["yl"] if v is not None]
    # Nothing anywhere near a fabricated -100%; the scale stays sane.
    assert max(abs(v) for v in moves) * payload.scales["pct_per_unit"] < 0.5


def test_provider_loader_drops_bars_with_no_usable_close():
    unusable = [
        Bar(timestamp=datetime(2024, 1, 1), open=1, high=1, low=1, close=0.0, volume=1),
        Bar(timestamp=datetime(2024, 1, 2), open=1, high=1, low=1, close=10.0, volume=1),
        Bar(timestamp=datetime(2024, 1, 3), open=1, high=1, low=1, close=11.0, volume=1),
    ]
    loaded = mg.ProviderLoader(_StubProvider({"AMD": unusable}), tickers=["AMD"]).load()
    assert len(loaded[0].bars) == 2


def test_provider_window_is_timezone_aware_so_the_host_clock_cannot_shift_it():
    """dca_live_adapters._to_utc_iso treats a NAIVE datetime as Eastern.

    A bare datetime.now() would therefore hand it the host's wall clock to be read as
    Eastern -- a Pacific host silently truncating three hours of history, a UTC host
    requesting hours into the future. The window a provider sees must not depend on
    where the process runs.
    """
    provider = _StubProvider({"AMD": list(make_bars(6))})
    captured = {}
    original = provider.get_intraday_bars

    def capture(ticker, start, end, interval_minutes):
        captured["start"], captured["end"] = start, end
        return original(ticker, start, end, interval_minutes)

    provider.get_intraday_bars = capture  # type: ignore[assignment]
    mg.ProviderLoader(provider, tickers=["AMD"]).load()

    assert captured["start"].tzinfo is not None
    assert captured["end"].tzinfo is not None
    # And within a minute of the real Eastern "now", not the host's local reading.
    delta = abs((captured["end"] - datetime.now(mg.EASTERN)).total_seconds())
    assert delta < 60, delta


def test_explicit_provider_window_dates_are_also_eastern():
    provider = _StubProvider({"AMD": list(make_bars(6))})
    captured = {}

    def capture(ticker, start, end, interval_minutes):
        captured["start"], captured["end"] = start, end
        return []

    provider.get_intraday_bars = capture  # type: ignore[assignment]
    mg.ProviderLoader(provider, tickers=["AMD"]).load(
        start=date(2024, 3, 1), end=date(2024, 3, 5)
    )
    assert captured["start"].tzinfo is not None and captured["end"].tzinfo is not None
    assert captured["start"].date() == date(2024, 3, 1)
    assert captured["end"].date() == date(2024, 3, 5)
    # Through the END of the requested day. 23:59 silently excluded the final minute --
    # no equity feed stamps a bar there, but the bound should mean what it says rather
    # than be right by accident of market hours.
    assert captured["end"].time() == time.max
    assert captured["start"].time() == time.min


def test_provider_loader_requires_an_explicit_roster():
    # A provider answers per-symbol queries; it cannot enumerate a universe.
    with pytest.raises(ValueError, match="explicit ticker list"):
        mg.ProviderLoader(_StubProvider({})).load()


def test_normalize_roster_keeps_an_explicit_empty_request_empty():
    assert mg.normalize_roster(None) is None
    assert mg.normalize_roster(None, fallback=["AMD"]) == ["AMD"]
    assert mg.normalize_roster([" amd ", "", "nvda"]) == ["AMD", "NVDA"]
    # The whole point: [] does NOT fall back. "Asked for nothing" and "did not filter"
    # are opposites, and the truthiness form conflates them.
    assert mg.normalize_roster([], fallback=["AMD"]) == []
    assert mg.normalize_roster(["", "  "], fallback=["AMD"]) == []


def _load_with_roster(roster, run_dir):
    """The three optional-roster call sites, each reduced to "what did it ask for?"."""
    provider = _StubProvider({"AMD": list(make_bars(6)), "NVDA": list(make_bars(6))})
    return {
        "ProviderLoader.load": lambda: (
            mg.ProviderLoader(provider, tickers=["AMD", "NVDA"]).load(roster)
        ),
        "ProviderLoader.__init__": lambda: (
            mg.ProviderLoader(provider, tickers=roster).load()
        ),
        "provider_loader_from_run_dir": lambda: (
            mg.provider_loader_from_run_dir(run_dir, tickers=roster).load()
        ),
    }


@pytest.mark.parametrize(
    "site",
    ["ProviderLoader.load", "ProviderLoader.__init__", "provider_loader_from_run_dir"],
)
def test_an_explicit_empty_roster_is_refused_at_every_site(site, tmp_path):
    """Parameterized over ALL THREE sites deliberately, not just the reported one.

    ``tickers or self._tickers`` was written three separate times in this module. Each
    round a reviewer found one, it was fixed alone, and the next round found the next --
    so the test asserts the property across every site that carries an optional roster,
    and a fourth site added without routing through normalize_roster will fail here.
    """
    run_dir = _write_run_dir(tmp_path / "run")
    with pytest.raises(ValueError, match="explicit ticker list"):
        _load_with_roster([], run_dir)[site]()


@pytest.mark.parametrize("site", ["ProviderLoader.load", "provider_loader_from_run_dir"])
def test_a_none_roster_still_falls_back_where_there_is_something_to_fall_back_to(site, tmp_path):
    """The other half of the property: `None` must NOT be refused like `[]` is.

    Only the two sites that have a fallback. ``ProviderLoader.__init__`` is excluded on
    purpose: a provider answers per-symbol queries and cannot enumerate a universe, so
    constructing one with no roster has nothing to widen INTO -- its `None` correctly
    reaches the same refusal, which
    test_provider_loader_requires_an_explicit_roster already asserts.
    """
    run_dir = _write_run_dir(tmp_path / "run")
    loaded = _load_with_roster(None, run_dir)[site]()
    assert [s.ticker for s in loaded]


def _write_run_dir(root: Path, symbol="AMD", interval="5minute", bars=6):
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(bars):
        entries.append({
            "begins_at": f"2024-01-02T1{i}:30:00Z",
            "open_price": "10", "high_price": "11", "low_price": "9",
            "close_price": str(10 + i * 0.1), "volume": "100",
        })
    raw.joinpath("historicals_batch1.json").write_text(
        json.dumps({"results": [{"symbol": symbol, "interval": interval, "bars": entries}]}),
        encoding="utf-8",
    )
    return root


def test_provider_loader_from_run_dir_replays_persisted_broker_responses(tmp_path):
    run_dir = _write_run_dir(tmp_path / "20260803_1230ET")
    loader = mg.provider_loader_from_run_dir(run_dir)
    assert loader.available_tickers() == ["AMD"]
    loaded = loader.load()
    assert len(loaded) == 1 and len(loaded[0].bars) == 6


def test_provider_loader_from_run_dir_keeps_an_empty_request_empty(tmp_path):
    """`--tickers @config` on an empty universe must refuse, not replay everything."""
    run_dir = _write_run_dir(tmp_path / "run")
    loader = mg.provider_loader_from_run_dir(run_dir, tickers=[])
    assert loader.available_tickers() == []
    with pytest.raises(ValueError, match="explicit ticker list"):
        loader.load()
    # ...while None still means "everything this run recorded".
    assert mg.provider_loader_from_run_dir(run_dir).available_tickers() == ["AMD"]


def test_provider_loader_from_run_dir_requires_persisted_responses(tmp_path):
    (tmp_path / "raw").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="historicals_batch"):
        mg.provider_loader_from_run_dir(tmp_path)


@pytest.mark.parametrize("depth", [1, 2])
def test_provider_loader_from_run_dir_unwraps_connector_envelopes(tmp_path, depth):
    """Persisted responses are not always bare {"results": [...]}.

    normalize_connector_payload embeds a resolved pointer under "data", and that can
    nest. run_dca unwraps those forms; a replay reader that does not would reject run
    directories holding exactly the archived inputs it exists to replay.
    """
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    inner = {"results": [{"symbol": "AMD", "interval": "5minute", "bars": [
        {"begins_at": f"2024-01-02T1{i}:30:00Z", "open_price": "10", "high_price": "11",
         "low_price": "9", "close_price": str(10 + i * 0.1), "volume": "100"}
        for i in range(5)
    ]}]}
    payload: dict[str, Any] = inner
    for _ in range(depth):
        payload = {"data": payload}
    raw.joinpath("historicals_batch1.json").write_text(json.dumps(payload), encoding="utf-8")

    loader = mg.provider_loader_from_run_dir(tmp_path)
    assert loader.available_tickers() == ["AMD"]
    assert len(loader.load()[0].bars) == 5


def test_unwrap_connector_envelope_survives_a_self_referential_payload():
    cyclic: dict = {}
    cyclic["data"] = cyclic
    assert mg._unwrap_connector_envelope(cyclic) is cyclic


def test_provider_loader_from_run_dir_ignores_unreadable_batches(tmp_path):
    run_dir = _write_run_dir(tmp_path / "run")
    (run_dir / "raw" / "historicals_batch2.json").write_text("{not json", encoding="utf-8")
    assert mg.provider_loader_from_run_dir(run_dir).available_tickers() == ["AMD"]


# ---------------------------------------------------------------------------
# resolve_loader
# ---------------------------------------------------------------------------


def test_resolve_loader_dispatches_each_source(tmp_path):
    assert isinstance(mg.resolve_loader("csv"), mg.CombinedCsvLoader)
    assert isinstance(mg.resolve_loader("archive", history_dir=tmp_path), mg.TickerArchiveLoader)
    assert isinstance(mg.resolve_loader("intraday", archive_dir=tmp_path), mg.IntradayArchiveLoader)


def test_resolve_loader_rejects_unknown_and_underspecified_sources():
    with pytest.raises(ValueError, match="unknown source"):
        mg.resolve_loader("bogus")
    with pytest.raises(ValueError, match="requires --run-dir"):
        mg.resolve_loader("run-dir")


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def test_percentile_interpolates_and_survives_degenerate_input():
    assert mg._percentile([], 50) == 0.0
    assert mg._percentile([7.0], 99) == 7.0
    assert mg._percentile([0.0, 10.0], 50) == pytest.approx(5.0)
    assert mg._percentile([0.0, 1.0, 2.0, 3.0], 100) == pytest.approx(3.0)


def test_stdev_and_mean_on_degenerate_input():
    assert mg._mean([]) == 0.0
    assert mg._stdev([1.0]) == 0.0
    assert mg._stdev([1.0, 3.0]) == pytest.approx(1.0)


def test_bar_closes_is_keyed_by_full_timestamp():
    bars = (
        Bar(timestamp=datetime(2024, 1, 1), open=1, high=1, low=1, close=100, volume=1),
        Bar(timestamp=datetime(2024, 1, 2), open=1, high=1, low=1, close=110, volume=1),
    )
    assert mg.bar_closes(mg.BarSeries("T", bars)) == {
        datetime(2024, 1, 1): 100, datetime(2024, 1, 2): 110,
    }


def test_bar_closes_keep_every_intraday_poll_not_one_per_day():
    # Keyed by full timestamp, several polls in one session must not collapse to one.
    bars = tuple(
        Bar(timestamp=datetime(2024, 1, 2, 10 + i), open=1, high=1, low=1,
            close=100 + i, volume=1)
        for i in range(4)
    )
    assert len(mg.bar_closes(mg.BarSeries("T", bars))) == 4


def test_correlation_of_a_series_with_itself_is_one():
    single = series("A", count=60)
    closes = mg.bar_closes(single)
    assert mg._correlation(closes, closes) == pytest.approx(1.0)


def test_correlation_treats_short_overlap_as_no_evidence():
    # Three shared days can produce a spurious 0.99. Rather than invent a sector,
    # short overlaps are reported as uncorrelated.
    left = {datetime(2024, 1, i + 1): 100.0 + i for i in range(5)}
    right = {datetime(2024, 1, i + 1): 200.0 + i * 2 for i in range(5)}
    assert mg._correlation(left, right) == 0.0


def test_correlation_aligns_returns_on_the_shared_axis_not_on_each_series_own_calendar():
    """The bug this round's fix targets, demonstrated rather than merely described.

    A return used to be computed against each series' OWN previous bar in isolation. At
    a shared timestamp, a series missing the bar just before it therefore contributed a
    multi-session return while a fully-populated peer contributed a one-session return
    at the very same nominal endpoint -- two different intervals paired as though they
    were the same observation. Removing an INTERIOR observation from one side of an
    otherwise-identical noisy pair must not measurably distort the correlation the way
    the old per-series-adjacency construction did.
    """
    days = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    rng = random.Random(3)
    factor = [rng.gauss(0, 0.02) for _ in days]
    left, right, dense = {}, {}, {}
    left_price = right_price = 100.0
    for i, d in enumerate(days):
        left_price *= 1 + factor[i] + rng.gauss(0, 0.002)
        right_price *= 1 + factor[i] + rng.gauss(0, 0.002)
        left[d] = left_price
        right[d] = right_price
        dense[d] = right_price
    gappy = {d: v for d, v in right.items() if d not in (days[10], days[25], days[40])}

    aligned = mg._correlation(left, gappy)
    reference = mg._correlation(left, dense)   # the same pair, no gap at all
    # Reconstruct the OLD per-series-adjacency correlation to prove the new one is
    # closer to the gap-free reference -- not merely that some number came out.
    def old_style_returns(closes_by_timestamp):
        out, previous = {}, None
        for ts in sorted(closes_by_timestamp):
            close = closes_by_timestamp[ts]
            if previous is not None and previous > 0:
                out[ts] = close / previous - 1.0
            previous = close
        return out

    def old_style_correlation(left_returns, right_returns):
        shared = sorted(set(left_returns) & set(right_returns))
        xs = [left_returns[d] for d in shared]
        ys = [right_returns[d] for d in shared]
        x_mean, y_mean = mg._mean(xs), mg._mean(ys)
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
        x_ss = sum((x - x_mean) ** 2 for x in xs)
        y_ss = sum((y - y_mean) ** 2 for y in ys)
        return numerator / math.sqrt(x_ss * y_ss)

    misaligned = old_style_correlation(old_style_returns(left), old_style_returns(gappy))
    assert abs(aligned - reference) < abs(misaligned - reference)


def test_correlation_overlap_is_counted_in_days_not_bars():
    """MIN_OVERLAP_DAYS means DAYS, including when the source is intraday.

    bar_closes() keys by full timestamp so intraday sources contribute every observation
    to the correlation itself. Counting those raw entries against the threshold, though,
    would let a single morning of five-minute bars clear a gate written to reject short,
    noisy overlap -- and a few hours of shared drift would then order the whole ticker
    axis.
    """
    one_session = [datetime(2024, 1, 2, 9, 30) + timedelta(minutes=5 * i) for i in range(40)]
    left = {t: 100.0 + (i % 7) for i, t in enumerate(one_session)}
    right = {t: 200.0 + (i % 7) * 2 for i, t in enumerate(one_session)}
    # 40 shared observations, but one calendar day.
    assert len(set(left) & set(right)) > mg.MIN_OVERLAP_DAYS
    assert mg._correlation(left, right) == 0.0

    # ...while the same count spread over distinct days is real evidence and passes.
    # Scaling every close by a constant factor leaves every RETURN unchanged, so this
    # stays a clean corr=1.0 fixture without needing a second synthetic price path.
    spread = [datetime(2024, 1, 2) + timedelta(days=i) for i in range(40)]
    left_spread = {t: 100.0 + (i % 7) for i, t in enumerate(spread)}
    right_spread = {t: 3.0 * v for t, v in left_spread.items()}
    assert mg._correlation(left_spread, right_spread) == pytest.approx(1.0)


def test_correlation_of_a_flat_series_is_zero_not_nan():
    days = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(60)]
    flat = {d: 100.0 for d in days}
    moving = {d: 100.0 + (i % 3) for i, d in enumerate(days)}
    assert mg._correlation(flat, moving) == 0.0


def test_correlation_distance_is_non_negative_even_at_perfect_correlation():
    # A hair over 1.0 from floating-point error would otherwise sqrt a negative.
    assert mg._correlation_distance([[1.0000000002]])[0][0] >= 0.0


# ---------------------------------------------------------------------------
# Seriation and clustering
# ---------------------------------------------------------------------------


def test_seriation_places_known_groups_contiguously():
    groups = {
        "alpha": ["A1", "A2", "A3", "A4"],
        "beta": ["B1", "B2", "B3", "B4"],
        "gamma": ["G1", "G2", "G3", "G4"],
    }
    grouping = mg.seriate_tickers(factor_series(groups))
    order = list(grouping.order)
    for names in groups.values():
        positions = sorted(order.index(n) for n in names)
        assert positions == list(range(positions[0], positions[0] + len(names))), (
            f"group {names} was scattered across the ticker axis: {order}"
        )


def test_cluster_cut_recovers_the_known_groups():
    groups = {"alpha": ["A1", "A2", "A3"], "beta": ["B1", "B2", "B3"], "gamma": ["G1", "G2", "G3"]}
    grouping = mg.seriate_tickers(factor_series(groups), clusters=3)
    recovered = {frozenset(c["tickers"]) for c in grouping.clusters}
    assert recovered == {frozenset(v) for v in groups.values()}


def test_seriation_is_stable_under_input_reordering():
    groups = {"alpha": ["A1", "A2", "A3"], "beta": ["B1", "B2", "B3"]}
    base = factor_series(groups)
    shuffled = list(base)
    random.Random(4).shuffle(shuffled)
    assert set(mg.seriate_tickers(base).order) == set(mg.seriate_tickers(shuffled).order)
    # Group membership must be identical regardless of the order rows arrived in.
    def members(grouping):
        return {frozenset(c["tickers"]) for c in grouping.clusters}
    assert members(mg.seriate_tickers(base)) == members(mg.seriate_tickers(shuffled))


def test_seriation_is_deterministic_across_repeat_calls():
    data = factor_series({"alpha": ["A1", "A2"], "beta": ["B1", "B2"]})
    assert mg.seriate_tickers(data).order == mg.seriate_tickers(data).order


def test_seriation_degenerate_inputs():
    assert mg.seriate_tickers([]).order == ()
    assert mg.seriate_tickers([]).source == "none"
    one = mg.seriate_tickers([series("SOLO")])
    assert one.order == ("SOLO",) and one.clusters == ()


def test_clusters_are_presented_left_to_right_along_the_ticker_axis():
    grouping = mg.seriate_tickers(
        factor_series({"a": ["A1", "A2"], "b": ["B1", "B2"], "c": ["C1", "C2"]}), clusters=3
    )
    order = list(grouping.order)
    firsts = [min(order.index(t) for t in c["tickers"]) for c in grouping.clusters]
    assert firsts == sorted(firsts)
    assert [c["id"] for c in grouping.clusters] == list(range(len(grouping.clusters)))


def test_auto_cluster_count_stays_in_a_sane_band():
    assert mg._auto_cluster_count(1) == 1
    assert 2 <= mg._auto_cluster_count(53) <= 8
    assert mg._auto_cluster_count(5000) == 8


# ---------------------------------------------------------------------------
# grouping_from_mapping
# ---------------------------------------------------------------------------


def test_grouping_from_full_export_is_used_as_is():
    data = [series("AAA"), series("BBB", seed=2), series("CCC", seed=3)]
    grouping = mg.grouping_from_mapping(
        {
            "source": "ml-forecaster",
            "ticker_order": ["CCC", "AAA", "BBB"],
            "clusters": [
                {"id": 0, "label": "high vol", "tickers": ["CCC", "AAA"], "color": "#123456"},
                {"id": 1, "label": "calm", "tickers": ["BBB"], "color": "#654321"},
            ],
        },
        data,
    )
    assert grouping.order == ("CCC", "AAA", "BBB")
    assert grouping.source == "ml-forecaster"
    assert grouping.clusters[0]["label"] == "high vol"


def test_grouping_from_export_appends_unclaimed_tickers_rather_than_dropping_them():
    # Silently omitting a ticker from a whole-market picture would misrepresent it.
    data = [series("AAA"), series("BBB", seed=2)]
    grouping = mg.grouping_from_mapping(
        {"clusters": [{"id": 0, "label": "only", "tickers": ["AAA"], "color": "#111111"}]}, data
    )
    assert grouping.clusters[-1]["label"] == "ungrouped"
    assert grouping.clusters[-1]["tickers"] == ["BBB"]
    assert set(grouping.order) == {"AAA", "BBB"}


def test_a_supplied_grouping_places_every_ticker_exactly_once():
    """One invariant over the whole document, not one rule per field.

    A grouping file is external input and BOTH of its fields land on the ticker axis.
    Abusing them together is the test, because fixing `ticker_order` alone is what left
    `clusters` broken:

    * a repeat in ``ticker_order`` drew the same series twice in one Pulse scene and
      gave it two Wave rows, while the subtitle counted it once;
    * a ticker claimed by two ``clusters`` took the LATER band's colour (the viewer's
      ``clusterOf`` is last-write-wins) while BOTH legend buttons advertised it, so
      hiding the earlier band left it on screen in a colour matching neither button.
    """
    data = [series("AAA"), series("BBB", seed=2), series("CCC", seed=3)]
    grouping = mg.grouping_from_mapping(
        {
            "ticker_order": ["BBB", "AAA", "BBB", "CCC", "AAA"],
            "clusters": [
                {"id": 0, "label": "first", "tickers": ["AAA", "BBB", "AAA"], "color": "#333333"},
                {"id": 1, "label": "second", "tickers": ["BBB", "CCC"], "color": "#444444"},
            ],
        },
        data,
    )
    assert grouping.order == ("BBB", "AAA", "CCC")
    assert len(grouping.order) == len(set(grouping.order))

    memberships = [t for cluster in grouping.clusters for t in cluster["tickers"]]
    assert sorted(memberships) == ["AAA", "BBB", "CCC"]
    assert len(memberships) == len(set(memberships))
    # First claim wins, so BBB stays with the band that named it first.
    by_label = {c["label"]: c["tickers"] for c in grouping.clusters}
    assert sorted(by_label["first"]) == ["AAA", "BBB"]
    assert by_label["second"] == ["CCC"]


def test_a_cluster_emptied_by_deduplication_is_dropped():
    """An empty band would draw a legend row whose click hides nothing."""
    data = [series("AAA")]
    grouping = mg.grouping_from_mapping(
        {
            "clusters": [
                {"id": 0, "label": "keeps-it", "tickers": ["AAA"], "color": "#333333"},
                {"id": 1, "label": "duplicate-only", "tickers": ["AAA"], "color": "#444444"},
            ],
        },
        data,
    )
    assert [c["label"] for c in grouping.clusters] == ["keeps-it"]


def test_grouping_from_export_ignores_unknown_tickers_and_malformed_entries():
    data = [series("AAA")]
    grouping = mg.grouping_from_mapping(
        {"clusters": ["not-a-mapping", {"label": "empty", "tickers": ["NOPE"]},
                      {"label": "real", "tickers": ["AAA"], "color": "#222222"}]},
        data,
    )
    assert [c["label"] for c in grouping.clusters] == ["real"]


@pytest.mark.parametrize("key", ["AAA", "aaa", " AaA "])
def test_flat_grouping_keys_are_matched_case_insensitively(key):
    """A lowercase-keyed document matched nothing and grouped everything as "ungrouped".

    Ticker names are already uppercase by the time they reach here, so the old
    `mapping.get(name) or mapping.get(name.upper())` was the same lookup twice. Nothing
    forbids an external file from writing its keys in lowercase.
    """
    grouping = mg.grouping_from_mapping({key: "tech"}, [series("AAA")])
    assert [c["label"] for c in grouping.clusters] == ["tech"]
    assert grouping.clusters[0]["tickers"] == ["AAA"]


def test_flat_grouping_respects_the_seriation_cap_per_band(monkeypatch):
    """Supplying a grouping file is the documented way to AVOID the O(n^3) pass.

    `--all-tickers-in-archive --group-source file` resolves thousands of symbols, and a
    sparse mapping puts every unmentioned one in a single "ungrouped" band -- easily over
    the cap. The default path already refuses above it (seriate_or_refuse); this is the
    same cap applied per band here, since a document that avoided the correlation pass on
    purpose must not have it silently re-imposed on its largest band.
    """
    monkeypatch.setattr(mg, "MAX_SERIATION_TICKERS", 2)
    data = [series(f"T{i}", seed=i) for i in range(4)]   # one band, over the lowered cap
    grouping = mg.grouping_from_mapping({}, data)         # empty mapping -> all "ungrouped"
    assert grouping.clusters[0]["label"] == "ungrouped"
    # Order preserved (not seriated) once over the cap: the loader's own order, which is
    # `names`'s order here.
    assert grouping.clusters[0]["tickers"] == [s.ticker for s in data]

    # Under the cap, seriation still runs -- the guard must not become a blanket skip.
    monkeypatch.setattr(mg, "MAX_SERIATION_TICKERS", 400)
    small = factor_series({"a": ["A1", "A2", "A3"]})
    grouping = mg.grouping_from_mapping({}, small)
    assert grouping.clusters[0]["tickers"] == list(mg.seriate_tickers(small, clusters=1).order)


def test_grouping_from_flat_label_mapping_seriates_within_each_label():
    data = factor_series({"x": ["X1", "X2", "X3"], "y": ["Y1", "Y2", "Y3"]})
    grouping = mg.grouping_from_mapping(
        {"X1": "tech", "X2": "tech", "X3": "tech", "Y1": "bonds", "Y2": "bonds", "Y3": "bonds"},
        data,
    )
    assert grouping.source == "file"
    labels = [c["label"] for c in grouping.clusters]
    assert labels == ["bonds", "tech"]
    assert set(grouping.order) == {"X1", "X2", "X3", "Y1", "Y2", "Y3"}


def test_grouping_from_flat_mapping_defaults_missing_tickers_to_ungrouped():
    grouping = mg.grouping_from_mapping({"AAA": "tech"}, [series("AAA"), series("ZZZ", seed=9)])
    ungrouped = [c for c in grouping.clusters if c["label"] == "ungrouped"]
    assert ungrouped and ungrouped[0]["tickers"] == ["ZZZ"]


@pytest.mark.parametrize(
    "hostile",
    ["url(https://example.invalid/x)", "red; background-image: url(http://x)",
     "#12345", "#GGGGGG", "", None, 42],
)
def test_a_grouping_file_cannot_smuggle_a_css_value(hostile):
    """The colour lands in an inline `background:` declaration in the viewer.

    A grouping file is untrusted input, and `url(...)` is legal CSS that would fetch
    over the network -- making the self-contained guarantee false. Attribute-escaping
    does not help; restricting the grammar does.
    """
    data = [series("AAA")]
    grouping = mg.grouping_from_mapping(
        {"clusters": [{"label": "x", "tickers": ["AAA"], "color": hostile}]}, data
    )
    assert grouping.clusters[0]["color"] in mg.CLUSTER_COLORS


def test_a_valid_hex_colour_is_preserved():
    data = [series("AAA")]
    grouping = mg.grouping_from_mapping(
        {"clusters": [{"label": "x", "tickers": ["AAA"], "color": "#AbCdEf"}]}, data
    )
    assert grouping.clusters[0]["color"] == "#AbCdEf"


def test_safe_color_accepts_only_six_digit_hex():
    assert mg._safe_color("#001122", "#fallback") == "#001122"
    assert mg._safe_color("#abc", "#ffffff") == "#ffffff"


def test_load_groups_file_round_trip(tmp_path):
    path = tmp_path / "groups.json"
    path.write_text(json.dumps({"AAA": "tech"}), encoding="utf-8")
    assert mg.load_groups_file(path) == {"AAA": "tech"}
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        mg.load_groups_file(bad)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_payload_is_invariant_to_a_pure_price_rescale():
    """Every channel is a RATIO against the prior close, so a share split -- or any
    constant rescale of the whole price path -- must not move a single vertex."""
    base = series("T", count=60)
    scaled = mg.BarSeries(
        ticker="T",
        bars=tuple(
            Bar(timestamp=b.timestamp, open=b.open * 7, high=b.high * 7,
                low=b.low * 7, close=b.close * 7, volume=b.volume)
            for b in base.bars
        ),
        source=base.source,
    )
    left = mg.build_payload([base], generated_at="fixed")
    right = mg.build_payload([scaled], generated_at="fixed")
    assert mg.build_payload_json(left) == mg.build_payload_json(right)


def test_payload_build_is_deterministic():
    data = [series("A"), series("B", seed=5)]
    first = mg.build_payload(data, generated_at="fixed")
    second = mg.build_payload(data, generated_at="fixed")
    assert mg.build_payload_json(first) == mg.build_payload_json(second)


def test_payload_uses_a_shared_date_axis_with_nulls_for_untraded_days():
    # A ticker missing a session must emit null, not shift its later bars left --
    # that would desynchronize it from the rest of the market.
    long_bars = make_bars(10)
    short = mg.BarSeries("SHORT", tuple(b for i, b in enumerate(long_bars) if i != 4))
    payload = mg.build_payload([mg.BarSeries("LONG", long_bars), short])
    # Nine frames, not ten: the earliest bar has no prior close, so no series can draw
    # there and the axis does not carry a session nobody can see.
    assert len(payload.dates) == 9
    short_row = next(s for s in payload.series if s["ticker"] == "SHORT")
    assert short_row["zc"][3] is None          # the session SHORT skipped
    assert len(short_row["zc"]) == 9


def test_a_frame_no_series_can_draw_is_not_on_the_axis():
    """The first bar has no prior close, so it earns no frame at all.

    It used to earn a null one: a blank opening Pulse scene, a session count larger than
    the drawable data, and a span beginning before the first observation anyone can see.
    This is the third and last shape of "the priorless first bar leaks" -- it reached the
    volume mean (fixed once), then the per-ticker volume scale (fixed again), and was
    still creating the axis entry itself.
    """
    bars = make_bars(10)
    payload = mg.build_payload([mg.BarSeries("T", bars)])

    assert len(payload.dates) == 9
    assert payload.dates[0] == bars[1].timestamp.date().isoformat()
    assert payload.series[0]["zc"][0] is not None
    # No frame anywhere on the axis is null for every series.
    assert not [
        i for i in range(len(payload.dates))
        if all(s["zc"][i] is None for s in payload.series)
    ]


def test_a_series_that_can_never_draw_is_refused_with_its_own_message():
    """One bar and no prior close is not "an empty window" -- say which it is."""
    lone = mg.BarSeries("T", make_bars(1))
    with pytest.raises(ValueError, match="no bar has a prior close"):
        mg.build_payload([lone])


def test_whole_market_mode_appends_a_centroid_and_uses_relative_volume():
    payload = mg.build_payload([series("A"), series("B", seed=3)], whole_market=True)
    assert payload.tickers[-1] == mg.MARKET_SERIES
    assert payload.scales["volume"]["mode"] == "relative_log"


def test_single_stock_mode_omits_the_centroid_and_scales_volume_per_ticker():
    payload = mg.build_payload([series("A"), series("B", seed=3)], whole_market=False)
    assert mg.MARKET_SERIES not in payload.tickers
    assert payload.scales["volume"]["mode"] == "per_ticker_log"


def test_centroid_is_the_mean_of_the_visible_channels():
    payload = mg.build_payload([series("A"), series("B", seed=3)], whole_market=True)
    market = next(s for s in payload.series if s["ticker"] == mg.MARKET_SERIES)
    others = [s for s in payload.series if s["ticker"] != mg.MARKET_SERIES]
    frame = 5
    expected = sum(s["zc"][frame] for s in others) / len(others)
    assert market["zc"][frame] == pytest.approx(expected, abs=1e-3)


def test_volume_channel_is_confined_to_the_unit_interval():
    payload = mg.build_payload([series("A"), series("B", seed=3)], whole_market=True)
    values = [v for s in payload.series for v in s["x"] if v is not None]
    assert min(values) >= 0.0 and max(values) <= 1.0


def test_intraday_bars_get_one_frame_each_not_one_per_day():
    """Several polls in one session must not collapse onto a single frame.

    Keyed by calendar date, later polls overwrote earlier ones while the discarded
    observations still moved the shared scale -- defeating the whole point of the
    intraday source.
    """
    bars = tuple(
        Bar(timestamp=datetime(2024, 1, 2, 10 + i), open=100, high=101, low=99,
            close=100 + i, volume=1000)
        for i in range(5)
    )
    payload = mg.build_payload([mg.BarSeries("AMD", bars)])
    # Four frames from five polls: the earliest has no prior close, so it is not drawable
    # and earns no frame. The property under test is one frame PER POLL for the rest,
    # rather than all five collapsing onto one calendar day.
    assert len(payload.dates) == 4
    assert payload.meta["intraday"] is True
    assert payload.dates[0].startswith("2024-01-02 11:")


def test_daily_bars_keep_plain_date_labels():
    payload = mg.build_payload([series("A", count=6)])
    assert payload.meta["intraday"] is False
    assert all(len(d) == 10 for d in payload.dates)


def test_unknown_volume_stays_null_rather_than_reading_as_average():
    """Zero volume means "not reported"; 0.5 on the axis means "exactly average"."""
    bars = tuple(
        Bar(timestamp=datetime(2024, 1, 1) + timedelta(days=i), open=10, high=11,
            low=9, close=10 + (i % 3), volume=0.0)
        for i in range(6)
    )
    for whole_market in (True, False):
        payload = mg.build_payload([mg.BarSeries("NOVOL", bars)], whole_market=whole_market)
        assert all(v is None for v in payload.series[0]["x"]), whole_market


def test_flat_volume_maps_to_the_axis_centre_rather_than_dividing_by_zero():
    bars = tuple(
        Bar(timestamp=datetime(2024, 1, 1) + timedelta(days=i), open=10, high=10,
            low=10, close=10, volume=1000.0)
        for i in range(5)
    )
    payload = mg.build_payload([mg.BarSeries("FLAT", bars)])
    assert payload.series[0]["x"][1] == pytest.approx(0.5)


def test_a_motionless_window_does_not_explode_the_scale():
    bars = tuple(
        Bar(timestamp=datetime(2024, 1, 1) + timedelta(days=i), open=10, high=10,
            low=10, close=10, volume=1000.0)
        for i in range(5)
    )
    payload = mg.build_payload([mg.BarSeries("FLAT", bars)])
    assert payload.scales["pct_per_unit"] >= mg._MIN_SCALE


def test_grouping_reorders_the_emitted_series():
    data = [series("AAA"), series("BBB", seed=2), series("CCC", seed=3)]
    grouping = mg.TickerGrouping(order=("CCC", "AAA", "BBB"), source="test")
    payload = mg.build_payload(data, grouping=grouping)
    assert payload.tickers == ("CCC", "AAA", "BBB")


def test_build_payload_refuses_an_empty_universe():
    with pytest.raises(ValueError, match="no usable series"):
        mg.build_payload([])


def test_payload_records_the_grouping_source_so_a_thin_run_is_visible():
    payload = mg.build_payload([series("A")])
    assert payload.grouping.source == "none"


# ---------------------------------------------------------------------------
# Serialization + schema
# ---------------------------------------------------------------------------


def test_payload_validates_against_the_published_schema():
    jsonschema = pytest.importorskip("jsonschema")
    data = factor_series({"a": ["A1", "A2"], "b": ["B1", "B2"]})
    payload = mg.build_payload(
        data, whole_market=True, grouping=mg.seriate_tickers(data), source="synthetic"
    )
    schema = json.loads((REPO_ROOT / "schemas" / "market_graph_payload.schema.json").read_text())
    jsonschema.validate(json.loads(mg.build_payload_json(payload)), schema)


def test_payload_to_dict_is_json_serializable_without_a_custom_encoder():
    payload = mg.build_payload([series("A")])
    json.dumps(mg.payload_to_dict(payload))


# ---------------------------------------------------------------------------
# Asset resolution + HTML emission
# ---------------------------------------------------------------------------


def test_viewer_asset_dir_prefers_the_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv(mg.VIZ_DIR_ENV, str(tmp_path))
    assert mg.viewer_asset_dir() == tmp_path


def test_viewer_asset_dir_finds_the_checkout_directory(monkeypatch):
    monkeypatch.delenv(mg.VIZ_DIR_ENV, raising=False)
    assert mg.viewer_asset_dir() == REPO_ROOT / "viz"


def test_viewer_asset_dir_finds_the_installed_wheel_layout(tmp_path, monkeypatch):
    installed = tmp_path / "share-viz"
    installed.mkdir()
    monkeypatch.setenv(mg.VIZ_DIR_ENV, "")
    monkeypatch.setattr(mg, "_INSTALLED_VIZ_DIR", installed)
    monkeypatch.setattr(mg, "__file__", str(tmp_path / "pkg" / "dca_market_graph.py"))
    monkeypatch.chdir(tmp_path)
    assert mg.viewer_asset_dir() == installed


def test_viewer_asset_dir_finds_a_working_directory_viz(tmp_path, monkeypatch):
    (tmp_path / "viz").mkdir()
    monkeypatch.setenv(mg.VIZ_DIR_ENV, "")
    monkeypatch.setattr(mg, "_INSTALLED_VIZ_DIR", tmp_path / "absent-install")
    monkeypatch.setattr(mg, "__file__", str(tmp_path / "pkg" / "dca_market_graph.py"))
    monkeypatch.chdir(tmp_path)
    assert mg.viewer_asset_dir() == tmp_path / "viz"


def test_viewer_asset_dir_falls_back_to_the_module_relative_path(tmp_path, monkeypatch):
    # Nothing resolves: no override, no installed share/, no ./viz. The final fallback
    # is module-relative, so the FileNotFoundError names a sensible path.
    monkeypatch.setenv(mg.VIZ_DIR_ENV, "")
    monkeypatch.setattr(mg, "_INSTALLED_VIZ_DIR", tmp_path / "absent-install")
    monkeypatch.setattr(mg, "__file__", str(tmp_path / "dca_market_graph.py"))
    monkeypatch.chdir(tmp_path)
    assert mg.viewer_asset_dir() == tmp_path / "viz"


def test_missing_asset_raises_and_names_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(mg.VIZ_DIR_ENV, str(tmp_path))
    with pytest.raises(FileNotFoundError, match=mg.VIZ_DIR_ENV):
        mg.build_viewer_html(mg.build_payload([series("A")]))


#: The single URL that legitimately appears in the emitted page. It is the XHTML
#: namespace literal three.js passes to ``document.createElementNS`` -- an identifier,
#: not a resource the browser ever fetches. Anything else means a subresource leaked in.
_ALLOWED_URLS = ["http://www.w3.org/1999/xhtml"]


def test_viewer_html_is_entirely_self_contained():
    """The whole point of the emitted file: it must open from file:// on a machine
    that has never seen this repository, with no network at all.

    Every assertion below compares a SMALL value. The page is ~770 KB, and asserting
    directly on it means pytest renders the entire string when the check fails.
    """
    payload = mg.build_payload([series("A"), series("B", seed=2)], whole_market=True)
    html = mg.build_viewer_html(payload)

    for marker in ("<script src", "<link ", "@import", "url(http", "importScripts"):
        assert marker not in html, f"emitted page references an external subresource: {marker}"

    assert sorted(set(re.findall(r"https?://[^\s\"'<>)]+", html))) == _ALLOWED_URLS

    assert "THREE" in html             # the vendored renderer really is inlined
    assert '"schema_version"' in html  # ...and so is the payload


@pytest.mark.parametrize("hostile_name", ["</script><img src=x>", "</SCRIPT><b>", "<!--x"])
def test_viewer_html_neutralizes_anything_that_could_start_an_html_token(hostile_name):
    # HTML end tags are case-insensitive and comment openers also break out, so the
    # escape targets "<" itself rather than any two-character sequence.
    hostile = mg.BarSeries(hostile_name, make_bars(6))
    html = mg.build_viewer_html(mg.build_payload([hostile]))
    payload_block = html.split('id="payload">')[1].split("</script>")[0]
    assert "<" not in payload_block
    assert "\\u003c" in payload_block


def test_escaped_payload_is_still_valid_json():
    # An earlier version emitted "\\!" for "<!--", which is not a JSON escape at all --
    # one such label made the entire page unparseable in the browser.
    hostile = mg.BarSeries("<!--</script>", make_bars(6))
    payload = mg.build_payload([hostile])
    escaped = mg._escape_script(mg.build_payload_json(payload))
    assert json.loads(escaped)["tickers"] == ["<!--</script>"]


def test_viewer_html_escapes_the_title():
    html = mg.build_viewer_html(mg.build_payload([series("A")]), title="<b>x</b>")
    assert "<title>&lt;b&gt;x&lt;/b&gt;</title>" in html


def test_viewer_html_does_not_expand_placeholders_written_in_the_title():
    html = mg.build_viewer_html(mg.build_payload([series("A")]), title="{{CSS}}")
    assert "<title>{{CSS}}</title>" in html
    assert "<title><style>" not in html


def test_viewer_html_can_omit_the_inlined_renderer_for_cheap_tests():
    html = mg.build_viewer_html(mg.build_payload([series("A")]), self_contained=False)
    assert len(html) < 200_000
    assert '"schema_version"' in html


@pytest.mark.parametrize("hostile_title", ["{{CSS}}", "{{VIEWER_JS}}", "{{THREE_JS}}", "{{PAYLOAD}}"])
def test_a_title_that_looks_like_a_placeholder_is_not_expanded(hostile_title):
    """Substitution is one pass, so replacement values are never re-scanned.

    Chained .replace() calls would insert the title first and then expand it as
    template syntax -- `--title '{{CSS}}'` would paste the whole stylesheet into the
    <title> element.
    """
    html = mg.build_viewer_html(
        mg.build_payload([series("A")]), title=hostile_title, self_contained=False
    )
    title_text = html.split("<title>")[1].split("</title>")[0]
    # The title survives verbatim (HTML-escaped), rather than being expanded.
    assert title_text == mg._escape_html(hostile_title)
    assert len(title_text) < 100


def test_unknown_placeholder_in_the_template_is_a_loud_error(tmp_path, monkeypatch):
    monkeypatch.setenv(mg.VIZ_DIR_ENV, str(tmp_path))
    (tmp_path / "vendor").mkdir()
    (tmp_path / "market_graph.html.tmpl").write_text("<b>{{NOPE}}</b>", encoding="utf-8")
    (tmp_path / "market_graph.css").write_text("", encoding="utf-8")
    (tmp_path / "market_graph.js").write_text("", encoding="utf-8")
    (tmp_path / "vendor" / "three.min.js").write_text("", encoding="utf-8")
    with pytest.raises(KeyError, match="NOPE"):
        mg.build_viewer_html(mg.build_payload([series("A")]))


def test_placeholders_are_all_substituted():
    html = mg.build_viewer_html(mg.build_payload([series("A")]))
    for placeholder in ("{{TITLE}}", "{{CSS}}", "{{VIEWER_JS}}", "{{THREE_JS}}", "{{PAYLOAD}}"):
        assert placeholder not in html


def test_viewer_js_does_not_build_the_tooltip_with_inner_html():
    js = (mg.viewer_asset_dir() / "market_graph.js").read_text(encoding="utf-8")
    assert "tooltip.innerHTML" not in js
    assert "tooltip.replaceChildren();" in js


def test_viewer_js_extrudes_a_degenerate_wave_axis_instead_of_bailing_out():
    """Both Wave dimensions get the same treatment, and neither aborts the render.

    A one-ticker payload was already extruded into a ribbon; a one-session payload hit
    ``frames < 2`` and opened the viewer on bare axes. Both are supported requests --
    build_payload deliberately emits that single frame -- so both degenerate dimensions
    are duplicated into a mesh that can form a quad, and only the genuinely empty case
    returns early. Kept as a source assertion because the browser half of this renderer
    has no test harness in the standard-library-only suite; the visual check is in the
    PR.
    """
    js = (mg.viewer_asset_dir() / "market_graph.js").read_text(encoding="utf-8")
    assert "frames < 2" not in js
    assert "frames < 1" in js
    assert "var strip = cols === 1;" in js
    # And the axes, the grid, and the camera all measure the SAME extent the mesh uses,
    # rather than three copies of the full-length constant framing a 3-unit strip.
    assert js.count("timeExtent()") >= 4


def test_write_market_graph_emits_both_artifacts(tmp_path):
    data = [series("A"), series("B", seed=2)]
    paths = mg.write_market_graph(
        data, tmp_path / "nested" / "out", whole_market=True,
        grouping=mg.seriate_tickers(data), self_contained=False,
    )
    assert set(paths) == {"viewer_html", "payload_json"}
    for path in paths.values():
        assert Path(path).is_file() and Path(path).stat().st_size > 0
    reloaded = json.loads(Path(paths["payload_json"]).read_text())
    assert reloaded["schema_version"] == mg.PAYLOAD_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def test_active_tickers_from_config_reads_the_universe():
    active = mg.active_tickers_from_config()
    assert "AAPL" in active and len(active) > 40


def test_active_tickers_from_config_subtracts_excluded():
    """A chart must not draw a name the live strategy refuses to trade.

    The ticker config permits a symbol in both lists, and ml-forecaster/train.py's
    _load_active_tickers subtracts `excluded` for exactly this reason. The bundled
    config's own `excluded` list is empty at the moment (BYDDY moved to the separate
    `whole_share` category instead of a fractional-buy exclusion -- see
    docs/whole_share_accumulation.md), so this only checks the real config never
    regresses the invariant, not that it currently exercises it; the overlap behavior
    itself is covered unconditionally by
    test_active_tickers_from_config_honours_an_overlap below.
    """
    from dca_config import get_config_dir

    data = json.loads((Path(get_config_dir()) / "tickers.json").read_text(encoding="utf-8"))
    excluded = {str(t).upper() for t in data.get("excluded", [])}
    assert not (set(mg.active_tickers_from_config()) & excluded)


def test_active_tickers_from_config_honours_an_overlap(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "tickers.json").write_text(
        json.dumps({"active": ["AAA", "BBB", "CCC"], "excluded": ["bbb"]}), encoding="utf-8"
    )
    assert mg.active_tickers_from_config() == ["AAA", "CCC"]


def test_active_tickers_from_config_includes_whole_share_tickers(tmp_path, monkeypatch):
    """Codex review ("Include whole-share symbols in market-graph config
    defaults"): a whole-share ticker (e.g. BYDDY, not fractional-tradable)
    lives in its own "whole_share" category, not "active" -- the graph must
    still draw it, since it is a real part of the live DCA universe. It
    must also respect "excluded" and avoid duplicating a symbol already
    present in "active".
    """
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "tickers.json").write_text(
        json.dumps(
            {
                "active": ["AAA", "BBB"],
                "excluded": ["CCC"],
                "whole_share": ["DDD", "bbb", "ccc"],
            }
        ),
        encoding="utf-8",
    )
    assert mg.active_tickers_from_config() == ["AAA", "BBB", "DDD"]


def test_active_tickers_from_config_rejects_a_non_list_whole_share(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "tickers.json").write_text(
        json.dumps({"active": ["AAA"], "whole_share": "DDD"}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        mg.active_tickers_from_config()


@pytest.mark.parametrize(
    ("contents", "why"),
    [
        ("[]", "a bare list makes .get an AttributeError"),
        ('"AAPL"', "a bare string is not a config either"),
        ('{"active": "AAPL"}', "an iterable string would become the tickers A, A, P, L"),
        ('{"active": ["AAA"], "excluded": null}', "iterating None is a TypeError"),
        ('{"active": null}', "same, on the other field"),
    ],
    ids=["bare-list", "bare-string", "active-is-a-string", "excluded-null", "active-null"],
)
def test_active_tickers_from_config_rejects_structurally_invalid_configs(
    tmp_path, monkeypatch, contents, why
):
    """Valid JSON is not a valid config, and the difference has to be a ValueError.

    The CLIs catch (OSError, ValueError). AttributeError and TypeError are not in that
    tuple, so before this each of these produced the traceback the boundary exists to
    prevent -- it just came through a different door than a syntax error would.
    """
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "tickers.json").write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        mg.active_tickers_from_config()


def test_validate_positive_int_refuses_what_would_silently_do_nothing():
    assert mg.validate_positive_int("3", "--clusters") == 3
    assert mg.validate_positive_int(1, "--clusters") == 1
    for bad in (0, -1, "0", "-4"):
        with pytest.raises(ValueError, match="positive integer"):
            mg.validate_positive_int(bad, "--clusters")
    for bad in ("", "two", None, 1.5):
        with pytest.raises(ValueError, match="must be an integer"):
            mg.validate_positive_int(bad, "--clusters")


def test_validate_payload_document_refuses_an_unknown_schema_version():
    good = mg.payload_to_dict(mg.build_payload([series("AAA")]))
    assert mg.validate_payload_document(good)["schema_version"] == mg.PAYLOAD_SCHEMA_VERSION

    with pytest.raises(ValueError, match="schema_version"):
        mg.validate_payload_document({**good, "schema_version": mg.PAYLOAD_SCHEMA_VERSION + 1})
    with pytest.raises(ValueError, match="schema_version"):
        mg.validate_payload_document({k: v for k, v in good.items() if k != "schema_version"})
    with pytest.raises(ValueError, match="JSON object"):
        mg.validate_payload_document([])
    for field in ("dates", "tickers", "series"):
        with pytest.raises(ValueError, match=field):
            mg.validate_payload_document({**good, field: None})


# ---------------------------------------------------------------------------
# Shared CLI policy -- one implementation, two front ends
# ---------------------------------------------------------------------------


def test_resolve_universe_defaults_only_the_archive():
    # The archive is the whole US universe; every other source is already scoped.
    assert mg.resolve_universe("csv", None) is None
    assert mg.resolve_universe("intraday", None) is None
    assert mg.resolve_universe("run-dir", None) is None
    archive_univ = mg.resolve_universe("archive", None)
    assert archive_univ is not None
    assert "AAPL" in archive_univ


def test_resolve_universe_respects_the_opt_out_and_explicit_requests():
    assert mg.resolve_universe("archive", None, all_tickers_in_archive=True) is None
    assert mg.resolve_universe("archive", ["NVDA"]) == ["NVDA"]
    # An explicitly empty request stays empty -- it must be refused, not widened.
    assert mg.resolve_universe("archive", []) == []
    assert mg.resolve_universe("csv", []) == []


@pytest.mark.parametrize("bad", [0, -1, mg.MAX_TRAIL + 1, "abc", None, 2.5j])
def test_validate_trail_rejects_unusable_values(bad):
    with pytest.raises(ValueError):
        mg.validate_trail(bad)


def test_validate_trail_accepts_the_boundaries():
    assert mg.validate_trail(1) == 1
    assert mg.validate_trail(str(mg.MAX_TRAIL)) == mg.MAX_TRAIL


def test_seriate_or_refuse_explains_itself_above_the_cap(monkeypatch):
    monkeypatch.setattr(mg, "MAX_SERIATION_TICKERS", 2)
    data = factor_series({"a": ["A1", "A2", "A3"]})
    with pytest.raises(ValueError, match="O\\(n\\^3\\)") as excinfo:
        mg.seriate_or_refuse(data)
    # The message must name a way forward, not just say no.
    assert "--group-source" in str(excinfo.value)


def test_seriate_or_refuse_matches_seriate_tickers_under_the_cap():
    data = factor_series({"a": ["A1", "A2"], "b": ["B1", "B2"]})
    assert mg.seriate_or_refuse(data).order == mg.seriate_tickers(data).order


def test_intraday_labels_are_marked_utc_and_daily_labels_are_not():
    """_parse_timestamp converts aware inputs to UTC, so intraday frames ARE UTC.

    Printed bare they read as the Eastern trading clock every other artifact in this
    repo is stamped in -- five hours wrong for most of the year. Not converted to
    Eastern instead, because _parse_timestamp leaves NAIVE inputs untouched: relabelling
    an unknown zone would swap a vague label for a confidently wrong one.
    """
    intraday = tuple(
        Bar(timestamp=datetime(2024, 1, 2, 14 + i), open=100, high=101, low=99,
            close=100 + i, volume=1000)
        for i in range(3)
    )
    labels = mg.build_payload([mg.BarSeries("AMD", intraday)]).dates
    assert all(label.endswith("Z") for label in labels)
    assert labels[0] == "2024-01-02 15:00Z"   # 14:00 is the priorless first bar

    daily = mg.build_payload([series("AMD", count=5)]).dates
    assert not any(label.endswith("Z") for label in daily)
    assert daily[0] == "2024-01-02"   # 01-01 is the priorless first bar


def test_artifact_span_is_filesystem_safe_for_intraday_payloads():
    """Intraday labels carry "HH:MM"; a colon is invalid in a Windows filename."""
    bars = tuple(
        Bar(timestamp=datetime(2024, 1, 2, 10 + i), open=100, high=101, low=99,
            close=100 + i, volume=1000)
        for i in range(4)
    )
    span = mg.artifact_span(mg.build_payload([mg.BarSeries("AMD", bars)]))
    assert ":" not in span and " " not in span
    assert span.startswith("2024-01-02T11")   # 10:00 is the priorless first bar


@pytest.mark.parametrize(
    "label",
    ["../../../tmp/escape", "/etc/passwd", "..", ".", "", "C:\\windows", "a/b", "x\\y"],
)
def test_artifact_span_is_always_a_single_path_component(label, tmp_path, monkeypatch):
    """The span reaches stream_root() as a path component, from an untrusted document.

    `--payload` dates are written by something other than this process, so a traversal
    or absolute label would otherwise walk straight out of $DCA_CRON_TRACKING_ROOT and
    create directories elsewhere. Sanitized in artifact_span rather than at one call
    site, because both CLIs and the matplotlib renderer build paths from it.
    """
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    payload = mg.MarketGraphPayload(
        title="", dates=(label, "2026-01-01"), tickers=(), series=(), scales={},
        grouping=mg.TickerGrouping(order=()),
    )
    span = mg.artifact_span(payload)
    assert "/" not in span and "\\" not in span
    assert span.strip(".") != ""

    resolved = mg.default_output_dir(payload).resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), resolved


@pytest.mark.parametrize("empty_key", ["dates", "tickers", "series"])
def test_an_empty_payload_list_is_refused_before_anything_indexes_it(empty_key):
    """Well-formed JSON that nothing downstream survives.

    artifact_span() indexes dates[0]; the matplotlib renderer slices a (0, N) array into
    plot_surface. Both used to fail obscurely -- IndexError, or a shape error naming
    neither the payload nor the cause -- so the emptiness is caught once, here.
    """
    good = mg.payload_to_dict(mg.build_payload([series("AAA")]))
    with pytest.raises(ValueError, match=f"{empty_key}.*empty"):
        mg.validate_payload_document({**good, empty_key: []})


def test_artifact_span_on_a_frameless_payload_says_so():
    """A public helper explains itself rather than raising IndexError from a subscript."""
    payload = mg.MarketGraphPayload(
        title="", dates=(), tickers=(), series=(), scales={},
        grouping=mg.TickerGrouping(order=()),
    )
    with pytest.raises(ValueError, match="no frames"):
        mg.artifact_span(payload)


def test_validate_payload_document_checks_the_nested_shape_too():
    """Not only the three top-level containers -- the schema's REQUIRED nested fields.

    {"schema_version":1,"dates":["2024-01-01"],"tickers":["A"],"series":[{}]} passed
    every earlier check and then _real_tickers() raised KeyError, the exact traceback
    this function exists to turn into a documented exit 1. Checked against
    schemas/market_graph_payload.schema.json's own required fields.
    """
    good = mg.payload_to_dict(mg.build_payload(
        [mg.BarSeries("AAA", make_bars(2))], frame_start=make_bars(2)[1].timestamp.date()
    ))
    # A series entry missing every channel.
    with pytest.raises(ValueError, match="missing a string 'ticker'"):
        mg.validate_payload_document({**good, "series": [{}]})
    # A channel present but the wrong length for the frame axis.
    bad_series = [{**good["series"][0], "x": good["series"][0]["x"] + [0.1]}]
    with pytest.raises(ValueError, match="channel 'x' must be a list of 1"):
        mg.validate_payload_document({**good, "series": bad_series})
    # A channel holding something other than a number or null.
    bad_series2 = [{**good["series"][0], "yl": ["not-a-number"]}]
    with pytest.raises(ValueError, match="only numbers and nulls"):
        mg.validate_payload_document({**good, "series": bad_series2})
    # scales.pct_per_unit, grouping.order, and title are all required too.
    with pytest.raises(ValueError, match="scales.pct_per_unit"):
        mg.validate_payload_document({**good, "scales": {}})
    with pytest.raises(ValueError, match="grouping.order"):
        mg.validate_payload_document({**good, "grouping": {}})
    with pytest.raises(ValueError, match="'title'"):
        mg.validate_payload_document({**good, "title": None})
    # The well-formed document still passes whole.
    assert mg.validate_payload_document(good)


def test_a_malformed_payload_date_is_refused_rather_than_sanitized():
    """Sanitizing keeps the write safe; refusing tells the operator the document is wrong."""
    # A genuine one-frame payload, so swapping `dates` for a single hostile or valid
    # label keeps every channel's length consistent with it -- the point under test is
    # the DATE check, not the also-real channel-length check exercised elsewhere.
    good = mg.payload_to_dict(mg.build_payload(
        [mg.BarSeries("AAA", make_bars(2))], frame_start=make_bars(2)[1].timestamp.date()
    ))
    assert len(good["dates"]) == 1
    for bad in ["../../escape", "not-a-date", "2026-13-99-", "2026-01-01 14:30"]:
        with pytest.raises(ValueError, match="malformed frame label"):
            mg.validate_payload_document({**good, "dates": [bad]})
    # The two documented shapes still pass.
    for ok in ["2026-01-01", "2026-01-01 14:30Z"]:
        assert mg.validate_payload_document({**good, "dates": [ok]})


def test_artifact_span_uses_the_payload_axis_not_one_ticker():
    long_bars = make_bars(20)
    short = mg.BarSeries("SHORT", long_bars[10:])
    payload = mg.build_payload([mg.BarSeries("LONG", long_bars), short])
    span = mg.artifact_span(payload)
    assert span == f"{payload.dates[0]}_{payload.dates[-1]}"
    assert span.startswith(long_bars[1].timestamp.date().isoformat())


def test_summarize_resolution_names_what_went_missing():
    resolved = [series("AAA")]
    text = mg.summarize_resolution(["AAA", "BBB", "CCC"], resolved)
    assert "1/3" in text and "BBB" in text and "CCC" in text


def test_summarize_resolution_truncates_a_long_missing_list():
    text = mg.summarize_resolution([f"T{i}" for i in range(20)], [])
    assert "more" in text


def test_summarize_resolution_without_a_request_just_counts():
    assert mg.summarize_resolution(None, [series("AAA")]) == "resolved 1 ticker(s)"


def test_wanted_normalizes_case_and_keeps_an_empty_request_empty():
    assert mg._wanted(None) is None
    assert mg._wanted([" aapl ", "", "msft"]) == {"AAPL", "MSFT"}
    # An explicit empty request must NOT become None: "asked for nothing" and "did not
    # filter" are opposites, and conflating them renders the whole archive.
    assert mg._wanted([]) == set()
    assert mg._wanted(["", "  "]) == set()


def test_average_linkage_on_an_empty_matrix_returns_nothing():
    assert mg._average_linkage([]) is None


def test_cut_clusters_cannot_exceed_the_leaf_count():
    data = factor_series({"a": ["A1", "A2"]})
    grouping = mg.seriate_tickers(data, clusters=99)
    assert len(grouping.clusters) <= len(data)


def test_scale_percentile_ignores_a_lone_outlier():
    """One earnings gap must not flatten every other name into invisibility.

    The window has to be long enough that the gap (and the snap-back on the following
    session) stays inside the top 2% of moves -- which is exactly the real-world
    condition the p98 scale is chosen for.
    """
    calm = make_bars(250, seed=3)
    spike = list(calm)
    spike[120] = Bar(timestamp=spike[120].timestamp, open=100, high=400, low=90,
                     close=380, volume=1e7)
    normal = mg.build_payload([mg.BarSeries("CALM", calm)])
    with_spike = mg.build_payload([mg.BarSeries("CALM", tuple(spike))])
    # The p98 scale barely moves, so ordinary days keep their amplitude...
    assert with_spike.scales["pct_per_unit"] < normal.scales["pct_per_unit"] * 2
    # ...and the outlier simply runs off the top of the scale rather than setting it.
    assert max(v for v in with_spike.series[0]["zc"] if v is not None) > 3.0


#: sha256 of the vendored three.js r149 UMD bundle, as published at
#: https://registry.npmjs.org/three/-/three-0.149.0.tgz and recorded in
#: viz/vendor/README.md. See the test below for why this is pinned rather than trusted.
_THREE_JS_SHA256 = "8a5f7249903b54d30f79f708699d2fed2d6a1d0741a4cd41377d1f01bb5a2271"


def test_vendored_three_js_matches_its_recorded_checksum():
    """The vendored bundle must be byte-identical to the published upstream artifact.

    This test is what makes two other decisions defensible:

    1.  ``test_config_invariants._ALLOWED`` exempts this file from the account-literal
        secrets scan, because minified WebGL is thousands of account-shaped integers
        (packed colours, bitmasks). An unverified exemption would be a blind spot in a
        secrets guard; pinning the hash means the exemption only ever covers bytes that
        provably match what npm published.
    2.  Shipping a 608 KB third-party blob into a repository with a stdlib-only runtime
        invariant at all. "It is upstream's code, unmodified" is only a real claim if
        something checks.

    If this fails, the file was edited or re-vendored. Re-verify it against the registry
    tarball (the commands are in viz/vendor/README.md) before updating this constant --
    do not simply paste in the new hash.
    """
    import hashlib

    path = REPO_ROOT / "viz" / "vendor" / "three.min.js"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == _THREE_JS_SHA256


def test_vendor_readme_records_the_same_checksum():
    """The documented provenance and the enforced pin must not drift apart."""
    readme = (REPO_ROOT / "viz" / "vendor" / "README.md").read_text(encoding="utf-8")
    assert _THREE_JS_SHA256 in readme


def test_module_imports_no_third_party_packages():
    """The stdlib-only runtime invariant, enforced rather than trusted."""
    import ast

    tree = ast.parse((REPO_ROOT / "dca_market_graph.py").read_text(encoding="utf-8"))
    allowed = {
        "__future__",
        "csv", "json", "math", "os", "sys", "dataclasses", "datetime", "pathlib",
        "typing", "ast", "contextlib", "re", "hashlib",
        # First-party siblings.
        "near_close_dca", "dca_ticker_paths", "dca_intraday_archive", "dca_paths",
        "dca_live_adapters", "dca_fileio", "dca_config",
    }
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            seen.add(node.module.split(".")[0])
    assert seen <= allowed, f"third-party import introduced: {sorted(seen - allowed)}"


def test_math_import_is_actually_used_for_log_volume():
    # Guards the log scaling: heavy-tailed volume compressed linearly would put every
    # ordinary session in the first few percent of the X axis.
    payload = mg.build_payload([series("A", volume=1_000.0), series("B", seed=2, volume=100_000_000.0)])
    assert math.isfinite(payload.scales["pct_per_unit"])
