"""Tests for features.py -- synthetic in-memory OHLCV only, no real archive."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import make_synthetic_ohlcv, write_ticker_csv
from features import (
    FORWARD_RETURN_HORIZON_DAYS,
    NUMERIC_FEATURE_COLUMNS,
    TARGET_COLUMN,
    build_dataset,
    build_feature_frame,
    compute_features,
    compute_forward_return_target,
    compute_label_end_date,
    discover_available_tickers,
    load_history_csv,
)


def test_load_history_csv_sorts_and_dedupes_keeping_the_later_row(tmp_path):
    frame = make_synthetic_ohlcv(n=30, seed=5)
    shuffled = frame.sample(frac=1.0, random_state=0).reset_index(drop=True)
    # Simulate a duplicate-Date merge anomaly: the SAME date appears twice
    # with different data. The later row (appended last) must win.
    dup_row = frame.iloc[10].copy()
    dup_row["Close"] = dup_row["Close"] + 999.0
    shuffled = pd.concat([shuffled, pd.DataFrame([dup_row])], ignore_index=True)
    path = tmp_path / "TEST.csv"
    shuffled.to_csv(path, index=False)

    loaded = load_history_csv(path)

    assert loaded["Date"].is_monotonic_increasing
    assert loaded["Date"].duplicated().sum() == 0
    matching_row = loaded[loaded["Date"] == frame.iloc[10]["Date"]]
    assert np.asarray(matching_row["Close"])[0] == pytest.approx(dup_row["Close"])


def test_load_history_csv_requires_the_core_ohlc_columns(tmp_path):
    path = tmp_path / "BAD.csv"
    pd.DataFrame({"Date": ["2024-01-02"], "Close": [10.0]}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="missing required column"):
        load_history_csv(path)


def test_load_history_csv_drops_only_the_row_with_an_unparseable_date(tmp_path):
    # A single garbage Date value must not poison the whole column: pandas'
    # read_csv-level date parsing would otherwise leave the ENTIRE column as
    # object dtype and this loader would raise on a healthy 29-row archive
    # over one corrupt row. Only that row should be dropped.
    frame = make_synthetic_ohlcv(n=30, seed=11)
    frame = frame.copy()
    # astype(object) first: the Date column starts as datetime64 dtype, which
    # (on recent pandas) refuses to accept a non-date string via .loc
    # assignment. Writing to CSV round-trips every Date back through a plain
    # string either way, so this only affects how the corrupt value gets in.
    frame["Date"] = frame["Date"].astype(object)
    frame.loc[15, "Date"] = "not-a-real-date"
    path = tmp_path / "GARBAGEDATE.csv"
    frame.to_csv(path, index=False)

    loaded = load_history_csv(path)

    assert len(loaded) == len(frame) - 1
    assert loaded["Date"].notna().all()
    assert pd.api.types.is_datetime64_any_dtype(loaded["Date"])


def test_load_history_csv_normalizes_timezone_aware_dates_to_naive(tmp_path):
    """Regression test for "Normalize timezone-aware archive dates before
    cutoff filtering".

    When a CSV's Date column carries an explicit UTC offset (e.g. an
    ISO-8601 "...Z" timestamp), pd.to_datetime() infers a TZ-AWARE
    datetime64 dtype for the whole column, but every downstream comparison
    (this module's own as_of cutoff filtering, train.py's staleness
    cutoffs) is built from a plain naive Timestamp/datetime. Comparing a
    tz-aware Series against a naive one raises an uncaught TypeError rather
    than a normal, coercible result -- train.py's dataset-construction
    refusal boundary only catches (OSError, ValueError), so this crashed
    the whole weekly retrain instead of the documented REFUSED result.
    """
    frame = make_synthetic_ohlcv(n=30, seed=19)
    frame = frame.copy()
    frame["Date"] = [d.strftime("%Y-%m-%dT00:00:00Z") for d in frame["Date"]]
    path = tmp_path / "TZAWARE.csv"
    frame.to_csv(path, index=False)

    loaded = load_history_csv(path)

    assert getattr(loaded["Date"].dtype, "tz", None) is None
    assert pd.api.types.is_datetime64_any_dtype(loaded["Date"])
    assert len(loaded) == len(frame)


def test_build_dataset_as_of_cutoff_does_not_raise_on_timezone_aware_archive_dates(tmp_path):
    """End-to-end companion to the load_history_csv test above: the as_of
    cutoff comparison inside _iter_ticker_frames() must not raise even when
    the source archive's Date column was originally timezone-aware."""
    frame = make_synthetic_ohlcv(n=60, seed=23, drift=0.0004)
    frame = frame.copy()
    naive_dates = frame["Date"]
    frame["Date"] = [d.strftime("%Y-%m-%dT00:00:00Z") for d in frame["Date"]]
    frame.to_csv(tmp_path / "AAA.csv", index=False)

    cutoff = naive_dates.iloc[40]
    truncated = build_dataset(tmp_path, ["AAA"], as_of=cutoff)

    assert not truncated.empty
    assert (truncated["date"] <= cutoff).all()


