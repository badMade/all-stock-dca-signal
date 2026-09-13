"""Tests for data.py: discovery, loading, adjustment, as-of truncation."""

from __future__ import annotations

import data
import numpy as np
import pandas as pd
import pytest

import conftest

make_synthetic_ohlcv = conftest.make_synthetic_ohlcv
write_ticker_csv = conftest.write_ticker_csv


class TestDiscoverAvailableTickers:
    def test_discovers_only_real_tickers_and_skips_archive_debris(self, synthetic_history_dir):
        # _delisted.txt, _intraday/, and AAA-backup.csv must all be invisible.
        assert data.discover_available_tickers(synthetic_history_dir) == ["AAA", "BBB", "CCC", "DDD", "ZZZ"]

    def test_explicit_list_dedupes_and_drops_missing_and_invalid(self, synthetic_history_dir):
        requested = ["BBB", "BBB", "AAA", "NOPE", "../evil", "aaa"]
        assert data.discover_available_tickers(synthetic_history_dir, requested) == ["BBB", "AAA"]

    def test_explicit_lowercase_tickers_are_normalized_not_dropped(self, synthetic_history_dir):
        # `--tickers aapl` must mean AAPL (Antigravity review finding).
        assert data.discover_available_tickers(synthetic_history_dir, ["aaa", "bbb"]) == ["AAA", "BBB"]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert data.discover_available_tickers(tmp_path / "absent") == []


class TestLoadHistoryCsv:
    def test_missing_required_column_raises(self, tmp_path):
        path = tmp_path / "DUMMY.csv"
        path.write_text("Date,Open,High,Low\n2021-01-04,1,2,0.5\n")
        with pytest.raises(ValueError, match="missing required column"):
            data.load_history_csv(path)

    def test_corrupt_rows_are_dropped_individually(self, tmp_path):
        path = tmp_path / "DUMMY.csv"
        path.write_text(
            "Date,Open,High,Low,Close\n"
            "2021-01-04,10,11,9,10.5\n"
            "not-a-date,10,11,9,10.5\n"
            "2021-01-06,10,11,9,not-a-number\n"
            "2021-01-07,10,11,9,10.2\n"
        )
        frame = data.load_history_csv(path)
        assert [d.strftime("%Y-%m-%d") for d in frame["Date"]] == ["2021-01-04", "2021-01-07"]

    def test_duplicate_dates_keep_last_written_row(self, tmp_path):
        path = tmp_path / "DUMMY.csv"
        path.write_text(
            "Date,Open,High,Low,Close\n2021-01-04,10,11,9,10.0\n2021-01-04,10,12,9,11.0\n",
        )
        frame = data.load_history_csv(path)
        assert len(frame) == 1
        assert frame.loc[0, "Close"] == 11.0

    def test_timezone_aware_dates_normalize_to_naive(self, tmp_path):
        path = tmp_path / "DUMMY.csv"
        path.write_text("Date,Open,High,Low,Close\n2021-01-04T00:00:00Z,10,11,9,10.0\n")
        frame = data.load_history_csv(path)
        assert getattr(frame["Date"].dt, "tz", None) is None

    def test_dates_spanning_a_daylight_saving_transition_parse_without_raising(self, tmp_path):
        # A real archive whose Date column carries an explicit UTC offset
        # per row can span a daylight-saving transition within the SAME
        # file (e.g. "-04:00" EDT rows followed by "-05:00" EST rows once
        # clocks fall back). Without utc=True, pandas cannot build one
        # homogeneous dtype from mixed offsets and raises ValueError
        # ("Mixed timezones detected..."), which the caller
        # (features.build_panel()) catches as "unreadable" and drops the
        # WHOLE ticker, even though every row here is perfectly valid
        # (Codex review: "Parse DST-varying timezone offsets in one UTC
        # pass").
        path = tmp_path / "DUMMY.csv"
        path.write_text(
            "Date,Open,High,Low,Close\n"
            "2021-11-05T09:30:00-04:00,10,11,9,10.0\n"  # EDT, before the fall-back
            "2021-11-08T09:30:00-05:00,10,12,9,11.0\n"  # EST, after the fall-back
        )
        frame = data.load_history_csv(path)
        assert getattr(frame["Date"].dt, "tz", None) is None
        assert [d.strftime("%Y-%m-%d") for d in frame["Date"]] == ["2021-11-05", "2021-11-08"]

    def test_intraday_timestamps_normalize_to_the_calendar_day(self, tmp_path):
        # Every downstream consumer (TickerSeries.dates/label_end_dates,
        # TrainingPanel.calendar/date_index, the harness's origin-date
        # re-anchoring, Octopus's training-reference resolution) treats a
        # row's Date as ONE TRADING DAY and matches it via exact equality
        # against other day-precision values. A valid CSV whose Date column
        # carries a real time-of-day component used to silently break one
        # such comparison at a time, across several review rounds each
        # catching a different call site (Codex review: "Match forecast
        # origins/queued reveals/training references by calendar day").
        # Normalizing here, the only place every row enters the system,
        # closes the whole bug class at its source.
        path = tmp_path / "DUMMY.csv"
        path.write_text("Date,Open,High,Low,Close\n2021-01-04T16:00:00,10,11,9,10.0\n")
        frame = data.load_history_csv(path)
        assert frame.loc[0, "Date"] == pd.Timestamp("2021-01-04")

    def test_intraday_timestamps_on_the_same_calendar_day_still_dedupe(self, tmp_path):
        # Normalizing to calendar-day precision BEFORE the existing
        # drop_duplicates(subset="Date") step means two rows that only
        # differ by time-of-day now collide -- confirm the existing
        # keep="last" contract still applies (deterministic on sort order),
        # not that they silently double the ticker's row count.
        path = tmp_path / "DUMMY.csv"
        path.write_text(
            "Date,Open,High,Low,Close\n"
            "2021-01-04T09:30:00,10,11,9,10.0\n"
            "2021-01-04T16:00:00,10,12,9,11.0\n"
        )
        frame = data.load_history_csv(path)
        assert len(frame) == 1
        assert frame.loc[0, "Close"] == 11.0


