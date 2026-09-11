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

- 23 per-ticker causal features: the original twelve (today's bar vs
  yesterday's close, range, close position, trailing returns/vol, volume
  z-score + missing flag, calendar gap), plus dollar-volume z-score and
  Amihud price impact (liquidity), distance from the 20-row mean and from
  the 20-row high (mean reversion), a short realized-vol leg against the
  long one (volatility regime), Wilder RSI and ADX and a signed
  same-direction streak (momentum, as bounded features rather than
  threshold rules), the intraday leg of the bar, and rolling return
  skew/kurtosis (tail-risk regime, read by the p10/p90 heads);
- 6 cross-sectional features, all needing the pooled universe: this
  ticker's return, momentum and volatility rank across that day, its
  rolling market beta, and two style-cluster features (its peer group's
  median return, and its rank inside that peer group);
- 7 market features shared by every ticker that day (median return,
  breadth, dispersion, trailing equal-weight returns/vol, universe size).

The style clusters behind those last two are a fundamentals-free sector
proxy: the universe is periodically re-clustered on return correlation over
a trailing window ending at (never after) the refit date, and labels are
aligned across refits by membership overlap so a group that persists keeps
its id. The label itself is carried as ``TickerSeries.cluster_id``, an int8
kept OUT of the float vector for the same reason day_of_week is.

An OPTIONAL "calculus" block (``extra_features=("calculus",)``) appends 5
more per-ticker columns: a literal second derivative of log price, the
direction realized volatility is moving, causal Savitzky-Golay slope and
curvature, and a fractionally differenced close. It is OFF by default and
appended at the TAIL of the layout, so every existing index keeps meaning
what it meant -- harness.py's ``_BAR_FEATURE_SLICE`` and baseline_model's
module-level ``FEATURE_NAMES.index(...)`` constants are positional and
resolved at import against the default tuple.

The market features feed the GBM baseline directly and are ALSO inputs to
the octopus market encoder -- both model families see the same raw market
information, one through statistics, one through learned attention, which
keeps the comparison honest. Day-of-week is kept separately as an int8
(embedded by the octopus, one-hot-able by the GBM) rather than pretending a
categorical is a float feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

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
_VOL_SHORT_WINDOW = 5
_VOLUME_WINDOW = 20
_SMA_WINDOW = 20
_DRAWDOWN_WINDOW = 20
_SHAPE_WINDOW = 20
_BETA_WINDOW = 20
_RSI_PERIOD = 14
_ADX_PERIOD = 14

#: Consecutive-day streaks are clipped like gap_days: past ten sessions in one
#: direction the count carries no more signal than "extended", and an
#: unbounded counter would dominate the frozen normalization statistics the
#: first time some ticker runs twenty straight green days.
_STREAK_CLIP = 10.0

#: Amihud's raw |return| / dollar-volume ratio for a liquid US equity lands
#: around 1e-10, deep in log1p's linear regime where the feature degenerates
#: into a rescaled absolute return and carries no liquidity information at
#: all. Scaling first puts it back in the part of the curve that separates a
#: thin book from a deep one.
_AMIHUD_SCALE = 1e9

#: Style-clustering defaults. The universe is re-clustered every
#: ``cluster_refresh`` calendar days from a trailing ``cluster_window`` of
#: returns ending at (never after) the refit date. Refreshing is deliberately
#: coarse: each refit builds an n_tickers x n_tickers distance matrix, which
#: on the full-universe archive is the most expensive thing in this module.
_DEFAULT_N_CLUSTERS = 8
_DEFAULT_CLUSTER_REFRESH = 60

#: Minimum share of the correlation window a ticker must actually have traded
#: through before it can be clustered -- a name with six overlapping days
#: correlates with anything.
_CLUSTER_MIN_COVERAGE = 0.5

#: Where the refit schedule is anchored. It is a CONSTANT, not a function of
#: the archive's length, and that is the whole point: an anchor derived from
#: len(calendar) makes a past label depend on how much FUTURE data happens to
#: be loaded, so rebuilding after one more day arrives would silently
#: renumber history (Copilot review). Refit positions are now
#: min(cluster_window, this) - 1 and every cluster_refresh after, which
#: appending rows can only extend, never move.
_CLUSTER_ANCHOR_ROWS = 20

#: Calendar-day gap clip: anything beyond two weeks of silence carries no
#: extra signal over "long halt" and would otherwise dominate normalization.
_GAP_CLIP_DAYS = 10.0

PER_TICKER_FEATURES: tuple[str, ...] = (
    # The original twelve. New names APPEND; they never reorder. A reorder
    # silently remaps every frozen normalization statistic and every
    # checkpointed input projection onto the wrong column, with no error
    # anywhere -- which is why tests/test_features.py pins this prefix.
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
    # Liquidity, mean reversion, and volatility regime.
    "dollar_volume_z",
    "dist_from_sma20",
    "realized_vol_5",
    # Continuous momentum: bounded oscillators fed as FEATURES, not as
    # threshold rules -- the model decides what an RSI of 0.8 is worth.
    "rsi_14",
    "adx_14",
    # The intraday leg of the bar. ret_open is already the overnight gap
    # (log(Open_t / Close_{t-1})), so a second "overnight" column would be
    # perfectly collinear with it; the half that was missing is the session
    # move, and the two now reconstruct ret_close exactly.
    "intraday_ret",
    "updown_streak",
    "drawdown_from_20d_high",
    # Distribution shape -- what the p10/p90 heads need in order to widen a
    # band before the tail arrives rather than after it.
    "ret_skew_20",
    "ret_kurt_20",
    "amihud_illiquidity",
)

