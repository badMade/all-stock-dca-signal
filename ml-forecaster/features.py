"""OHLCV archive -> per-day, per-ticker feature rows for the pooled sizing model.

Own small CSV loader deliberately -- does NOT cross-import
ticker-advisor/pricing.py, keeping the two sibling tools independently
deployable (each has its own requirements.txt/pyproject.toml and can be
`pip install`ed and run without the other). Unlike pricing.py, this loader
never falls back to a live yfinance download: this repo makes no network
calls at all (CLAUDE.md's "This repo contains no credentials and makes no
network calls"), so there is no archive-vs-live reconciliation to do here --
only read the local archive, validate it, and coerce/sort/dedupe it.

Every feature below is computed "as of" day T using ONLY rows at or before T
(pandas .diff()/.pct_change()/.rolling() all trail backward by construction,
and nothing here ever calls .shift() with a negative period on a FEATURE
column). test_features.py's test_compute_features_no_lookahead() asserts
this directly: day T's feature row is bit-for-bit identical whether computed
from the full history or from history truncated at T, and unaffected by
perturbing every row strictly after T.

The forward-return TARGET (compute_forward_return_target) is the one
deliberate exception -- it is the training label, not a feature, and is
computed and named separately so it can never be accidentally fed into the
feature matrix itself (model.py's design matrix is built only from
NUMERIC_FEATURE_COLUMNS / CATEGORICAL_FEATURE_COLUMNS, which never include
TARGET_COLUMN).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

# Mirrors schemas/tickers.schema.json's own "active" item pattern -- the
# single source of truth for what a valid ticker symbol looks like in this
# repo (see dca_inverse_martingale.py's identical _TICKER_SYMBOL_PATTERN for
# the same reuse, and train.py's own comment on why it imports this one
# instead of keeping its own copy). discover_available_tickers() below joins
# an explicit ticker UNVALIDATED against history_dir; a value containing a
# leading "/" (an absolute path) or ".." would otherwise let `Path.joinpath`
# escape history_dir entirely -- an absolute right-hand operand REPLACES the
# left side outright in pathlib -- and read/pool an arbitrary CSV elsewhere
# on disk into the live sizing model (Codex review: "Validate explicit
# ticker symbols before resolving CSV paths").
_TICKER_SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,6}$")

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- index 0 is already this module's own directory
# (ml-forecaster/, set by the interpreter before any of this module's code
# runs), and putting REPO_ROOT ahead of it would let a same-named top-level
# module there shadow this subproject's own submodules. Mirrors the exact
# pattern ticker-advisor/pricing.py and ticker-advisor/advise_tickers.py
# already use for the identical import (see those files' own comments on
# why index 1, not 0).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402

REQUIRED_COLUMNS = ("Date", "Open", "High", "Low", "Close")
_NUMERIC_RAW_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")

# Rolling-window sizes, in trading days.
_VOLATILITY_WINDOW_DAYS = 20
_RSI_WINDOW_DAYS = 14
_FIFTY_TWO_WEEK_WINDOW_DAYS = 252
_VOLUME_AVERAGE_WINDOW_DAYS = 20

#: Forward-return horizon for the training target, in trading days.
FORWARD_RETURN_HORIZON_DAYS = 5

#: Neutral placeholder for volume_rel_20d_avg when an archive carries no
#: usable Volume column -- "exactly average volume, no signal either way" --
#: rather than dropping the feature and destabilizing the pooled model's
#: fixed column schema across tickers that do vs. don't have volume data.
_NEUTRAL_VOLUME_RATIO = 1.0

#: Numeric feature columns, in the fixed order model.py's design matrix uses.
#: Every one of these is computed strictly from data at-or-before the row's
#: own day -- see the module docstring.
NUMERIC_FEATURE_COLUMNS: tuple[str, ...] = (
    "drop_vs_prior_close_pct",
    "realized_vol_20d",
    "rsi_14",
    "drawdown_from_52w_high_pct",
    "trailing_return_5d_pct",
    "trailing_return_20d_pct",
    "volume_rel_20d_avg",
)

#: Categorical feature columns -- one-hot encoded by model.py, not here,
#: since a pooled model's ticker vocabulary is only known once every
#: ticker's history has been assembled (see model.py's fit()/_design_matrix).
CATEGORICAL_FEATURE_COLUMNS: tuple[str, ...] = ("ticker", "day_of_week")

#: Forward N-trading-day realized return (percent), conditional on the row
#: being a dip day (drop_vs_prior_close_pct > 0) -- filtering to dip days is
#: the CALLER's job (features.build_dataset() just computes the `dip_day`
#: flag; train.py/evaluate.py filter on it). DELIBERATELY forward-looking;
#: never a feature -- see the module docstring.
TARGET_COLUMN = "forward_5d_return_pct"


def load_history_csv(path: Path) -> pd.DataFrame:
    """Read one ticker's OHLCV archive CSV into a Date-sorted, deduplicated frame.

    Requires Date/Open/High/Low/Close; Adj Close and Volume are read if
    present but not required (compute_features() degrades gracefully when
    Volume is absent -- see its own docstring). Unparseable rows (a bad Date,
    a non-numeric Close) are dropped rather than raising, since a single
    corrupted row in a multi-year archive must not make the whole ticker
    unusable for training.
    """
    # Deliberately NOT parse_dates=["Date"] here: when even one row's Date is
    # unparseable, pandas' read_csv-level date parsing leaves the WHOLE
    # column as object dtype rather than raising -- so the
    # is_datetime64_any_dtype check below would fail closed on the entire
    # ticker over a single bad row, contradicting this docstring's
    # corruption-tolerance contract. Parsing separately with
    # errors="coerce" turns exactly the unparseable row(s) into NaT, which
    # the dropna(subset=["Date", ...]) below then discards individually --
    # same per-row-drop treatment already given to a non-numeric Close.
    frame = pd.read_csv(path)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing required column(s): {missing}")
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    # A CSV whose Date column carries an explicit UTC offset (e.g.
    # "2025-01-02T00:00:00Z") makes pd.to_datetime() infer a TZ-AWARE
    # datetime64 dtype for the whole column, whereas every naive-datetime
    # comparison this module and its callers make against archive dates
    # (`_iter_ticker_frames`'s own `as_of_day_start` cutoff below, and
    # train.py's staleness cutoffs against the "date" column this becomes)
    # is built from a plain naive Timestamp/datetime. Comparing a tz-aware
    # Series against a naive one raises `TypeError: Invalid comparison`
    # rather than the finite/coercible result every caller assumes --
    # train.py's dataset-construction refusal boundary only catches
    # (OSError, ValueError), not TypeError, so this crashed the whole
    # weekly retrain with an uncaught traceback instead of the documented
    # REFUSED result (Codex review: "Normalize timezone-aware archive dates
    # before cutoff filtering"). Normalize once, here at the source, so
    # every downstream consumer of "Date"/"date" can keep assuming naive
    # timestamps -- converting to UTC first (not dropping the offset
    # outright) preserves the correct calendar day regardless of which
    # offset the archive happened to use.
    if getattr(frame["Date"].dtype, "tz", None) is not None:
        frame["Date"] = frame["Date"].dt.tz_convert("UTC").dt.tz_localize(None)
    for column in _NUMERIC_RAW_COLUMNS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not pd.api.types.is_datetime64_any_dtype(frame["Date"]):
        raise ValueError(f"{path}: Date column did not parse to datetimes")
    frame = frame.dropna(subset=["Date", "Close"])
    # Stable sort + keep="last" on a duplicate Date: a later-written row wins,
    # matching this repo's own archive-merge convention (see
    # ticker-advisor/pricing.py's identical duplicate-Date handling).
    frame = frame.sort_values("Date", kind="stable")
    frame = frame.drop_duplicates(subset="Date", keep="last")
    return frame.reset_index(drop=True)


def discover_available_tickers(history_dir: Path, tickers: Iterable[str] | None = None) -> list[str]:
    """Tickers with a present `<ticker>.csv` under `history_dir`.

    `tickers=None` discovers every archived ticker (sorted); an explicit list
    is filtered down to the ones that actually have a CSV, silently dropping
    the rest -- callers that need to know what was dropped should diff their
    own input against this function's return value. A repeated symbol in
    that explicit list (e.g. `--tickers AAPL AAPL MSFT`) is deduplicated,
    preserving first-occurrence order -- otherwise `_iter_ticker_frames()`
    would append that ticker's full history twice, silently doubling its
    weight in the pooled Ridge fit and in every walk-forward metric used to
    authorize publication (Codex review: "Deduplicate explicitly requested
    training tickers").
    """
    if tickers is not None:
        seen: set[str] = set()
        deduped = []
        for ticker in tickers:
            if ticker not in seen:
                seen.add(ticker)
                deduped.append(ticker)
        # A ticker outside the repository's own symbol pattern -- e.g. an
        # absolute path or a "../"-relative one -- is filtered out here,
        # BEFORE it's ever joined into a filesystem path below, exactly like
        # a ticker with no CSV present: silently dropped, not raised, so
        # callers that need to know what was rejected diff their own input
        # against this function's return value (same contract as the
        # missing-CSV case just below).
        deduped = [ticker for ticker in deduped if _TICKER_SYMBOL_PATTERN.match(ticker)]
        return [ticker for ticker in deduped if (history_dir / f"{ticker}.csv").is_file()]
    if not history_dir.is_dir():
        return []
    # A case-sensitive filesystem can hold BOTH "AAPL.csv" and "aapl.csv" --
    # left undeduplicated, each would be pooled as a separate "ticker" with
    # its own full history (silently doubling one symbol's effective
    # weight, or worse, splitting genuinely different date ranges across
    # two "tickers"), even though every downstream consumer (prediction
    # output, the recorded evaluation universe, published adjustments)
    # already normalizes with `.upper()` and would collapse them into one
    # -- concealing the real, duplicated population that was actually used
    # to authorize publication (Codex review: "Deduplicate case-folded
    # archive symbols"). One stem per canonical uppercase symbol is kept,
    # preferring the already-canonically-cased file when present -- the
    # same "silently filter, diff to detect" contract this function's
    # explicit-list branch above already documents, not a raise, since a
    # stray duplicate here is far more likely to be leftover manual-testing
    # debris than a signal an operator needs surfaced mid-retrain.
    # A stem outside the repository's own symbol pattern -- e.g. an
    # auxiliary/backup file like "AAPL-backup.csv" left in the archive
    # directory -- must never be treated as a real ticker. Unlike the
    # explicit-list branch above, this default (--all-tickers-in-archive)
    # branch previously validated NOTHING: every `*.csv` stem was pooled
    # into the Ridge fit and walk-forward gate regardless of shape,
    # silently distorting the published adjustments for the REAL tickers
    # with an invalid one's history, while never itself corresponding to
    # any configured symbol (Codex review: "Filter invalid archive stems
    # before pooled training"). Filtered BEFORE the case-fold dedup below,
    # so an invalid stem can't win a case-fold collision against a valid one.
    stems = sorted(path.stem for path in history_dir.glob("*.csv") if _TICKER_SYMBOL_PATTERN.match(path.stem))
    chosen: dict[str, str] = {}
    for stem in stems:
        upper = stem.upper()
        if upper not in chosen or stem == upper:
            chosen[upper] = stem
    return sorted(chosen.values())


def _preferred_price_series(frame: pd.DataFrame) -> pd.Series:
    """Per-row price series for every return/volatility/RSI/drawdown/target computation.

    Prefers `Adj Close` (split/dividend-adjusted) over raw `Close`, but only
    where `Adj Close` is present, non-null, and positive for that specific
    row; falls back to raw `Close` row-by-row otherwise (an archive that
    predates the `Adj Close` column entirely, or one where it is null/
    non-positive for a handful of rows) -- a ticker is never dropped and no
    row is discarded just because one field is unusable, matching this
    module's existing corruption-tolerance stance (see load_history_csv()'s
    docstring on the same principle for a bad Date).

    This matters because a raw `Close` series reads a stock split (or a
    large special dividend) as a massive fake one-day move: e.g. a 4-for-1
    split makes `Close` drop ~75% overnight even though nothing about the
    position's value changed. Every quantity this module derives from price
    -- day-over-day return, realized volatility, RSI, drawdown-from-52-week-
    high, and the forward-return training target -- would then treat the
    split date (and the target window that follows it) as an extreme market
    move, contaminating both model fitting and the walk-forward metrics that
    gate live publication (docs/ticker_advisory.md's ml-forecaster section).
    `Open`/`High`/`Low`/`Volume` are deliberately left raw: nothing here
    derives return/volatility/target math from them, so adjusting them would
    be a larger, unrequested change.
    """
    close = frame["Close"].astype(float)
    # The raw Close FALLBACK itself must be validated too -- a zero,
    # negative, or infinite raw Close (a genuinely corrupted archive row)
    # used to be returned unvalidated whenever Adj Close was unusable.
    # build_dataset()/build_feature_frame() only dropna() the required
    # columns, so a non-finite fallback value would survive into a feature/
    # target and abort Ridge fitting downstream, the same class of failure
    # already fixed for Adj Close itself. Coercing an invalid raw Close to
    # NaN here (rather than returning it) lets that SAME existing dropna()
    # discard the row correctly, with no new drop logic needed (Codex
    # review: "Validate the raw close before using it as a fallback").
    close_valid = close.notna() & np.isfinite(close) & (close > 0.0)
    close_safe = close.where(close_valid)
    if "Adj Close" not in frame.columns:
        return close_safe
    adjusted = frame["Adj Close"].astype(float)
    # notna() alone does not catch +/-inf -- and unlike NaN, "inf > 0.0" is
    # TRUE, so an infinite Adj Close previously passed this predicate as
    # "usable" instead of falling back. A row whose forward-return target
    # window ends on that row would then carry an infinite target value,
    # which survives dropna() (inf is not NaN either) and aborts Ridge
    # fitting on the scheduled retrain (Codex review: "Fall back from
    # non-finite adjusted closes").
    usable = adjusted.notna() & np.isfinite(adjusted) & (adjusted > 0.0)
    # A handful of rows with an unusable Adj Close (a provider gap, not a
    # whole-column outage) used to splice raw Close directly into an
    # otherwise-adjusted series -- fine away from any split/dividend event,
    # but around one, Close and Adj Close sit on materially different
    # scales, so one bad row creates an artificial jump INTO the raw scale
    # and back OUT of it on the very next row: exactly the fake one-day
    # move this whole function exists to prevent, and those finite (if
    # wrong) values survive dropna() untouched (Codex review: "Avoid mixing
    # raw and adjusted price scales").
    #
    # A SECOND earlier version of this fix interpolated directly within the
    # Adj Close series (linear fill between the nearest valid values on
    # EITHER side of a gap). That correctly avoids mixing scales, but breaks
    # this module's own central, tested invariant instead: "every feature is
    # computed 'as of' day T using ONLY rows at or before T" (see the module
    # docstring, and test_compute_features_no_lookahead()). Interpolating a
    # gap using a LATER valid row leaks that later row's price into day T's
    # own feature computation -- and since compute_forward_return_target()
    # is built from this SAME series, a missing label-window endpoint could
    # silently interpolate in a price from AFTER walk_forward_splits()'s own
    # leakage boundary, producing overly optimistic walk-forward metrics
    # that authorize publication on lookahead-contaminated data (Codex
    # review: "Avoid future-directed interpolation of adjusted prices").
    #
    # Forward-fill ONLY: infer a gap row's price from the adjustment ratio
    # (Adj Close / Close) of the most recent PRIOR row where both are valid,
    # applied to THIS row's own raw Close -- never a later row's ratio or
    # price. This still gets a gap row wrong if it lands exactly ON a
    # split/dividend's effective date (the ratio genuinely changes there,
    # and the only ratio available as-of that date is the stale pre-event
    # one) -- a real, accepted residual gap, but a narrower and less severe
    # one than either a raw-scale splice or a lookahead leak: it requires a
    # provider gap to coincide with a split/dividend event on the exact same
    # row, and even then produces one row's finite-but-wrong price, not a
    # metrics-wide evaluation bias. A leading run of rows with no PRIOR
    # valid ratio to forward-fill from at all (including the whole series,
    # if Adj Close is unusable everywhere) falls back to that row's own raw
    # Close -- the same "archive predates Adj Close" fallback above, now
    # also covering "Adj Close is present but wholly unusable so far".
    ratio = (adjusted / close_safe).where(usable & close_valid)
    ratio_forward_filled = ratio.ffill()
    inferred = close_safe * ratio_forward_filled
    inferred_valid = inferred.notna() & np.isfinite(inferred) & (inferred > 0.0)
    combined = adjusted.where(usable, inferred.where(inferred_valid))
    return combined.where(combined.notna() & np.isfinite(combined) & (combined > 0.0), close_safe)


def compute_features(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """One feature row per trading day in `frame` -- see module docstring on no-lookahead.

    `frame` must already be Date-sorted ascending (load_history_csv()
    guarantees this). Returns a DataFrame with NUMERIC_FEATURE_COLUMNS,
    "ticker", "day_of_week", "Date", and a bookkeeping "close" column (the
    _preferred_price_series() value used for every derived quantity below --
    see that function's docstring for the Adj-Close-preferred, Close-
    fallback rule -- never one of NUMERIC_FEATURE_COLUMNS and must not be fed
    to the model).
    """
    close = _preferred_price_series(frame)
    daily_return = close.pct_change()

    prior_close = close.shift(1)
    # Positive == a drop (price fell) -- matches near_close_dca.py's own
    # drop_vs_previous_close convention exactly:
    # `(previous_close - decision_price) / previous_close`.
    drop_vs_prior_close_pct = ((prior_close - close) / prior_close) * 100.0

    realized_vol_20d = daily_return.rolling(_VOLATILITY_WINDOW_DAYS).std(ddof=0) * 100.0

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.rolling(_RSI_WINDOW_DAYS).mean()
    avg_loss = loss.rolling(_RSI_WINDOW_DAYS).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        relative_strength = avg_gain / avg_loss
        rsi_14 = 100.0 - (100.0 / (1.0 + relative_strength))
    # avg_loss == 0 makes relative_strength inf/NaN above: no losses in the
    # trailing window means RSI is 100 if there was any gain at all, or 50
    # (neutral -- a perfectly flat window, not "maximally overbought") if
    # there was neither gain nor loss.
    rsi_14 = rsi_14.where(avg_loss != 0, np.where(avg_gain > 0, 100.0, 50.0))
    # Still-warming-up rows (fewer than _RSI_WINDOW_DAYS of history so far)
    # must stay NaN, not get swept into the avg_loss==0 "flat window" branch
    # above just because an all-NaN rolling mean also compares unequal-to-
    # zero as False... (NaN != 0 is True in pandas/numpy, which would
    # otherwise wrongly route warmup rows through the .where() TRUE branch
    # and keep the correctly-computed NaN -- restated explicitly here so the
    # warmup contract doesn't depend on that NaN-comparison subtlety).
    rsi_14 = rsi_14.where(avg_gain.notna() & avg_loss.notna())

    # min_periods=1 (not the full 252-day window) -- a ticker with under a
    # year of archived history still gets a meaningful "drawdown from the
    # highest close SEEN SO FAR" from day one, computed only from rows at or
    # before the current one, which is exactly as no-lookahead-safe as
    # waiting for a full 252-day window; it just means "52-week high" is
    # really "high over whatever trailing window exists" for an early row.
    rolling_52w_high = close.rolling(_FIFTY_TWO_WEEK_WINDOW_DAYS, min_periods=1).max()
    drawdown_from_52w_high_pct = ((close - rolling_52w_high) / rolling_52w_high) * 100.0

    trailing_return_5d_pct = close.pct_change(5) * 100.0
    trailing_return_20d_pct = close.pct_change(20) * 100.0

    if "Volume" in frame.columns and frame["Volume"].notna().any():
        volume = frame["Volume"].astype(float)
        # A non-finite (or negative) raw Volume must be excluded BEFORE the
        # rolling mean, not left in it: a single infinite value makes
        # rolling_avg_volume infinite for every one of the (up to
        # VOLUME_AVERAGE_WINDOW_DAYS-1) subsequent windows that include it,
        # and "finite / inf" silently evaluates to 0.0 -- a spurious "zero
        # volume" feature, not NaN -- for every OTHER row in those windows.
        # Only the corrupted row's own ratio (inf/inf -> NaN) would be
        # caught by the existing dropna() downstream; up to 19 neighboring
        # rows would quietly carry a corrupted value into training (Codex
        # review: "Reject non-finite volume before rolling calculations").
        # .rolling(..., min_periods=1).mean() already skips NaN entries
        # within its window, so replacing the bad value with NaN here (not
        # dropping the row) contains the damage to only that row's own
        # feature, exactly like the invalid-close handling above.
        volume = volume.where(np.isfinite(volume) & (volume >= 0))
        rolling_avg_volume = volume.rolling(_VOLUME_AVERAGE_WINDOW_DAYS, min_periods=1).mean()
        volume_rel_20d_avg = (volume / rolling_avg_volume).where(rolling_avg_volume > 0, _NEUTRAL_VOLUME_RATIO)
    else:
        # Degrade gracefully: this archive doesn't carry (usable) volume.
        # A neutral constant, not an omitted column -- see
        # _NEUTRAL_VOLUME_RATIO's own comment on why a fixed schema across
        # every pooled ticker matters more here than precision for the
        # tickers that do have volume.
        volume_rel_20d_avg = pd.Series(_NEUTRAL_VOLUME_RATIO, index=frame.index)

    out = pd.DataFrame(
        {
            "Date": frame["Date"].to_numpy(),
            "ticker": ticker,
            "drop_vs_prior_close_pct": drop_vs_prior_close_pct,
            "realized_vol_20d": realized_vol_20d,
            "rsi_14": rsi_14,
            "drawdown_from_52w_high_pct": drawdown_from_52w_high_pct,
            "trailing_return_5d_pct": trailing_return_5d_pct,
            "trailing_return_20d_pct": trailing_return_20d_pct,
            "volume_rel_20d_avg": volume_rel_20d_avg.to_numpy(),
            "day_of_week": pd.DatetimeIndex(frame["Date"]).dayofweek,
            "close": close.to_numpy(),
        }
    )
    return out


def _require_positive_horizon(horizon: int) -> None:
    """Guard shared by compute_forward_return_target()/compute_label_end_date().

    Both are a forward `.shift(-horizon)`; a zero or negative horizon makes
    that shift look BACKWARD instead of forward, so the "target" would read
    prices from before the row's own date rather than after it. Walk-forward
    eligibility (evaluate.walk_forward_splits()) is decided from
    compute_label_end_date()'s output, so a backward shift would let rows
    from inside the test window into training -- leaked, over-optimistic
    metrics that could pass guarded_publish()'s gate. train.py's CLI is the
    primary defense (its --horizon argparse type= rejects a nonpositive
    value before any dataset is built at all, per train.py's
    _positive_int()), but these functions are public and importable
    directly, so this second guard keeps a non-CLI caller from producing
    silently-leaked labels too.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be a strictly positive integer, got {horizon}")


def compute_forward_return_target(
    frame: pd.DataFrame, horizon: int = FORWARD_RETURN_HORIZON_DAYS
) -> pd.Series:
    """Forward `horizon`-trading-day realized return (percent) -- the training LABEL.

    DELIBERATELY forward-looking: reads rows T+1..T+horizon, which is exactly
    what a FEATURE must never do (see module docstring). The last `horizon`
    rows of any ticker's history have no future close yet and come back NaN;
    build_dataset() drops those rows before training/evaluation ever sees
    them. Computed from _preferred_price_series(), not raw Close -- see that
    function's docstring for why (a stock split would otherwise read as a
    fake extreme return for whichever target window straddles the split
    date). Raises ValueError for a nonpositive `horizon` -- see
    _require_positive_horizon().
    """
    _require_positive_horizon(horizon)
    close = _preferred_price_series(frame)
    future_close = close.shift(-horizon)
    return ((future_close - close) / close) * 100.0


def compute_label_end_date(frame: pd.DataFrame, horizon: int = FORWARD_RETURN_HORIZON_DAYS) -> pd.Series:
    """The trading date `horizon` rows ahead of each row -- the END of that row's target window.

    Row-aligned to `frame["Date"]`; NaT for the trailing `horizon` rows of
    any ticker's history, exactly mirroring compute_forward_return_target()'s
    own NaN tail (both are the identical forward `.shift(-horizon)`, one on
    Date and one on the preferred price series). Raises ValueError for a
    nonpositive `horizon` -- see _require_positive_horizon().

    This is what evaluate.walk_forward_splits() uses -- NOT each row's own
    `date` -- to decide whether a row is safe to put in a fold's training
    set: a row whose own date is before a test fold's start but whose LABEL
    (computed from prices up to and including this date) still leaks
    test-period closes into training if it is left in. See evaluate.py's
    module docstring for the full walk-forward-leakage rationale.
    """
    _require_positive_horizon(horizon)
    return frame["Date"].shift(-horizon)


def _iter_ticker_frames(
    history_dir: Path, tickers: Iterable[str] | None, *, as_of: "pd.Timestamp | None" = None
) -> Iterable[tuple[str, pd.DataFrame, pd.DataFrame]]:
    """Yield (ticker, raw_ohlcv, feature_frame) for every resolved, non-empty ticker archive.

    Shared by build_dataset() (which additionally attaches the forward-return
    target and label-end-date, both of which need `raw`'s own Close/Date
    series) and build_feature_frame() (which needs only the features
    themselves) so the two stay computed identically off the same CSV read
    and the same compute_features() call -- no risk of the two frames'
    feature values ever drifting apart from each other.

    ``as_of``, when given, drops every row dated on or after its OWN
    calendar day BEFORE compute_features()/the forward-return target ever
    see the archive -- train.py's --now can be set earlier than the archive
    tail for reproducible/test replay runs, and without this truncation a
    "historical" run would silently train, evaluate, and score against rows
    (and rolling features/targets computed FROM those rows) that had not
    happened yet as of the replayed "now", contaminating the artifact with
    lookahead while it still claims the earlier run_at (Codex review:
    "Exclude observations after the effective run time").

    The boundary is `as_of`'s own CALENDAR DAY, not `as_of` itself: an
    archived daily row's Date is always midnight-normalized and represents
    that FULL trading day's close, which an intraday `as_of` (e.g. 10 AM,
    before the session closes) has not actually observed yet even though
    midnight-of-that-day is chronologically "<=" a 10 AM timestamp. Rather
    than reproduce Eastern-market-hours/DST logic here (a heavier dependency
    this light, stdlib-adjacent module deliberately avoids), the row for
    `as_of`'s own day is always excluded, not just days strictly after it --
    conservative in the rare case a live run genuinely happens after that
    day's close (that day's row simply isn't visible until the NEXT
    invocation), but never wrong in the direction that matters: it can never
    admit a row whose close was not truly locked in yet (Codex review:
    "Exclude the current day's close from intraday replays").
    """
    resolved_tickers = discover_available_tickers(history_dir, tickers)
    as_of_day_start = pd.Timestamp(as_of).normalize() if as_of is not None else None
    for ticker in resolved_tickers:
        raw = load_history_csv(history_dir / f"{ticker}.csv")
        if as_of_day_start is not None:
            raw = raw[raw["Date"] < as_of_day_start].reset_index(drop=True)
        if raw.empty:
            continue
        yield ticker, raw, compute_features(raw, ticker)


def build_dataset(
    history_dir: Path,
    tickers: Iterable[str] | None = None,
    *,
    horizon: int = FORWARD_RETURN_HORIZON_DAYS,
    as_of: "pd.Timestamp | None" = None,
) -> pd.DataFrame:
    """Pooled feature+target rows across every requested (or discovered) ticker.

    One row per (ticker, trading day): "date", "ticker", every
    NUMERIC_FEATURE_COLUMNS value, "day_of_week", "close", TARGET_COLUMN,
    "label_end_date", and a "dip_day" flag (drop_vs_prior_close_pct > 0).
    Rows missing any required numeric feature or the target -- rolling-window
    warmup at the start of a series, or the last `horizon` days at the end
    where no future close exists yet -- are dropped, so a caller training on
    this frame never sees a partially-populated row. Sorted chronologically
    (ties broken by ticker) so callers can walk-forward split directly on it.

    ``as_of``, when given, is forwarded to :func:`_iter_ticker_frames` --
    rows dated on or after its own calendar day are excluded before any
    feature/target computation even runs (see that function's own docstring
    for why the boundary is the whole day, not the exact timestamp), so a
    replay's earlier "now" naturally trims its own trailing `horizon` rows
    to NaN target too (the same dropna() below that already handles the
    archive's own true tail does the rest -- no separate
    end-of-archive-vs-as_of case needed).

    This target-trimmed frame is for FITTING/EVALUATING a model only. It
    deliberately drops each ticker's most recent `horizon` trading days (no
    target available yet), so it must NEVER be used to build a "score
    today's features" prediction snapshot -- use build_feature_frame() for
    that (see its own docstring for why the distinction matters).
    """
    empty_columns = [
        "date",
        "ticker",
        *NUMERIC_FEATURE_COLUMNS,
        "day_of_week",
        "close",
        TARGET_COLUMN,
        "label_end_date",
        "dip_day",
    ]
    frames = []
    for _ticker, raw, ticker_features in _iter_ticker_frames(history_dir, tickers, as_of=as_of):
        ticker_features[TARGET_COLUMN] = compute_forward_return_target(raw, horizon).to_numpy()
        ticker_features["label_end_date"] = compute_label_end_date(raw, horizon).to_numpy()
        frames.append(ticker_features)
    if not frames:
        return pd.DataFrame(columns=empty_columns)

    pooled = pd.concat(frames, ignore_index=True)
    pooled = pooled.rename(columns={"Date": "date"})
    required_columns = [*NUMERIC_FEATURE_COLUMNS, TARGET_COLUMN]
    # np.isfinite() rather than dropna(): every column here is a RATIO
    # (a return, a drawdown, a volume-vs-average) or a rolling statistic
    # derived from individually-validated, finite, positive prices --
    # np.isfinite/positivity checks are already applied to each raw price
    # CELL upstream (compute_features()'s own _preferred_price_series() and
    # friends), but the RATIO/DIFFERENCE computed from two such cells can
    # still overflow to +/-inf given an extreme enough price pair (e.g. a
    # near-zero denominator), even though neither input was individually
    # invalid. dropna(subset=...) alone only removes NaN -- inf survives it
    # untouched -- so an affected row would reach Ridge fitting/evaluation
    # and either abort the whole scheduled retrain with an uncaught error
    # or silently produce non-finite metrics, instead of the loader's own
    # documented per-row corruption-tolerance contract (Codex review: "Drop
    # derived non-finite rows before model fitting"). np.isfinite() rejects
    # NaN the same way dropna() did, so this fully replaces it rather than
    # supplementing it.
    pooled = pooled[np.isfinite(pooled[required_columns].to_numpy(dtype=float)).all(axis=1)]
    pooled["dip_day"] = pooled["drop_vs_prior_close_pct"] > 0
    pooled = pooled.sort_values(["date", "ticker"], kind="stable").reset_index(drop=True)
    return pooled


def build_feature_frame(
    history_dir: Path, tickers: Iterable[str] | None = None, *, as_of: "pd.Timestamp | None" = None
) -> pd.DataFrame:
    """Pooled feature-only rows across every requested (or discovered) ticker -- NO target column.

    One row per (ticker, trading day): "date", "ticker", every
    NUMERIC_FEATURE_COLUMNS value, "day_of_week", "close", and a "dip_day"
    flag. Rows are dropped only for rolling-window warmup (a missing
    NUMERIC_FEATURE_COLUMNS value) -- UNLIKE build_dataset(), the trailing
    `horizon` days of each ticker's history are kept, since there is no
    target column here to require.

    ``as_of``, when given, is forwarded to :func:`_iter_ticker_frames` --
    for a replay whose "now" is earlier than the archive tail, this is what
    makes train.py's "most recent available row" mean "most recent as of
    the replay", not the real archive's true latest row (which would be
    scoring on data from the future relative to that replay).

    This is the frame train.py's prediction step must score against: the
    published sizing adjustment is meant to represent the model's read of
    each ticker's CURRENT (most recently archived) state, and build_dataset()
    can never provide that current-tail row -- it drops exactly those rows
    because their forward-return target isn't computable yet. Training/
    evaluation still uses build_dataset()'s target-trimmed frame; only the
    prediction input changes.
    """
    empty_columns = ["date", "ticker", *NUMERIC_FEATURE_COLUMNS, "day_of_week", "close", "dip_day"]
    frames = [
        ticker_features
        for _ticker, _raw, ticker_features in _iter_ticker_frames(history_dir, tickers, as_of=as_of)
    ]
    if not frames:
        return pd.DataFrame(columns=empty_columns)

    pooled = pd.concat(frames, ignore_index=True)
    pooled = pooled.rename(columns={"Date": "date"})
    # np.isfinite(), not dropna() -- see build_dataset()'s identical fix and
    # its own comment for why a derived ratio/rolling-statistic feature can
    # overflow to +/-inf even when every underlying price cell was
    # individually validated. This is the live SCORING path
    # (TrainedModel.predict_return_pct reads this frame directly), so an
    # inf feature reaching prediction would produce a non-finite multiplier
    # from a real model call, not merely a blocked retrain.
    pooled = pooled[np.isfinite(pooled[list(NUMERIC_FEATURE_COLUMNS)].to_numpy(dtype=float)).all(axis=1)]
    pooled["dip_day"] = pooled["drop_vs_prior_close_pct"] > 0
    pooled = pooled.sort_values(["date", "ticker"], kind="stable").reset_index(drop=True)
    return pooled


__all__ = [
    "CATEGORICAL_FEATURE_COLUMNS",
    "FORWARD_RETURN_HORIZON_DAYS",
    "NUMERIC_FEATURE_COLUMNS",
    "REQUIRED_COLUMNS",
    "TARGET_COLUMN",
    "build_dataset",
    "build_feature_frame",
    "compute_features",
    "compute_forward_return_target",
    "compute_label_end_date",
    "configured_ticker_history_dir",
    "discover_available_tickers",
    "load_history_csv",
]