def test_compute_features_degrades_gracefully_without_volume():
    frame = make_synthetic_ohlcv(n=60, seed=7, with_volume=False)
    features = compute_features(frame, "NOVOL")
    assert (features["volume_rel_20d_avg"] == 1.0).all()


def test_compute_features_an_infinite_volume_does_not_contaminate_the_rolling_window(tmp_path):
    """Regression test for "Reject non-finite volume before rolling
    calculations". An infinite raw Volume must not make the 20-day rolling
    average infinite for every subsequent window that includes it -- that
    would make "finite / inf" silently evaluate to 0.0 (a spurious "zero
    volume" feature, not NaN) for up to 19 neighboring rows, none of which
    would be caught by the existing dropna() downstream (only the bad row's
    own inf/inf -> NaN ratio would be).
    """
    frame = make_synthetic_ohlcv(n=60, seed=37)
    bad_index = 20
    frame = frame.copy()
    frame["Volume"] = frame["Volume"].astype(float)
    frame.loc[bad_index, "Volume"] = float("inf")

    features = compute_features(frame, "BADVOL")

    # The corrupted row's own ratio is NaN (will be dropped downstream).
    assert pd.isna(features["volume_rel_20d_avg"].iloc[bad_index])
    # Every row in the SAME 20-day window that includes the bad row (i.e.
    # the 19 rows immediately after it) must carry a genuine, finite,
    # nonzero ratio computed from the OTHER valid volumes in that window --
    # not a spurious 0.0 from dividing by an infinite rolling average.
    window = features["volume_rel_20d_avg"].iloc[bad_index + 1 : bad_index + 20]
    assert window.notna().all()
    assert np.isfinite(window).all()
    assert (window != 0.0).all()


def test_compute_features_no_lookahead():
    """A feature computed 'as of' day T must never read day T+1 data.

    Builds features from the FULL synthetic history, then rebuilds them from
    only the rows through day T (inclusive) and checks day T's row is
    IDENTICAL either way. Also perturbs every row strictly AFTER T to wildly
    different prices and reconfirms day T is unaffected -- a naive centered
    rolling window (or an accidental negative .shift()) would fail either
    check.
    """
    frame = make_synthetic_ohlcv(n=120, seed=11)
    cutoff = 80  # comfortably past every rolling window's warmup

    full_features = compute_features(frame, "AAA")
    day_t_from_full = full_features.iloc[cutoff]

    truncated = frame.iloc[: cutoff + 1].copy()
    truncated_features = compute_features(truncated, "AAA")
    day_t_from_truncated = truncated_features.iloc[cutoff]

    for column in (*NUMERIC_FEATURE_COLUMNS, "day_of_week"):
        assert day_t_from_full[column] == pytest.approx(day_t_from_truncated[column])

    perturbed = frame.copy()
    future_mask = perturbed.index > cutoff
    for column in ("Open", "High", "Low", "Close", "Adj Close"):
        perturbed.loc[future_mask, column] = perturbed.loc[future_mask, column] * 1000.0
    perturbed_features = compute_features(perturbed, "AAA")
    day_t_from_perturbed = perturbed_features.iloc[cutoff]

    for column in (*NUMERIC_FEATURE_COLUMNS, "day_of_week"):
        assert day_t_from_full[column] == pytest.approx(day_t_from_perturbed[column])


def test_compute_forward_return_target_rejects_nonpositive_horizon():
    """A nonpositive horizon would shift BACKWARD, leaking future info as if
    it were history -- see train.py's --horizon CLI guard for the primary
    fix; this pins the same guard on the underlying function for a caller
    that reaches it directly, not just through the CLI."""
    frame = make_synthetic_ohlcv(n=20, seed=3)
    with pytest.raises(ValueError, match="positive"):
        compute_forward_return_target(frame, horizon=0)
    with pytest.raises(ValueError, match="positive"):
        compute_forward_return_target(frame, horizon=-3)


def test_compute_label_end_date_rejects_nonpositive_horizon():
    frame = make_synthetic_ohlcv(n=20, seed=3)
    with pytest.raises(ValueError, match="positive"):
        compute_label_end_date(frame, horizon=0)
    with pytest.raises(ValueError, match="positive"):
        compute_label_end_date(frame, horizon=-1)


def test_compute_forward_return_target_reads_ahead_and_trims_the_tail():
    frame = make_synthetic_ohlcv(n=40, seed=13)
    target = compute_forward_return_target(frame, horizon=5)
    close = frame["Close"].to_numpy()

    expected_first = (close[5] - close[0]) / close[0] * 100.0
    assert target.iloc[0] == pytest.approx(expected_first)
    # The last `horizon` rows have no future close yet.
    assert target.iloc[-5:].isna().all()


def test_compute_label_end_date_matches_forward_return_targets_own_nan_tail():
    frame = make_synthetic_ohlcv(n=40, seed=17)
    label_end_date = compute_label_end_date(frame, horizon=5)
    target = compute_forward_return_target(frame, horizon=5)

    # Both are the identical forward shift(-horizon) -- one on Date, one on
    # Close -- so their NaN/NaT tails must line up exactly.
    assert label_end_date.isna().equals(target.isna())
    assert label_end_date.iloc[0] == frame["Date"].iloc[5]
    assert label_end_date.iloc[-5:].isna().all()