CROSS_SECTIONAL_FEATURES: tuple[str, ...] = (
    "cs_return_rank",
    # cs_return_rank ranks ONE day's return; these rank the same universe on
    # horizon and on risk, and mkt_beta_20 relates the ticker back to the
    # market series the market table already computes.
    "cs_momentum_rank",
    "cs_vol_rank",
    "mkt_beta_20",
    # Style clustering: a fundamentals-free sector proxy. No sector,
    # industry, or fundamental fact is supplied to it -- the groups are
    # whatever the return correlations say they are.
    "cluster_ret_median",
    "cs_cluster_rank",
)

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


#: --- The optional "calculus" block (off by default) -------------------
#: Trailing rows between the two realized_vol_20 readings vol20_roc
#: compares. A week: short enough to register a regime change quickly,
#: long enough that the 20-row windows being compared barely overlap.
_VOL_ROC_LAG = 5

#: Bound on vol20_roc. A log-ratio of realized vols is unbounded, and one
#: degenerate reading (a ticker emerging from a flat, zero-vol stretch)
#: would otherwise dominate this column's SINGLE global normalization
#: mean/std for the whole universe. +-3.0 is a 20x change in realized vol.
VOL_ROC_CLIP = 3.0

#: Savitzky-Golay: fit a degree-2 polynomial to the trailing SG_WINDOW log
#: prices and read its 1st/2nd derivative AT THE WINDOW'S RIGHT EDGE.
#: Deliberately NOT scipy.signal.savgol_filter -- that one is CENTERED, so
#: it reads future rows and would break this module's no-lookahead
#: invariant (and scipy is only a transitive dependency here anyway).
SG_WINDOW = 11
SG_DEGREE = 2

#: Fractional differentiation (Lopez de Prado, Advances in Financial
#: Machine Learning ch. 5): difference by a FRACTIONAL order instead of
#: integer order 1, so the series sheds most of its trend while keeping the
#: long memory that ret_close throws away. d and tau are chosen by warm-up
#: budget, not by a stationarity test: at d=0.4 a tau of 1e-3 truncates the
#: kernel at 55 taps, where 1e-4 would need 282. The harness only forecasts
#: rows where EVERY feature is finite, so a kernel wider than min_history
#: would delay every ticker's first forecast and drop short-history tickers
#: outright -- silently contradicting "missing stocks are never blockers".
#: (Choosing d by an ADF test, as the book does, would also mean taking a
#: statsmodels dependency this subproject does not have.)
FRAC_DIFF_D = 0.4
FRAC_DIFF_TAU = 1e-3
FRAC_DIFF_MAX_WIDTH = 60


def _frac_diff_weights(d: float, tau: float, max_width: int) -> np.ndarray:
    """Truncated binomial weights w_k = -w_{k-1} (d - k + 1) / k, w_0 = 1."""
    weights = [1.0]
    while len(weights) < max_width:
        following = -weights[-1] * (d - len(weights) + 1) / len(weights)
        if abs(following) < tau:
            break
        weights.append(following)
    return np.array(weights, dtype=np.float64)


def _savgol_kernels(width: int, degree: int) -> tuple[np.ndarray, np.ndarray]:
    """Fixed FIR kernels for the 1st/2nd derivative at a trailing window's edge.

    Positions run 0, -1, ... -(width - 1) to match the newest-first window
    ordering in _trailing_kernel(), so the polynomial's derivatives at
    position 0 ARE the derivatives at day t. Both kernels sum to zero (the
    derivative of a constant is zero), which is what makes them comparable
    across a $5 and a $500 ticker without any anchoring.
    """
    positions = np.arange(0, -width, -1.0)
    inverse = np.linalg.pinv(np.vander(positions, degree + 1, increasing=True))
    return inverse[1], 2.0 * inverse[2]


FRAC_DIFF_WEIGHTS: np.ndarray = _frac_diff_weights(FRAC_DIFF_D, FRAC_DIFF_TAU, FRAC_DIFF_MAX_WIDTH)
SG_SLOPE_KERNEL, SG_CURVATURE_KERNEL = _savgol_kernels(SG_WINDOW, SG_DEGREE)

CALCULUS_FEATURES: tuple[str, ...] = (
    "ret_accel",
    "vol20_roc",
    "sg_slope",
    "sg_curvature",
    "frac_diff_close",
)

#: Optional feature blocks, appended at the TAIL of FEATURE_NAMES in the
#: order requested. Tail-appending is load-bearing, not cosmetic: see the
#: module docstring.
FEATURE_BLOCKS: dict[str, tuple[str, ...]] = {"calculus": CALCULUS_FEATURES}


