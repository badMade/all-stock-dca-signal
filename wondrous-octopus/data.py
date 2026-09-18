"""Archive I/O and panel containers for wondrous-octopus.

This module owns everything between the on-disk per-ticker CSV archive and an
in-memory, numpy-backed panel the models and harness consume:

- discovery of ``<TICKER>.csv`` files (underscore-prefixed archive metadata
  and invalid stems are never tickers);
- corruption-tolerant CSV loading (adapted from ml-forecaster/features.py's
  load_history_csv -- siblings deliberately do not import each other, see the
  root CLAUDE.md);
- split/dividend adjustment of the FULL bar. ml-forecaster left Open/High/Low
  raw because only Close-derived math mattered there; here O/H/L/C are the
  prediction targets, so an unadjusted 4:1 split would look like a fake -75%
  overnight move in all four components. The whole system therefore operates
  in adjusted space, and each row's adjustment ratio is kept so dollar-space
  conversion stays auditable;
- as-of truncation with the same calendar-day-exclusive rule as
  ml-forecaster/features.py's _iter_ticker_frames: rows dated on or after the
  as-of instant's own calendar day are dropped, never admitted;
- the ``TickerSeries`` / ``TrainingPanel`` containers filled in by
  features.build_panel() (containers live here, the builder lives in
  features.py, so the import graph stays acyclic: features -> data).

Missing or broken tickers are never blockers: an absent, unreadable, empty,
or malformed CSV is recorded in ``TrainingPanel.skipped`` and the run
proceeds without it. Only an archive that yields ZERO usable tickers is a
hard error -- fail closed on "no data at all", tolerant of "some data
missing".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

# Same symbol shape the repo's core uses (see ml-forecaster/features.py's
# _TICKER_SYMBOL_PATTERN and its review-trail comment): joined-unvalidated
# ticker names could otherwise escape history_dir via pathlib's
# absolute-right-operand behavior, and archive debris like "AAA-backup.csv"
# must never be pooled as a real ticker.
_TICKER_SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,6}$")

REQUIRED_COLUMNS = ("Date", "Open", "High", "Low", "Close")
_NUMERIC_RAW_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits")

#: Relative tolerance when validating bar geometry after adjustment. Real
#: archives carry cent-level rounding that can put High a hair under
#: max(Open, Close); that is data noise, not corruption, and must not cost
#: the row. Anything beyond this tolerance is a genuinely impossible bar.
_BAR_CONSISTENCY_RTOL = 1e-4

#: Names of the four forecast components, in the fixed order every target
#: array, forecast dict, and metrics table uses.
COMPONENTS = ("open", "high", "low", "close")

#: Inclusive bounds of what datetime64[ns] can represent. Dates outside this
#: window are unusable downstream (and crash the cast -- see load_history_csv).
_NS_EPOCH_MIN = pd.Timestamp("1677-09-22")
_NS_EPOCH_MAX = pd.Timestamp("2262-04-10")


def discover_available_tickers(history_dir: Path, tickers: list[str] | None = None) -> list[str]:
    """Tickers with a present ``<ticker>.csv`` under ``history_dir``.

    Adapted from ml-forecaster/features.py:discover_available_tickers,
    keeping its contracts: an explicit list is deduplicated
    (first-occurrence order), symbol-pattern-filtered BEFORE any path join,
    and silently reduced to the tickers that actually have a CSV; the
    default branch case-folds duplicate stems to one canonical symbol.

    The ``*.csv`` glob plus the symbol pattern already exclude the archive's
    underscore-prefixed metadata entries (``_delisted.txt``,
    ``_update_progress.json``, the ``_intraday/`` subtree) and debris like
    ``AAA-backup.csv``; the explicit startswith guard below is defense in
    depth that also documents the archive layout. The repo-root
    ``historical_stock_data.csv`` (the dead Polygon stub) lives outside any
    history dir, so this glob can never see it.
    """
    if tickers is not None:
        seen: set[str] = set()
        deduped = []
        for ticker in tickers:
            # Case-normalize BEFORE dedup/pattern-match: `--tickers aapl`
            # from a CLI user must mean AAPL, not a silent drop (Antigravity
            # review: "Case sensitivity drops valid ticker names"). The
            # dedup then also collapses aapl/AAPL to one entry.
            upper = ticker.upper()
            if upper not in seen:
                seen.add(upper)
                deduped.append(upper)
        deduped = [ticker for ticker in deduped if _TICKER_SYMBOL_PATTERN.match(ticker)]
        return [ticker for ticker in deduped if (history_dir / f"{ticker}.csv").is_file()]
    if not history_dir.is_dir():
        return []
    stems = sorted(
        path.stem
        for path in history_dir.glob("*.csv")
        if not path.stem.startswith("_") and _TICKER_SYMBOL_PATTERN.match(path.stem)
    )
    chosen: dict[str, str] = {}
    for stem in stems:
        upper = stem.upper()
        if upper not in chosen or stem == upper:
            chosen[upper] = stem
    return sorted(chosen.values())


def load_history_csv(path: Path) -> pd.DataFrame:
    """Read one ticker's archive CSV into a Date-sorted, deduplicated frame.

    Adapted from ml-forecaster/features.py:load_history_csv, preserving its
    corruption-tolerance contract (drop the bad row, keep the ticker) and
    its tz-normalization (see that function's review-trail comments for why
    Date parsing is separate from read_csv and why tz-aware columns are
    converted to naive UTC). Extended for this subproject: Open/High/Low are
    REQUIRED non-null per row, because here they are prediction targets, not
    optional context.
    """
    frame = pd.read_csv(path)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing required column(s): {missing}")
    # utc=True: a real archive whose Date column carries an explicit UTC
    # offset per row can span a daylight-saving transition within the
    # SAME file (e.g. "-05:00" and "-04:00" rows) -- without utc=True,
    # pandas cannot build one homogeneous dtype from mixed offsets and
    # raises ValueError("Mixed timezones detected..."), which the caller
    # (features.build_panel()) catches as "unreadable" and drops the
    # WHOLE ticker, even though every row is perfectly valid (Codex
    # review: "Parse DST-varying timezone offsets in one UTC pass").
    # Naive (no-offset) Date columns are unaffected: utc=True localizes
    # them as UTC directly, and the tz-aware branch just below converts
    # right back to naive -- the same end result as before this change.
    parsed_date: Any = pd.to_datetime(frame["Date"], errors="coerce", utc=True)
    if getattr(getattr(parsed_date, "dtype", None), "tz", None) is not None:
        parsed_date = parsed_date.dt.tz_convert("UTC").dt.tz_localize(None)
    # Calendar-day precision, not a raw timestamp: every downstream consumer
    # (TickerSeries.dates/label_end_dates, TrainingPanel.calendar/date_index,
    # the harness's origin-date re-anchoring, Octopus's training-reference
    # resolution) treats a row's Date as ONE TRADING DAY and matches it via
    # exact equality against other day-precision values -- a valid CSV whose
    # Date column happens to carry a real time-of-day component silently
    # broke one such comparison at a time, across several separate review
    # rounds each catching a different call site (Codex review: "Match
    # forecast origins/queued reveals/training references by calendar
    # day"). Normalizing once, here -- the only place every row enters the
    # system -- closes the whole bug class instead of teaching each
    # comparison site to tolerate a raw timestamp. No downstream code reads
    # sub-day precision (nothing in this tree touches .dt.hour/.dt.time).
    # errors="coerce" does NOT catch every malformed date: dateutil happily
    # reads a truncated "04-27" (year missing -- real corruption present in
    # the archive, e.g. CBU.csv's first data row) as 0001-04-27, which is a
    # perfectly valid Timestamp and survives the dropna below. It is NOT
    # representable in datetime64[ns] (range 1677-09-21 .. 2262-04-11), and
    # features.build_panel()'s `to_numpy(dtype="datetime64[ns]")` SEGFAULTS
    # on the out-of-range cast under pandas 3.0.5 / numpy 2.5.1 rather than
    # raising -- taking down a multi-hour full-archive run with no traceback
    # and no indication of which ticker was at fault. Forced to NaT here, at
    # the one place every row enters the system, so the existing dropna
    # treats it like any other unusable row: logged as a dropped row, never
    # a blocker (docs/wondrous_octopus.md's "missing/corrupt tickers are
    # never blockers" contract, which a segfault plainly violated).
    normalized = parsed_date.dt.normalize()
    out_of_ns_range = (normalized < _NS_EPOCH_MIN) | (normalized > _NS_EPOCH_MAX)
    frame["Date"] = normalized.mask(out_of_ns_range)
    for column in _NUMERIC_RAW_COLUMNS:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not pd.api.types.is_datetime64_any_dtype(frame["Date"]):
        raise ValueError(f"{path}: Date column did not parse to datetimes")
    frame = frame.dropna(subset=["Date", "Open", "High", "Low", "Close"])
    frame = frame.sort_values("Date", kind="stable")
    frame = frame.drop_duplicates(subset="Date", keep="last")
    return frame.reset_index(drop=True)


def adjusted_ohlc(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Scale the whole bar into split/dividend-adjusted space.

    The per-row ratio ``Adj Close / Close`` (validated row-wise the same way
    ml-forecaster/features.py:_preferred_price_series validates its inputs:
    present, finite, positive) multiplies Open/High/Low/Close, keeping every
    bar internally consistent while removing split/dividend discontinuities.
    The applied ratio is kept in an ``adj_ratio`` column so a forecast made
    in adjusted space can be converted back to raw next-day dollars -- using
    day-t's ratio, which is wrong only across an overnight split (rare,
    documented in the README, auditable because the ratio is ledgered).

    Rows whose ADJUSTED bar is geometrically impossible (High materially
    below max(Open, Close), or Low materially above min(Open, Close) --
    beyond cent-rounding tolerance), or whose Adj Close is unusable (missing,
    non-finite, or non-positive -- a ratio 1.0 fallback there would silently
    switch just that row onto the raw price basis while its neighbors stay
    adjusted, an artificial jump across any split/dividend in the ticker's
    history), are dropped and counted, mirroring
    scripts/generate_buy_candidates.py's internally-inconsistent-bar
    rejection: a bar that cannot have happened must not train or score the
    model. Returns ``(adjusted_frame, dropped_row_count)``.
    """
    frame = frame.copy()
    ratio = pd.Series(1.0, index=frame.index)
    usable_ratio = pd.Series(True, index=frame.index)
    if "Adj Close" in frame.columns:
        candidate = frame["Adj Close"] / frame["Close"]
        usable_ratio = (
            frame["Adj Close"].notna()
            & np.isfinite(frame["Adj Close"])
            & (frame["Adj Close"] > 0)
            & (frame["Close"] > 0)
        )
        # The 1.0 fallback below keeps the arithmetic below finite so an
        # unusable row can still be DROPPED cleanly (folded into `keep`),
        # rather than silently switching that one row onto the raw price
        # basis while its neighbors stay adjusted -- across a 4:1 split
        # that reads as an artificial ~4x jump instead of a clean gap
        # (Codex review: "Drop invalid adjusted-close rows instead of
        # switching bases").
        ratio = candidate.where(usable_ratio, 1.0)
    for column in ("Open", "High", "Low", "Close"):
        frame[column] = frame[column] * ratio
    frame["adj_ratio"] = ratio
    body_high = np.maximum(frame["Open"], frame["Close"])
    body_low = np.minimum(frame["Open"], frame["Close"])
    consistent = (
        (frame["High"] >= body_high * (1.0 - _BAR_CONSISTENCY_RTOL))
        & (frame["Low"] <= body_low * (1.0 + _BAR_CONSISTENCY_RTOL))
        # The two tolerance checks above are independent and can both pass
        # a crossed bar (High slightly below Low, each within tolerance of
        # its own body bound) -- explicitly reject High < Low too, or a
        # negative intraday range and contradictory high/low targets slip
        # through this loader's corruption-rejection contract (Codex
        # review: "Reject bars whose high is below their low").
        & (frame["High"] >= frame["Low"])
    )
    # Positive prices are a precondition for the log-return space everything
    # downstream lives in; a zero/negative price row is as impossible as a
    # broken bar and gets the same treatment.
    positive = (frame["Open"] > 0) & (frame["High"] > 0) & (frame["Low"] > 0) & (frame["Close"] > 0)
    # A row of +inf OHLC values passes both the geometry and positivity
    # checks above (inf >= inf, inf > 0) and would otherwise poison every
    # shared market feature computed from it -- one corrupt CSV row must not
    # suppress predictions market-wide (Codex review: "Drop non-finite OHLC
    # rows before building market features").
    finite = (
        np.isfinite(frame["Open"])
        & np.isfinite(frame["High"])
        & np.isfinite(frame["Low"])
        & np.isfinite(frame["Close"])
    )
    keep = consistent & positive & finite & usable_ratio
    dropped = int((~keep).sum())
    if dropped:
        frame = frame.loc[keep].reset_index(drop=True)
    return frame, dropped


