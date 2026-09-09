"""Tests for features.py -- the no-lookahead regression test lives here."""

from __future__ import annotations

import data
import features
import numpy as np
import pandas as pd
import pytest

import conftest

make_market_returns = conftest.make_market_returns
make_synthetic_ohlcv = conftest.make_synthetic_ohlcv
write_ticker_csv = conftest.write_ticker_csv
compute_ticker_features = features.compute_ticker_features
build_panel = features.build_panel
FEATURE_NAMES = features.FEATURE_NAMES
MARKET_SUMMARY_NAMES = features.MARKET_SUMMARY_NAMES


def _adjusted(frame):
    adjusted, _ = data.adjusted_ohlc(frame)
    return adjusted


class TestComputeTickerFeatures:
    def test_target_alignment_is_next_row_vs_this_close(self):
        frame = _adjusted(make_synthetic_ohlcv(n=40, seed=11))
        computed = compute_ticker_features(frame)
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
        computed = compute_ticker_features(frame)
        # Row 29 is the last row before the gap; its label end is the first
        # row AFTER the gap -- a ticker's t+1 is its own next trading row.
        assert computed.loc[29, "label_end_date"] == frame.loc[30, "Date"]
        assert (frame.loc[30, "Date"] - frame.loc[29, "Date"]).days > 10

    def test_doji_bar_close_position_is_neutral(self):
        frame = _adjusted(make_synthetic_ohlcv(n=30, seed=13))
        price = frame.loc[10, "Close"]
        frame.loc[10, ["Open", "High", "Low", "Close"]] = price
        computed = compute_ticker_features(frame)
        assert computed.loc[10, "close_position"] == 0.5

    def test_missing_volume_degrades_gracefully(self):
        frame = _adjusted(make_synthetic_ohlcv(n=40, seed=14, with_volume=False))
        computed = compute_ticker_features(frame)
        assert (computed["volume_z"] == 0.0).all()
        assert (computed["volume_missing"] == 1.0).all()

    def test_per_row_missing_volume_is_flagged_per_row(self):
        # A ticker with volume on most days must flag exactly the NaN days,
        # not report volume_missing == 0 everywhere (review regression).
        frame = _adjusted(make_synthetic_ohlcv(n=60, seed=16))
        frame.loc[30, "Volume"] = np.nan
        frame.loc[31, "Volume"] = -5.0  # corrupt negative counts as missing too
        computed = compute_ticker_features(frame)
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
        computed = compute_ticker_features(frame)
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
        computed = compute_ticker_features(frame)
        tail = computed.loc[41:59, "volume_z"]
        assert not (tail == 0.0).all()
        assert np.isfinite(tail).all()

    def test_warmup_rows_carry_nan_not_garbage(self):
        frame = _adjusted(make_synthetic_ohlcv(n=40, seed=15))
        computed = compute_ticker_features(frame)
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
        clean = build_panel(clean_dir, initial_train_end=cutoff)
        perturbed = build_panel(perturbed_dir, initial_train_end=cutoff)
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
        clean = build_panel(clean_dir, initial_train_end=cutoff)
        perturbed = build_panel(perturbed_dir, initial_train_end=cutoff)
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
        clean = build_panel(clean_dir, initial_train_end=early_cutoff)
        perturbed = build_panel(perturbed_dir, initial_train_end=early_cutoff)
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
        panel = build_panel(clean_dir, initial_train_end=early_cutoff)
        assert panel.normalization["fell_back_to_partial_rows"] is True
        index = FEATURE_NAMES.index("trailing_ret_20")
        assert not np.isnan(panel.normalization["mean"][index])
        assert not np.isnan(panel.normalization["std"][index])
        assert panel.normalization["mean"][index] == 0.0
        assert panel.normalization["std"][index] == 1.0

    def test_market_summary_before_cutoff_is_unaffected_by_future(self, tmp_path):
        clean_dir, perturbed_dir, cutoff = self._build_archives(tmp_path)
        clean = build_panel(clean_dir, initial_train_end=cutoff)
        perturbed = build_panel(perturbed_dir, initial_train_end=cutoff)
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

    with_none = build_panel(synthetic_history_dir, initial_train_end=None)
    with_nat = build_panel(synthetic_history_dir, initial_train_end=pd.NaT)
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

    panel = build_panel(history_dir, end=end_day)
    series = panel.series["AAA"]
    assert len(series.dates) == len(frame)  # the intraday-stamped final bar survived truncation
    assert series.dates[-1] == np.datetime64(end_day)  # normalized to the calendar day, not the raw timestamp


class TestMarketFeatures:
    def test_market_median_matches_hand_computation(self, tmp_path):
        history = tmp_path / "ticker_data"
        history.mkdir()
        for ticker, seed in (("AAA", 21), ("BBB", 22), ("CCC", 23)):
            write_ticker_csv(history, ticker, make_synthetic_ohlcv(n=80, seed=seed))
        panel = build_panel(history, min_history=30)
        date = pd.Timestamp(panel.calendar[40])
        returns = []
        for ticker in panel.tickers:
            series = panel.series[ticker]
            row = int(np.where(series.dates == np.datetime64(date))[0][0])
            returns.append(series.features[row, FEATURE_NAMES.index("ret_close")])
        median_column = FEATURE_NAMES.index("mkt_median_ret")
        series = panel.series["AAA"]
        row = int(np.where(series.dates == np.datetime64(date))[0][0])
        assert series.features[row, median_column] == pytest.approx(float(np.median(returns)), rel=1e-5)

    def test_cross_sectional_rank_spans_zero_to_one(self, synthetic_history_dir):
        panel = build_panel(synthetic_history_dir, min_history=60)
        rank_column = FEATURE_NAMES.index("cs_return_rank")
        date = pd.Timestamp(panel.series["AAA"].dates[200])
        ranks = []
        for ticker, row in panel.date_index[date]:
            value = panel.series[ticker].features[row, rank_column]
            if np.isfinite(value):
                ranks.append(float(value))
        assert min(ranks) == 0.0
        assert max(ranks) == 1.0

    def test_market_summary_aligns_with_calendar(self, synthetic_history_dir):
        panel = build_panel(synthetic_history_dir, min_history=60)
        assert panel.market_summary.shape == (len(panel.calendar), len(MARKET_SUMMARY_NAMES))
        position = panel.calendar_pos[pd.Timestamp(panel.calendar[100])]
        assert position == 100


# ---------------------------------------------------------------------------
# Feature-set extension (TDD: these fail until features.py grows the columns).
#
# Seven additions, in three groups, all computable from data at-or-before
# their own row so the existing TestNoLookahead perturbation tests cover them
# for free once implemented (those compare the WHOLE feature matrix):
#
#   liquidity / mean-reversion / vol-regime
#     dollar_volume_z   -- volume_z ignores price level; a $5 and a $500 name
#                          with equal share volume are not equally liquid
#     dist_from_sma20   -- mean-reversion pressure, distinct from
#                          trailing_ret_20 (a return, not a distance)
#     realized_vol_5    -- short leg of a vol-regime pair against
#                          realized_vol_20 (vol clustering)
#   continuous momentum (bounded oscillators, fed as FEATURES not as rules --
#   the repo's 60-minute trend-validation gate is a hard filter; here the same
#   information enters as something the GBM can split on and the octopus can
#   attend to)
#     rsi_14            -- Wilder RSI, rescaled to [0, 1]
#     adx_14            -- Wilder ADX (trend STRENGTH, direction-free),
#                          rescaled to [0, 1]
#   cross-sectional peer ranking (cs_return_rank ranks ONE day's return; these
#   rank the same universe on horizon and on risk)
#     cs_momentum_rank  -- rank of trailing_ret_20 across that day's universe
#     cs_vol_rank       -- rank of realized_vol_20 across that day's universe
# ---------------------------------------------------------------------------