def resolve_feature_names(extra_features: Sequence[str] | None = ()) -> tuple[str, ...]:
    """The feature layout for a given set of optional blocks.

    Refuses an unknown block name rather than falling back to the default
    layout: a typo in a run's config must not leave that run quietly
    missing the features it claims to have.

    Typed as a Sequence so a caller passing a list type-checks (Antigravity
    review: "Type hint flexibility for extra_features"), which opens one
    hole worth closing explicitly: a bare ``str`` IS a Sequence[str], so
    ``extra_features="calculus"`` would type-check and then iterate
    CHARACTERS, refusing on 'c'. Caught by name here instead.
    """
    # None means "no optional blocks", the same way build_panel()'s
    # tickers=None means "every ticker": callers threading an optional
    # argument through a wrapper layer default it to None, and a bare
    # TypeError out of dict.fromkeys() would be a poor way to say this
    # (Antigravity review: "Graceful None handling in
    # resolve_feature_names").
    if extra_features is None:
        extra_features = ()
    if isinstance(extra_features, str):
        raise ValueError(
            f"REFUSED: extra_features must be a sequence of block names, not the bare string "
            f"{extra_features!r}; pass ({extra_features!r},)"
        )
    names = FEATURE_NAMES
    # dict.fromkeys: de-duplicate while preserving order, so a block named
    # twice in a config does not append its columns twice.
    for block in dict.fromkeys(extra_features):
        if block not in FEATURE_BLOCKS:
            raise ValueError(
                f"REFUSED: unknown feature block {block!r}; known blocks: {sorted(FEATURE_BLOCKS)}"
            )
        names = names + FEATURE_BLOCKS[block]
    return names


def _trailing_kernel(values: np.ndarray, kernel: np.ndarray, *, anchored: bool = False) -> np.ndarray:
    """Apply a fixed FIR ``kernel`` to each row's TRAILING window.

    ``out[t] = kernel @ [values[t], values[t-1], ...]``, NaN until the
    window fills -- causal by construction, which is the only reason these
    columns can exist in this module at all.

    ``anchored`` subtracts each window's OLDEST observation before the dot
    product. The fractional-differencing kernel is truncated, so its weights
    do not sum to zero (sum(w) = +0.136 at d=0.4) and a raw application to
    log price would leak the price LEVEL: a ticker priced 100x higher with
    an identical return shape lands +0.63 away on a column whose own signal
    std is 0.033, and the panel fits ONE global mean/std per column, so
    nothing downstream would wash it out. Anchoring removes the level term
    exactly. The Savitzky-Golay kernels already sum to zero and need none.

    Vectorized over the whole series (sliding_window_view + one matmul)
    rather than looped per row: this runs once per ticker over a 26-year
    archive, where a Python-level loop is a real cost.
    """
    width = kernel.size
    out = np.full(values.shape[0], np.nan, dtype=np.float64)
    if values.shape[0] < width:
        return out
    # Oldest-first windows, reversed to newest-first so index 0 is day t.
    ordered = np.lib.stride_tricks.sliding_window_view(values, width)[:, ::-1]
    if anchored:
        ordered = ordered - ordered[:, -1:]
    out[width - 1 :] = ordered @ kernel
    return out


def _add_calculus_features(out: pd.DataFrame, close: pd.Series) -> None:
    """Append the optional calculus block to a computed feature frame."""
    # Acceleration: the discrete second derivative of log price. ret_close
    # is already "vs this ticker's own previous trading row", so its diff
    # stays on the ticker's own clock across halts and gaps -- the same
    # convention the targets use.
    out["ret_accel"] = out["ret_close"].diff()

    # Vol-of-vol DIRECTION, not level: realized_vol_20 says how volatile
    # things are, this says which way that is moving.
    realized = out["realized_vol_20"]
    now = realized.to_numpy()
    lagged = realized.shift(_VOL_ROC_LAG).to_numpy()
    # A halted or thinly-traded ticker prints the same close for weeks and
    # realized_vol_20 goes to EXACTLY 0, where a naive log-ratio is +-inf or
    # NaN. Same graceful-degradation contract as close_position's 0.5 on a
    # doji and volume_z's 0.0 on a missing row: neutral 0.0, never a
    # non-finite value that would drop the ticker from the harness's
    # all-features-finite eligibility filter for a month of valid days.
    usable = np.isfinite(now) & np.isfinite(lagged) & (now > 0) & (lagged > 0)
    warmed = np.isfinite(now) & np.isfinite(lagged)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(usable, np.log(np.where(usable, now, 1.0) / np.where(usable, lagged, 1.0)), 0.0)
    out["vol20_roc"] = np.where(warmed, np.clip(ratio, -VOL_ROC_CLIP, VOL_ROC_CLIP), np.nan)

    # errstate: a corrupt non-positive close makes np.log emit a
    # RuntimeWarning per bad row. The value it produces is already handled
    # -- log(0) is -inf, which the Savitzky-Golay columns carry through as
    # -inf and the ANCHORED frac-diff turns into NaN (-inf minus -inf);
    # either way the row is non-finite, so it fails the harness's
    # all-features-finite eligibility check, which is the correct outcome.
    # The warning is therefore pure log noise on a full-universe run
    # (Antigravity review: "Suppress RuntimeWarning on np.log for
    # non-positive prices").
    with np.errstate(invalid="ignore", divide="ignore"):
        log_price = np.log(close.to_numpy(dtype=np.float64))
    out["sg_slope"] = _trailing_kernel(log_price, SG_SLOPE_KERNEL)
    out["sg_curvature"] = _trailing_kernel(log_price, SG_CURVATURE_KERNEL)
    out["frac_diff_close"] = _trailing_kernel(log_price, FRAC_DIFF_WEIGHTS, anchored=True)


#: Which function computes each optional block. Kept beside FEATURE_BLOCKS
#: and checked against it at import: a block added to one registry and not
#: the other used to mean the columns were NAMED in the layout but never
#: COMPUTED, which surfaces only as a wholly-NaN feature much later
#: (Antigravity review: "Modular feature block dispatching").
_BLOCK_BUILDERS: dict[str, Any] = {"calculus": _add_calculus_features}

