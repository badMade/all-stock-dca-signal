"""Causal features, next-day OHLC targets, and panel assembly.

THE invariant of this module is no-lookahead: every feature value at a row
uses data at-or-before that row's own day, and every cross-sectional market
feature for a date uses only that date's (and earlier dates') bars. The
regression tests in tests/test_features.py enforce this bit-for-bit: perturb
any data after position k and nothing at or before k may change.

Targets are DELIBERATELY forward-looking (that is what a target is): row t's
target is day t+1's O/H/L/C expressed as log-returns against day t's
adjusted close. The harness's trainability guard -- a row may only enter a
training batch once its ``label_end_date`` (the next row's date) is at or
before the simulation cursor -- is the horizon-1 specialization of
ml-forecaster/evaluate.py's ``label_end_date < test_start`` walk-forward
rule (adapted; siblings deliberately do not import each other).

Feature layout (one float32 vector per ticker-day, ``FEATURE_NAMES`` order):

- 12 per-ticker causal features (today's bar vs yesterday's close, range,
  close position, trailing returns/vol, volume z-score + missing flag,
  calendar gap);
- 1 cross-sectional feature (this ticker's return rank across the universe
  that day);
- 7 market features shared by every ticker that day (median return,
  breadth, dispersion, trailing equal-weight returns/vol, universe size).

The market features feed the GBM baseline directly and are ALSO inputs to
the octopus market encoder -- both model families see the same raw market
information, one through statistics, one through learned attention, which
keeps the comparison honest. Day-of-week is kept separately as an int8
(embedded by the octopus, one-hot-able by the GBM) rather than pretending a
categorical is a float feature.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from data import (
    TickerSeries,
    TrainingPanel,
    adjusted_ohlc,
    discover_available_tickers,
    load_history_csv,
    truncate_as_of,
)

#: Trailing-window sizes, in this ticker's own trading rows.
_TRAIL_SHORT = 5
_TRAIL_LONG = 20
_VOL_WINDOW = 20
_VOLUME_WINDOW = 20

#: Calendar-day gap clip: anything beyond two weeks of silence carries no
#: extra signal over "long halt" and would otherwise dominate normalization.
_GAP_CLIP_DAYS = 10.0

PER_TICKER_FEATURES: tuple[str, ...] = (
    "ret_open",
    "ret_high",
    "ret_low",
    "ret_close",
    "intraday_range",
    "close_position",
    "trailing_ret_5",
    "trailing_ret_20",
    "realized_vol_20",
    "volume_z",
    "volume_missing",
    "gap_days",
)

CROSS_SECTIONAL_FEATURES: tuple[str, ...] = ("cs_return_rank",)

MARKET_FEATURES: tuple[str, ...] = (
    "mkt_median_ret",
    "mkt_frac_down2",
    "mkt_dispersion_iqr",
    "mkt_ret_5",
    "mkt_ret_20",
    "mkt_vol_20",
    "mkt_log_count",
)

FEATURE_NAMES: tuple[str, ...] = PER_TICKER_FEATURES + CROSS_SECTIONAL_FEATURES + MARKET_FEATURES

#: The market-auxiliary-head target: next-day market summary, stored per
#: calendar day in TrainingPanel.market_summary in THIS order.
MARKET_SUMMARY_NAMES: tuple[str, ...] = ("mkt_median_ret", "mkt_frac_down2", "mkt_dispersion_iqr")

#: Floor for normalization standard deviations -- a constant feature (e.g.
#: volume_missing in an all-volume archive) must normalize to 0, not inf.
_STD_FLOOR = 1e-8


def compute_ticker_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker causal features + targets for one adjusted, Date-sorted frame.

    Returns a frame with Date, the PER_TICKER_FEATURES columns, day_of_week,
    the four target columns (``target_open`` .. ``target_close``),
    ``label_end_date``, ``ref_close``, and ``adj_ratio``. Warm-up rows carry
    NaN in trailing-window features; the final row carries NaN targets and
    NaT label_end_date. No row is dropped here -- consumers filter on
    finiteness, and the row indices must stay aligned with the ticker's full
    trading history for eligibility counting.
    """
    out = pd.DataFrame({"Date": frame["Date"].to_numpy()})
    close = frame["Close"].astype(float)
    prev_close = close.shift(1)
    out["ret_open"] = np.log(frame["Open"] / prev_close)
    out["ret_high"] = np.log(frame["High"] / prev_close)
    out["ret_low"] = np.log(frame["Low"] / prev_close)
    out["ret_close"] = np.log(close / prev_close)
    out["intraday_range"] = np.log(frame["High"] / frame["Low"])
    bar_span = frame["High"] - frame["Low"]
    # A doji day (High == Low) has no meaningful position-in-range; 0.5 is
    # the neutral "middle of the bar" placeholder, same graceful-degradation
    # spirit as ml-forecaster's neutral volume ratio.
    out["close_position"] = np.where(bar_span > 0, (close - frame["Low"]) / bar_span, 0.5)
    out["trailing_ret_5"] = np.log(close / close.shift(_TRAIL_SHORT))
    out["trailing_ret_20"] = np.log(close / close.shift(_TRAIL_LONG))
    out["realized_vol_20"] = out["ret_close"].rolling(_VOL_WINDOW).std()
    if "Volume" in frame.columns and frame["Volume"].notna().any():
        volume = frame["Volume"].astype(float)
        # Row-level missingness, not a whole-column verdict: a ticker with
        # volume on most days but NaN (or corrupt negative) volume on some
        # must flag exactly those rows, instead of reporting volume_missing
        # = 0 everywhere while their z-scores silently degrade to neutral
        # (Antigravity review: "Whole-dataframe check masks row-level
        # missing volume").
        # ~isfinite catches NaN AND +-inf in one check (not just isna()): a
        # corrupt CSV's Volume=inf survived as a "present" row before,
        # producing an infinite volume_z while volume_missing stayed 0 --
        # the harness's all-features-finite eligibility check then silently
        # dropped that ticker's forecast for the day instead of degrading
        # to the documented neutral volume_z (Codex review: "Treat
        # positive-infinite volume as missing").
        missing = ~np.isfinite(volume) | (volume < 0)
        log_volume = np.log1p(volume.clip(lower=0))
        # Exclude corrupt/missing rows from the rolling stats entirely
        # (not just clip them to a value that still enters the window):
        # pandas' default rolling min_periods == window means ONE NaN
        # poisons every window touching it to NaN too, which the old code
        # then silently replaced with a neutral 0.0 for roughly a month of
        # otherwise-valid days -- and a negative corrupt volume clipped to
        # 0 (log1p(0) = 0, not NaN) biased those windows instead of being
        # excluded. min_periods=1 lets a window with a few missing rows
        # still compute real statistics from what's left (Codex review:
        # "Exclude missing volume from trailing statistics").
        log_volume_clean = log_volume.where(~missing)
        vol_mean = log_volume_clean.rolling(_VOLUME_WINDOW, min_periods=1).mean()
        vol_std = log_volume_clean.rolling(_VOLUME_WINDOW, min_periods=1).std()
        with np.errstate(invalid="ignore"):
            out["volume_z"] = np.where(
                (~missing) & (vol_std > 0), (log_volume - vol_mean) / vol_std, 0.0
            )
        out["volume_missing"] = missing.astype(float)
    else:
        out["volume_z"] = 0.0
        out["volume_missing"] = 1.0
    gap = frame["Date"].diff().dt.days.astype(float)
    out["gap_days"] = gap.clip(upper=_GAP_CLIP_DAYS)
    out["day_of_week"] = frame["Date"].dt.dayofweek.astype(np.int8)
    # Targets: the NEXT row's bar as log-returns vs THIS row's close. A
    # ticker's "t+1" is its own next trading row, whatever the calendar gap
    # -- ragged histories are handled by eligibility, never by forward-fill.
    target_columns = (
        ("Open", "target_open"), ("High", "target_high"), ("Low", "target_low"), ("Close", "target_close")
    )
    for column, target in target_columns:
        out[target] = np.log(frame[column].shift(-1) / close)
    out["label_end_date"] = frame["Date"].shift(-1)
    out["ref_close"] = close.to_numpy()
    out["adj_ratio"] = frame["adj_ratio"].to_numpy() if "adj_ratio" in frame.columns else 1.0
    return out