def truncate_as_of(frame: pd.DataFrame, as_of: pd.Timestamp | None) -> pd.DataFrame:
    """Drop rows dated on or after ``as_of``'s own calendar day.

    The calendar-day-EXCLUSIVE rule from ml-forecaster/features.py's
    _iter_ticker_frames: a replay asking for state "as of" some instant must
    never see that day's own (possibly partial) bar, only fully closed prior
    days. ``as_of=None`` keeps everything.
    """
    if as_of is None or pd.isna(as_of):
        # NaT guard: `Date < NaT` is False for EVERY row, so an unguarded
        # NaT would silently truncate the whole archive to nothing -- the
        # exact inverse of what a null cutoff means (Antigravity review:
        # "pd.NaT truncation risk"). NaT = no cutoff, same as None.
        return frame
    day_start = pd.Timestamp(as_of)
    if day_start.tzinfo is not None:
        # Archive dates are tz-naive UTC (load_history_csv normalizes them);
        # a tz-aware cutoff like "...T18:00:00Z" would raise on comparison
        # instead of truncating (Codex review: "Normalize timezone-aware
        # as-of cutoffs"). Convert, don't strip: the calendar day must be
        # the UTC one the archive speaks.
        day_start = day_start.tz_convert("UTC").tz_localize(None)
    day_start = day_start.normalize()
    return cast(pd.DataFrame, frame.loc[frame["Date"] < day_start].reset_index(drop=True))


