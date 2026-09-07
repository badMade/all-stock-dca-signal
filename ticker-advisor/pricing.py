"""Price-history fetch for candidate tickers, preferring the repo's local archive.

Reuses the same archive location `scripts/generate_per_ticker_sizing.py` reads
from (`dca_ticker_paths.configured_ticker_history_dir()`, overridable via
`DCA_TICKER_HISTORY_DIR`) so a candidate that happens to already have local
history doesn't trigger an avoidable network call. Falls back to a live
`yfinance` download for anything not already archived -- most candidates,
since the archive only covers the existing active universe.
"""

from __future__ import annotations

import re
import sys
import threading
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_URL_PATTERN = re.compile(r"https?://\S+")
_yfinance_download_lock = threading.Lock()


def _now_utc_naive() -> pd.Timestamp:
    """The current instant, UTC, with tzinfo stripped for tz-naive comparisons.

    A bare `pd.Timestamp.now()` uses the HOST's local system time -- on a
    runner/container/dev machine whose configured timezone sits behind
    UTC, an archive row genuinely dated "today" (UTC, how this archive's
    own downloader stamps it) could compare as later than a local "now"
    still on "yesterday", wrongly flagging a legitimate row as
    future-dated (or a legitimately fresh archive as artificially stale)
    purely as a function of the host's timezone configuration rather than
    anything about the data itself (Antigravity review on PR #403:
    "System Timezone Edge Case in Archive Staleness Check"). Anchoring to
    UTC removes that host-environment dependency.
    """
    return pd.Timestamp.now(timezone.utc).tz_localize(None)


def _resolve_now(now: pd.Timestamp | None) -> pd.Timestamp:
    """Resolve an optional caller-supplied instant to a tz-naive UTC pd.Timestamp.

    Every future-dated-row/current-date-row/staleness check in this module
    used to call _now_utc_naive() independently, at whatever moment that
    particular check happened to run -- on a run spanning UTC midnight
    (plausible with a cold multi-GB FinBERT download or a large candidate
    batch, since the benchmark is fetched before candidates), the
    finalized-session cutoff could shift mid-run: a benchmark fetched just
    before midnight drops "today" as one date, while a candidate fetched
    just after midnight retains that same date as historical, and
    candidates on opposite sides of midnight can even use different
    cutoffs from each other. Health, backtest, and benchmark-alignment
    results then cover inconsistent sessions even though run_metadata.
    generated_at records one single run instant (Antigravity review on PR
    #403: "Anchor price cutoffs to the run timestamp"). Passing the SAME
    `now` through every call in one fetch_price_history() invocation (and
    the same one across every candidate + the benchmark, from
    advise_tickers.py's single captured run timestamp) fixes the cutoff
    for the whole run. ``None`` (the default for any direct/test caller
    that doesn't care) falls back to the real current instant.
    """
    if now is None:
        return _now_utc_naive()
    resolved = pd.Timestamp(now)
    if resolved.tzinfo is None:
        return resolved
    # tz_localize(None) on an aware Timestamp discards the offset and keeps
    # the LOCAL wall-clock value rather than converting the instant to UTC
    # -- e.g. 2026-08-10T00:30+14:00 (actually 2026-08-09T10:30 UTC) would
    # resolve here to August 10, silently shifting every future/current-
    # session filter and staleness check in this run by up to a day
    # depending on the caller's offset (Codex review on PR #403: "Convert
    # aware run cutoffs to UTC before stripping timezone"). tz_convert()
    # first moves the same instant onto the UTC timeline; only THEN is the
    # (now-UTC) tzinfo dropped, matching this function's own "tz-naive UTC"
    # contract.
    return resolved.tz_convert(timezone.utc).tz_localize(None)


def _sanitize_fetch_error(exc: Exception) -> str:
    """Redact URLs from a live-download exception before it's persisted to disk.

    A yfinance failure through an authenticated proxy, or one whose message
    otherwise embeds a signed/request URL, would otherwise carry that URL
    into ``PriceHistoryUnavailable`` or the stale-archive fallback reason --
    both of which score_candidate() persists into scores.json/scores.md/the
    proposed diff. Mirrors signals.py's ``_sanitize_fetch_error`` (Codex
    review on PR #403: "Sanitize live price-download failures before
    persistence").
    """
    return _URL_PATTERN.sub("<redacted-url>", str(exc))

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- index 0 is already this module's own directory
# (ticker-advisor/), and putting REPO_ROOT ahead of it would let a
# same-named top-level module there shadow this subproject's own
# submodules (Antigravity review on PR #403: "sys.path Precedences in
# advise_tickers.py", same risk here).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402

REQUIRED_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Adj Close", "Volume")
_NUMERIC_COLUMNS = tuple(column for column in REQUIRED_COLUMNS if column != "Date")

# Above this many days between an archived series' last row and "today", the
# archive is treated as stale (Codex review on PR #403: "Refuse stale
# archives instead of ranking old data").
_STALE_ARCHIVE_WARNING_DAYS = 10

# Below this fraction of the requested lookback window, the archive is
# treated as incomplete even if its last row is recent -- e.g. a newly
# initialized or truncated CSV with only a few weeks of history when 3 years
# were requested. Without this check, health/backtest metrics would compute
# on too few observations even though yfinance could supply the full
# multi-year window (Codex review on PR #403: "Refresh archives that lack
# the requested history").
_MIN_ARCHIVE_COVERAGE_FRACTION = 0.5

# Trading days per year, used only to translate a requested lookback window
# into an expected row count -- a sparse/gapped archive can have a first and
# last row far enough apart to clear the calendar-span check above while
# still holding only a handful of actual observations (e.g. two rows 200
# days apart), which the span check alone can't catch (Codex review on PR
# #403: "Count observations when judging archive completeness").
_TRADING_DAYS_PER_YEAR = 252