class TestAdjustedOhlc:
    def test_split_discontinuity_is_removed(self, market_factor):
        raw = make_synthetic_ohlcv(seed=2, market_returns=market_factor, market_beta=1.0, split_at=130)
        # The raw close halves overnight at the split; adjusted must not.
        raw_jump = np.abs(np.diff(np.log(raw["Close"].to_numpy())))
        assert raw_jump.max() > 0.5  # the fake -50% move is present pre-adjustment
        adjusted, dropped = data.adjusted_ohlc(raw)
        assert dropped == 0
        adj_jump = np.abs(np.diff(np.log(adjusted["Close"].to_numpy())))
        assert adj_jump.max() < 0.2
        # Ratio column records what was applied: 0.5 before a 2:1 split.
        assert adjusted["adj_ratio"].iloc[0] == pytest.approx(0.5)
        assert adjusted["adj_ratio"].iloc[-1] == pytest.approx(1.0)

    def test_whole_bar_is_scaled_consistently(self, market_factor):
        raw = make_synthetic_ohlcv(seed=2, market_returns=market_factor, market_beta=1.0, split_at=130)
        adjusted, _ = data.adjusted_ohlc(raw)
        # Every adjusted bar stays geometrically valid.
        assert (adjusted["High"] >= np.maximum(adjusted["Open"], adjusted["Close"]) * 0.9999).all()
        assert (adjusted["Low"] <= np.minimum(adjusted["Open"], adjusted["Close"]) * 1.0001).all()

    def test_impossible_bars_are_dropped_and_counted(self):
        frame = make_synthetic_ohlcv(n=30, seed=7)
        frame.loc[10, "High"] = frame.loc[10, "Low"] * 0.5  # high far below the body
        frame.loc[20, "Close"] = -1.0  # negative price
        adjusted, dropped = data.adjusted_ohlc(frame)
        assert dropped == 2
        assert len(adjusted) == 28

    def test_unusable_adj_close_rows_are_dropped_not_switched_to_raw_basis(self):
        # A row with a missing/negative/non-finite Adj Close used to fall
        # back to ratio 1.0 for THAT row while its neighbors kept the real
        # split/dividend ratio -- silently switching that one day onto the
        # raw price basis (e.g. ~4x across a 4:1 split) instead of a clean
        # gap. Drop the row instead (Codex review regression: "Drop
        # invalid adjusted-close rows instead of switching bases").
        frame = make_synthetic_ohlcv(n=30, seed=8)
        frame.loc[5, "Adj Close"] = np.nan
        frame.loc[6, "Adj Close"] = -2.0
        dropped_dates = {frame.loc[5, "Date"], frame.loc[6, "Date"]}
        adjusted, dropped = data.adjusted_ohlc(frame)
        assert dropped == 2
        assert len(adjusted) == 28
        assert not dropped_dates & set(adjusted["Date"])

    def test_crossed_bar_high_below_low_is_dropped(self):
        # The two independent tolerance checks (High vs body-high, Low vs
        # body-low) can each individually pass a bar where High < Low --
        # e.g. open=close=100, high=99.995, low=100.005 (Codex review
        # regression: "Reject bars whose high is below their low").
        frame = make_synthetic_ohlcv(n=30, seed=10)
        frame.loc[15, ["Open", "Close", "Adj Close"]] = 100.0
        frame.loc[15, "High"] = 99.995
        frame.loc[15, "Low"] = 100.005
        adjusted, dropped = data.adjusted_ohlc(frame)
        assert dropped == 1
        assert len(adjusted) == 29

    def test_infinite_ohlc_row_is_dropped_not_market_poisoning(self):
        # An all-+inf row passes both the bar-geometry check (inf >= inf)
        # and the positivity check (inf > 0) unless finiteness is checked
        # explicitly -- and would otherwise make every shared market feature
        # computed from that day infinite for every OTHER ticker too (Codex
        # review regression).
        frame = make_synthetic_ohlcv(n=30, seed=9)
        frame.loc[12, ["Open", "High", "Low", "Close"]] = np.inf
        adjusted, dropped = data.adjusted_ohlc(frame)
        assert dropped == 1
        assert len(adjusted) == 29
        assert np.isfinite(adjusted[["Open", "High", "Low", "Close"]].to_numpy()).all()