PER_TICKER_FEATURES = features.PER_TICKER_FEATURES
CROSS_SECTIONAL_FEATURES = features.CROSS_SECTIONAL_FEATURES

#: The pre-extension names, in their pre-extension order. New features append;
#: they never reorder. Index stability inside a group is not cosmetic -- a
#: reordered group silently remaps every frozen normalization statistic and
#: every checkpointed input projection onto the wrong column.
_LEGACY_PER_TICKER = (
    "ret_open", "ret_high", "ret_low", "ret_close", "intraday_range", "close_position",
    "trailing_ret_5", "trailing_ret_20", "realized_vol_20", "volume_z", "volume_missing", "gap_days",
)


def _frame_from_closes(closes, *, volumes=None, start="2021-01-04"):
    """A bar-consistent OHLCV frame around an explicit close path.

    Open is the prior close (a flat overnight), and High/Low straddle the body
    by 20bp, so High >= max(Open, Close) and Low <= min(Open, Close) hold by
    construction and data.adjusted_ohlc() never drops a row. Adj Close == Close
    keeps adj_ratio at 1.0, so a test's hand-computation on `closes` is exactly
    what compute_ticker_features() sees.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    frame = pd.DataFrame(
        {
            "Date": pd.bdate_range(start=start, periods=n),
            "Open": opens,
            "High": np.maximum(opens, closes) * 1.002,
            "Low": np.minimum(opens, closes) * 0.998,
            "Close": closes,
            "Adj Close": closes,
            "Dividends": np.zeros(n),
            "Stock Splits": np.zeros(n),
            "Volume": np.full(n, 1_000_000.0) if volumes is None else np.asarray(volumes, dtype=float),
        }
    )
    return frame


def _flat_frame(n=60, price=100.0):
    """A perfectly flat series: Open == High == Low == Close every row.

    True range is 0 on every bar, which is the degenerate input that must
    produce a documented neutral value rather than NaN/inf from a 0/0.
    """
    return pd.DataFrame(
        {
            "Date": pd.bdate_range(start="2021-01-04", periods=n),
            "Open": np.full(n, price),
            "High": np.full(n, price),
            "Low": np.full(n, price),
            "Close": np.full(n, price),
            "Adj Close": np.full(n, price),
            "Dividends": np.zeros(n),
            "Stock Splits": np.zeros(n),
            "Volume": np.full(n, 1_000_000.0),
        }
    )


def _trending_closes(n=60, *, step=0.01, start_price=100.0):
    """A strictly monotonic close path; `step` < 0 trends down."""
    return start_price * np.cumprod(np.full(n, 1.0 + step))


class TestFeatureNameRegistry:
    """The tuple layout itself is a contract -- pin it before the math."""

    #: Computable from ONE ticker's own bars -> compute_ticker_features().
    NEW_PER_TICKER = (
        "dollar_volume_z", "dist_from_sma20", "realized_vol_5", "rsi_14", "adx_14",
        "intraday_ret", "updown_streak", "drawdown_from_20d_high", "ret_skew_20",
        "ret_kurt_20", "amihud_illiquidity",
    )
    #: Need the pooled universe (peer ranks, the market series, the style
    #: clusters) -> the cross-sectional pass in build_panel().
    NEW_CROSS_SECTIONAL = (
        "cs_momentum_rank", "cs_vol_rank", "mkt_beta_20", "cluster_ret_median", "cs_cluster_rank",
    )

    def test_new_names_are_registered_in_their_groups(self):
        for name in self.NEW_PER_TICKER:
            assert name in PER_TICKER_FEATURES, f"{name} must be a per-ticker feature"
        for name in self.NEW_CROSS_SECTIONAL:
            assert name in CROSS_SECTIONAL_FEATURES, f"{name} must be a cross-sectional feature"

    def test_existing_names_keep_their_positions(self):
        # Append-only: the legacy names stay the leading prefix of their own
        # group, and cs_return_rank stays first among the cross-sectional
        # features. A reorder remaps frozen normalization stats onto the
        # wrong columns without any error surfacing.
        assert PER_TICKER_FEATURES[: len(_LEGACY_PER_TICKER)] == _LEGACY_PER_TICKER
        assert CROSS_SECTIONAL_FEATURES[0] == "cs_return_rank"

    def test_feature_names_is_the_three_groups_concatenated_without_duplicates(self):
        assert FEATURE_NAMES == PER_TICKER_FEATURES + CROSS_SECTIONAL_FEATURES + features.MARKET_FEATURES
        assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)

    def test_every_registered_feature_is_actually_computed(self, synthetic_history_dir):
        # build_panel() selects FEATURE_NAMES out of the joined frame, so a
        # name registered but never computed raises there rather than
        # producing a silently-empty column -- assert the panel builds and
        # that each new column has real, non-constant values after warm-up.
        panel = build_panel(synthetic_history_dir, min_history=60)
        series = panel.series["AAA"]
        for name in self.NEW_PER_TICKER + self.NEW_CROSS_SECTIONAL:
            column = series.features[:, FEATURE_NAMES.index(name)]
            settled = column[120:]
            assert np.isfinite(settled).all(), f"{name} must be finite once warmed up"
            assert settled.std() > 0.0, f"{name} must vary, not be a constant placeholder"


class TestDollarVolumeZ:
    def test_matches_the_rolling_zscore_of_log_dollar_volume(self):
        rng = np.random.default_rng(31)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.02, size=25))
        volumes = rng.integers(500_000, 5_000_000, size=25).astype(float)
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes, volumes=volumes)))
        # Same shape as volume_z: log1p of the raw quantity, a 20-row rolling
        # window with min_periods=1, pandas' default ddof=1 standard deviation.
        log_dollar = np.log1p(closes * volumes)
        window = log_dollar[5:25]
        expected = (log_dollar[24] - window.mean()) / window.std(ddof=1)
        assert computed.loc[24, "dollar_volume_z"] == pytest.approx(expected, rel=1e-9)

    def test_is_not_a_restatement_of_volume_z(self):
        # Constant share volume, moving price: volume_z is pinned at its
        # neutral 0.0 (zero rolling std) while participation in DOLLARS is
        # plainly changing. If dollar_volume_z were just volume_z under a new
        # name it would be flat here too, and the feature would be dead weight.
        closes = _trending_closes(n=60, step=0.01)
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        assert (computed["volume_z"] == 0.0).all()
        assert np.abs(computed.loc[5:, "dollar_volume_z"]).max() > 0.0

    def test_missing_volume_rows_are_neutral_and_never_poison_the_window(self):
        # Exactly the contract volume_z already carries: a NaN / negative /
        # infinite volume row degrades to the neutral 0.0, is flagged through
        # volume_missing, and is EXCLUDED from the rolling statistics rather
        # than clipped into them (min_periods=1 keeps the neighbours real).
        frame = _adjusted(make_synthetic_ohlcv(n=80, seed=32))
        frame["Volume"] = frame["Volume"].astype(float)
        frame.loc[40, "Volume"] = np.nan
        frame.loc[41, "Volume"] = -5.0
        frame.loc[42, "Volume"] = np.inf
        computed = compute_ticker_features(frame)
        for row in (40, 41, 42):
            assert computed.loc[row, "volume_missing"] == 1.0
            assert computed.loc[row, "dollar_volume_z"] == 0.0
        assert np.isfinite(computed["dollar_volume_z"]).all()
        neighbours = computed.loc[43:61, "dollar_volume_z"]
        assert not (neighbours == 0.0).all(), "one corrupt row must not blank a month of z-scores"

    def test_absent_volume_column_degrades_to_neutral(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=40, seed=33, with_volume=False)))
        assert (computed["dollar_volume_z"] == 0.0).all()
        assert (computed["volume_missing"] == 1.0).all()


class TestDistanceFromMovingAverage:
    def test_matches_log_close_over_trailing_20_row_mean(self):
        rng = np.random.default_rng(34)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.018, size=40))
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        expected = np.log(closes[30] / closes[11:31].mean())
        assert computed.loc[30, "dist_from_sma20"] == pytest.approx(expected, rel=1e-9)

    def test_warmup_rows_are_nan_not_zero(self):
        # A 20-row mean is undefined before 20 rows exist. NaN is the honest
        # answer (the harness's all-features-finite eligibility check then
        # simply holds the ticker back); 0.0 would be a fabricated "exactly
        # at its average" reading for every new listing's first month.
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=60, seed=35)))
        assert np.isnan(computed.loc[10, "dist_from_sma20"])
        assert np.isfinite(computed.loc[25, "dist_from_sma20"])

    def test_sign_tracks_position_relative_to_the_mean(self):
        rising = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=60, step=0.01))))
        falling = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=60, step=-0.01))))
        assert rising.loc[40, "dist_from_sma20"] > 0
        assert falling.loc[40, "dist_from_sma20"] < 0

    def test_flat_series_sits_exactly_on_its_mean(self):
        computed = compute_ticker_features(_adjusted(_flat_frame(n=60)))
        assert computed.loc[40, "dist_from_sma20"] == pytest.approx(0.0, abs=1e-12)


class TestShortHorizonRealizedVol:
    def test_matches_the_5_row_std_of_close_returns(self):
        rng = np.random.default_rng(36)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=40))
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        returns = np.log(closes[1:] / closes[:-1])
        # returns[k] is frame row k+1, so row 30's five-row window is rows
        # 26..30 == returns[25:30]. The original slice here was returns[26:31]
        # (rows 27..31), one day into the future, and a correct causal
        # implementation would have failed this specification (Copilot review:
        # "This expected window is shifted one day forward").
        expected = returns[25:30].std(ddof=1)
        assert computed.loc[30, "realized_vol_5"] == pytest.approx(expected, rel=1e-9)

    def test_warmup_rows_are_nan(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=40, seed=37)))
        assert np.isnan(computed.loc[3, "realized_vol_5"])
        assert np.isfinite(computed.loc[10, "realized_vol_5"])

    def test_reacts_to_a_vol_regime_shift_before_the_20_row_window_does(self):
        # The whole point of carrying BOTH horizons: their ratio is the
        # regime signal. Calm for 100 rows, then a burst -- five days in, the
        # short window is entirely inside the burst while the long window is
        # still three-quarters calm.
        rng = np.random.default_rng(38)
        calm = rng.normal(0.0, 0.002, size=100)
        burst = rng.normal(0.0, 0.05, size=20)
        closes = 100.0 * np.cumprod(1.0 + np.concatenate([calm, burst]))
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        assert computed.loc[105, "realized_vol_5"] > computed.loc[105, "realized_vol_20"]


def _wilder_rsi(closes, period=14):
    """The rsi_14 contract, spelled out.

    Wilder smoothing, NOT a simple rolling mean: the first average is the
    plain mean of the first `period` gains/losses, and every later average
    retains (period-1)/period of the previous one. The saturation, bounds,
    flat-series and warm-up cases below pass under either scheme, so without
    this reference the specification does not actually say which one to
    implement (Copilot review: "These tests never distinguish Wilder
    smoothing from a simple rolling-average implementation").
    """
    closes = np.asarray(closes, dtype=float)
    delta = np.diff(closes)
    gain = np.maximum(delta, 0.0)
    loss = np.maximum(-delta, 0.0)
    out = np.full(len(closes), np.nan)
    if len(delta) < period:
        return out

    def level(average_gain, average_loss):
        if average_gain == 0.0 and average_loss == 0.0:
            return 0.5  # no movement at all: the documented neutral midpoint
        if average_loss == 0.0:
            return 1.0  # all gains: RS unbounded, RSI saturates
        return (100.0 - 100.0 / (1.0 + average_gain / average_loss)) / 100.0

    average_gain = gain[:period].mean()
    average_loss = loss[:period].mean()
    out[period] = level(average_gain, average_loss)
    for row in range(period + 1, len(closes)):
        average_gain = (average_gain * (period - 1) + gain[row - 1]) / period
        average_loss = (average_loss * (period - 1) + loss[row - 1]) / period
        out[row] = level(average_gain, average_loss)
    return out


def _wilder_adx(high, low, close, period=14):
    """The adx_14 contract: Wilder directional movement, doubly smoothed."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), np.nan)
    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    true_range = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])]
    )

    def wilder_sum(values):
        """Running sum seeded with the first `period` values, then decayed."""
        result = np.full(len(values), np.nan)
        if len(values) < period:
            return result
        accumulator = values[:period].sum()
        result[period - 1] = accumulator
        for index in range(period, len(values)):
            accumulator = accumulator - accumulator / period + values[index]
            result[index] = accumulator
        return result

    smoothed_tr = wilder_sum(true_range)
    smoothed_plus = wilder_sum(plus_dm)
    smoothed_minus = wilder_sum(minus_dm)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus_di = np.where(smoothed_tr > 0, 100.0 * smoothed_plus / smoothed_tr, 0.0)
        minus_di = np.where(smoothed_tr > 0, 100.0 * smoothed_minus / smoothed_tr, 0.0)
        spread = plus_di + minus_di
        dx = np.where(spread > 0, 100.0 * np.abs(plus_di - minus_di) / spread, 0.0)
    adx = np.full(len(dx), np.nan)
    first = period - 1  # dx is only meaningful once the smoothed sums exist
    if len(dx) >= first + period:
        accumulator = dx[first : first + period].mean()
        adx[first + period - 1] = accumulator
        for index in range(first + period, len(dx)):
            accumulator = (accumulator * (period - 1) + dx[index]) / period
            adx[index] = accumulator
    out[1:] = adx / 100.0
    return out


