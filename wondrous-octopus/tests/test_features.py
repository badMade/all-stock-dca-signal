"""Tests for features.py -- the no-lookahead regression test lives here."""

from __future__ import annotations

import data
import features
import numpy as np
import pandas as pd
import pytest

from conftest import make_market_returns, make_synthetic_ohlcv, write_ticker_csv


def _adjusted(frame):
    adjusted, _ = data.adjusted_ohlc(frame)
    return adjusted


class TestComputeTickerFeatures:
    def test_target_alignment_is_next_row_vs_this_close(self):
        frame = _adjusted(make_synthetic_ohlcv(n=40, seed=11))
        computed = features.compute_ticker_features(frame)
        t = 20
        expected = np.log(frame.loc[t + 1, "Close"] / frame.loc[t, "Close"])
        assert computed.loc[t, "target_close"] == pytest.approx(expected, rel=1e-12)
        expected_high = np.log(frame.loc[t + 1, "High"] / frame.loc[t, "Close"])
        assert computed.loc[t, "target_high"] == pytest.approx(expected_high, rel=1e-12)
        # Final row: nothing to predict, NaN target, NaT label end.
        assert np.isnan(computed["target_close"].iloc[-1])
        assert pd.isna(computed["label_end_date"].iloc[-1])

    def test_label_end_date_is_the_next_trading_row_even_across_gaps(self):
        frame = _adjusted(make_synthetic_ohlcv(n=60, seed=12, gap=(30, 10)))
        computed = features.compute_ticker_features(frame)
        # Row 29 is the last row before the gap; its label end is the first
        # row AFTER the gap -- a ticker's t+1 is its own next trading row.
        assert computed.loc[29, "label_end_date"] == frame.loc[30, "Date"]
        assert (frame.loc[30, "Date"] - frame.loc[29, "Date"]).days > 10

    def test_doji_bar_close_position_is_neutral(self):
        frame = _adjusted(make_synthetic_ohlcv(n=30, seed=13))
        price = frame.loc[10, "Close"]
        frame.loc[10, ["Open", "High", "Low", "Close"]] = price
        computed = features.compute_ticker_features(frame)
        assert computed.loc[10, "close_position"] == 0.5

    def test_missing_volume_degrades_gracefully(self):
        frame = _adjusted(make_synthetic_ohlcv(n=40, seed=14, with_volume=False))
        computed = features.compute_ticker_features(frame)
        assert (computed["volume_z"] == 0.0).all()
        assert (computed["volume_missing"] == 1.0).all()

    def test_per_row_missing_volume_is_flagged_per_row(self):
        # A ticker with volume on most days must flag exactly the NaN days,
        # not report volume_missing == 0 everywhere (review regression).
        frame = _adjusted(make_synthetic_ohlcv(n=60, seed=16))
        frame.loc[30, "Volume"] = np.nan
        frame.loc[31, "Volume"] = -5.0  # corrupt negative counts as missing too
        computed = features.compute_ticker_features(frame)
        assert computed.loc[30, "volume_missing"] == 1.0
        assert computed.loc[31, "volume_missing"] == 1.0
        assert computed.loc[29, "volume_missing"] == 0.0
        assert computed.loc[30, "volume_z"] == 0.0  # neutral, never NaN
        assert np.isfinite(computed["volume_z"]).all()

    def test_infinite_volume_is_flagged_missing_not_an_infinite_zscore(self):
        # A corrupt CSV's Volume=inf survived isna()/negative checks as a
        # "present" row before, producing an infinite volume_z while
        # volume_missing stayed 0 -- the harness's all-features-finite
        # eligibility check would then silently drop that ticker's
        # forecast for the day instead of degrading to the documented
        # neutral volume_z (Codex review regression: "Treat
        # positive-infinite volume as missing").
        frame = _adjusted(make_synthetic_ohlcv(n=60, seed=18))
        frame["Volume"] = frame["Volume"].astype(float)
        frame.loc[30, "Volume"] = np.inf
        computed = features.compute_ticker_features(frame)
        assert computed.loc[30, "volume_missing"] == 1.0
        assert computed.loc[30, "volume_z"] == 0.0
        assert np.isfinite(computed["volume_z"]).all()

    def test_missing_volume_does_not_erase_a_month_of_zscores(self):
        # A single corrupt row's log-volume must not enter the rolling
        # window at all: pandas' default min_periods==window means one NaN
        # poisons every window touching it, which the old code then
        # silently replaced with a neutral 0.0 for roughly a month of
        # otherwise-valid days (Codex review regression: "Exclude missing
        # volume from trailing statistics").
        frame = _adjusted(make_synthetic_ohlcv(n=80, seed=17))
        frame.loc[40, "Volume"] = np.nan
        computed = features.compute_ticker_features(frame)
        tail = computed.loc[41:59, "volume_z"]
        assert not (tail == 0.0).all()
        assert np.isfinite(tail).all()

    def test_warmup_rows_carry_nan_not_garbage(self):
        frame = _adjusted(make_synthetic_ohlcv(n=40, seed=15))
        computed = features.compute_ticker_features(frame)
        assert np.isnan(computed.loc[0, "ret_close"])
        assert np.isnan(computed.loc[10, "trailing_ret_20"])
        assert np.isfinite(computed.loc[25, "trailing_ret_20"])