if set(_BLOCK_BUILDERS) != set(FEATURE_BLOCKS):
    raise RuntimeError(
        "REFUSED: feature block registries disagree; "
        f"FEATURE_BLOCKS={sorted(FEATURE_BLOCKS)} builders={sorted(_BLOCK_BUILDERS)}"
    )


def _wilder_smooth(values: np.ndarray, period: int, *, start: int = 0) -> np.ndarray:
    """Wilder's smoothed average, seeded by a simple mean.

    The seed is the plain mean of ``values[start : start + period]``, placed
    at index ``start + period - 1``; every later row keeps (period-1)/period
    of the previous value. That recursion IS an exponentially weighted mean
    with alpha = 1/period, but pandas' ``ewm`` seeds from the FIRST
    observation rather than from a simple mean -- the two agree only
    asymptotically, and Wilder's own formulation (which every hand-computed
    expectation in tests/test_features.py pins) uses the mean. So the seed is
    planted by hand and only the decay is handed to ewm, which keeps this
    vectorized instead of a per-row Python loop over a 26-year archive.

    Rows before the seed are NaN: a smoothed average of fewer than `period`
    observations is not defined, and 0.0 there would read as a real reading.
    """
    out = np.full(len(values), np.nan)
    end = start + period
    if end > len(values):
        return out
    seeded = np.full(len(values), np.nan)
    seeded[end - 1] = values[start:end].mean()
    seeded[end:] = values[end:]
    smoothed = pd.Series(seeded).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()
    out[end - 1 :] = smoothed[end - 1 :]
    return out


def _wilder_rsi(close: pd.Series, period: int) -> np.ndarray:
    """Wilder RSI rescaled to [0, 1]; 0.5 when there is no movement at all."""
    delta = close.diff()
    gains = delta.clip(lower=0.0).to_numpy()
    losses = (-delta).clip(lower=0.0).to_numpy()
    # start=1 skips row 0, which has no prior close and therefore no change:
    # the seed must average `period` real deltas, not period-1 of them plus a
    # NaN.
    average_gain = _wilder_smooth(gains, period, start=1)
    average_loss = _wilder_smooth(losses, period, start=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        relative_strength = average_gain / average_loss
        rsi = 1.0 - 1.0 / (1.0 + relative_strength)
    # All gains and no losses saturates at 1.0 through the inf above; all
    # losses lands on 0.0. Neither gains NOR losses is 0/0 -- a flat stretch
    # has no strength to report either way, so it reads neutral, the same
    # graceful degradation close_position gives a doji bar.
    flat = (average_gain == 0.0) & (average_loss == 0.0)
    rsi = np.where(flat, 0.5, rsi)
    return np.where(np.isnan(average_gain) | np.isnan(average_loss), np.nan, rsi)


def _wilder_adx(frame: pd.DataFrame, period: int) -> np.ndarray:
    """Wilder ADX rescaled to [0, 1] -- trend STRENGTH, direction-free.

    Being direction-free is what keeps it from restating rsi_14 or
    trailing_ret_20: a hard trend scores high whichever way it points.
    """
    high = frame["High"].to_numpy(dtype=float)
    low = frame["Low"].to_numpy(dtype=float)
    close = frame["Close"].to_numpy(dtype=float)
    out = np.full(len(close), np.nan)
    if len(close) < 2:
        return out
    # Directional movement: only the side that expanded further counts, and
    # only when it expanded at all -- an inside day contributes nothing.
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    true_range = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])]
    )
    # Wilder smooths RUNNING SUMS here, but +DI and -DI are both divided by
    # the smoothed true range, so the shared 1/period factor cancels and the
    # smoothed AVERAGES give identical ratios.
    smoothed_range = _wilder_smooth(true_range, period)
    smoothed_plus = _wilder_smooth(plus_dm, period)
    smoothed_minus = _wilder_smooth(minus_dm, period)
    with np.errstate(invalid="ignore", divide="ignore"):
        # A range of zero (a perfectly flat stretch) means there is no
        # movement to attribute a direction to. 0.0 -- "no measurable trend"
        # -- keeps the row finite; an unguarded 0/0 would make the whole
        # feature vector non-finite and the harness's all-features-finite
        # eligibility check would then silently drop the ticker's forecast
        # for that day instead of reporting anything.
        positive = np.where(smoothed_range > 0, smoothed_plus / smoothed_range, 0.0)
        negative = np.where(smoothed_range > 0, smoothed_minus / smoothed_range, 0.0)
        spread = positive + negative
        directional_index = np.where(spread > 0, np.abs(positive - negative) / spread, 0.0)
    # ADX is the DX line smoothed a second time, so its warm-up is twice as
    # long: `period` rows of directional movement, then `period` of DX.
    smoothed_index = _wilder_smooth(directional_index, period, start=period - 1)
    out[1:] = smoothed_index
    return out


def _signed_streak(close: pd.Series) -> np.ndarray:
    """Signed count of consecutive same-direction closes, clipped.

    +3 is a third straight up day, -2 a second straight down day, and an
    unchanged close is 0: neither up nor down, and the count restarts. This
    carries direction PERSISTENCE, which trailing_ret_5 / trailing_ret_20
    cannot -- they measure how far price moved, not how many sessions in a
    row it moved the same way.
    """
    direction = pd.Series(np.sign(close.diff().to_numpy()), index=close.index)
    # Vectorized run lengths: a new group starts wherever the sign changes
    # (row 0's NaN differs from its own shifted NaN, so it opens its own
    # group and stays NaN through the multiplication below).
    group = (direction != direction.shift()).cumsum()
    position = direction.groupby(group).cumcount() + 1
    streak = direction * position
    return np.clip(streak.to_numpy(dtype=float), -_STREAK_CLIP, _STREAK_CLIP)