class TestContinuousMomentumOscillators:
    """RSI and ADX as bounded [0, 1] features, not as threshold rules."""

    def test_rsi_matches_wilder_smoothing_on_a_non_degenerate_path(self):
        # A mixed up/down path, where Wilder's decayed average and a simple
        # 14-row rolling mean genuinely disagree.
        rng = np.random.default_rng(64)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=80))
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        expected = _wilder_rsi(closes)
        for row in (14, 20, 40, 79):
            assert computed.loc[row, "rsi_14"] == pytest.approx(expected[row], rel=1e-9), row
        # Guard the guard: a simple rolling mean must NOT satisfy the above,
        # or this test would pass against the implementation it exists to
        # rule out.
        returns = pd.Series(np.concatenate([[np.nan], np.diff(closes)]))
        simple_gain = returns.clip(lower=0).rolling(14).mean()
        simple_loss = (-returns.clip(upper=0)).rolling(14).mean()
        simple = (1.0 - 1.0 / (1.0 + simple_gain / simple_loss)).to_numpy()
        assert not np.isclose(simple[40], expected[40], rtol=1e-6)

    def test_adx_matches_wilder_smoothing_on_a_non_degenerate_path(self):
        rng = np.random.default_rng(65)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=100))
        frame = _adjusted(_frame_from_closes(closes))
        computed = compute_ticker_features(frame)
        expected = _wilder_adx(frame["High"], frame["Low"], frame["Close"])
        for row in (27, 40, 70, 99):
            assert computed.loc[row, "adx_14"] == pytest.approx(expected[row], rel=1e-9), row

    def test_rsi_is_one_when_every_close_rises(self):
        # All gains, zero losses: RS is unbounded, RSI saturates at 100 ->
        # 1.0 on the rescaled feature. The zero-loss division must saturate,
        # not produce inf/NaN.
        computed = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=60, step=0.01))))
        assert computed.loc[40, "rsi_14"] == pytest.approx(1.0, abs=1e-9)

    def test_rsi_is_zero_when_every_close_falls(self):
        computed = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=60, step=-0.01))))
        assert computed.loc[40, "rsi_14"] == pytest.approx(0.0, abs=1e-9)

    def test_rsi_is_neutral_on_a_flat_series(self):
        # Neither gains nor losses: 0/0. 0.5 is the neutral midpoint, same
        # graceful-degradation spirit as close_position's doji fallback.
        computed = compute_ticker_features(_adjusted(_flat_frame(n=60)))
        assert computed.loc[40, "rsi_14"] == pytest.approx(0.5, abs=1e-12)

    def test_rsi_stays_inside_the_unit_interval(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=200, seed=39)))
        settled = computed.loc[30:, "rsi_14"]
        assert np.isfinite(settled).all()
        assert settled.min() >= 0.0
        assert settled.max() <= 1.0

    def test_rsi_warmup_rows_are_nan(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=200, seed=40)))
        assert np.isnan(computed.loc[5, "rsi_14"])
        assert np.isfinite(computed.loc[40, "rsi_14"])

    def test_adx_saturates_on_a_one_directional_trend(self):
        # Bars march monotonically in one direction, so one side of the
        # directional movement is zero on every row: DX = 100 -> 1.0. ADX is
        # direction-FREE, so the up and down trends both saturate -- that is
        # what makes it non-redundant with rsi_14 and trailing_ret_20.
        rising = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=80, step=0.01))))
        falling = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=80, step=-0.01))))
        assert rising.loc[60, "adx_14"] == pytest.approx(1.0, abs=1e-6)
        assert falling.loc[60, "adx_14"] == pytest.approx(1.0, abs=1e-6)

    def test_adx_is_zero_on_a_flat_series_not_nan(self):
        # True range is 0 on every bar: both the ATR normalization and the
        # DI+ / DI- sum are 0. "No measurable trend" is 0.0; an unguarded
        # 0/0 here would make the whole row non-finite and the harness would
        # silently drop the ticker's forecast for that day.
        computed = compute_ticker_features(_adjusted(_flat_frame(n=80)))
        assert computed.loc[60, "adx_14"] == pytest.approx(0.0, abs=1e-12)

    def test_adx_stays_inside_the_unit_interval(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=200, seed=41)))
        settled = computed.loc[60:, "adx_14"]
        assert np.isfinite(settled).all()
        assert settled.min() >= 0.0
        assert settled.max() <= 1.0

    def test_adx_warmup_rows_are_nan(self):
        # ADX is doubly smoothed (14 rows of DX, itself needing 14 rows of
        # directional movement), so its warm-up is longer than RSI's.
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=200, seed=42)))
        assert np.isnan(computed.loc[5, "adx_14"])
        assert np.isfinite(computed.loc[60, "adx_14"])

    def test_choppy_market_scores_lower_than_a_clean_trend(self):
        # "Choppy" has to mean the HIGH/LOW ENVELOPE turns, not merely that
        # daily returns alternate sign. Alternating +2%/-2% returns compound
        # to a slow net decline, which drags the highs and the lows
        # monotonically downward -- a clean downtrend that ADX is right to
        # score at 1.0. A drift-free oscillation is the real contrast case.
        trend = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=120, step=0.005))))
        rng = np.random.default_rng(66)
        oscillation = 100.0 * (1.0 + 0.02 * np.sin(2 * np.pi * np.arange(120) / 8.0))
        chop = compute_ticker_features(
            _adjusted(_frame_from_closes(oscillation * (1.0 + rng.normal(0.0, 0.0015, size=120))))
        )
        assert chop.loc[100, "adx_14"] < 0.25
        assert chop.loc[100, "adx_14"] < trend.loc[100, "adx_14"]