class TestNoLookahead:
    """Perturb everything after day D; nothing at or before D may move."""

    def _build_archives(self, tmp_path):
        market = make_market_returns()
        specs = {
            "AAA": dict(seed=1, market_returns=market, market_beta=1.0),
            "BBB": dict(seed=2, market_returns=market, market_beta=1.0, split_at=130),
            "ZZZ": dict(seed=5, market_beta=0.0),
        }
        frames = {ticker: make_synthetic_ohlcv(**spec) for ticker, spec in specs.items()}
        cutoff = frames["AAA"]["Date"].iloc[150]

        clean_dir = tmp_path / "clean"
        perturbed_dir = tmp_path / "perturbed"
        for directory in (clean_dir, perturbed_dir):
            directory.mkdir()
        for ticker, frame in frames.items():
            write_ticker_csv(clean_dir, ticker, frame)
            mutated = frame.copy()
            future = mutated["Date"] > cutoff
            for column in ("Open", "High", "Low", "Close", "Adj Close"):
                mutated.loc[future, column] = mutated.loc[future, column] * 1.5
            write_ticker_csv(perturbed_dir, ticker, mutated)
        return clean_dir, perturbed_dir, cutoff

    def test_features_before_cutoff_are_bit_identical(self, tmp_path):
        clean_dir, perturbed_dir, cutoff = self._build_archives(tmp_path)
        clean = features.build_panel(clean_dir, initial_train_end=cutoff)
        perturbed = features.build_panel(perturbed_dir, initial_train_end=cutoff)
        for ticker in clean.tickers:
            clean_series = clean.series[ticker]
            perturbed_series = perturbed.series[ticker]
            past = clean_series.dates <= np.datetime64(cutoff)
            np.testing.assert_array_equal(clean_series.features[past], perturbed_series.features[past])
            # Targets are knowable once their label end has passed; those and
            # only those must match (the cutoff row's own target reads the
            # perturbed next day and is expected to differ).
            knowable = past & (clean_series.label_end_dates <= np.datetime64(cutoff))
            np.testing.assert_array_equal(clean_series.targets[knowable], perturbed_series.targets[knowable])

    def test_normalization_stats_frozen_at_window_are_unaffected_by_future(self, tmp_path):
        clean_dir, perturbed_dir, cutoff = self._build_archives(tmp_path)
        clean = features.build_panel(clean_dir, initial_train_end=cutoff)
        perturbed = features.build_panel(perturbed_dir, initial_train_end=cutoff)
        assert clean.normalization["mean"] == perturbed.normalization["mean"]
        assert clean.normalization["std"] == perturbed.normalization["std"]

    def test_normalization_fallback_stays_bounded_by_cutoff_no_leakage(self, tmp_path):
        # If NO ticker has a fully finite feature row before the cutoff
        # (every selected ticker's warm-up hasn't completed yet), the
        # fallback must still not read PAST the cutoff -- the old fallback
        # used the full, unbounded history, so for an Octopus-only run
        # whose normalization stats get frozen into every historical
        # forecast, future data (like this perturbation) would silently
        # bleed into the initial fit (Codex review regression: "Refuse
        # normalization from post-cutoff history").
        clean_dir, perturbed_dir, _cutoff = self._build_archives(tmp_path)
        probe = make_synthetic_ohlcv(seed=1, market_returns=make_market_returns(), market_beta=1.0)
        early_cutoff = probe["Date"].iloc[5]  # inside every ticker's warm-up -- forces the fallback
        clean = features.build_panel(clean_dir, initial_train_end=early_cutoff)
        perturbed = features.build_panel(perturbed_dir, initial_train_end=early_cutoff)
        assert clean.normalization["fell_back_to_partial_rows"] is True
        assert perturbed.normalization["fell_back_to_partial_rows"] is True
        # equal_nan=True: harmless belt-and-suspenders now that zero-
        # observation columns get finite defaults rather than NaN (see
        # test_normalization_gives_finite_defaults_to_columns_with_zero_observations),
        # kept in case a future column genuinely has no fallback value.
        np.testing.assert_allclose(clean.normalization["mean"], perturbed.normalization["mean"], equal_nan=True)
        np.testing.assert_allclose(clean.normalization["std"], perturbed.normalization["std"], equal_nan=True)

    def test_normalization_gives_finite_defaults_to_columns_with_zero_observations(self, tmp_path):
        # A column with ZERO finite observations anywhere in the fit window
        # (here: trailing_ret_20/realized_vol_20 need 20 prior rows, but the
        # cutoff only admits 6) used to store NaN mean/std; octopus_model's
        # _normalize() then turns EVERY row of that feature into 0.0
        # forever, even once real data becomes available later in the
        # replay, silently discarding part of the input (Codex review
        # regression: "Give wholly missing normalization columns finite
        # defaults").
        clean_dir, _perturbed_dir, _cutoff = self._build_archives(tmp_path)
        probe = make_synthetic_ohlcv(seed=1, market_returns=make_market_returns(), market_beta=1.0)
        early_cutoff = probe["Date"].iloc[5]  # inside every ticker's 20-row trailing warm-up
        panel = features.build_panel(clean_dir, initial_train_end=early_cutoff)
        assert panel.normalization["fell_back_to_partial_rows"] is True
        index = features.FEATURE_NAMES.index("trailing_ret_20")
        assert not np.isnan(panel.normalization["mean"][index])
        assert not np.isnan(panel.normalization["std"][index])
        assert panel.normalization["mean"][index] == 0.0
        assert panel.normalization["std"][index] == 1.0

    def test_market_summary_before_cutoff_is_unaffected_by_future(self, tmp_path):
        clean_dir, perturbed_dir, cutoff = self._build_archives(tmp_path)
        clean = features.build_panel(clean_dir, initial_train_end=cutoff)
        perturbed = features.build_panel(perturbed_dir, initial_train_end=cutoff)
        past_positions = [
            position for date, position in clean.calendar_pos.items() if date <= pd.Timestamp(cutoff)
        ]
        np.testing.assert_array_equal(
            clean.market_summary[past_positions], perturbed.market_summary[past_positions]
        )