def _normalize_session_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Truncate a DatetimeIndex to trading-date granularity (midnight, no time-of-day).

    An archived row's Date can retain a nonzero intraday time component --
    e.g. a market-close timestamp -- and is deliberately NOT rejected for
    that alone (see the future-date filter's own comment on why a same-day
    row with a nonzero time is still valid). A live yfinance frame is
    typically already midnight-indexed. Two frames representing the SAME
    trading sessions but with different time-of-day components compare as
    entirely different index labels, so advise_tickers.py's benchmark-
    calendar reindex (`frame.index.union(expected_sessions)`) would insert a
    second, null row for each such day instead of recognizing it as the
    session already present -- corrupting gap detection and beta/alpha
    alignment between a candidate and its benchmark (Codex review on PR
    #403: "Normalize daily timestamps before calendar reindexing"). Applied
    once here, at every point this module hands a frame back to a caller,
    so every consumer's calendar is already date-only and compares cleanly.

    The archive path's own duplicate-Date removal (_load_from_archive's
    `indexed.index.duplicated(keep="last")`) runs on the EXACT, still-
    intraday-timestamped labels, before this normalization -- two rows for
    the same trading date at, say, midnight and 16:00 are distinct labels
    at that point and survive it untouched. Truncating both onto the same
    date label here, without deduplicating again afterward, would silently
    reintroduce that duplicate-label row _load_from_archive() already
    guards against: _price_returns()/the vectorbt backtest count an extra
    daily observation, and benchmark alignment's inner join produces a
    many-to-many match that duplicates returns (Codex review on PR #403:
    "Deduplicate sessions after normalizing timestamps"). keep="last"
    matches every other dedup in this module: a later-written row wins.
    """
    frame = frame.copy()
    frame.index = frame.index.normalize()
    return frame[~frame.index.duplicated(keep="last")]

# In both cases, a live yfinance fetch is preferred over ranking on
# stale/incomplete data -- EVERY health/backtest metric, not just beta/alpha,
# gets computed from whatever window this function returns. The archive is
# still used as a last-resort fallback if the live fetch itself fails, with
# a clear warning either way.


class PriceHistoryUnavailable(Exception):
    """Raised when neither the local archive nor yfinance can produce history."""


def _load_from_archive(ticker: str, history_dir: Path, *, now: pd.Timestamp | None = None) -> pd.DataFrame | None:
    path = history_dir / f"{ticker}.csv"
    if not path.is_file():
        return None
    resolved_now = _resolve_now(now)
    try:
        frame = pd.read_csv(path, parse_dates=["Date"])
        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            return None
        # read_csv() does not raise on a non-numeric OHLCV cell (e.g. a
        # stray "oops") -- it silently accepts an object-dtype column, and
        # that frame would otherwise be returned as usable. _price_returns()
        # then calls pct_change() on it BEFORE score_candidate()'s per-signal
        # exception guards, so a TypeError there aborts the whole candidate
        # batch instead of degrading just this one archive to "unavailable"
        # and falling through to yfinance. errors="raise" here reuses the
        # except block below for exactly that fallback (Codex review on PR
        # #403: "Validate numeric archive columns before accepting them").
        for column in _NUMERIC_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        # errors="raise" above only rejects a column that's ENTIRELY
        # unparseable -- a column that's blank/NaN for most or all rows
        # while still holding a couple of valid numbers sails through
        # unchanged. An archive whose "Adj Close" is unusable this way
        # while "Close" stays valid clears every completeness check below
        # (they count rows/span, not per-column NaN-ness), so it's returned
        # as usable, and score_candidate() then selects the near-empty
        # "Adj Close" series -- preferred whenever the column exists --
        # silently losing health/backtest data instead of falling back to
        # "Close" (Codex review on PR #403: "Reject archives with unusable
        # adjusted prices").
        # Price-column usability (Close/Adj Close validity) is deliberately
        # NOT checked here against the whole raw archive -- fetch_price_
        # history() applies _ensure_usable_price_columns() to the WINDOWED
        # frame instead, right before it's actually returned. An archive
        # can have valid prices in old history but be mostly blank in the
        # specific recent lookback window that gets scored (or vice
        # versa), and evaluating coverage over the unwindowed frame here
        # would both reject archives that are fine within the requested
        # window and accept ones that aren't (Codex review on PR #403:
        # "Measure adjusted-price coverage in the requested window").
        # sort_index()'s default quicksort is not stable -- for equal-Date
        # rows (the exact duplicate-Date case handled below) it can reorder
        # them relative to their original CSV order, so `duplicated(keep=
        # "last")` further down is not reliably keeping the file's LATER
        # row as the comment there claims; it can retain an older/corrupt
        # price instead of the later correction (Codex review on PR #403:
        # "Preserve file order when resolving duplicate dates"). A stable
        # sort keeps same-Date rows in their original relative order so
        # keep="last" means what it says.
        indexed = frame.set_index("Date").sort_index(kind="stable")
        if not isinstance(indexed.index, pd.DatetimeIndex):
            # An unparseable Date value doesn't always make read_csv's
            # parse_dates raise -- pandas can silently leave the column as a
            # plain string/object Index instead. That index has no `.tz`
            # attribute, so _tz_naive() would raise AttributeError on it
            # downstream, escaping both the yfinance fallback and
            # PriceHistoryUnavailable and aborting the whole advisory batch
            # (Codex review on PR #403: "Reject invalid archive dates before
            # accepting the frame").
            return None
        # Normalize to tz-naive HERE, before the future-date comparison
        # below -- a CSV whose Date column happens to include a UTC/tz
        # offset (e.g. "2024-01-01T00:00:00+00:00") parses to a tz-AWARE
        # DatetimeIndex, and comparing that against the tz-naive
        # `pd.Timestamp.now().normalize()` raises TypeError. That's caught
        # by this function's own broad except below, so it degrades to
        # "archive unavailable" rather than crashing, but unnecessarily
        # forces a live yfinance fetch for an otherwise perfectly usable
        # archive (Antigravity review on PR #403: "Potential TypeError on
        # Tz-Aware Timestamps in CSV Archive Loading"). fetch_price_
        # history() already re-normalizes with _tz_naive() on its own
        # archived-and-accepted path, so doing it here too is a no-op for
        # the already-tz-naive common case.
        indexed = _tz_naive(indexed)
        # A partially-empty Date column still parses to a DatetimeIndex
        # overall (the type check above passes), but can hold individual
        # NaT entries -- _archive_deficiency()'s `last_row.normalize()`
        # raises AttributeError on a NaT last_row, and score_candidate()
        # doesn't catch that (only PriceHistoryUnavailable), aborting the
        # whole advisory batch instead of falling through to yfinance
        # (Codex review on PR #403: "Reject archives whose date index
        # contains NaT"). If every row was NaT, the resulting empty frame
        # is treated as "no archive" by fetch_price_history's `not
        # archived.empty` check further down, same as a missing file.
        indexed = indexed[indexed.index.notna()]
        if indexed.empty:
            return None
        # A mistyped future date (or a bad system-clock write) would
        # otherwise become `last_row` in _archive_deficiency(), making
        # `staleness` NEGATIVE and so falsely "fresh" -- its large span and
        # normal row count also clear the other completeness checks.
        # _windowed() then anchors the lookback to that bogus future
        # timestamp and typically returns just the one bad row, silently
        # eliminating health/backtest signals even though a live refresh
        # was available (Codex review on PR #403: "Reject future-dated
        # archive rows"). Compared against the actual current moment, not
        # normalized to midnight -- normalizing the threshold would reject
        # a legitimately same-day row that happens to carry a nonzero
        # intraday time component (e.g. a market-close timestamp), treating
        # a valid observation as "future" just because it's later in the
        # day than midnight (Antigravity review on PR #403: "Potential
        # Edge Case in pricing.py Date Filtering"). UTC, not the host's
        # local system time -- see _now_utc_naive()'s own docstring.
        # resolved_now (a caller-supplied run instant, or _now_utc_naive()
        # if none was given -- see _resolve_now()'s own docstring), not a
        # fresh _now_utc_naive() call here, so this and every other date
        # check in one run share the same cutoff (Antigravity review on PR
        # #403: "Anchor price cutoffs to the run timestamp").
        indexed = indexed[indexed.index <= resolved_now]
        if indexed.empty:
            return None
        # Today's row, if present, is dropped unconditionally too -- an
        # archive refreshed before market close could have written an
        # evolving/non-final "today" bar the same way a live yfinance
        # download can (fetch_price_history()'s own live path already
        # drops it for that exact reason). Without this, a mixed archive/
        # live batch (or an archive vs. its own benchmark, fetched via
        # different paths) would be scored through DIFFERENT effective
        # closing dates -- the archive keeping a possibly-partial today's
        # bar while the live path always excludes it (Antigravity review
        # on PR #403: "Exclude today's bar from archived histories").
        indexed = indexed[indexed.index.normalize() < resolved_now.normalize()]
        if indexed.empty:
            return None
        # A duplicate Date row (a source anomaly, or a prior interrupted/
        # corrupted merge -- exactly the "self-healing" scenario the
        # incremental-update script this archive is populated by is meant
        # to fix on the NEXT run, not necessarily already fixed) makes
        # _beta_alpha's `pd.concat([..., ...], axis=1, join="inner")`
        # produce a many-to-many alignment on that duplicated label,
        # inflating the aligned series and distorting beta/alpha
        # (Antigravity review on PR #403: "Deduplicate Dates in Price
        # History to Prevent Cartesian Join Skew"). keep="last" matches
        # this repo's own merge convention: a later-written row wins.
        return indexed[~indexed.index.duplicated(keep="last")]
    except Exception:
        # A truncated file, malformed quoting, bad encoding, or an unusable
        # Date column must degrade to "treat the archive as unavailable and
        # fall through to the live yfinance path" -- not crash the whole
        # command (a malformed benchmark archive) or escape score_candidate's
        # best-effort contract (a malformed candidate archive) (Codex review
        # on PR #403: "Fall back when an archive CSV cannot be parsed").
        return None


def _load_from_yfinance(ticker: str, lookback_years: int) -> pd.DataFrame | None:
    import yfinance as yf

    # Yahoo Finance addresses share classes with a hyphen (BRK-B), not the
    # dot config/tickers.json's own schema requires (BRK.B,
    # ^[A-Z.]{1,6}$) -- the dotted form can't be supplied directly (the
    # schema filter that validates candidates before scoring rejects
    # hyphens), so passing it straight through to yfinance silently loses
    # health/backtest data for exactly the share-class tickers this
    # schema was designed to accept. Translation is scoped to this one
    # request; reports and proposed config edits keep using the original
    # dotted symbol throughout (Codex review on PR #403: "Translate
    # dotted share-class symbols for Yahoo").
    yahoo_ticker = ticker.replace(".", "-")
    with _yfinance_download_lock:
        try:
            frame = yf.download(
                yahoo_ticker,
                period=f"{lookback_years}y",
                auto_adjust=False,
                progress=False,
                multi_level_index=False,
            )
        except TypeError:
            # multi_level_index is a comparatively recent yf.download() kwarg;
            # an older pinned yfinance (this repo doesn't pin a version -- see
            # requirements.txt) raises TypeError on an unrecognized keyword
            # rather than ignoring it, which would make the whole fallback path
            # unusable. Retry without it and flatten the (ticker, field)
            # MultiIndex columns older/default yfinance returns ourselves
            # instead (Copilot review on PR #403).
            frame = yf.download(yahoo_ticker, period=f"{lookback_years}y", auto_adjust=False, progress=False)
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        # Previously only flattened in the TypeError-retry branch above --
        # some yfinance versions/conditions can still return MultiIndex
        # columns even when multi_level_index=False is accepted without
        # raising, so this must run unconditionally on whichever branch
        # produced the frame (Antigravity review on PR #403: "Always
        # Flatten MultiIndex Columns"). Which level holds the field name
        # ("Close", "Open", ...) vs. the ticker symbol has flipped across
        # yfinance versions/flags -- blindly taking level 0 could flatten
        # a (ticker, field)-ordered frame into duplicate ticker-symbol
        # column names, silently discarding "Close" and making
        # _validate_live_frame() reject perfectly good downloaded data
        # (Antigravity review on PR #403: "MultiIndex Level Resolution
        # Risk in _load_from_yfinance"). Level 0 stays the fallback when
        # neither level is recognizable, matching the previous
        # unconditional behavior.
        if "Close" in frame.columns.get_level_values(0):
            frame.columns = frame.columns.get_level_values(0)
        elif frame.columns.nlevels > 1 and "Close" in frame.columns.get_level_values(1):
            frame.columns = frame.columns.get_level_values(1)
        else:
            frame.columns = frame.columns.get_level_values(0)
    frame.index.name = "Date"
    return frame


def _tz_naive(frame: pd.DataFrame) -> pd.DataFrame:
    """Strip any tz-awareness from a DatetimeIndex.

    The local archive (parsed from plain CSV dates) is always tz-naive;
    yfinance's DatetimeIndex is tz-aware on some versions/data. Mixing the
    two -- e.g. a candidate freshly downloaded via yfinance against a
    benchmark loaded from the archive -- makes pandas raise
    "Cannot join tz-naive with tz-aware DatetimeIndex" out of _beta_alpha's
    inner join, which score_candidate() then swallows as a generic "health
    signal failed" for every online candidate (Antigravity review on PR
    #403: "Timezone Mismatch Between Archive and yfinance Data"). Normalizing
    both paths to tz-naive here, once, is simpler and more robust than
    guarding every join site downstream.
    """
    if frame.index.tz is not None:
        frame = frame.copy()
        frame.index = frame.index.tz_localize(None)
    return frame


def _validate_live_frame(frame: pd.DataFrame) -> pd.DataFrame | None:
    """Normalize a downloaded yfinance frame's SHAPE (not yet its price coverage/freshness).

    A nonempty frame from yfinance can still be unusable in ways the
    archive path already rejects/normalizes but this one didn't check: a
    non-DatetimeIndex, no "Close" column, a tz-aware index, a duplicate
    Date label, or nonnumeric price columns. Without this, score_candidate()
    can raise KeyError on a missing "Close" column BEFORE its own
    per-signal exception guards (aborting the whole advisory batch instead
    of degrading just this one candidate) (Codex review on PR #403:
    "Validate live price frames before returning them"). Returns None
    (triggering the archive fallback / PriceHistoryUnavailable, same as
    "no live data returned") when the frame can't be normalized into
    something structurally usable.

    Deliberately does NOT also check price coverage/freshness
    (_ensure_usable_price_columns()) -- the caller (fetch_price_history())
    runs that AFTER its own future-dated-row and current-date-row filters,
    so coverage/freshness reflects the frame that's ACTUALLY returned, not
    one that still includes a row about to be dropped.
    """
    if not isinstance(frame.index, pd.DatetimeIndex) or "Close" not in frame.columns:
        return None
    frame = frame.copy()
    # Normalized to tz-naive HERE, before anything below (including the
    # caller's own future/current-date filters and _ensure_usable_price_
    # columns()'s freshness check) ever compares this index against
    # _now_utc_naive() -- yfinance commonly returns a tz-AWARE index, and
    # comparing that against a tz-naive timestamp raises TypeError. That
    # used to happen safely inside fetch_price_history() itself (outside
    # any try/except, but AFTER this function had already returned), but
    # _ensure_usable_price_columns()'s later freshness check runs INSIDE
    # this function, before the caller gets a chance to normalize --
    # letting a tz-aware live/benchmark frame raise here, uncaught by
    # score_candidate()'s narrower `except PriceHistoryUnavailable`, and
    # abort the whole advisory batch (Antigravity review on PR #403:
    # "Normalize live indices before validating price freshness").
    frame = _tz_naive(frame)
    # Sorted and deduplicated the same way _load_from_archive() already
    # is (stable sort, keep="last" on a duplicate-Date row) -- a
    # duplicate yfinance timestamp left in place makes _price_returns()
    # treat the movement across two rows for the SAME session as a
    # separate daily return, the vectorbt backtest treats both rows as
    # distinct `freq="1D"` bars, and _beta_alpha's `pd.concat(...,
    # join="inner")` benchmark alignment can duplicate observations on
    # the shared label -- the archive path already guards all three;
    # nothing here previously did for the live-download path
    # (Antigravity review on PR #403: "Deduplicate live price dates
    # before scoring").
    frame = frame.sort_index(kind="stable")
    frame = frame[~frame.index.duplicated(keep="last")]
    try:
        # notna()-only validation still accepts a string/object-dtype
        # column ("Close" holding "N/A", say) -- a nonempty, correctly-
        # indexed frame with nonnumeric prices previously sailed through
        # this function, and _price_returns()'s pct_change() then raised
        # TypeError BEFORE score_candidate() enters either per-signal
        # exception guard, aborting the whole advisory batch instead of
        # falling back or degrading just this one candidate. Reuses the
        # same errors="raise" coercion _load_from_archive() already
        # applies to archived frames (Codex review on PR #403: "Reject
        # nonnumeric live price columns").
        for column in _NUMERIC_COLUMNS:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="raise")
    except (ValueError, TypeError):
        return None
    # Coverage/freshness validation (_ensure_usable_price_columns()) now
    # happens in fetch_price_history() itself, AFTER the future-dated-row
    # and current-date-row filters run -- checking coverage/freshness
    # here, before those filters, could pass based on a still-present
    # today's row that the caller is about to drop moments later, when
    # the frame's REAL final freshness (after that row is gone) might
    # not actually clear the bar (Antigravity review on PR #403:
    # "Normalize live indices before validating price freshness").
    return frame


def _ensure_usable_price_columns(
    frame: pd.DataFrame, *, require_fresh: bool = True, now: pd.Timestamp | None = None
) -> pd.DataFrame | None:
    """Ensure `frame` has a usable "Adj Close", falling back to "Close"; reject if neither is usable.

    Applied to the frame that will actually be returned/scored (a live
    download as-is, or an archive frame AFTER windowing) rather than an
    unwindowed archive's whole row count -- a frame can have valid prices
    in old history but be mostly blank in the specific lookback window
    that gets returned, and _archive_deficiency()'s row-count check alone
    doesn't catch a windowed-price problem (Codex review on PR #403:
    "Measure adjusted-price coverage in the requested window").

    ``require_fresh=False`` skips _column_is_fresh()'s staleness check --
    fetch_price_history()'s own last-resort fallback path deliberately
    tolerates a stale archive (that's the whole point of it being a
    fallback: better than nothing, with its own stale_archive_fallback_
    reason warning already attached elsewhere), so enforcing freshness
    there would make the SAME staleness this function is meant to guard
    against on the PRIMARY path instead defeat the fallback mechanism
    entirely, turning "use the stale archive as a last resort" into
    "reject it and raise PriceHistoryUnavailable."

    "Close" is only required to be usable when "Adj Close" itself needs
    the fallback -- both scoring paths prefer "Adj Close", so an otherwise
    scoreable frame with a sufficient Adj Close but sparse/blank Close
    must not be rejected just because Close alone wouldn't pass (Codex
    review on PR #403: "Accept valid adjusted prices without requiring
    Close"). When the fallback IS needed, Close's own validity still
    matters: previously it was copied in unchecked, letting a frame with
    no usable price series at all be returned as "usable" instead of
    falling back to the archive or raising PriceHistoryUnavailable (Codex
    review on PR #403: "Reject an unusable live Close fallback").
    """
    if frame.empty:
        return None
    if frame.columns.duplicated().any():
        # A duplicate-labeled column ("Adj Close" appearing twice, from an
        # unflattened MultiIndex or a malformed CSV) makes
        # frame["Adj Close"] return a DataFrame instead of a Series --
        # .notna().sum() then returns a per-column Series, and the boolean
        # check below raises "ValueError: The truth value of a Series is
        # ambiguous" instead of degrading this frame to unusable like any
        # other malformed input (Antigravity review on PR #403: "Duplicate
        # Column Handling in _ensure_usable_price_columns"). Keep the
        # FIRST occurrence, matching pandas' own de-duplication
        # convention.
        frame = frame.loc[:, ~frame.columns.duplicated()]
    # +/-inf in a price column (a corrupt source row, or a division-by-
    # zero artifact upstream) isn't a valid price -- notna() alone treats
    # it as "present," so a frame with a stray inf could pass the coverage
    # checks below and then feed _price_returns()'s pct_change() a
    # transition FROM inf that computes to a FINITE -1.0 (not inf), a
    # fabricated "100% loss" that survives that function's own
    # np.isfinite() filter untouched since -1.0 IS finite (Antigravity
    # review on PR #403: "Reject non-finite price observations before
    # scoring"). Replacing inf with NaN here treats it as a missing
    # observation instead, consistent with how every other gap in this
    # data is already handled downstream.
    frame = frame.copy()
    for column in ("Adj Close", "Close"):
        if column in frame.columns:
            frame[column] = frame[column].replace([np.inf, -np.inf], np.nan)
            # A zero or negative price is equally not a valid observation
            # (a stock/ETF doesn't legitimately trade at $0 or below; a 0
            # is far more likely a corrupt/missing-data sentinel) -- left
            # in place, notna() alone still counts it as "present" toward
            # coverage, and the FINITE transition INTO it
            # (_price_returns()'s pct_change() computing exactly -1.0)
            # fabricates a "100% loss" that survives that function's own
            # np.isfinite() filter untouched, while a zero price also
            # reaches vectorbt's backtest unmasked (Antigravity review on
            # PR #403: "Reject zero and negative prices before scoring").
            # NaN already fails `<= 0` (comparisons with NaN are always
            # False), so this only masks genuine non-positive values.
            frame[column] = frame[column].mask(frame[column] <= 0)
    adj_close_usable = (
        "Adj Close" in frame.columns
        and frame["Adj Close"].notna().sum() >= len(frame) * _MIN_ARCHIVE_COVERAGE_FRACTION
        and (not require_fresh or _column_is_fresh(frame["Adj Close"], now=now))
    )
    # Both current callers (_validate_live_frame, and _load_from_archive's
    # REQUIRED_COLUMNS check) already guarantee "Close" is present before
    # reaching here, but this function is a shared, independently-callable
    # helper -- indexing frame["Close"] without checking first would raise
    # an unhandled KeyError for any other/future caller that doesn't carry
    # that guarantee, instead of degrading like every other malformed-
    # input case here (Antigravity review on PR #403: "Potential KeyError
    # Guard in _ensure_usable_price_columns").
    close_usable = (
        "Close" in frame.columns
        and frame["Close"].notna().sum() >= len(frame) * _MIN_ARCHIVE_COVERAGE_FRACTION
        and (not require_fresh or _column_is_fresh(frame["Close"], now=now))
    )
    if not adj_close_usable and not close_usable:
        return None
    # A column that's usable overall (coverage/freshness) can still have
    # an INTERNAL gap (a hole between its first and last valid
    # observation) -- score_candidate() skips BOTH health and backtest
    # for any candidate whose SELECTED column has one. Previously Adj
    # Close was always preferred once merely "usable," even when it had
    # an internal gap and Close (fully contiguous) was sitting right
    # there as a gap-free alternative -- unnecessarily discarding two
    # weighted signals despite having a usable substitute (Antigravity
    # review on PR #403: "Fall back when adjusted prices contain internal
    # gaps"). Prefer whichever usable column is gap-free; only fall back
    # to a gappy one when neither usable column is contiguous.
    adj_close_gap_free = adj_close_usable and not _has_internal_gap(frame["Adj Close"])
    close_gap_free = close_usable and not _has_internal_gap(frame["Close"])
    if adj_close_gap_free and close_gap_free:
        # Both are safe to use as-is (neither would bridge a gap into a
        # fabricated multi-session bar) -- but "gap-free" only means no
        # hole BETWEEN the first and last valid observation. A column
        # whose valid values are clustered at one EDGE of the window
        # (e.g. Adj Close populated only for the most recent half, with
        # the older half entirely null) still clears coverage/freshness
        # and _has_internal_gap alike, yet represents materially LESS
        # usable history than a fully-populated Close sitting right there
        # -- score_candidate() then computes health/backtest over a
        # shorter effective lookback than peers scored on their full
        # window, skewing cross-candidate z-score normalization (Codex
        # review on PR #403: "Prefer complete Close history over
        # truncated adjusted prices"). Prefer whichever gap-free column
        # actually covers more of the window; an exact tie keeps the
        # existing Adj Close preference (dividend-adjusted is financially
        # more correct when coverage is equal).
        if frame["Close"].notna().sum() > frame["Adj Close"].notna().sum():
            frame["Adj Close"] = frame["Close"]
        return frame
    if adj_close_gap_free:
        return frame
    if close_gap_free:
        frame["Adj Close"] = frame["Close"]
        return frame
    if adj_close_usable:
        return frame
    frame["Adj Close"] = frame["Close"]
    return frame


def _has_internal_gap(series: pd.Series) -> bool:
    """Whether `series` has a null observation BETWEEN its first and last valid one.

    A leading or trailing gap doesn't compact anything once dropna() runs
    -- only an INTERNAL gap silently bridges two sessions into one
    fabricated daily bar downstream (score_candidate()'s own has_internal_gap
    computes the identical thing for the frame it ultimately selects; this
    mirrors it here so _ensure_usable_price_columns() can choose the
    gap-free column BEFORE that choice is made, not after).
    """
    first_valid = series.first_valid_index()
    last_valid = series.last_valid_index()
    if first_valid is None or last_valid is None:
        return False
    return bool(series.loc[first_valid:last_valid].isna().any())


def _column_is_fresh(series: pd.Series, *, now: pd.Timestamp | None = None) -> bool:
    """Whether `series`'s LAST VALID (non-null) observation is recent enough to trust.

    The coverage checks above only count non-null values -- a column that's
    >= 50% populated overall but has every valid value clustered at the
    START of the window (the recent portion entirely blank) passes coverage
    even though its effective last price could be months or years old. The
    frame-level staleness checks (_archive_deficiency's index.max(),
    fetch_price_history()'s live-frame staleness check) only look at the
    INDEX's last row, not the last VALID PRICE -- a frame can be
    "index-fresh" (recent row labels) while being "price-stale" (those
    recent rows are blank), sailing through both undetected.
    score_candidate() deliberately ignores TRAILING gaps (only an internal
    gap disqualifies backtest/health), so this staleness is otherwise never
    caught anywhere else in the pipeline (Antigravity review on PR #403:
    "Check freshness of the selected price series").
    """
    if not isinstance(series.index, pd.DatetimeIndex):
        # Both current callers (_load_from_archive, _validate_live_frame)
        # already guarantee a DatetimeIndex before reaching here, but this
        # function is a shared, independently-callable helper -- calling
        # .normalize() on a non-Timestamp index label would otherwise
        # raise AttributeError for any other/future caller (including
        # tests exercising the coverage/inf/duplicate-column checks in
        # isolation) that doesn't carry that guarantee.
        return True
    last_valid = series.last_valid_index()
    if last_valid is None:
        return False
    # _resolve_now(), not a fresh _now_utc_naive() call, so this shares
    # the caller's single run-level cutoff (Antigravity review on PR
    # #403: "Anchor price cutoffs to the run timestamp").
    return _resolve_now(now).normalize() - last_valid.normalize() <= timedelta(days=_STALE_ARCHIVE_WARNING_DAYS)


def _windowed(frame: pd.DataFrame, last_row: pd.Timestamp, lookback_years: int) -> pd.DataFrame:
    cutoff = last_row - pd.DateOffset(years=lookback_years)
    return frame[frame.index >= cutoff]


def _archive_deficiency(
    archived: pd.DataFrame, lookback_years: int, *, now: pd.Timestamp | None = None
) -> tuple[pd.Timestamp, str] | None:
    """Return (last_row, reason) if the archive needs a live refresh, else None."""
    last_row = archived.index.max()
    first_row = archived.index.min()
    # UTC, not the host's local system time -- see _now_utc_naive()'s own
    # docstring (Antigravity review on PR #403: "System Timezone Edge Case
    # in Archive Staleness Check"). resolved via _resolve_now(), not a
    # fresh _now_utc_naive() call, so this shares fetch_price_history()'s
    # single run-level cutoff (Antigravity review on PR #403: "Anchor
    # price cutoffs to the run timestamp").
    staleness = _resolve_now(now).normalize() - last_row.normalize()
    span_days = (last_row - first_row).days
    requested_days = lookback_years * 365
    reasons = []
    if staleness > timedelta(days=_STALE_ARCHIVE_WARNING_DAYS):
        reasons.append(f"{staleness.days} days stale (last row {last_row.date()})")
    if span_days < requested_days * _MIN_ARCHIVE_COVERAGE_FRACTION:
        reasons.append(f"only {span_days}d of history for a {requested_days}d lookback request")
    requested_trading_days = lookback_years * _TRADING_DAYS_PER_YEAR
    # Count rows actually inside the window fetch_price_history() will
    # return (_windowed() anchored at `last_row`), not the archive's total
    # row count -- hundreds of rows older than the requested lookback can
    # clear a whole-archive count while the windowed frame that's actually
    # scored holds almost nothing, letting a near-empty recent window pass
    # as "complete" (Codex review on PR #403: "Count rows inside the
    # requested archive window").
    windowed_row_count = int((archived.index >= (last_row - pd.DateOffset(years=lookback_years))).sum())
    if windowed_row_count < requested_trading_days * _MIN_ARCHIVE_COVERAGE_FRACTION:
        reasons.append(
            f"only {windowed_row_count} rows within the {lookback_years}y lookback window "
            f"needing ~{requested_trading_days}"
        )
    return (last_row, "; ".join(reasons)) if reasons else None


def fetch_price_history(
    ticker: str,
    *,
    lookback_years: int = 3,
    history_dir: Path | None = None,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Return a Date-indexed, tz-naive OHLCV frame for `ticker`, archive-first.

    A stale OR incomplete archive (see _STALE_ARCHIVE_WARNING_DAYS /
    _MIN_ARCHIVE_COVERAGE_FRACTION) is not returned directly -- a live
    yfinance fetch is attempted first, and the archive is used only as a
    last resort if that fetch also fails, always with a clear warning
    either way.

    ``now`` anchors every future-dated-row/current-date-row/staleness
    check in this call to a single caller-supplied instant instead of
    each one independently calling _now_utc_naive() -- see
    _resolve_now()'s own docstring for why (Antigravity review on PR
    #403: "Anchor price cutoffs to the run timestamp"). Defaults to
    ``None`` (every check falls back to the real "now").

    Raises PriceHistoryUnavailable if neither a usable archive nor a live
    yfinance download can produce any rows -- callers must not silently score
    a candidate on an empty series.
    """
    resolved_now = _resolve_now(now)
    resolved_history_dir = history_dir if history_dir is not None else configured_ticker_history_dir()
    archived = _load_from_archive(ticker, resolved_history_dir, now=resolved_now)
    fallback: tuple[pd.DataFrame, pd.Timestamp, str] | None = None
    if archived is not None and not archived.empty:
        archived = _tz_naive(archived)
        deficiency = _archive_deficiency(archived, lookback_years, now=resolved_now)
        if deficiency is None:
            windowed = _windowed(archived, archived.index.max(), lookback_years)
            usable = _ensure_usable_price_columns(windowed, now=resolved_now)
            if usable is not None:
                return _normalize_session_index(usable)
            # The row-count/span/staleness checks in _archive_deficiency()
            # all passed, but the WINDOWED frame's prices are unusable
            # anyway (e.g. valid Close/Adj Close only outside the requested
            # lookback) -- treat this the same as any other deficiency so a
            # live refresh is still attempted (Codex review on PR #403:
            # "Measure adjusted-price coverage in the requested window").
            deficiency = (
                archived.index.max(),
                "unusable Close/Adj Close prices within the requested lookback window",
            )
        last_row, reason = deficiency
        fallback = (archived, last_row, reason)

    def _use_fallback_or_raise(live_failure_reason: str, exc: Exception | None = None) -> pd.DataFrame:
        if fallback is None:
            raise PriceHistoryUnavailable(
                f"no price history for {ticker!r} in {resolved_history_dir} or via yfinance ({live_failure_reason})"
            ) from exc
        archive, last_row, archive_reason = fallback
        warning = (
            f"{resolved_history_dir / f'{ticker}.csv'} is {archive_reason} and a live refresh failed "
            f"({live_failure_reason}) -- using it as a last resort; health/backtest metrics reflect this gap"
        )
        windowed = _windowed(archive, last_row, lookback_years)
        # require_fresh=False -- this IS the deliberate "use a stale
        # archive as a last resort" path; the staleness is already
        # disclosed via `warning`/stale_archive_fallback_reason below, not
        # something to additionally reject here (see _ensure_usable_price_
        # columns()'s own docstring on this parameter).
        usable = _ensure_usable_price_columns(windowed, require_fresh=False, now=resolved_now)
        if usable is None:
            raise PriceHistoryUnavailable(
                f"no price history for {ticker!r} in {resolved_history_dir} or via yfinance "
                f"({live_failure_reason}); the archive fallback's own prices are also unusable"
            ) from exc
        print(f"warning: {warning}", file=sys.stderr)
        # A stderr-only warning is invisible to anyone who only ever reads
        # scores.json/scores.md/the proposed diff -- an ADD recommendation
        # built on this stale/truncated fallback would look identical to
        # one built on current data. Attaching the reason via .attrs lets
        # score_candidate() (the only caller that persists results) carry
        # it into the candidate's own `errors` list, where it's visible
        # alongside the recommendation it affected (Codex review on PR
        # #403: "Persist stale-archive fallback warnings in result
        # artifacts").
        usable.attrs["stale_archive_fallback_reason"] = warning
        return _normalize_session_index(usable)

    try:
        downloaded = _load_from_yfinance(ticker, lookback_years)
    except ImportError as exc:
        return _use_fallback_or_raise(
            "yfinance isn't installed to fall back to (pip install -r ticker-advisor/requirements.txt)", exc
        )
    except Exception as exc:
        # yfinance can raise all sorts of things for a transient network
        # blip, a rate limit, or a provider-side error (timeouts, connection
        # errors, its own internal exceptions) -- none of those should abort
        # the whole multi-candidate advisory run, only this one candidate's
        # scoring (Codex review on PR #403: "Convert live price-download
        # failures into unavailable history").
        return _use_fallback_or_raise(f"yfinance download failed: {_sanitize_fetch_error(exc)}", exc)
    if downloaded is None or downloaded.empty:
        return _use_fallback_or_raise("no live data returned")
    downloaded = _validate_live_frame(downloaded)
    if downloaded is None:
        return _use_fallback_or_raise("live data was malformed (missing/invalid index or price columns)")
    # _validate_live_frame() already normalized this to tz-naive internally
    # (Antigravity review on PR #403: "Normalize live indices before
    # validating price freshness") -- no separate _tz_naive() call needed
    # here anymore.
    # A single corrupt future-dated row (a provider glitch or a bad
    # system-clock write upstream) would otherwise become `last_row`
    # below, making `staleness` NEGATIVE and so falsely "fresh" no matter
    # how stale the REST of the frame actually is -- the same failure
    # mode _load_from_archive()'s own future-date rejection already
    # guards against for the archive path (Antigravity review on PR
    # #403: "Reject future-dated live price rows"). Filtered here, AFTER
    # _tz_naive(), so the comparison is tz-naive on both sides -- yfinance
    # commonly returns a tz-AWARE index, and comparing that directly
    # against _now_utc_naive() would raise the same TypeError the archive
    # path's own tz-aware-date fix addressed. resolved_now, not a fresh
    # _now_utc_naive() call, so this shares the archive path's cutoff for
    # this same run (Antigravity review on PR #403: "Anchor price cutoffs
    # to the run timestamp").
    downloaded = downloaded[downloaded.index <= resolved_now]
    if downloaded.empty:
        return _use_fallback_or_raise("live data was entirely future-dated")
    # Today's row, if present, is dropped unconditionally -- run before
    # the candidate's regular session has closed, yfinance can include
    # the CURRENT date's still-evolving "1d" bar (it isn't in the future,
    # so the filter above doesn't catch it). This module has no market-
    # hours/holiday-calendar knowledge to tell a genuinely final bar
    # (the advisor happened to run after close) from a partial one, and
    # candidates are downloaded sequentially after the benchmark, so an
    # in-progress bar wouldn't even be a consistent snapshot across
    # candidates -- the conservative choice is to always treat "today"
    # as provisional and score from the last confirmed-final close
    # instead (Antigravity review on PR #403: "Exclude an unfinished
    # live daily bar").
    downloaded = downloaded[downloaded.index.normalize() < resolved_now.normalize()]
    if downloaded.empty:
        return _use_fallback_or_raise("live data had no confirmed-final session before today")
    # A delisted/inactive symbol can still return structurally valid,
    # well-covered data from yfinance -- its last row is just old (e.g.
    # ending years ago at delisting). _validate_live_frame() only checks
    # shape and coverage, never freshness, so that frame previously sailed
    # straight through and health/backtest scores were computed entirely
    # from obsolete data, even though an equally stale ARCHIVE would have
    # been rejected by _archive_deficiency()'s identical staleness check
    # and either refreshed or fallen back with a visible warning
    # (Antigravity review on PR #403: "Reject stale live histories before
    # scoring"). Routed through the same _use_fallback_or_raise() path a
    # failed live fetch already uses, so a still-usable (if also stale)
    # archive is preferred over silently trusting stale "live" data.
    last_row = downloaded.index.max()
    staleness = resolved_now.normalize() - last_row.normalize()
    if staleness > timedelta(days=_STALE_ARCHIVE_WARNING_DAYS):
        return _use_fallback_or_raise(f"live data is {staleness.days} days stale (last row {last_row.date()})")
    # A newly-listed ticker can return a short but otherwise well-formed,
    # fresh, fully-covered history from yfinance -- e.g. 40 sessions when
    # a 3y lookback was requested. That already clears
    # strategy_health_score()'s own minimum-observation floor, so the
    # candidate would get annualized health metrics and a partial
    # backtest computed from a tiny fraction of the requested period,
    # skewing cross-candidate z-score normalization against peers scored
    # on their full requested window -- the same completeness problem
    # _archive_deficiency()'s row-count check already guards against for
    # the archive path, nothing previously did for live data (Antigravity
    # review on PR #403: "Validate live history coverage before returning
    # it").
    requested_trading_days = lookback_years * _TRADING_DAYS_PER_YEAR
    if len(downloaded) < requested_trading_days * _MIN_ARCHIVE_COVERAGE_FRACTION:
        return _use_fallback_or_raise(
            f"live data has only {len(downloaded)} rows for a {lookback_years}y lookback "
            f"request (~{requested_trading_days} expected)"
        )
    # Price-column coverage/freshness, run HERE (after the future/current-
    # date filters above), not inside _validate_live_frame() -- checking
    # it earlier could pass based on a still-present today's row that was
    # about to be dropped moments later, when the frame's REAL final
    # freshness (after that row is gone) might not actually clear the bar
    # (Antigravity review on PR #403: "Normalize live indices before
    # validating price freshness").
    usable = _ensure_usable_price_columns(downloaded, now=resolved_now)
    if usable is None:
        return _use_fallback_or_raise("live data's Adj Close/Close prices are unusable or stale")
    return _normalize_session_index(usable)