class TestCrossSectionalPeerRanks:
    """cs_return_rank ranks one day's return; these rank horizon and risk."""

    def test_momentum_rank_extremes_match_the_underlying_ordering(self, synthetic_history_dir):
        panel = build_panel(synthetic_history_dir, min_history=60)
        self._assert_rank_matches_source(panel, source="trailing_ret_20", rank="cs_momentum_rank")

    def test_vol_rank_extremes_match_the_underlying_ordering(self, synthetic_history_dir):
        panel = build_panel(synthetic_history_dir, min_history=60)
        self._assert_rank_matches_source(panel, source="realized_vol_20", rank="cs_vol_rank")

    @staticmethod
    def _assert_rank_matches_source(panel, *, source, rank):
        source_column = FEATURE_NAMES.index(source)
        rank_column = FEATURE_NAMES.index(rank)
        date = pd.Timestamp(panel.series["AAA"].dates[200])
        observed = []
        for ticker, row in panel.date_index[date]:
            value = panel.series[ticker].features[row, source_column]
            ranked = panel.series[ticker].features[row, rank_column]
            if np.isfinite(value):
                observed.append((value, ranked))
        assert len(observed) > 2, "need a real peer group on this date"
        observed.sort()
        # Rank is a [0, 1] position within that day's universe: the smallest
        # source value sits at 0.0, the largest at 1.0, and the mapping is
        # monotonic in between.
        assert observed[0][1] == pytest.approx(0.0)
        assert observed[-1][1] == pytest.approx(1.0)
        ranks = [ranked for _value, ranked in observed]
        assert ranks == sorted(ranks)

    def test_a_ticker_trading_alone_gets_the_neutral_midpoint(self, tmp_path):
        # Same rule cs_return_rank already follows: with no peers there is no
        # ranking to do, and 0.5 is the neutral placeholder. Ranking a lone
        # ticker as 0.0 or 1.0 would tell the model it was the day's worst or
        # best name in the market.
        history = tmp_path / "ticker_data"
        history.mkdir()
        write_ticker_csv(history, "AAA", make_synthetic_ohlcv(n=200, seed=43))
        panel = build_panel(history, min_history=60)
        series = panel.series["AAA"]
        for name in ("cs_momentum_rank", "cs_vol_rank"):
            column = series.features[:, FEATURE_NAMES.index(name)]
            settled = column[60:]
            np.testing.assert_allclose(settled, 0.5)

    def test_ranks_are_nan_while_their_source_feature_is_still_warming_up(self, synthetic_history_dir):
        # The rank inherits its source's warm-up: a ticker with no
        # trailing_ret_20 yet has no momentum to rank, and inventing a 0.5
        # there would put every new listing at the exact centre of the
        # universe for its first month.
        panel = build_panel(synthetic_history_dir, min_history=60)
        series = panel.series["AAA"]
        assert np.isnan(series.features[10, FEATURE_NAMES.index("cs_momentum_rank")])
        assert np.isnan(series.features[10, FEATURE_NAMES.index("cs_vol_rank")])

    def test_ranks_stay_inside_the_unit_interval(self, synthetic_history_dir):
        panel = build_panel(synthetic_history_dir, min_history=60)
        for ticker in panel.tickers:
            for name in ("cs_momentum_rank", "cs_vol_rank"):
                column = panel.series[ticker].features[:, FEATURE_NAMES.index(name)]
                settled = column[np.isfinite(column)]
                assert settled.min() >= 0.0
                assert settled.max() <= 1.0