def _market_table(long_frame: pd.DataFrame) -> pd.DataFrame:
    """Per-calendar-day market aggregates from the pooled (Date, ret_close) rows.

    Every aggregate for a date is computed strictly from that date's own
    returns (plus trailing windows of earlier dates for the _5/_20 series) --
    day t+1 never reaches day t's row, which the no-lookahead test asserts.
    """
    grouped = long_frame.groupby("Date")["ret_close"]
    # Vectorized, not grouped.apply(lambda ...): a Python lambda per calendar
    # day is a real cost over a 26-year x full-universe pooled frame
    # (Antigravity review: "Slow custom lambda in grouped aggregation").
    down2 = (long_frame["ret_close"] <= -0.02).astype(float)
    table = pd.DataFrame(
        {
            "mkt_median_ret": grouped.median(),
            "mkt_count": grouped.count(),
            "mkt_frac_down2": down2.groupby(long_frame["Date"]).mean(),
            "mkt_q25": grouped.quantile(0.25),
            "mkt_q75": grouped.quantile(0.75),
            "mkt_mean_ret": grouped.mean(),
        }
    ).sort_index()
    table["mkt_dispersion_iqr"] = table["mkt_q75"] - table["mkt_q25"]
    table["mkt_ret_5"] = table["mkt_mean_ret"].rolling(_TRAIL_SHORT).sum()
    table["mkt_ret_20"] = table["mkt_mean_ret"].rolling(_TRAIL_LONG).sum()
    table["mkt_vol_20"] = table["mkt_mean_ret"].rolling(_VOL_WINDOW).std()
    table["mkt_log_count"] = np.log1p(table["mkt_count"])
    return table