def test_nat_initial_train_end_means_no_window_not_zero_rows(synthetic_history_dir):
    # pd.Timestamp(None) is NaT; an unguarded NaT cutoff would match zero
    # rows and silently trip the full-history fallback (review regression).
    import pandas as pd

    with_none = features.build_panel(synthetic_history_dir, initial_train_end=None)
    with_nat = features.build_panel(synthetic_history_dir, initial_train_end=pd.NaT)
    assert with_nat.normalization["mean"] == with_none.normalization["mean"]
    assert with_nat.normalization["fell_back_to_partial_rows"] is False


def test_end_includes_a_bar_with_a_non_midnight_timestamp(tmp_path):
    # A raw archive Date column can carry a non-midnight timestamp.
    # load_history_csv() now normalizes it to the calendar day at
    # ingestion (data.py: "Calendar-day precision, not a raw timestamp"),
    # so by the time this truncation runs the bar's date is already
    # midnight-aligned -- but build_panel()'s OWN .dt.normalize() on both
    # sides of the --end comparison stays as defense in depth for a panel
    # built from data that bypasses load_history_csv() (Codex review
    # regression: "Treat --end as an inclusive calendar day"). Comparing
    # a raw (non-normalized) timestamp directly against pd.Timestamp(end)
    # (midnight) used to drop every bar later than midnight on --end's
    # own day, ending the replay one trading day early despite
    # build_panel() documenting end as inclusive of its whole day.
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    frame = make_synthetic_ohlcv(seed=1)
    last_index = frame.index[-1]
    end_day = frame.loc[last_index, "Date"]  # midnight, as an ISO --end value would parse
    frame.loc[last_index, "Date"] = end_day + pd.Timedelta(hours=16)  # an intraday-stamped final bar
    write_ticker_csv(history_dir, "AAA", frame)

    panel = features.build_panel(history_dir, end=end_day)
    series = panel.series["AAA"]
    assert len(series.dates) == len(frame)  # the intraday-stamped final bar survived truncation
    assert series.dates[-1] == np.datetime64(end_day)  # normalized to the calendar day, not the raw timestamp