# ---------------------------------------------------------------------------
# Second extension batch: bar decomposition, streaks, drawdown, distribution
# shape, price impact, market loading, and correlation-based style clustering.
#
# Same filter as the first batch -- pure OHLCV-derivable and causal by
# construction -- with one addition that is a genuine architecture change
# rather than a column: the clustering below adds per-row state to
# TickerSeries and a periodic refit inside build_panel. It is called out
# separately in the docs for that reason.
#
# NOTE ON NAMING: `ret_open` is ALREADY log(Open_t / Close_{t-1}) -- it is the
# overnight gap. A second column computing the same quantity would be
# perfectly collinear with it. The half that is genuinely missing is the
# INTRADAY leg, log(Close_t / Open_t) -- exactly the difference that isolates
# gap-fill behaviour from the overnight move. It is named `intraday_ret`.
# ---------------------------------------------------------------------------

#: Consecutive-day streaks are clipped like gap_days: past ten sessions in one
#: direction the count carries no more signal than "extended" and would
#: otherwise dominate the frozen normalization statistics.
_STREAK_CLIP = 10.0

#: Amihud's raw ratio for a liquid US equity lands around 1e-10; multiplying
#: before log1p keeps the feature out of log1p's linear regime, where it would
#: just be a rescaled |return| and carry no liquidity information at all.
_AMIHUD_SCALE = 1e9


def _write_archive(history_dir, frames):
    history_dir.mkdir(parents=True, exist_ok=True)
    for ticker, frame in frames.items():
        write_ticker_csv(history_dir, ticker, frame)
    return history_dir


def _factor_archive(groups, *, n=260, idio_vol=0.0002, factor_vol=0.015, seed=90):
    """Frames whose tickers fall into deliberate correlation blocks.

    Each group gets its OWN independent daily factor and every member follows
    it almost exactly -- a little idiosyncratic noise so the correlation
    matrix stays well conditioned, but the block structure is unmistakable.
    This is the ground truth the style clustering has to recover without
    being told any sector, industry, or fundamental fact.
    """
    rng = np.random.default_rng(seed)
    frames = {}
    for group_index, (_label, tickers) in enumerate(sorted(groups.items())):
        factor = rng.normal(0.0003, factor_vol, size=n)
        for member_index, ticker in enumerate(tickers):
            noise = rng.normal(0.0, idio_vol, size=n)
            closes = 50.0 * np.cumprod(1.0 + factor + noise)
            volumes = np.full(n, 1_000_000.0 + 1000.0 * (group_index * 10 + member_index))
            frames[ticker] = _frame_from_closes(closes, volumes=volumes)
    return frames


def _identical_ticker_archive(tickers=("AAA", "BBB", "CCC"), *, n=260, seed=91):
    """Tickers with IDENTICAL return paths.

    Then the equal-weight market mean return IS each ticker's own return, so
    every ticker's rolling market beta must come out at exactly 1.0 -- an
    exact expected value, not an approximate one.
    """
    rng = np.random.default_rng(seed)
    closes = 50.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.015, size=n))
    return {ticker: _frame_from_closes(closes) for ticker in tickers}


def _perturb_future(frames, cutoff):
    """Scale every bar strictly after `cutoff` by 1.5 -- the leakage probe."""
    mutated = {}
    for ticker, frame in frames.items():
        copy = frame.copy()
        future = copy["Date"] > cutoff
        for column in ("Open", "High", "Low", "Close", "Adj Close"):
            copy.loc[future, column] = copy.loc[future, column] * 1.5
        mutated[ticker] = copy
    return mutated


class TestBarDecomposition:
    def test_intraday_ret_is_close_over_open(self):
        rng = np.random.default_rng(50)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.02, size=40))
        frame = _adjusted(_frame_from_closes(closes))
        computed = compute_ticker_features(frame)
        expected = np.log(frame.loc[25, "Close"] / frame.loc[25, "Open"])
        assert computed.loc[25, "intraday_ret"] == pytest.approx(expected, rel=1e-12)

    def test_intraday_and_overnight_legs_sum_back_to_the_daily_return(self):
        # ret_open is the overnight leg, intraday_ret the session leg, and
        # together they must reconstruct ret_close exactly. If this identity
        # fails, one of the three is measured against the wrong reference
        # price and the decomposition is not a decomposition.
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=60, seed=51)))
        rebuilt = computed["ret_open"] + computed["intraday_ret"]
        # atol as well as rtol: the identity is exact in real arithmetic but
        # not in floating point, and a daily return near 1e-5 makes a 1e-16
        # absolute rounding difference look like a 1e-12 RELATIVE one. The
        # absolute bound is the meaningful check here.
        np.testing.assert_allclose(
            rebuilt.iloc[1:], computed["ret_close"].iloc[1:], rtol=1e-9, atol=1e-12
        )

    def test_intraday_ret_needs_no_prior_row(self):
        # Unlike every other return feature here, this one is computed inside
        # a single bar -- row 0 is real data, not warm-up.
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=40, seed=52)))
        assert np.isfinite(computed.loc[0, "intraday_ret"])
        assert np.isnan(computed.loc[0, "ret_close"])