@dataclass
class TickerSeries:
    """One ticker's fully prepared arrays, aligned row-for-row.

    ``features``/``targets`` may hold NaN inside each row's warm-up window
    (trailing statistics not yet defined) and on the final row (no next row
    to target); consumers filter on finiteness. ``label_end_dates[i]`` is
    the NEXT row's date -- the day row ``i``'s target becomes knowable --
    which is exactly the quantity the harness's trainability guard compares
    against the simulation cursor (the horizon-1 specialization of
    ml-forecaster/evaluate.py's label_end_date < test_start rule). NaT on
    the final row.
    """

    ticker: str
    ticker_id: int
    dates: np.ndarray  # datetime64[ns], sorted ascending, unique
    features: np.ndarray  # (n, F) float32
    day_of_week: np.ndarray  # (n,) int8, Monday=0 -- embedded/encoded per model
    #: (n,) int8 style-cluster label, 0 = not assigned yet (the correlation
    #: window has not filled). Kept OUT of ``features`` for the same reason
    #: day_of_week is: it is a categorical, and a float column where 3 sits
    #: between 2 and 4 would invent an ordering the labels do not have. Both
    #: models consume it as a category -- embedded by the octopus, appended
    #: as a column the GBM can split on -- so it travels with every request
    #: window rather than living only on the panel.
    cluster_id: np.ndarray
    targets: np.ndarray  # (n, 4) float32 -- next-row log-returns vs this row's close
    label_end_dates: np.ndarray  # (n,) datetime64[ns], NaT on the last row
    ref_close: np.ndarray  # (n,) float64, ADJUSTED close (log-return <-> price pivot)
    adj_ratio: np.ndarray  # (n,) float64, Adj Close / Close actually applied

    def __len__(self) -> int:
        return len(self.dates)