def build_panel(
    history_dir: Path,
    *,
    tickers: list[str] | None = None,
    max_tickers: int | None = None,
    end: pd.Timestamp | None = None,
    as_of: pd.Timestamp | None = None,
    initial_train_end: pd.Timestamp | None = None,
    min_history: int = 60,
) -> TrainingPanel:
    """Assemble the full in-memory panel from the per-ticker CSV archive.

    Missing/broken/short tickers are recorded in ``panel.skipped`` and never
    block the build (fail closed only on ZERO usable tickers). ``end`` and
    ``as_of`` both truncate (``end`` is inclusive of its day, ``as_of`` is
    calendar-day-exclusive, matching the CLI's --end/--now semantics).
    ``max_tickers`` keeps the N tickers with the longest usable history
    (alphabetical tie-break) -- deterministic, and biased toward the tickers
    that give the models the most to learn from. NOTE: there is deliberately
    no ``start`` truncation here -- prediction start is the harness's
    concern; training reaches as far back as each archive goes.

    Normalization statistics (per-feature mean/std) are computed over rows
    whose date is at or before ``initial_train_end`` (all rows when None)
    and FROZEN into the panel -- the captured-at-training-time pattern of
    ml-forecaster/model.py's design-matrix stats: online updates must never
    silently re-normalize history.
    """
    discovered = discover_available_tickers(history_dir, tickers)
    skipped: dict[str, str] = {}
    if tickers is not None:
        for ticker in tickers:
            # discover_available_tickers() case-normalizes the requested
            # list (e.g. "aapl" -> "AAPL") before matching it against the
            # archive; comparing the ORIGINAL spelling here would then mark
            # a successfully-loaded ticker as skipped just because the user
            # typed it lowercase (Codex review: "Avoid marking normalized
            # ticker requests as skipped").
            normalized = ticker.upper()
            if normalized not in discovered:
                skipped[normalized] = "no_csv_in_archive"

    frames: dict[str, pd.DataFrame] = {}
    dropped_bar_rows: dict[str, int] = {}
    for ticker in discovered:
        path = history_dir / f"{ticker}.csv"
        try:
            frame = load_history_csv(path)
        except (OSError, ValueError) as error:
            skipped[ticker] = f"unreadable: {error.__class__.__name__}"
            continue
        frame = truncate_as_of(frame, as_of)
        if end is not None:
            # Calendar-day inclusive, per this function's own docstring --
            # NOT a raw timestamp comparison. load_history_csv() accepts
            # and preserves whatever time-of-day component an archive's
            # Date column carries; comparing an intraday bar directly
            # against pd.Timestamp(end) (midnight) would drop every bar
            # later than midnight on --end's own day, silently ending the
            # replay one trading day early (Codex review: "Treat --end as
            # an inclusive calendar day").
            end_day = pd.Timestamp(end).normalize()
            frame = frame.loc[frame["Date"].dt.normalize() <= end_day].reset_index(drop=True)
        if "Adj Close" not in frame.columns:
            # adjusted_ohlc() only rejects individual rows with an unusable
            # Adj Close VALUE; a column that is absent entirely never
            # triggers that per-row check, so the ticker would silently keep
            # the default ratio 1.0 for its ENTIRE history -- treating every
            # split/dividend discontinuity in it as a real price move
            # (Codex review: "Require adjusted close before accepting a
            # ticker"). This is malformed/non-canonical data the same way a
            # missing required OHLC column is -- skip the ticker.
            skipped[ticker] = "missing_adj_close_column"
            continue
        frame, dropped = adjusted_ohlc(frame)
        if dropped:
            dropped_bar_rows[ticker] = dropped
        # min_history rows of warm-up plus one predictable row -- eligible
        # for a live forecast (row index == min_history satisfies
        # eligible_on()'s idx >= min_history rule) even though that row's
        # own target isn't known yet. Requiring a second (target) row here
        # would delay a short-history ticker's first forecast by a full
        # trading day, contradicting the documented "joins once it has
        # min_history prior rows" contract; target finiteness is a
        # separate, training-only filter downstream (Codex review: "Admit
        # tickers as soon as the warm-up is complete").
        if len(frame) < min_history + 1:
            skipped[ticker] = "too_short"
            continue
        frames[ticker] = frame

    if max_tickers is not None and len(frames) > max_tickers:
        # Longest history first, alphabetical tie-break: deterministic, and
        # subsets lean toward the deepest training signal.
        ranked = sorted(frames, key=lambda t: (-len(frames[t]), t))
        keep = set(ranked[:max_tickers])
        for ticker in ranked[max_tickers:]:
            skipped[ticker] = "over_max_tickers"
        frames = {ticker: frames[ticker] for ticker in sorted(keep)}

    if not frames:
        raise ValueError(
            "REFUSED: no usable tickers in archive "
            f"{history_dir} (skipped: {dict(sorted(skipped.items())[:10])}{'...' if len(skipped) > 10 else ''})"
        )

    per_ticker = {ticker: compute_ticker_features(frame) for ticker, frame in frames.items()}

    # --- Cross-sectional pass: pooled long frame of each day's returns. ---
    long_frame = pd.concat(
        [computed[["Date", "ret_close"]].assign(ticker=ticker) for ticker, computed in per_ticker.items()],
        ignore_index=True,
    ).dropna(subset=["ret_close"])
    market = _market_table(long_frame)
    # Per-row cross-sectional rank in [0, 1]; 0.5 when a ticker trades alone
    # that day (no peers to rank against).
    grouped = long_frame.groupby("Date")["ret_close"]
    rank = grouped.rank(method="average")
    count = grouped.transform("count")
    long_frame["cs_return_rank"] = np.where(count > 1, (rank - 1.0) / (count - 1.0), 0.5)
    rank_lookup = long_frame.set_index(["ticker", "Date"])["cs_return_rank"]

    calendar = np.array(sorted(market.index.to_numpy()), dtype="datetime64[ns]")
    calendar_pos = {pd.Timestamp(date): position for position, date in enumerate(calendar)}
    market_aligned = market.reindex(pd.DatetimeIndex(calendar))
    market_summary = market_aligned[list(MARKET_SUMMARY_NAMES)].to_numpy(dtype=np.float32)

    tickers_sorted = sorted(frames)
    series: dict[str, TickerSeries] = {}
    date_index: dict[pd.Timestamp, list[tuple[str, int]]] = {}
    for vocab_index, ticker in enumerate(tickers_sorted):
        computed = per_ticker[ticker]
        dates = computed["Date"].to_numpy(dtype="datetime64[ns]")
        joined = computed.join(market, on="Date")
        ticker_rank = rank_lookup.loc[ticker].reindex(computed["Date"]).to_numpy()
        joined["cs_return_rank"] = ticker_rank
        feature_matrix = joined[list(FEATURE_NAMES)].to_numpy(dtype=np.float32)
        target_matrix = computed[["target_open", "target_high", "target_low", "target_close"]].to_numpy(
            dtype=np.float32
        )
        series[ticker] = TickerSeries(
            ticker=ticker,
            ticker_id=vocab_index + 1,  # 0 is reserved for "unknown"
            dates=dates,
            features=feature_matrix,
            day_of_week=computed["day_of_week"].to_numpy(dtype=np.int8),
            targets=target_matrix,
            label_end_dates=computed["label_end_date"].to_numpy(dtype="datetime64[ns]"),
            ref_close=computed["ref_close"].to_numpy(dtype=np.float64),
            adj_ratio=computed["adj_ratio"].to_numpy(dtype=np.float64),
        )
        for row_index, date in enumerate(dates):
            date_index.setdefault(pd.Timestamp(date), []).append((ticker, row_index))

    normalization = _fit_normalization(series, initial_train_end)
    return TrainingPanel(
        series=series,
        tickers=tickers_sorted,
        calendar=calendar,
        date_index=date_index,
        feature_names=FEATURE_NAMES,
        normalization=normalization,
        min_history=min_history,
        market_summary=market_summary,
        calendar_pos=calendar_pos,
        skipped=skipped,
        dropped_bar_rows=dropped_bar_rows,
    )