def compute_ticker_features(frame: pd.DataFrame, *, extra_features: Sequence[str] = ()) -> pd.DataFrame:
    """Per-ticker causal features + targets for one adjusted, Date-sorted frame.

    ``extra_features`` names optional blocks to append (see
    :func:`resolve_feature_names`); the default is the base layout alone.

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
        log_volume = pd.Series(np.log1p(volume.clip(lower=0)), index=volume.index)
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
        # Share volume ignores price level: a $5 name and a $500 name that
        # trade the same number of shares are not equally liquid, and it is
        # the dollars that decide whether a graded-ladder buy moves the
        # book. Same shape as volume_z (log1p, 20-row window, min_periods=1
        # so one corrupt row cannot blank a month) over price x volume.
        dollar_volume = close * volume.clip(lower=0)
        log_dollar = pd.Series(np.log1p(dollar_volume), index=volume.index)
        log_dollar_clean = log_dollar.where(~missing)
        dollar_mean = log_dollar_clean.rolling(_VOLUME_WINDOW, min_periods=1).mean()
        dollar_std = log_dollar_clean.rolling(_VOLUME_WINDOW, min_periods=1).std()
        with np.errstate(invalid="ignore"):
            out["dollar_volume_z"] = np.where(
                (~missing) & (dollar_std > 0), (log_dollar - dollar_mean) / dollar_std, 0.0
            )
        # Amihud price impact: how far the price moved per dollar traded.
        # Same data as dollar_volume_z, opposite lens -- that one asks how
        # busy the name is, this one how expensive it is to move.
        tradable = (~missing) & (dollar_volume > 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            impact = np.log1p(
                _AMIHUD_SCALE * out["ret_close"].abs() / dollar_volume.where(dollar_volume > 0)
            )
        # A halted (zero-volume) day would divide by zero; the neutral 0.0
        # keeps the row finite and eligible. Row 0 stays NaN through
        # ret_close: no prior close means no move to attribute to anything.
        out["amihud_illiquidity"] = impact.where(tradable, 0.0)
    else:
        out["volume_z"] = 0.0
        out["volume_missing"] = 1.0
        out["dollar_volume_z"] = 0.0
        out["amihud_illiquidity"] = 0.0
    # Mean-reversion pressure. Distinct from trailing_ret_20, which is a
    # return: this is a DISTANCE from a level, which is what a tree can
    # split on to say "stretched" without needing to know the horizon.
    out["dist_from_sma20"] = np.log(close / close.rolling(_SMA_WINDOW).mean())
    # Distance from the recent PEAK rather than from a mean -- bounded above
    # at 0 and only ever negative, so it behaves differently from
    # dist_from_sma20 exactly where it matters, at a turning point.
    out["drawdown_from_20d_high"] = np.log(close / close.rolling(_DRAWDOWN_WINDOW).max())
    # The short leg of a volatility-regime pair against realized_vol_20:
    # their ratio is the vol-clustering signal, and neither horizon alone
    # says whether the market just changed gear.
    out["realized_vol_5"] = out["ret_close"].rolling(_VOL_SHORT_WINDOW).std()
    # Intraday leg of the bar; ret_open is already the overnight one, and
    # the two sum back to ret_close exactly.
    out["intraday_ret"] = np.log(close / frame["Open"])
    out["updown_streak"] = _signed_streak(close)
    out["rsi_14"] = _wilder_rsi(close, _RSI_PERIOD)
    out["adx_14"] = _wilder_adx(frame, _ADX_PERIOD)
    # Tail-risk regime, read straight by the p10/p90 heads: a window that
    # has been throwing fat left tails is one where the band should already
    # be wider before the next one lands.
    shape_std = out["ret_close"].rolling(_SHAPE_WINDOW).std()
    # A window with no return variation has no distributional shape to
    # report. pandas answers 0.0 skew and -3.0 excess kurtosis there (the
    # degenerate limit), and -3.0 would read to the model as an extremely
    # THIN-tailed regime rather than as "no information" -- so both go
    # neutral. The guard fires only on a genuinely flat window: while the
    # rolling std is still NaN (warm-up) the raw NaN is kept.
    degenerate = shape_std.notna() & (shape_std <= 0)
    out["ret_skew_20"] = out["ret_close"].rolling(_SHAPE_WINDOW).skew().mask(degenerate, 0.0)
    out["ret_kurt_20"] = out["ret_close"].rolling(_SHAPE_WINDOW).kurt().mask(degenerate, 0.0)
    date_col: Any = pd.to_datetime(frame["Date"])
    gap = date_col.diff().dt.days.astype(float)
    out["gap_days"] = gap.clip(upper=_GAP_CLIP_DAYS)
    out["day_of_week"] = date_col.dt.dayofweek.astype(np.int8)
    # Optional blocks read the base columns computed above, so they are
    # appended here rather than interleaved. resolve_feature_names() is the
    # single place that validates block names, so a typo refuses here too
    # rather than silently returning the base layout.
    resolve_feature_names(extra_features)
    for block in dict.fromkeys(extra_features or ()):
        _BLOCK_BUILDERS[block](out, close)
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


def _cross_sectional_rank(frame: pd.DataFrame, column: str) -> pd.Series:
    """Per-day rank of ``column`` across the universe, in [0, 1].

    0.5 when a ticker has no peers that day (nothing to rank against, and
    calling a lone name the day's best or worst would be a fabrication).
    NaN wherever the SOURCE value is NaN: a ticker still inside its own
    trailing warm-up has no momentum or volatility to rank, and a
    manufactured 0.5 there would place every new listing at the exact centre
    of the market for its first month.
    """
    values = frame[column]
    grouped = values.groupby(frame["Date"])
    rank = grouped.rank(method="average")
    # transform("count") counts non-NaN, so a day's peer group is the
    # tickers that actually have the quantity, not everything that traded.
    count = grouped.transform("count")
    ranked = pd.Series(
        np.where(count > 1, (rank - 1.0) / (count - 1.0), 0.5), index=frame.index
    )
    return ranked.where(values.notna())


def _peer_median(values: np.ndarray) -> np.ndarray:
    """Leave-one-out median: for each element, the median of the OTHERS.

    Excluding self is what makes the result peer information rather than a
    restatement of the ticker's own return, which the model already has as
    ret_close. A lone member has no peers, so it reads 0.0.

    Computed from order statistics instead of an n-times-repeated median:
    removing one element from a sorted run shifts the middle by at most one
    position, so the whole group is answered in a couple of vectorized
    lookups.
    """
    size = len(values)
    if size == 1:
        return np.zeros(1)
    order = np.argsort(values, kind="stable")
    ordered = values[order]
    remaining = size - 1
    positions = np.arange(size)
    if remaining % 2 == 1:
        middle = remaining // 2
        # Removing an element at or before the middle shifts everything
        # after it down one slot, so the new middle comes from one later.
        answer = np.where(positions <= middle, ordered[middle + 1], ordered[middle])
    else:
        low, high = remaining // 2 - 1, remaining // 2
        left = np.where(positions <= low, ordered[low + 1], ordered[low])
        right = np.where(positions <= high, ordered[high + 1], ordered[high])
        answer = (left + right) / 2.0
    out = np.empty(size, dtype=float)
    out[order] = answer
    return out


def _align_cluster_labels(
    assignment: dict[str, int], previous: dict[str, int], n_clusters: int
) -> dict[str, int]:
    """Renumber a fresh clustering so a surviving group keeps its old id.

    Agglomerative labels are arbitrary per fit: nothing makes the group
    called 2 at one refit the same group at the next. Since the id is
    embedded by the octopus and split on by the GBM, an unaligned relabel
    would silently change what the category MEANS halfway through a run,
    while determinism and block-recovery checks both keep passing. Groups
    are matched to the previous refit by membership overlap, largest first,
    and anything left over takes a free id.
    """
    members: dict[int, set[str]] = {}
    for ticker, raw in assignment.items():
        members.setdefault(raw, set()).add(ticker)
    prior: dict[int, set[str]] = {}
    for ticker, label in previous.items():
        if label > 0:
            prior.setdefault(label, set()).add(ticker)

    overlaps = [
        (len(group & prior_group), raw, prior_label)
        for raw, group in members.items()
        for prior_label, prior_group in prior.items()
        if group & prior_group
    ]
    # Deterministic tie-break on (raw, prior_label) so two equally good
    # matches never resolve differently between two builds of the same data.
    overlaps.sort(key=lambda item: (-item[0], item[1], item[2]))
    mapping: dict[int, int] = {}
    taken: set[int] = set()
    for _shared, raw, prior_label in overlaps:
        if raw in mapping or prior_label in taken:
            continue
        mapping[raw] = prior_label
        taken.add(prior_label)
    free = [label for label in range(1, n_clusters + 1) if label not in taken]
    for raw in sorted(members):
        if raw not in mapping:
            mapping[raw] = free.pop(0)
    return {ticker: mapping[raw] for ticker, raw in assignment.items()}


def _cluster_once(window: pd.DataFrame, n_clusters: int) -> dict[str, int]:
    """One correlation clustering over a trailing window of returns.

    ``window`` is (dates x tickers) of ret_close ending at -- never after --
    the refit date, which is what keeps the whole thing causal. Tickers that
    barely traded through the window, or never moved, are left out: a name
    with six overlapping observations correlates with anything, and a
    constant one correlates with nothing.
    """
    minimum = max(2, int(len(window) * _CLUSTER_MIN_COVERAGE))
    # Coverage is required; VARIANCE deliberately is not. A ticker that
    # never moved through the window has an undefined correlation with
    # everything, but excluding it would leave it permanently unassigned,
    # and the two cluster-derived features would then be NaN on every one
    # of its rows -- costing it all 36 features, since the harness's
    # eligibility check needs the whole vector finite. It is admitted here
    # and the undefined correlations become the uncorrelated distance below.
    usable = [ticker for ticker in window.columns if window[ticker].count() >= minimum]
    if not usable:
        return {}
    effective = max(1, min(n_clusters, len(usable) // 2))
    if effective <= 1 or len(usable) < 2:
        # One group: no distance matrix needed, and sklearn will not fit a
        # single cluster over a single sample anyway.
        return dict.fromkeys(usable, 1)
    # min_periods applies the same floor PAIRWISE. The per-ticker coverage
    # check above admits two names that each traded half the window; without
    # this they could still share only a couple of days and produce a
    # spurious +/-1 correlation from that overlap alone (Copilot review).
    # Under-overlapped pairs come back NaN and take the uncorrelated
    # distance below.
    correlation = window[usable].corr(min_periods=minimum)
    # 1 - corr is the standard correlation distance. A pair with no
    # overlapping days has no correlation to speak of, and 1.0 (the
    # uncorrelated distance) is the honest filler.
    # np.array (not .to_numpy()) because pandas can hand back a read-only
    # view of its own block, and the repairs below write in place.
    distance = np.array(1.0 - correlation, dtype=float)
    distance[~np.isfinite(distance)] = 1.0
    np.fill_diagonal(distance, 0.0)
    distance = np.clip((distance + distance.T) / 2.0, 0.0, 2.0)
    from sklearn.cluster import AgglomerativeClustering

    model = AgglomerativeClustering(
        n_clusters=effective, metric="precomputed", linkage="average"
    )
    raw = model.fit_predict(distance)
    return {ticker: int(label) + 1 for ticker, label in zip(usable, raw, strict=True)}


def _assign_clusters(
    returns_wide: pd.DataFrame,
    calendar: np.ndarray,
    *,
    n_clusters: int,
    cluster_window: int,
    cluster_refresh: int,
) -> pd.DataFrame:
    """Style-cluster label per (calendar day, ticker); 0 = not assigned yet.

    The universe is re-clustered every ``cluster_refresh`` calendar days from
    the trailing ``cluster_window``, and each assignment holds until the next
    refit. Nothing here reads a bar later than its own refit date, so the
    perturbation tests cover this the same way they cover every other
    feature.
    """
    index = pd.DatetimeIndex(calendar)
    labels = pd.DataFrame(np.nan, index=index, columns=returns_wide.columns, dtype=float)
    previous: dict[str, int] = {}
    effective_cap = max(1, min(n_clusters, len(returns_wide.columns) // 2 or 1))
    # Anchored to a constant, never to len(index): the window below is a
    # tail(), so an archive shorter than cluster_window simply clusters from
    # less history instead of not at all, and appending rows can only add
    # later refits -- it can never move or unassign an earlier one.
    first_refit = min(cluster_window, _CLUSTER_ANCHOR_ROWS) - 1
    for position in range(first_refit, len(index), cluster_refresh):
        refit_date = index[position]
        window = returns_wide.loc[:refit_date].tail(cluster_window)
        assignment = _cluster_once(window, n_clusters)
        if not assignment:
            continue
        assignment = _align_cluster_labels(assignment, previous, effective_cap)
        # Every ticker is reset to unassigned FIRST, then the successful
        # assignments overlay it. Writing only the assignments would let
        # ffill carry a stale label forward for a ticker that dropped out of
        # this refit (coverage fell away), and its rows would then be ranked
        # inside a peer group it is no longer part of (Copilot review).
        labels.loc[refit_date, :] = 0.0
        for ticker, label in assignment.items():
            labels.loc[refit_date, ticker] = label
        previous = assignment
    # Hold each assignment until the next refit; days before the first one
    # stay unassigned rather than borrowing a future grouping.
    return labels.ffill().fillna(0).astype(np.int8)


def build_panel(
    history_dir: Path,
    *,
    tickers: list[str] | None = None,
    max_tickers: int | None = None,
    end: pd.Timestamp | None = None,
    as_of: pd.Timestamp | None = None,
    initial_train_end: pd.Timestamp | None = None,
    min_history: int = 60,
    n_clusters: int = _DEFAULT_N_CLUSTERS,
    cluster_window: int | None = None,
    cluster_refresh: int = _DEFAULT_CLUSTER_REFRESH,
    extra_features: Sequence[str] = (),
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

    ``extra_features`` names optional feature blocks to append to the
    layout (see :func:`resolve_feature_names`). The default is the base
    layout, byte-for-byte what every existing checkpoint and frozen
    normalization vector was built against.

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
            date_s: Any = pd.to_datetime(frame["Date"])
            frame = frame.loc[date_s.dt.normalize() <= end_day].reset_index(drop=True)
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

    feature_names = resolve_feature_names(extra_features)
    per_ticker = {
        ticker: compute_ticker_features(frame, extra_features=extra_features)
        for ticker, frame in frames.items()
    }

    # --- Cross-sectional pass: pooled long frame of each day's returns. ---
    # trailing_ret_20 / realized_vol_20 ride along so the universe can be
    # ranked on horizon and on risk, not only on today's move. dropna is on
    # ret_close alone: a ticker whose trailing windows have not warmed up yet
    # still belongs to the day's market, it just has no momentum to rank.
    ranked_columns = ("ret_close", "trailing_ret_20", "realized_vol_20")
    long_frame = pd.concat(
        [
            computed[["Date", *ranked_columns]].assign(ticker=ticker)
            for ticker, computed in per_ticker.items()
        ],
        ignore_index=True,
    ).dropna(subset=["ret_close"])
    market = _market_table(long_frame)
    long_frame["cs_return_rank"] = _cross_sectional_rank(long_frame, "ret_close")
    long_frame["cs_momentum_rank"] = _cross_sectional_rank(long_frame, "trailing_ret_20")
    long_frame["cs_vol_rank"] = _cross_sectional_rank(long_frame, "realized_vol_20")

    # --- Style clustering: a fundamentals-free sector proxy. ---
    returns_wide = long_frame.pivot_table(index="Date", columns="ticker", values="ret_close")
    calendar_index = np.array(sorted(market.index.to_numpy()), dtype="datetime64[ns]")
    returns_wide = returns_wide.reindex(pd.DatetimeIndex(calendar_index))
    # Derived from min_history rather than fixed, so --min-history stays the
    # one knob that decides how much warm-up a ticker needs; a hard-coded
    # window silently overrode it (Copilot review).
    effective_window = cluster_window if cluster_window is not None else max(
        _CLUSTER_ANCHOR_ROWS, min_history
    )
    cluster_labels = _assign_clusters(
        returns_wide,
        calendar_index,
        n_clusters=n_clusters,
        cluster_window=effective_window,
        cluster_refresh=cluster_refresh,
    )
    stacked = cluster_labels.stack()
    stacked.index = stacked.index.set_names(["Date", "ticker"])
    long_frame["cluster_id"] = (
        pd.MultiIndex.from_arrays([long_frame["Date"], long_frame["ticker"]])
        .map(stacked)
        .to_numpy()
    )
    long_frame["cluster_id"] = long_frame["cluster_id"].fillna(0).astype(int)
    # Peer features are defined only inside a real cluster; an unassigned row
    # (cluster 0, before the correlation window has filled) reports NaN
    # rather than a peer group that does not exist yet.
    assigned = long_frame["cluster_id"] > 0
    peers = long_frame.loc[assigned]
    within = peers.groupby(["Date", "cluster_id"])["ret_close"]
    peer_count = within.transform("count")
    peer_rank = within.rank(method="average")
    long_frame["cs_cluster_rank"] = pd.Series(np.nan, index=long_frame.index)
    long_frame.loc[assigned, "cs_cluster_rank"] = np.where(
        peer_count > 1, (peer_rank - 1.0) / (peer_count - 1.0), 0.5
    )
    # One transform call per (day, cluster) group -- tens of thousands over
    # the full archive, not one per row, which is what makes a Python-level
    # callable affordable here.
    long_frame["cluster_ret_median"] = pd.Series(np.nan, index=long_frame.index)
    long_frame.loc[assigned, "cluster_ret_median"] = within.transform(
        lambda values: _peer_median(values.to_numpy(dtype=float))
    )

    lookup_columns = [
        "cs_return_rank",
        "cs_momentum_rank",
        "cs_vol_rank",
        "cluster_ret_median",
        "cs_cluster_rank",
    ]
    rank_lookup = long_frame.set_index(["ticker", "Date"])[lookup_columns]

    calendar = calendar_index
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
        ticker_ranks = rank_lookup.loc[ticker].reindex(computed["Date"])
        for column in lookup_columns:
            joined[column] = ticker_ranks[column].to_numpy()
        # Rolling market loading on the ticker's OWN rows, against the
        # equal-weight market return the market table already computes. Beta
        # is cov/var, NOT a correlation: a name can track the market's
        # direction perfectly and still move twice as far, and it is the
        # amplitude that says how much of tomorrow is really a market call.
        own_return = joined["ret_close"]
        market_return = joined["mkt_mean_ret"]
        covariance = own_return.rolling(_BETA_WINDOW).cov(market_return)
        market_variance = market_return.rolling(_BETA_WINDOW).var()
        with np.errstate(invalid="ignore", divide="ignore"):
            beta = covariance / market_variance
        # A flat market has no variance to regress against; 0.0 ("no
        # measurable loading") keeps the row finite, while a window that
        # simply has not filled yet stays NaN.
        joined["mkt_beta_20"] = beta.where(market_variance > 0, 0.0).where(market_variance.notna())
        # fillna before the int cast: a ticker can trade on a day that never
        # entered the calendar (its own first row, whose ret_close is NaN and
        # so never reached the pooled frame). Reindexing yields NaN there,
        # and casting NaN straight to int8 is undefined -- numpy warns and
        # writes garbage. Unassigned is 0, the same as any pre-refit row.
        cluster_column = (
            cluster_labels[ticker].reindex(computed["Date"]).fillna(0).to_numpy(dtype=np.int8)
        )
        feature_matrix = joined[list(feature_names)].to_numpy(dtype=np.float32)
        target_matrix = computed[["target_open", "target_high", "target_low", "target_close"]].to_numpy(
            dtype=np.float32
        )
        series[ticker] = TickerSeries(
            ticker=ticker,
            ticker_id=vocab_index + 1,  # 0 is reserved for "unknown"
            dates=dates,
            features=feature_matrix,
            day_of_week=computed["day_of_week"].to_numpy(dtype=np.int8),
            cluster_id=cluster_column,
            targets=target_matrix,
            label_end_dates=computed["label_end_date"].to_numpy(dtype="datetime64[ns]"),
            ref_close=computed["ref_close"].to_numpy(dtype=np.float64),
            adj_ratio=computed["adj_ratio"].to_numpy(dtype=np.float64),
        )
        for row_index, date in enumerate(dates):
            date_index.setdefault(pd.Timestamp(date), []).append((ticker, row_index))

    normalization = _fit_normalization(series, initial_train_end, feature_names)
    return TrainingPanel(
        series=series,
        tickers=tickers_sorted,
        calendar=calendar,
        date_index=date_index,
        feature_names=feature_names,
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
    # panel.feature_names, not FEATURE_NAMES: a panel built with optional
    # blocks must refit over ITS layout, not the default one.
    panel.normalization = _fit_normalization(panel.series, initial_train_end, panel.feature_names)


def _fit_normalization(
    series: dict[str, TickerSeries],
    initial_train_end: pd.Timestamp | None,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
) -> dict:
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
        n_features = len(feature_names)
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