class TestMarketFeatures:
    def test_market_median_matches_hand_computation(self, tmp_path):
        history = tmp_path / "ticker_data"
        history.mkdir()
        for ticker, seed in (("AAA", 21), ("BBB", 22), ("CCC", 23)):
            write_ticker_csv(history, ticker, make_synthetic_ohlcv(n=80, seed=seed))
        panel = features.build_panel(history, min_history=30)
        date = pd.Timestamp(panel.calendar[40])
        returns = []
        for ticker in panel.tickers:
            series = panel.series[ticker]
            row = int(np.where(series.dates == np.datetime64(date))[0][0])
            returns.append(series.features[row, features.FEATURE_NAMES.index("ret_close")])
        median_column = features.FEATURE_NAMES.index("mkt_median_ret")
        series = panel.series["AAA"]
        row = int(np.where(series.dates == np.datetime64(date))[0][0])
        assert series.features[row, median_column] == pytest.approx(float(np.median(returns)), rel=1e-5)

    def test_cross_sectional_rank_spans_zero_to_one(self, synthetic_history_dir):
        panel = features.build_panel(synthetic_history_dir, min_history=60)
        rank_column = features.FEATURE_NAMES.index("cs_return_rank")
        date = pd.Timestamp(panel.series["AAA"].dates[200])
        ranks = []
        for ticker, row in panel.date_index[date]:
            value = panel.series[ticker].features[row, rank_column]
            if np.isfinite(value):
                ranks.append(float(value))
        assert min(ranks) == 0.0
        assert max(ranks) == 1.0

    def test_market_summary_aligns_with_calendar(self, synthetic_history_dir):
        panel = features.build_panel(synthetic_history_dir, min_history=60)
        assert panel.market_summary.shape == (len(panel.calendar), len(features.MARKET_SUMMARY_NAMES))
        position = panel.calendar_pos[pd.Timestamp(panel.calendar[100])]
        assert position == 100