def refreeze_normalization(panel: TrainingPanel, initial_train_end: pd.Timestamp | None) -> None:
    """Recompute the panel's frozen normalization window in place.

    Recovery retrains use this to move the fit window forward: the refit
    must see everything the run has revealed so far, not the original
    initial window (Codex review: "Retrain through the current walk-forward
    cursor"). Ordinary online updates never call this -- the freeze rule
    stands; a retrain is the one sanctioned re-freeze.
    """
    panel.normalization = _fit_normalization(panel.series, initial_train_end)


def _fit_normalization(series: dict[str, TickerSeries], initial_train_end: pd.Timestamp | None) -> dict:
    """Per-feature mean/std over finite rows inside the initial-fit window.

    Falls back to the full history if the window holds no fully finite row
    (a pathological but possible tiny-archive case) -- better stats from
    "everything" than a refusal, and the fallback is recorded in the stats
    dict so a report can surface it.
    """
    # pd.isna guard: pd.Timestamp(None) is NaT, and `dates <= NaT` is False
    # for EVERY row -- a caller passing an unguarded None would silently
    # collapse the window to zero rows and trip the full-history fallback
    # (Antigravity review: "pd.Timestamp(None) returns NaT, triggering
    # unintended normalization fallback"). NaT means "no window", same as None.
    if initial_train_end is None or pd.isna(initial_train_end):
        cutoff = None
    else:
        cutoff = pd.Timestamp(initial_train_end)

    def moments(limit: pd.Timestamp | None, *, require_all_finite: bool) -> tuple[np.ndarray, np.ndarray, int]:
        """Per-column (mean, std) via one streaming pass over each ticker's
        OWN rows, never concatenating them into a single materialized
        array. On the documented full-universe --resume --retrain path
        this reaches tens of millions of rows; retaining every per-ticker
        boolean-masked block before np.concatenate() allocated a second
        full copy of that history on top of the resident panel (Codex
        review: "Stream normalization moments instead of materializing all
        rows"). Matches np.nanmean/np.nanstd(ddof=0)'s per-column,
        NaN-skipping semantics exactly.
        """
        n_features = len(FEATURE_NAMES)
        count = np.zeros(n_features, dtype=np.float64)
        total = np.zeros(n_features, dtype=np.float64)
        total_sq = np.zeros(n_features, dtype=np.float64)
        n_rows = 0
        limit64 = np.datetime64(limit) if limit is not None else None
        for ticker_series in series.values():
            finite = np.isfinite(ticker_series.features)
            mask = finite.all(axis=1) if require_all_finite else finite.any(axis=1)
            if limit64 is not None:
                mask &= ticker_series.dates <= limit64
            if not mask.any():
                continue
            block = ticker_series.features[mask].astype(np.float64)
            block_finite = np.isfinite(block)
            count += block_finite.sum(axis=0)
            total += np.where(block_finite, block, 0.0).sum(axis=0)
            total_sq += np.where(block_finite, block * block, 0.0).sum(axis=0)
            n_rows += len(block)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(count > 0, total / count, np.nan)
            variance = np.where(count > 0, total_sq / count - mean * mean, np.nan)
        std = np.sqrt(np.maximum(variance, 0.0))
        return mean, std, n_rows

    mean, std, n_rows = moments(cutoff, require_all_finite=True)
    fallback = False
    if n_rows == 0 and cutoff is not None:
        # The old fallback here read the FULL, UNBOUNDED history (past the
        # cutoff) -- for an Octopus-only run whose normalization stats get
        # frozen into every historical forecast, that means every
        # prediction across the whole replay would be biased by future
        # data, contaminating the advertised strict walk-forward
        # experiment (Codex review: "Refuse normalization from post-cutoff
        # history"). Stay within the cutoff instead: relax "every feature
        # finite" to "at least one finite feature" and skip individual
        # NaNs (rolling-window warm-up) per column, rather than reading
        # beyond the boundary.
        mean, std, n_rows = moments(cutoff, require_all_finite=False)
        fallback = True
    if n_rows == 0:
        raise ValueError("REFUSED: no finite feature rows anywhere in the panel; archive too small to model")
    # A column with ZERO finite observations anywhere in the fit window (an
    # early --initial-train-end can leave a longer trailing-window feature
    # with no warmed-up rows yet) makes its mean/std NaN here; _normalize()
    # would then turn EVERY row of that feature into 0.0 forever -- not
    # just its warm-up rows -- even once real data becomes available later
    # in the replay (Codex review: "Give wholly missing normalization
    # columns finite defaults"). Neutral mean=0/std=1 leaves those values
    # unscaled instead of permanently discarding them.
    no_observations = np.isnan(mean)
    mean = np.where(no_observations, 0.0, mean)
    std = np.where(no_observations, 1.0, np.maximum(std, _STD_FLOOR))
    return {
        "mean": [float(value) for value in mean],
        "std": [float(value) for value in std],
        "rows_used": n_rows,
        "window_end": str(cutoff.date()) if cutoff is not None else None,
        "fell_back_to_partial_rows": fallback,
    }