@dataclass
class TrainingPanel:
    """The assembled in-memory dataset handle every model and the harness share.

    ``tickers`` is the frozen vocabulary in sorted order; ``ticker_id`` is
    ``tickers.index(t) + 1`` because id 0 is reserved for "unknown ticker"
    (the octopus model's graceful-degradation embedding, mirroring
    ml-forecaster's all-zero one-hot for unseen tickers). ``date_index``
    maps each calendar date to the ``(ticker, row_index)`` pairs that traded
    that day -- the harness's per-step worklist. ``skipped`` records every
    ticker the build could NOT use and why; skipping is deliberately
    non-fatal (missing stocks are never blockers), but a build in which
    EVERYTHING was skipped raises instead of returning an empty panel.
    """

    series: dict[str, TickerSeries]
    tickers: list[str]
    calendar: np.ndarray  # datetime64[ns], sorted union of all series' dates
    date_index: dict[pd.Timestamp, list[tuple[str, int]]]
    feature_names: tuple[str, ...]
    normalization: dict[str, list[float]]  # {"mean": [...], "std": [...]} over the fit window
    min_history: int
    #: Per-calendar-day market summary (median return, fraction down >=2%,
    #: return dispersion IQR) aligned to ``calendar`` -- the octopus model's
    #: market-auxiliary-head target looks up day t+1's row here.
    market_summary: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.float32))
    calendar_pos: dict[pd.Timestamp, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    dropped_bar_rows: dict[str, int] = field(default_factory=dict)

    def ticker_id(self, ticker: str) -> int:
        """Vocabulary id for ``ticker``; 0 (unknown) when outside the vocabulary."""
        series = self.series.get(ticker)
        return series.ticker_id if series is not None else 0

    def eligible_on(self, date: pd.Timestamp) -> list[tuple[str, int]]:
        """(ticker, row_index) pairs predictable on ``date``.

        Eligible = the ticker traded that day AND has at least
        ``min_history`` prior rows (the warm-up rule that makes a
        just-listed ticker a non-blocker rather than an error: it simply
        waits here until it qualifies).
        """
        return [(ticker, idx) for ticker, idx in self.date_index.get(date, []) if idx >= self.min_history]