class TestUpDownStreak:
    def test_counts_signed_consecutive_same_direction_closes(self):
        # up, up, up, down, down -- the streak is a signed count, so direction
        # persistence is carried separately from the magnitude that
        # trailing_ret_5 / trailing_ret_20 already encode.
        closes = np.array([100.0, 101.0, 102.0, 103.0, 102.0, 101.0])
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        assert np.isnan(computed.loc[0, "updown_streak"])  # no prior close, no direction
        assert computed.loc[1, "updown_streak"] == 1.0
        assert computed.loc[2, "updown_streak"] == 2.0
        assert computed.loc[3, "updown_streak"] == 3.0
        assert computed.loc[4, "updown_streak"] == -1.0
        assert computed.loc[5, "updown_streak"] == -2.0

    def test_an_unchanged_close_breaks_the_streak(self):
        closes = np.array([100.0, 101.0, 102.0, 102.0, 103.0])
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        assert computed.loc[2, "updown_streak"] == 2.0
        assert computed.loc[3, "updown_streak"] == 0.0  # flat: neither up nor down
        assert computed.loc[4, "updown_streak"] == 1.0  # and the count restarts

    def test_long_streaks_are_clipped_like_gap_days(self):
        rising = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=40, step=0.01))))
        falling = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=40, step=-0.01))))
        assert rising.loc[30, "updown_streak"] == _STREAK_CLIP
        assert falling.loc[30, "updown_streak"] == -_STREAK_CLIP


class TestDrawdownFromRollingHigh:
    def test_matches_log_close_over_trailing_20_row_max(self):
        rng = np.random.default_rng(53)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=40))
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        expected = np.log(closes[30] / closes[11:31].max())
        assert computed.loc[30, "drawdown_from_20d_high"] == pytest.approx(expected, rel=1e-9)

    def test_is_never_positive_and_is_exactly_zero_at_a_new_high(self):
        # Distance from a PEAK, not from a mean: bounded above at 0 and only
        # ever negative. That asymmetry is what makes it behave differently
        # from dist_from_sma20 at turning points.
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=120, seed=54)))
        settled = computed.loc[25:, "drawdown_from_20d_high"]
        assert settled.max() <= 1e-12
        at_new_highs = compute_ticker_features(_adjusted(_frame_from_closes(_trending_closes(n=60, step=0.01))))
        assert at_new_highs.loc[40, "drawdown_from_20d_high"] == pytest.approx(0.0, abs=1e-12)

    def test_warmup_rows_are_nan(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=60, seed=55)))
        assert np.isnan(computed.loc[10, "drawdown_from_20d_high"])
        assert np.isfinite(computed.loc[25, "drawdown_from_20d_high"])


class TestReturnDistributionShape:
    """Rolling skew/kurtosis -- what the p10/p90 heads need to widen a band."""

    def test_skew_and_kurtosis_match_the_rolling_moments_of_ret_close(self):
        rng = np.random.default_rng(56)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=60))
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        returns = pd.Series(np.concatenate([[np.nan], np.log(closes[1:] / closes[:-1])]))
        assert computed.loc[40, "ret_skew_20"] == pytest.approx(
            returns.rolling(20).skew().iloc[40], rel=1e-9
        )
        # Fisher (excess) kurtosis, matching pandas' rolling kurt: 0 is
        # normal-tailed, positive is fat-tailed.
        assert computed.loc[40, "ret_kurt_20"] == pytest.approx(
            returns.rolling(20).kurt().iloc[40], rel=1e-9
        )

    def test_a_left_tail_shock_shows_up_as_negative_skew_and_fat_tails(self):
        rng = np.random.default_rng(57)
        returns = rng.normal(0.0, 0.004, size=60)
        returns[35] = -0.25  # one crash day inside the window ending at row 40
        closes = 100.0 * np.cumprod(1.0 + returns)
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes)))
        assert computed.loc[40, "ret_skew_20"] < -0.5
        assert computed.loc[40, "ret_kurt_20"] > 3.0

    def test_zero_variance_window_reports_neutral_shape_not_a_degenerate_limit(self):
        # A window with no return variation has no distributional shape.
        # 0.0/0.0 is the neutral reading; pandas' own degenerate excess
        # kurtosis for a constant window is -3.0, which the model would
        # otherwise read as an extremely THIN-tailed regime rather than as
        # "no information".
        computed = compute_ticker_features(_adjusted(_flat_frame(n=60)))
        assert computed.loc[40, "ret_skew_20"] == pytest.approx(0.0, abs=1e-12)
        assert computed.loc[40, "ret_kurt_20"] == pytest.approx(0.0, abs=1e-12)

    def test_warmup_rows_are_nan(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=60, seed=58)))
        for name in ("ret_skew_20", "ret_kurt_20"):
            assert np.isnan(computed.loc[10, name])
            assert np.isfinite(computed.loc[30, name])


class TestAmihudIlliquidity:
    def test_matches_scaled_log1p_of_return_per_dollar_traded(self):
        rng = np.random.default_rng(59)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=40))
        volumes = rng.integers(500_000, 5_000_000, size=40).astype(float)
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes, volumes=volumes)))
        ret = np.log(closes[30] / closes[29])
        expected = np.log1p(_AMIHUD_SCALE * abs(ret) / (closes[30] * volumes[30]))
        assert computed.loc[30, "amihud_illiquidity"] == pytest.approx(expected, rel=1e-9)

    def test_more_dollars_traded_for_the_same_move_means_less_price_impact(self):
        # The whole content of the measure: it is the move PER dollar traded,
        # so it must fall when the same move is absorbed by a deeper book.
        rng = np.random.default_rng(60)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=40))
        thin = compute_ticker_features(_adjusted(_frame_from_closes(closes, volumes=np.full(40, 1e5))))
        deep = compute_ticker_features(_adjusted(_frame_from_closes(closes, volumes=np.full(40, 1e8))))
        assert thin.loc[30, "amihud_illiquidity"] > deep.loc[30, "amihud_illiquidity"]

    def test_zero_or_missing_dollar_volume_is_neutral_not_infinite(self):
        # A zero-volume halt day would divide by zero. The row must degrade
        # to the documented neutral 0.0 -- an inf here fails the harness's
        # all-features-finite eligibility check and silently drops the
        # ticker's forecast for the day, the same failure mode the
        # infinite-volume_z regression above was written for.
        rng = np.random.default_rng(61)
        closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.02, size=40))
        volumes = np.full(40, 1_000_000.0)
        invalid_rows = {20: 0.0, 21: np.nan, 22: -5.0, 23: np.inf}
        for row, value in invalid_rows.items():
            volumes[row] = value
        computed = compute_ticker_features(_adjusted(_frame_from_closes(closes, volumes=volumes)))
        for row in invalid_rows:
            assert computed.loc[row, "amihud_illiquidity"] == 0.0
        assert np.isfinite(computed.loc[1:, "amihud_illiquidity"]).all()
        # The corrupt rows must ALSO be the ones volume_z and dollar_volume_z
        # already call missing, so the three volume-derived features agree on
        # which rows have no usable participation data instead of each
        # deciding separately. Row 20 is deliberately excluded: a zero volume
        # is finite and non-negative, so it is a real (halted) observation
        # rather than a missing one, and only its Amihud ratio needs the
        # divide-by-zero guard.
        for row in (21, 22, 23):
            assert computed.loc[row, "volume_missing"] == 1.0

        absent = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=40, seed=62, with_volume=False)))
        assert (absent["amihud_illiquidity"] == 0.0).all()

    def test_first_row_has_no_return_so_no_impact_measure(self):
        computed = compute_ticker_features(_adjusted(make_synthetic_ohlcv(n=40, seed=63)))
        assert np.isnan(computed.loc[0, "amihud_illiquidity"])