def test_build_dataset_pools_tickers_drops_incomplete_rows_and_flags_dip_days(synthetic_history_dir):
    dataset = build_dataset(synthetic_history_dir, ["AAA", "BBB", "CCC"])

    assert set(dataset["ticker"].unique()) == {"AAA", "BBB", "CCC"}
    required_columns = [*NUMERIC_FEATURE_COLUMNS, TARGET_COLUMN]
    assert not dataset[required_columns].isna().to_numpy().any()
    assert not dataset["label_end_date"].isna().any()
    assert (dataset.loc[dataset["dip_day"], "drop_vs_prior_close_pct"] > 0).all()
    assert (dataset.loc[~dataset["dip_day"], "drop_vs_prior_close_pct"] <= 0).all()
    assert dataset["date"].is_monotonic_increasing


def test_build_dataset_and_build_feature_frame_drop_rows_with_infinite_ratios(tmp_path):
    """Regression test for "Drop derived non-finite rows before model fitting".

    Every raw Close cell here is individually finite and positive (even the
    "extreme" one), so _preferred_price_series() lets all of them through --
    this is deliberately NOT a corrupted-cell scenario, those are covered by
    test_compute_features_*'s own invalid-close tests. Instead, one row's
    Close is set far enough below the rest of the series (1e-307, still a
    normal positive float64) that a RATIO computed against a normal-scale
    neighbor overflows float64 to +/-inf, even though both cells it was
    computed from were individually valid. dropna(subset=...) alone (the
    pre-fix behavior) only strips NaN -- inf survives it untouched -- so
    this proves the np.isfinite() filter in both build_dataset() (the target
    column) and build_feature_frame() (a feature column, on the live scoring
    path) actually catches the overflow rather than merely not regressing
    the existing dropna() cases.
    """
    history_dir = tmp_path / "ticker_data"
    history_dir.mkdir()
    n = 60
    extreme_idx = 30
    dates = pd.bdate_range(start="2021-01-04", periods=n)
    close = np.full(n, 50.0)
    close[extreme_idx] = 1e-307
    frame = pd.DataFrame({"Date": dates, "Open": close, "High": close, "Low": close, "Close": close})
    write_ticker_csv(history_dir, "ZZZ", frame)

    # Sanity check, independent of features.py's own implementation: the
    # crafted data really does produce a non-finite ratio at the target
    # (day of the extreme close) and at the very next row's day-over-day
    # feature -- otherwise the assertions below would trivially pass without
    # exercising the overflow path at all.
    close_series = pd.Series(close.tolist())
    target = ((close_series.shift(-FORWARD_RETURN_HORIZON_DAYS) - close_series) / close_series) * 100.0
    assert not np.isfinite(target.iloc[extreme_idx])
    drop_pct = ((close_series.shift(1) - close_series) / close_series.shift(1)) * 100.0
    assert not np.isfinite(drop_pct.iloc[extreme_idx + 1])

    dataset = build_dataset(history_dir, ["ZZZ"])
    required_columns = [*NUMERIC_FEATURE_COLUMNS, TARGET_COLUMN]
    assert not dataset.empty
    assert np.isfinite(dataset[required_columns].to_numpy(dtype=float)).all()
    extreme_date = dates[extreme_idx]
    adjacent_date = dates[extreme_idx + 1]
    assert extreme_date not in set(dataset["date"])
    assert adjacent_date not in set(dataset["date"])

    feature_frame = build_feature_frame(history_dir, ["ZZZ"])
    assert not feature_frame.empty
    assert np.isfinite(feature_frame[list(NUMERIC_FEATURE_COLUMNS)].to_numpy(dtype=float)).all()
    # Unlike build_dataset(), the extreme row's own FEATURES are all finite
    # (only its forward-looking TARGET overflows, and build_feature_frame()
    # has no target column at all) -- it is legitimately still scoreable, so
    # it must survive here even though build_dataset() had to drop it.
    assert extreme_date in set(feature_frame["date"])
    assert adjacent_date not in set(feature_frame["date"])


def test_build_dataset_skips_tickers_with_no_archive(synthetic_history_dir):
    dataset = build_dataset(synthetic_history_dir, ["AAA", "NOPE"])
    assert set(dataset["ticker"].unique()) == {"AAA"}