class TestTruncateAsOf:
    def test_rows_on_or_after_the_as_of_day_are_dropped(self):
        frame = make_synthetic_ohlcv(n=10)
        cutoff = frame["Date"].iloc[5]
        truncated = data.truncate_as_of(frame, cutoff + pd.Timedelta(hours=15))
        # The as-of instant's own calendar day is excluded, not just later ones.
        assert truncated["Date"].max() < cutoff

    def test_none_keeps_everything(self):
        frame = make_synthetic_ohlcv(n=10)
        assert len(data.truncate_as_of(frame, None)) == 10

    def test_nat_as_of_means_no_cutoff_not_total_truncation(self):
        # pd.NaT compares False against every date; an unguarded NaT would
        # silently wipe the whole archive (review regression).
        frame = make_synthetic_ohlcv(n=10)
        assert len(data.truncate_as_of(frame, pd.NaT)) == 10

    def test_timezone_aware_as_of_is_normalized_not_a_crash(self):
        # "--now 2021-...T18:00:00Z" is a supported ISO form; archive dates
        # are tz-naive UTC, so the cutoff must convert (review regression).
        frame = make_synthetic_ohlcv(n=10)
        cutoff = frame["Date"].iloc[5]
        aware = f"{cutoff.date()}T18:00:00+00:00"
        truncated = data.truncate_as_of(frame, aware)
        assert truncated["Date"].max() < cutoff


def test_panel_eligibility_requires_min_history(synthetic_history_dir):
    import features

    panel = features.build_panel(synthetic_history_dir, min_history=60)
    aaa = panel.series["AAA"]
    early_day = pd.Timestamp(aaa.dates[10])
    warm_day = pd.Timestamp(aaa.dates[100])
    early = dict(panel.eligible_on(early_day))
    warm = dict(panel.eligible_on(warm_day))
    assert "AAA" not in early  # row 10 has fewer than 60 prior rows
    assert "AAA" in warm
    # The late IPO (CCC) is absent early on -- a non-blocker, not an error.
    assert "CCC" not in early