class TestRollingMarketBeta:
    """How market-driven vs idiosyncratic a ticker currently is."""

    def test_identical_tickers_have_a_beta_of_exactly_one(self, tmp_path):
        # With every ticker on the same return path the equal-weight market
        # mean IS each ticker's own return, so cov(r, r) / var(r) = 1 exactly.
        history = _write_archive(tmp_path / "identical", _identical_ticker_archive())
        panel = build_panel(history, min_history=60)
        column = FEATURE_NAMES.index("mkt_beta_20")
        for ticker in panel.tickers:
            settled = panel.series[ticker].features[60:, column]
            np.testing.assert_allclose(settled, 1.0, rtol=1e-5)

    def test_a_market_correlated_ticker_loads_higher_than_an_uncorrelated_one(
        self, synthetic_history_dir
    ):
        # AAA is built with market_beta 1.0, ZZZ with 0.0 (the fixture's
        # control ticker). The feature must be able to tell them apart.
        panel = build_panel(synthetic_history_dir, min_history=60)
        column = FEATURE_NAMES.index("mkt_beta_20")
        correlated = np.nanmean(panel.series["AAA"].features[60:, column])
        uncorrelated = np.nanmean(panel.series["ZZZ"].features[60:, column])
        assert correlated > uncorrelated

    def test_beta_matches_a_hand_computed_non_unit_loading(self, tmp_path):
        # The identical-ticker case above is exactly 1.0 under cov/var AND
        # under a plain correlation, and the AAA-vs-ZZZ ordering test pins
        # neither the window length nor the estimator. Three tickers with
        # DIFFERENT loadings on one shared factor make the true beta non-unit
        # and unequal to the correlation, so an implementation using
        # correlation, the full history, or the wrong window fails here
        # (Copilot review: "The exact-one case cannot distinguish beta
        # (cov/var) from correlation").
        rng = np.random.default_rng(70)
        factor = rng.normal(0.0, 0.012, size=200)
        loadings = {"AAA": 2.0, "BBB": 0.5, "CCC": 1.0}
        frames, paths = {}, {}
        for ticker, loading in loadings.items():
            closes = 50.0 * np.cumprod(1.0 + loading * factor + rng.normal(0.0, 0.002, size=200))
            paths[ticker] = closes
            frames[ticker] = _frame_from_closes(closes)
        panel = build_panel(_write_archive(tmp_path / "loadings", frames), min_history=60)

        # returns[k] is frame row k+1; row 0 has no return and is excluded
        # from the market mean, exactly as build_panel's pooled frame does.
        returns = {t: np.log(c[1:] / c[:-1]) for t, c in paths.items()}
        market = np.mean(np.column_stack([returns[t] for t in sorted(returns)]), axis=1)
        row = 150
        own_window = returns["AAA"][row - 20 : row]  # rows 131..150
        market_window = market[row - 20 : row]
        expected = np.cov(own_window, market_window, ddof=1)[0, 1] / np.var(market_window, ddof=1)
        assert expected == pytest.approx(2.0 / np.mean(list(loadings.values())), rel=0.2)  # non-unit
        observed = panel.series["AAA"].features[row, FEATURE_NAMES.index("mkt_beta_20")]
        assert observed == pytest.approx(expected, rel=1e-5)

    def test_warmup_rows_are_nan(self, synthetic_history_dir):
        panel = build_panel(synthetic_history_dir, min_history=60)
        column = FEATURE_NAMES.index("mkt_beta_20")
        series = panel.series["AAA"]
        assert np.isnan(series.features[10, column])
        assert np.isfinite(series.features[60, column])

    def test_zero_market_variance_is_neutral_not_a_division_by_zero(self, tmp_path):
        # A flat market (every ticker unchanged every day) has no variance to
        # regress against. 0.0 -- "no measurable loading" -- keeps the row
        # finite and eligible.
        frames = {ticker: _flat_frame(n=200) for ticker in ("AAA", "BBB", "CCC")}
        history = _write_archive(tmp_path / "flat", frames)
        panel = build_panel(history, min_history=60)
        column = FEATURE_NAMES.index("mkt_beta_20")
        settled = panel.series["AAA"].features[60:, column]
        assert np.isfinite(settled).all()
        np.testing.assert_allclose(settled, 0.0, atol=1e-12)