def test_build_dataset_returns_empty_frame_for_no_tickers(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    dataset = build_dataset(empty_dir, [])
    assert dataset.empty
    assert "ticker" in dataset.columns
    assert "label_end_date" in dataset.columns


def test_build_feature_frame_retains_the_unlabeled_tail_that_build_dataset_drops(synthetic_history_dir):
    """Regression test for the "score current features, not the target-
    trimmed tail" bug: build_dataset() drops each ticker's most recent
    `horizon` trading days (no target yet); build_feature_frame() must keep
    them, since it never requires a target at all."""
    dataset = build_dataset(synthetic_history_dir, ["AAA"], horizon=FORWARD_RETURN_HORIZON_DAYS)
    feature_frame = build_feature_frame(synthetic_history_dir, ["AAA"])

    dataset_dates = set(dataset.loc[dataset["ticker"] == "AAA", "date"])
    feature_dates = set(feature_frame.loc[feature_frame["ticker"] == "AAA", "date"])

    # The feature frame's most recent date is strictly newer than the
    # dataset's -- exactly the target-unavailable tail build_dataset() had
    # to drop.
    assert max(feature_dates) > max(dataset_dates)
    tail_only_in_features = feature_dates - dataset_dates
    assert len(tail_only_in_features) == FORWARD_RETURN_HORIZON_DAYS

    # No target column at all -- this is a feature-only frame.
    assert TARGET_COLUMN not in feature_frame.columns

    # Rows present in BOTH frames must carry identical feature values --
    # build_feature_frame() must not compute anything differently, only
    # retain more rows.
    shared_dates = dataset_dates & feature_dates
    sample_date = sorted(shared_dates)[-1]
    from_dataset = dataset[(dataset["ticker"] == "AAA") & (dataset["date"] == sample_date)].iloc[0]
    from_features = feature_frame[
        (feature_frame["ticker"] == "AAA") & (feature_frame["date"] == sample_date)
    ].iloc[0]
    for column in NUMERIC_FEATURE_COLUMNS:
        assert from_dataset[column] == pytest.approx(from_features[column])


def test_build_feature_frame_skips_tickers_with_no_archive(synthetic_history_dir):
    feature_frame = build_feature_frame(synthetic_history_dir, ["AAA", "NOPE"])
    assert set(feature_frame["ticker"].unique()) == {"AAA"}


def test_build_feature_frame_returns_empty_frame_for_no_tickers(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    feature_frame = build_feature_frame(empty_dir, [])
    assert feature_frame.empty
    assert "ticker" in feature_frame.columns
    assert TARGET_COLUMN not in feature_frame.columns


def test_build_feature_frame_as_of_excludes_rows_after_the_cutoff(tmp_path):
    """Regression test for "Exclude observations after the effective run
    time". A replay's --now can be earlier than the archive tail; rows
    dated after `as_of` must be excluded from feature computation entirely
    (not merely filtered from the result afterward), so a "historical" run
    never trains, evaluates, or scores against data that had not happened
    yet as of the replayed "now".
    """
    frame = make_synthetic_ohlcv(n=60, seed=7)
    frame.to_csv(tmp_path / "AAA.csv", index=False)

    cutoff = frame["Date"].iloc[40]
    full = build_feature_frame(tmp_path, ["AAA"])
    truncated = build_feature_frame(tmp_path, ["AAA"], as_of=cutoff)

    assert full["date"].max() > cutoff  # the untruncated archive really does extend past the cutoff
    assert not truncated.empty
    # Strictly less than, not <=: the cutoff's OWN day is excluded too (see
    # test_build_feature_frame_as_of_excludes_the_same_calendar_day_even_when_as_of_is_intraday).
    assert truncated["date"].max() < cutoff
    assert (truncated["date"] < cutoff).all()


def test_build_feature_frame_as_of_excludes_the_same_calendar_day_even_when_as_of_is_intraday(tmp_path):
    """Regression test for "Exclude the current day's close from intraday
    replays". An archived daily row's Date is midnight-normalized and
    represents that FULL trading day's close -- an intraday `as_of` (e.g.
    10 AM) on that SAME calendar day has not actually observed that close
    yet, even though midnight-of-that-day is chronologically "<=" 10 AM.
    The row for `as_of`'s own day must be excluded regardless of what time
    of day `as_of` itself is.
    """
    frame = make_synthetic_ohlcv(n=60, seed=23)
    frame.to_csv(tmp_path / "AAA.csv", index=False)

    same_day_midnight = frame["Date"].iloc[40]
    intraday_as_of = same_day_midnight + pd.Timedelta(hours=10)  # 10 AM that same day

    truncated = build_feature_frame(tmp_path, ["AAA"], as_of=intraday_as_of)

    assert same_day_midnight not in set(truncated["date"])
    assert truncated["date"].max() < same_day_midnight


def test_build_dataset_as_of_excludes_rows_after_the_cutoff(tmp_path):
    frame = make_synthetic_ohlcv(n=60, seed=13, drift=0.0004)
    frame.to_csv(tmp_path / "AAA.csv", index=False)

    cutoff = frame["Date"].iloc[40]
    full = build_dataset(tmp_path, ["AAA"])
    truncated = build_dataset(tmp_path, ["AAA"], as_of=cutoff)

    assert full["date"].max() > cutoff
    assert not truncated.empty
    assert (truncated["date"] <= cutoff).all()
    # label_end_date (the forward-return window's own end) must also never
    # reach past the cutoff -- a row whose target was computed using a
    # future close beyond `as_of` would be lookahead-contaminated even if
    # the row's OWN date happens to sit at or before the cutoff.
    assert (truncated["label_end_date"] <= cutoff).all()


def test_build_feature_frame_as_of_matches_full_history_for_rows_before_the_cutoff(tmp_path):
    """Truncation must not change any SURVIVING row's own feature values --
    only remove rows after the cutoff, mirroring compute_features()'s own
    no-lookahead guarantee (see this module's docstring)."""
    frame = make_synthetic_ohlcv(n=60, seed=17)
    frame.to_csv(tmp_path / "AAA.csv", index=False)

    cutoff = frame["Date"].iloc[40]
    full = build_feature_frame(tmp_path, ["AAA"])
    truncated = build_feature_frame(tmp_path, ["AAA"], as_of=cutoff)

    sample_date = frame["Date"].iloc[20]  # well before the cutoff
    from_full = full[full["date"] == sample_date].iloc[0]
    from_truncated = truncated[truncated["date"] == sample_date].iloc[0]
    for column in NUMERIC_FEATURE_COLUMNS:
        assert from_full[column] == pytest.approx(from_truncated[column])


def test_discover_available_tickers_filters_to_present_csvs(synthetic_history_dir):
    assert discover_available_tickers(synthetic_history_dir, ["AAA", "ZZZ"]) == ["AAA"]
    assert discover_available_tickers(synthetic_history_dir) == ["AAA", "BBB", "CCC"]


def test_discover_available_tickers_missing_dir_returns_empty(tmp_path):
    assert discover_available_tickers(tmp_path / "does-not-exist") == []


def test_discover_available_tickers_deduplicates_a_repeated_explicit_request(synthetic_history_dir):
    """Regression test for "Deduplicate explicitly requested training
    tickers". `--tickers AAPL AAPL MSFT`-style repetition must not survive
    into the resolved ticker list -- otherwise the caller's
    _iter_ticker_frames() appends that ticker's full history twice, silently
    doubling its weight in the pooled fit and every walk-forward metric.
    First-occurrence order is preserved and a duplicate of a ticker that
    ISN'T actually present is still dropped (no CSV, same as a non-repeated
    absent ticker)."""
    assert discover_available_tickers(synthetic_history_dir, ["AAA", "AAA", "BBB"]) == ["AAA", "BBB"]
    assert discover_available_tickers(synthetic_history_dir, ["BBB", "AAA", "BBB", "AAA"]) == ["BBB", "AAA"]
    assert discover_available_tickers(synthetic_history_dir, ["ZZZ", "ZZZ"]) == []


def test_discover_available_tickers_rejects_path_traversal_in_explicit_symbols(synthetic_history_dir, tmp_path):
    """Regression test for "Validate explicit ticker symbols before
    resolving CSV paths". A ticker string containing an absolute path or
    ".." components must never be joined into a filesystem path at all --
    an absolute right-hand operand REPLACES the left side outright in
    pathlib, so `history_dir / f"{ticker}.csv"` for an absolute `ticker`
    resolves OUTSIDE history_dir entirely, letting an explicit --tickers
    request pool an arbitrary CSV elsewhere on disk into the live model.
    """
    outside_csv = tmp_path / "outside.csv"
    write_ticker_csv(tmp_path, "outside", make_synthetic_ohlcv(seed=99, n=30))
    assert outside_csv.is_file()  # sanity: the file genuinely exists, just outside history_dir

    absolute_path_ticker = str(tmp_path / "outside")
    assert discover_available_tickers(synthetic_history_dir, [absolute_path_ticker, "AAA"]) == ["AAA"]

    traversal_ticker = "../outside"
    assert discover_available_tickers(synthetic_history_dir, [traversal_ticker, "AAA"]) == ["AAA"]


def test_discover_available_tickers_deduplicates_case_folded_archive_symbols(synthetic_history_dir):
    """Regression test for "Deduplicate case-folded archive symbols". A
    case-sensitive filesystem can hold both "AAA.csv" and "aaa.csv" -- left
    undeduplicated under the default (--all-tickers-in-archive) discovery
    path, both would be pooled as separate "tickers" with their own full
    history, even though every downstream consumer (prediction output, the
    recorded evaluation universe, published adjustments) already
    normalizes with .upper() and would collapse them into one. Exactly one
    stem per canonical uppercase symbol must survive, preferring the
    already-canonically-cased file.
    """
    write_ticker_csv(synthetic_history_dir, "aaa", make_synthetic_ohlcv(seed=100, n=30))
    assert (synthetic_history_dir / "aaa.csv").is_file()  # sanity: genuinely a second file

    resolved = discover_available_tickers(synthetic_history_dir)
    assert resolved.count("AAA") + resolved.count("aaa") == 1  # exactly one wins, and it's the canonical "AAA"
    assert "AAA" in resolved
    assert "aaa" not in resolved
    assert resolved == ["AAA", "BBB", "CCC"]


def test_discover_available_tickers_filters_invalid_archive_stems(synthetic_history_dir):
    """Regression test for "Filter invalid archive stems before pooled
    training". An auxiliary/backup file left in the archive directory (e.g.
    "AAA-backup.csv") must never be treated as a real ticker under the
    default (--all-tickers-in-archive) discovery path -- unlike the
    explicit-list branch, this branch previously validated nothing at all,
    so every "*.csv" stem was pooled into the Ridge fit regardless of shape.
    """
    write_ticker_csv(synthetic_history_dir, "AAA-backup", make_synthetic_ohlcv(seed=101, n=30))
    assert (synthetic_history_dir / "AAA-backup.csv").is_file()  # sanity

    resolved = discover_available_tickers(synthetic_history_dir)
    assert "AAA-backup" not in resolved
    assert resolved == ["AAA", "BBB", "CCC"]


def test_compute_features_uses_adjusted_close_across_a_split_not_raw_close(tmp_path):
    """Regression test for computing return/vol/RSI/drawdown/target off raw
    Close instead of Adj Close: a 2-for-1 split makes raw Close nearly halve
    between the two rows while Adj Close stays continuous. The computed
    day-over-day "drop" must reflect the smooth ADJUSTED return (near zero),
    not the ~50% fake crash raw Close alone would produce.
    """
    frame = pd.DataFrame(
        {
            "Date": ["2024-01-02", "2024-01-03"],
            "Open": [99.0, 50.0],
            "High": [101.0, 51.0],
            "Low": [98.0, 49.5],
            "Close": [100.0, 50.5],
            "Adj Close": [50.0, 50.5],
        }
    )
    path = tmp_path / "SPLIT.csv"
    frame.to_csv(path, index=False)
    loaded = load_history_csv(path)

    features = compute_features(loaded, "SPLIT")
    day_after_split_drop = features["drop_vs_prior_close_pct"].iloc[1]

    # From the adjusted series (50.0 -> 50.5) this is a small ~1% GAIN, so
    # drop_vs_prior_close_pct (positive == a drop) is a small NEGATIVE
    # number -- nowhere near the ~49.5% fake "drop" raw Close alone would
    # produce ((100.0 - 50.5) / 100.0 * 100).
    assert day_after_split_drop == pytest.approx(-1.0, abs=0.5)
    assert day_after_split_drop > -10.0  # guards against regressing to the raw-Close bug

    # The forward-return target must be built off the same adjusted series.
    target = compute_forward_return_target(loaded, horizon=1)
    assert target.iloc[0] == pytest.approx(1.0, abs=0.5)


def test_compute_features_falls_back_to_raw_close_when_adj_close_column_is_absent(tmp_path):
    """A pre-Adj-Close archive (no such column at all) must still work via raw Close."""
    frame_no_adj = make_synthetic_ohlcv(n=60, seed=23).drop(columns=["Adj Close"])
    path = tmp_path / "NOADJ.csv"
    frame_no_adj.to_csv(path, index=False)
    loaded = load_history_csv(path)
    assert "Adj Close" not in loaded.columns

    features = compute_features(loaded, "NOADJ")
    expected_close = loaded["Close"].astype(float)
    expected_drop = ((expected_close.shift(1) - expected_close) / expected_close.shift(1)) * 100.0
    pd.testing.assert_series_equal(
        features["drop_vs_prior_close_pct"].reset_index(drop=True),
        expected_drop.reset_index(drop=True),
        check_names=False,
    )


@pytest.mark.parametrize("bad_close", [0.0, -5.0, float("inf")])
def test_compute_features_treats_an_invalid_raw_close_fallback_as_missing(tmp_path, bad_close):
    """Regression test for "Validate the raw close before using it as a
    fallback". A zero/negative/infinite raw Close (a corrupted archive row)
    must not be used as a silent fallback value when there is no Adj Close
    column to prefer instead -- it must become NaN so the existing dropna()
    in build_dataset()/build_feature_frame() discards the row, the same way
    an invalid Adj Close alone already does, rather than a non-finite
    feature/target surviving into Ridge fitting. (A NaN raw Close itself is
    already excluded earlier, at load_history_csv()'s own required-column
    dropna() -- this test covers the three values that are NOT NaN and so
    reach _preferred_price_series() unfiltered.)
    """
    frame = make_synthetic_ohlcv(n=30, seed=31).drop(columns=["Adj Close"])
    frame = frame.copy()
    frame.loc[15, "Close"] = bad_close
    path = tmp_path / "BADCLOSE.csv"
    frame.to_csv(path, index=False)
    loaded = load_history_csv(path)

    features = compute_features(loaded, "BADCLOSE")
    # The corrupted row's own close-derived feature must be NaN, not a
    # value computed from the invalid close.
    assert pd.isna(features["drop_vs_prior_close_pct"].iloc[15])
    # The NEXT row's drop (which references row 15's close via .shift(1))
    # must also be NaN, not a garbage/divide-by-zero percentage.
    assert pd.isna(features["drop_vs_prior_close_pct"].iloc[16])


def test_compute_features_falls_back_to_raw_close_when_adj_close_values_are_null(tmp_path):
    """An archive with an `Adj Close` column that is entirely null/unusable
    must fall back to raw Close, not fail or produce NaN features."""
    frame = make_synthetic_ohlcv(n=60, seed=29)
    frame["Adj Close"] = None
    path = tmp_path / "NULLADJ.csv"
    frame.to_csv(path, index=False)
    loaded = load_history_csv(path)
    assert loaded["Adj Close"].isna().all()

    features = compute_features(loaded, "NULLADJ")
    expected_close = loaded["Close"].astype(float)
    expected_drop = ((expected_close.shift(1) - expected_close) / expected_close.shift(1)) * 100.0
    pd.testing.assert_series_equal(
        features["drop_vs_prior_close_pct"].reset_index(drop=True),
        expected_drop.reset_index(drop=True),
        check_names=False,
    )


def test_compute_features_infers_a_forward_filled_ratio_row_by_row_not_whole_ticker(tmp_path):
    """One row's bad Adj Close must not force the WHOLE ticker onto raw
    Close: this is a PER-ROW fallback, not a per-ticker/all-or-nothing one.

    Row 1 has a null Adj Close (simulating a data-quality gap); rows 0 and 2
    have valid ones, both on a ~2-for-1-split-adjusted scale relative to raw
    Close. Regression test for "Avoid mixing raw and adjusted price scales":
    splicing row 1's RAW Close (101.0) directly into this otherwise-adjusted
    series would create a fake ~100% one-day jump into raw scale and back
    out of it on the very next row. Instead, row 1's price is inferred from
    row 0's own ratio (Adj Close / Close, forward-filled -- never a LATER
    row's ratio, which would leak future information into day 1's own
    feature computation; see test_compute_features_no_lookahead()), applied
    to row 1's own raw Close.
    """
    frame = pd.DataFrame(
        {
            "Date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "Open": [99.0, 100.0, 101.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [98.0, 99.0, 100.0],
            "Close": [100.0, 101.0, 102.0],
            "Adj Close": [50.0, None, 51.5],
        }
    )
    path = tmp_path / "PARTIAL.csv"
    frame.to_csv(path, index=False)
    loaded = load_history_csv(path)

    features = compute_features(loaded, "PARTIAL")

    # Row 1's inferred price: raw Close (101.0) * row 0's own ratio
    # (50.0 / 100.0 == 0.5), forward-filled -- 50.5 -- on the SAME adjusted
    # scale as its neighbor, not the raw 101.0 a row-by-row splice would use.
    row1_inferred_price = 101.0 * 0.5
    expected_row1_drop = ((50.0 - row1_inferred_price) / 50.0) * 100.0
    assert features["drop_vs_prior_close_pct"].iloc[1] == pytest.approx(expected_row1_drop)
    assert abs(features["drop_vs_prior_close_pct"].iloc[1]) < 5.0  # nowhere near a fake ~100% jump

    # Row 2: both it (adjusted 51.5) and its prior row (row 1's inferred
    # ~50.5, on the SAME scale) participate normally.
    expected_row2_drop = ((row1_inferred_price - 51.5) / row1_inferred_price) * 100.0
    assert features["drop_vs_prior_close_pct"].iloc[2] == pytest.approx(expected_row2_drop)


def test_compute_features_never_uses_a_later_rows_price_to_fill_a_gap(tmp_path):
    """Regression test for "Avoid future-directed interpolation of adjusted
    prices". A prior version of this fix filled a gap row by interpolating
    between the nearest valid Adj Close on EITHER side -- correct against
    scale-mixing, but a genuine lookahead leak: day T's own feature value
    (and, since compute_forward_return_target() shares this same series, a
    training LABEL) could depend on a price from AFTER day T, contaminating
    walk-forward evaluation with information that would not exist yet in a
    live run. This pins the no-lookahead contract directly: perturbing a
    LATER row's Adj Close must never change an EARLIER gap row's own
    feature output.
    """
    base_frame = pd.DataFrame(
        {
            "Date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "Open": [99.0, 100.0, 101.0],
            "High": [101.0, 102.0, 103.0],
            "Low": [98.0, 99.0, 100.0],
            "Close": [100.0, 101.0, 102.0],
            "Adj Close": [50.0, None, 51.5],
        }
    )
    perturbed_frame = base_frame.copy()
    perturbed_frame.loc[2, "Adj Close"] = 999.0  # a wildly different LATER value

    base_path, perturbed_path = tmp_path / "BASE.csv", tmp_path / "PERTURBED.csv"
    base_frame.to_csv(base_path, index=False)
    perturbed_frame.to_csv(perturbed_path, index=False)

    base_features = compute_features(load_history_csv(base_path), "BASE")
    perturbed_features = compute_features(load_history_csv(perturbed_path), "PERTURBED")

    # Row 1 (the gap row) must be COMPLETELY unaffected by row 2's own Adj
    # Close, since row 2 is strictly AFTER row 1 and would not exist yet in
    # a live evaluation as of row 1's own date.
    assert base_features["drop_vs_prior_close_pct"].iloc[1] == pytest.approx(
        perturbed_features["drop_vs_prior_close_pct"].iloc[1]
    )


def test_compute_features_accepts_a_stale_ratio_at_a_split_boundary_as_a_known_limitation(tmp_path):
    """A gap row that lands exactly ON a split/dividend's effective date is
    a real, accepted residual limitation of the forward-fill-only fix
    above: the adjustment ratio genuinely changes right there, and the only
    ratio available AS OF that date (without lookahead) is the stale
    pre-event one. This is narrower and less severe than either a raw-scale
    splice or a lookahead leak -- it requires a provider gap to coincide
    with a split/dividend event on the exact same row, and even then
    produces one row's finite-but-wrong price, not a metrics-wide
    evaluation bias. Documented here as a known, accepted tradeoff, not
    silently relied upon.
    """
    frame = pd.DataFrame(
        {
            "Date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
            "Open": [24.5, 25.5, 100.0, 26.5],
            "High": [25.5, 26.5, 105.0, 27.5],
            "Low": [24.0, 25.0, 95.0, 26.0],
            "Close": [100.0, 104.0, 26.0, 27.0],  # 4-for-1 split effective at row 2
            "Adj Close": [25.0, 26.0, None, 27.0],
        }
    )
    path = tmp_path / "SPLITGAP.csv"
    frame.to_csv(path, index=False)
    loaded = load_history_csv(path)

    features = compute_features(loaded, "SPLITGAP")

    # Row 2's inferred price: raw Close (26.0) * row 1's own (stale,
    # pre-split) ratio (26.0 / 104.0 == 0.25) -- 6.5 -- the accepted-wrong
    # value at this exact, narrow edge case.
    row2_inferred_price = 26.0 * 0.25
    expected_row2_drop = ((26.0 - row2_inferred_price) / 26.0) * 100.0
    assert features["drop_vs_prior_close_pct"].iloc[2] == pytest.approx(expected_row2_drop)


@pytest.mark.parametrize("bad_adj_close", [float("inf"), float("-inf"), float("nan")])
def test_compute_features_falls_back_to_raw_close_for_an_unanchored_leading_gap(tmp_path, bad_adj_close):
    """Regression test for "Fall back from non-finite adjusted closes".

    Unlike NaN, "inf > 0.0" is TRUE -- an infinite Adj Close previously
    passed the old usable predicate (notna() & (adjusted > 0.0)) instead of
    falling back to Close, and an infinite value surviving into the forward-
    return target would abort Ridge fitting on the scheduled retrain. Row 0
    has NO valid Adj Close before it to forward-fill a ratio from -- and
    borrowing row 1's LATER ratio would be lookahead -- so it falls back to
    its own raw Close, the only as-of-day-0 information available at all.
    """
    frame = pd.DataFrame(
        {
            "Date": ["2024-01-02", "2024-01-03"],
            "Open": [99.0, 100.0],
            "High": [101.0, 102.0],
            "Low": [98.0, 99.0],
            "Close": [100.0, 101.0],
            "Adj Close": [bad_adj_close, 51.0],
        }
    )
    path = tmp_path / "NONFINITE.csv"
    frame.to_csv(path, index=False)
    loaded = load_history_csv(path)

    features = compute_features(loaded, "NONFINITE")

    # Row 0's price falls back to raw Close (100.0); row 1's drop is
    # computed against that fallback via .shift(1), not against inf/NaN.
    expected_row1_drop = ((100.0 - 51.0) / 100.0) * 100.0
    assert features["drop_vs_prior_close_pct"].iloc[1] == pytest.approx(expected_row1_drop)
    assert np.isfinite(features["drop_vs_prior_close_pct"].iloc[1])


def _frame_from_closes(closes):
    n = len(closes)
    return pd.DataFrame({"Date": pd.bdate_range("2018-06-01", periods=n), "Close": closes, "Adj Close": closes})


def test_forward_target_is_blanked_when_its_window_holds_a_corrupt_day():
    # 3.0 -> 0.015 is a -99.5% day, then 0.015 -> 0.32 is a +2,033% day (the QUBT 2018 shell prints).
    closes = [6.0, 5.0, 4.0, 3.0, 0.015, 0.32, 3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.0, 4.1]
    target = compute_forward_return_target(_frame_from_closes(closes), horizon=5)
    # The -99.5% day is index 4, the +2,033% day is index 5. Rows 0-4 have one of them inside T+1..T+5, so their
    # labels are blanked; row 5's window (indices 6-10) is clean and keeps its ordinary return.
    assert target.iloc[:5].isna().all()
    assert target.iloc[5] == pytest.approx((closes[10] - closes[5]) / closes[5] * 100.0)


def test_forward_target_keeps_legitimate_extreme_but_sub_threshold_moves():
    # +251% in a day (LUNR-style) and -75% are real moves; only >=900% rises / <=-90% drops are treated as corrupt.
    closes = [10.0, 10.0, 35.1, 35.0, 8.75, 9.0, 9.1, 9.2, 9.3, 9.4, 9.5]
    target = compute_forward_return_target(_frame_from_closes(closes), horizon=5)
    assert target.iloc[:6].notna().all()