def test_panel_skips_broken_and_short_tickers_without_blocking(synthetic_history_dir):
    import features

    (synthetic_history_dir / "EEE.csv").write_text("garbage that is not a csv at all\x00\n")
    write_ticker_csv(synthetic_history_dir, "FFF", make_synthetic_ohlcv(n=20, seed=9))  # far too short
    panel = features.build_panel(synthetic_history_dir, min_history=60)
    assert sorted(panel.series) == ["AAA", "BBB", "CCC", "DDD", "ZZZ"]
    assert panel.skipped["EEE"].startswith("unreadable")
    assert panel.skipped["FFF"] == "too_short"


def test_panel_skips_a_ticker_whose_csv_lacks_the_adj_close_column_entirely(synthetic_history_dir):
    # adjusted_ohlc() only rejects individual rows with an unusable Adj
    # Close VALUE; a column that is absent entirely never reaches that
    # per-row check, so the ticker used to keep the default ratio 1.0 for
    # its ENTIRE history -- silently treating every split/dividend
    # discontinuity as a real price move (Codex review regression: "Require
    # adjusted close before accepting a ticker").
    import features

    frame = make_synthetic_ohlcv(seed=7).drop(columns=["Adj Close"])
    write_ticker_csv(synthetic_history_dir, "HHH", frame)
    panel = features.build_panel(synthetic_history_dir, min_history=60)
    assert "HHH" not in panel.series
    assert panel.skipped["HHH"] == "missing_adj_close_column"


def test_panel_admits_a_ticker_with_exactly_min_history_plus_one_rows(synthetic_history_dir):
    # A ticker with exactly min_history+1 rows has an eligible final row
    # (index == min_history satisfies eligible_on()'s idx >= min_history
    # rule) even though that row's own target isn't known yet -- requiring
    # a second row here delayed a short-history ticker's first live
    # forecast by a full trading day, contradicting the documented "joins
    # once it has min_history prior rows" contract (Codex review
    # regression: "Admit tickers as soon as the warm-up is complete").
    import features

    write_ticker_csv(synthetic_history_dir, "GGG", make_synthetic_ohlcv(n=61, seed=11))
    panel = features.build_panel(synthetic_history_dir, min_history=60)
    assert "GGG" not in panel.skipped
    assert "GGG" in panel.series
    series = panel.series["GGG"]
    assert len(series.dates) == 61
    last_row = 60
    assert np.isfinite(series.features[last_row]).all()
    eligible_last_day = dict(panel.eligible_on(pd.Timestamp(series.dates[last_row])))
    assert "GGG" in eligible_last_day
    assert eligible_last_day["GGG"] == last_row


def test_panel_refuses_archive_with_zero_usable_tickers(tmp_path):
    import features

    empty = tmp_path / "ticker_data"
    empty.mkdir()
    (empty / "AAA.csv").write_text("Date,Open,High,Low,Close\n")  # header only
    with pytest.raises(ValueError, match="REFUSED: no usable tickers"):
        features.build_panel(empty)


def test_panel_lowercase_ticker_request_is_loaded_not_marked_skipped(synthetic_history_dir):
    import features

    # discover_available_tickers() case-normalizes an explicit --tickers
    # list ("aapl" -> "AAPL") before matching it against the archive; a
    # successfully-loaded ticker must not ALSO show up in panel.skipped
    # under its original lowercase spelling just because the caller typed
    # it that way (Codex review regression: "Avoid marking normalized
    # ticker requests as skipped").
    panel = features.build_panel(synthetic_history_dir, tickers=["aaa", "bbb"], min_history=60)
    assert sorted(panel.series) == ["AAA", "BBB"]
    assert panel.skipped == {}


def test_panel_max_tickers_keeps_longest_histories_deterministically(synthetic_history_dir):
    import features

    panel = features.build_panel(synthetic_history_dir, max_tickers=2, min_history=60)
    # AAA (260 rows) and ZZZ (260 rows) tie with BBB (260) and DDD (250);
    # longest-history-then-alphabetical keeps AAA and BBB.
    assert panel.tickers == ["AAA", "BBB"]
    assert panel.skipped["CCC"] == "over_max_tickers"