class TestStyleClustering:
    """A fundamentals-free 'sector' proxy: correlation blocks, refit causally.

    This is the one addition in these batches that is more than a column --
    it adds per-row cluster state to TickerSeries and a periodic refit inside
    build_panel. The clustering itself is deterministic (agglomerative,
    average linkage, correlation distance) and is refit from a trailing
    window of returns strictly at-or-before the refit date, so the existing
    perturbation tests cover it the same way they cover every other feature.
    """

    #: Ticker names are letters only on purpose: the archive's own symbol
    #: pattern (data._TICKER_SYMBOL_PATTERN) is ^[A-Z.]{1,6}$, so a
    #: digit-bearing fixture name is silently never discovered and the panel
    #: build refuses with an empty archive instead of failing the assertion
    #: the test meant to make.
    GROUPS = {"alpha": ["AAA", "AAB", "AAC"], "beta": ["BBA", "BBB", "BBC"]}

    def test_cluster_id_recovers_the_true_correlation_blocks(self, tmp_path):
        history = _write_archive(tmp_path / "blocks", _factor_archive(self.GROUPS))
        panel = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        row = -1  # a late row, well past the first refit
        assignments = {ticker: int(panel.series[ticker].cluster_id[row]) for ticker in panel.tickers}
        alpha = {assignments[ticker] for ticker in self.GROUPS["alpha"]}
        beta = {assignments[ticker] for ticker in self.GROUPS["beta"]}
        assert len(alpha) == 1, f"alpha group split across clusters: {assignments}"
        assert len(beta) == 1, f"beta group split across clusters: {assignments}"
        assert alpha != beta, f"the two independent factors collapsed into one cluster: {assignments}"

    def test_cluster_id_is_kept_out_of_the_float_feature_vector(self, tmp_path):
        # Same rule day_of_week already follows: a categorical is carried as
        # its own int8 array for the octopus to embed and the GBM to one-hot,
        # never smuggled into FEATURE_NAMES as if a cluster label were a
        # continuous quantity where 3 sits between 2 and 4.
        history = _write_archive(tmp_path / "blocks", _factor_archive(self.GROUPS))
        panel = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        assert "cluster_id" not in FEATURE_NAMES
        series = panel.series["AAA"]
        assert series.cluster_id.dtype == np.int8
        assert len(series.cluster_id) == len(series.dates)

    def test_clustering_is_deterministic_across_builds(self, tmp_path):
        history = _write_archive(tmp_path / "blocks", _factor_archive(self.GROUPS))
        first = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        second = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        for ticker in first.tickers:
            np.testing.assert_array_equal(first.series[ticker].cluster_id, second.series[ticker].cluster_id)

    def test_cluster_labels_stay_stable_across_refits(self, tmp_path):
        # Agglomerative labels are arbitrary per fit: nothing in the algorithm
        # makes "cluster 2" at one refit the same group at the next. Because
        # the id is embedded by the octopus and one-hot encoded by the GBM, a
        # silent relabel at a refit boundary changes what the category MEANS
        # mid-run while determinism, block recovery, and every other test here
        # still pass. The contract is that each refit's labels are aligned to
        # the previous assignment by membership overlap, so a group that
        # persists keeps its id (Copilot review: "Repeat-build determinism
        # does not ensure stable category semantics across periodic refits").
        history = _write_archive(tmp_path / "stable", _factor_archive(self.GROUPS))
        panel = build_panel(
            history, min_history=60, n_clusters=2, cluster_window=120, cluster_refresh=20
        )
        for ticker in panel.tickers:
            cluster_id = panel.series[ticker].cluster_id
            assigned = cluster_id[cluster_id != 0]
            assert len(assigned) > 0, f"{ticker} was never assigned"
            assert set(assigned.tolist()) == {int(assigned[0])}, (
                f"{ticker} changed cluster across a refit despite a stable block structure: "
                f"{sorted(set(assigned.tolist()))}"
            )

    def test_group_members_share_an_id_at_every_assigned_row(self, tmp_path):
        history = _write_archive(tmp_path / "stable", _factor_archive(self.GROUPS))
        panel = build_panel(
            history, min_history=60, n_clusters=2, cluster_window=120, cluster_refresh=20
        )
        for members in self.GROUPS.values():
            ids = np.stack([panel.series[ticker].cluster_id for ticker in members])
            assigned = (ids != 0).all(axis=0)
            assert assigned.any()
            assert (ids[:, assigned] == ids[0, assigned]).all()

    def test_cluster_assignments_before_a_cutoff_ignore_the_future(self, tmp_path):
        # The leakage probe, applied to the clustering itself: a refit dated
        # on or before the cutoff must not see the perturbed future bars, so
        # every assignment at or before the cutoff is identical.
        frames = _factor_archive(self.GROUPS)
        cutoff = frames["AAA"]["Date"].iloc[180]
        clean = _write_archive(tmp_path / "clean", frames)
        perturbed = _write_archive(tmp_path / "perturbed", _perturb_future(frames, cutoff))
        options = dict(min_history=60, n_clusters=2, cluster_window=120)
        clean_panel = build_panel(clean, **options)
        perturbed_panel = build_panel(perturbed, **options)
        for ticker in clean_panel.tickers:
            clean_series = clean_panel.series[ticker]
            past = clean_series.dates <= np.datetime64(cutoff)
            np.testing.assert_array_equal(
                clean_series.cluster_id[past], perturbed_panel.series[ticker].cluster_id[past]
            )

    def test_rows_before_the_first_refit_are_unassigned(self, tmp_path):
        # Cluster 0 is reserved for "not assigned yet" -- the correlation
        # window has not filled. The derived peer features must be NaN there
        # rather than reporting a peer group that does not exist yet.
        history = _write_archive(tmp_path / "blocks", _factor_archive(self.GROUPS))
        panel = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        series = panel.series["AAA"]
        assert series.cluster_id[10] == 0
        assert series.cluster_id[-1] != 0
        for name in ("cluster_ret_median", "cs_cluster_rank"):
            assert np.isnan(series.features[10, FEATURE_NAMES.index(name)])

    def test_peer_median_excludes_the_ticker_itself(self, tmp_path):
        # Two-member clusters make this exact: the "median of my peers" is
        # simply the OTHER member's return. Including self would make the
        # feature partly a restatement of ret_close, which the model already
        # has, instead of genuine peer information.
        groups = {"alpha": ["AAA", "AAB"], "beta": ["BBA", "BBB"]}
        history = _write_archive(tmp_path / "pairs", _factor_archive(groups))
        panel = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        peer_column = FEATURE_NAMES.index("cluster_ret_median")
        ret_column = FEATURE_NAMES.index("ret_close")
        row = -1
        assert int(panel.series["AAA"].cluster_id[row]) == int(panel.series["AAB"].cluster_id[row])
        assert panel.series["AAA"].features[row, peer_column] == pytest.approx(
            panel.series["AAB"].features[row, ret_column], rel=1e-5
        )

    def test_within_cluster_rank_spans_the_unit_interval(self, tmp_path):
        history = _write_archive(tmp_path / "blocks", _factor_archive(self.GROUPS))
        panel = build_panel(history, min_history=60, n_clusters=2, cluster_window=120)
        rank_column = FEATURE_NAMES.index("cs_cluster_rank")
        row = -1
        by_cluster: dict[int, list[float]] = {}
        for ticker in panel.tickers:
            series = panel.series[ticker]
            by_cluster.setdefault(int(series.cluster_id[row]), []).append(
                float(series.features[row, rank_column])
            )
        for cluster, ranks in by_cluster.items():
            assert min(ranks) == pytest.approx(0.0), cluster
            assert max(ranks) == pytest.approx(1.0), cluster

    def test_a_single_member_cluster_gets_the_neutral_midpoint(self, tmp_path):
        # Same rule as cs_return_rank's lone-ticker case: with no peers there
        # is nothing to rank against and no peer return to summarize.
        history = _write_archive(tmp_path / "solo", _factor_archive({"alpha": ["AAA"]}))
        panel = build_panel(history, min_history=60, n_clusters=1, cluster_window=120)
        series = panel.series["AAA"]
        assigned = series.cluster_id != 0
        np.testing.assert_allclose(series.features[assigned, FEATURE_NAMES.index("cs_cluster_rank")], 0.5)
        np.testing.assert_allclose(
            series.features[assigned, FEATURE_NAMES.index("cluster_ret_median")], 0.0
        )

    def test_requested_cluster_count_is_clamped_to_the_universe(self, tmp_path):
        # An archive smaller than the requested k must not degenerate into
        # one cluster per ticker (every peer group empty, both derived
        # features permanently neutral) and must not raise.
        history = _write_archive(tmp_path / "tiny", _factor_archive({"alpha": ["AAA", "AAB", "AAC"]}))
        panel = build_panel(history, min_history=60, n_clusters=8, cluster_window=120)
        row = -1
        sizes: dict[int, int] = {}
        for ticker in panel.tickers:
            cluster = int(panel.series[ticker].cluster_id[row])
            sizes[cluster] = sizes.get(cluster, 0) + 1
        assert max(sizes.values()) > 1, f"clamp produced singleton clusters only: {sizes}"
