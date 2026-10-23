#!/usr/bin/env python3
"""Fetch the NYSE TICK breadth index's full available history from Yahoo Finance
and save it as one CSV in the shared local ticker-history archive, alongside
every per-ticker CSV `stock-history-downloader`/the daily ticker-update flow
already maintains there.

Data only. Nothing in this repo reads _breadth/TICK.csv: it is not wired into any
signal, sizing, or gate in `near_close_dca.py` or elsewhere. It exists purely
as an archived market-breadth series for later analysis, matching this
subproject's sibling `dca-sizing-signals/README.md`'s "does not write to live
DCA sizing" posture, just simpler -- there is no suggester here at all, only
a fetch-and-save script.

Yahoo Finance addresses the NYSE TICK index as "^TICK" (the "$TICK"/"C:TICK"
spellings some other vendors use are not valid yfinance symbols). `period="max"`
pulls Yahoo's entire available history for the symbol -- the same "all
available data" range the per-ticker archive downloader requests for equities
-- rather than a fixed lookback window, so this series covers as much of the
archive's own date range as Yahoo actually has for an index (Yahoo's own TICK
history is much shorter than most equities' -- do not expect it to reach back
as far as, say, AAPL.csv).

This series is deliberately stored in an ``_breadth/`` SUBDIRECTORY of the
archive (``<history_dir>/_breadth/TICK.csv``), NOT as a top-level
``TICK.csv``/``_TICK.csv`` sitting directly alongside the per-ticker CSVs, for
two independent reasons surfaced in review:

  * The documented daily archive-refresh flow (`tickers-daily-update/SKILL.md`
    and its canonical replacement) enumerates every ``{TICKER}.csv`` DIRECTLY
    under the archive root and re-fetches each stem AS A YAHOO SYMBOL. That
    external updater is not part of this repo, so its exact glob/filter logic
    is unverified -- an earlier cut of this module relied on an underscore-
    prefixed top-level filename ("_TICK.csv") on the assumption that updater
    also skips underscore stems the same way the training pipelines below do,
    but nothing here can actually confirm that (Codex review: "Move the
    breadth CSV outside the updater glob"). A subdirectory is a STRUCTURAL
    guarantee instead of a filename-convention one: a top-level ``*.csv``
    glob (recursive or not, underscore-aware or not) never descends into it,
    the same way the archive's existing ``_intraday/`` subtree already
    doesn't appear in one.
  * ml-forecaster/features.py's and wondrous-octopus/data.py's training
    pipelines assume every archived series is a positive asset price (they
    mask/drop nonpositive values before computing returns) -- but NYSE TICK is
    a SIGNED breadth count that regularly goes negative. Auto-discovering it
    the same way as an equity would feed discontinuous, meaningless "price"
    features into the pooled Ridge model that backs live sizing adjustments
    (Codex review: "Keep TICK out of price-model ticker discovery").

Both training pipelines' own discover_available_tickers() helpers glob only
``*.csv`` DIRECTLY under the archive root (never recursing into a
subdirectory) -- the same reason `_intraday/`'s per-poll archive is already
invisible to them. Storing this series under `_breadth/` therefore also
keeps it out of their default `--all-tickers-in-archive` discovery, on top
of (not instead of) the external updater's own top-level-only glob. It still
lives inside the same shared archive directory, and anything that wants
breadth data specifically can read `_breadth/TICK.csv` by its exact path.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- index 0 is already this script's own directory
# (ticker-market-breadth/), matching ticker-advisor/pricing.py's identical
# sys.path setup and its documented reasoning (don't let REPO_ROOT shadow
# this subproject's own submodules).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_fileio import atomic_write_text  # noqa: E402
from dca_provenance import write_sidecar  # noqa: E402
from dca_ticker_paths import configured_ticker_history_dir  # noqa: E402
from scripts.file_lock import cross_process_lock  # noqa: E402

#: Yahoo Finance's own symbol for the NYSE TICK index.
YAHOO_SYMBOL = "^TICK"

#: Subdirectory of the archive this series is stored under -- see the module
#: docstring for why a subdirectory (not a top-level filename convention) is
#: the structural fix, matching the existing `_intraday/` subtree.
ARCHIVE_SUBDIRECTORY = "_breadth"

#: Filename within ARCHIVE_SUBDIRECTORY. A bare "TICK.csv" is fine here --
#: unlike a top-level file, nothing globs this directory's contents as if
#: they were per-ticker archive entries, so no filename-prefix convention is
#: needed for it too (though it wouldn't hurt).
ARCHIVE_FILENAME = "TICK.csv"


#: The NYSE TICK index's own exchange timezone -- yfinance anchors its daily
#: bars to exchange-local midnight in this zone, not UTC midnight.
_EXCHANGE_TIMEZONE = "America/New_York"


def _normalize_date_index(frame):
    """Return `frame` with a timezone-naive, calendar-date-only "Date" index.

    yfinance returns an exchange-timezone-aware index, and a full
    `period="max"` download commonly spans a DST transition -- consecutive
    rows can carry different UTC offsets (-05:00 / -04:00). With the
    currently installable pandas 3.x, re-reading such a mixed-offset column
    back from CSV via `parse_dates=True` leaves it as plain strings instead
    of a DatetimeIndex, so comparing it against a freshly downloaded (still
    tz-aware) Timestamp raises TypeError -- breaking every refresh past the
    first DST boundary. Routing BOTH the freshly downloaded frame (in
    fetch_tick_history()) and whatever is read back from disk (in
    _existing_archive_frame()) through this exact same normalization means
    the archive is always written with, and always re-parsed as, plain
    timezone-naive calendar dates -- nothing downstream ever has to
    reconcile two different representations (Codex review: "Normalize
    archive dates before comparing refreshes").

    `pd.to_datetime(..., utc=True)` is only an intermediate, lossless
    parsing step -- it resolves whatever mixed offsets are present (either a
    real tz-aware DatetimeIndex from yfinance, or mixed-offset strings read
    back from CSV) to absolute instants without discarding any information.
    Normalizing to a calendar date directly off THAT UTC instant would still
    be wrong: yfinance's daily bars are anchored to exchange-local midnight
    (America/New_York), not UTC midnight, so a bar timestamped late in the
    NY day can already have rolled over to the next UTC calendar date, and
    `.normalize()` on the UTC instant would silently shift it forward by one
    day. Converting back to the exchange timezone BEFORE dropping tz info
    and normalizing recovers the original local calendar date regardless of
    which UTC offset it happened to be encoded with (Antigravity review:
    "Timezone Normalization Assumption").

    `utc=True` is used only as a fallback for a genuinely mixed-offset
    column, not unconditionally -- see the try/except below.
    """
    import pandas as pd

    frame = frame.copy()
    try:
        # Naive input, and single-consistent-offset tz-aware input, both
        # parse straightforwardly. Only a genuinely MIXED-offset column
        # (e.g. an archive whose rows span a DST boundary, read back as
        # strings from CSV) raises ValueError here.
        index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    except ValueError:
        index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    if index.tz is not None:
        # Reinterpreting an ALREADY-NAIVE index this way would wrongly
        # treat it as a UTC instant and shift it into the previous
        # exchange-local day -- so this branch, and the exchange-timezone
        # conversion described above, only ever runs for genuinely
        # tz-aware input.
        index = index.tz_convert(_EXCHANGE_TIMEZONE).tz_localize(None)
    frame.index = index.normalize()
    frame.index.name = "Date"
    return frame


def fetch_tick_history():
    """Download ^TICK's full available daily history from Yahoo Finance.

    Raises RuntimeError if yfinance returns no rows -- callers must not
    silently overwrite a good archive file with an empty one.
    """
    import yfinance as yf

    frame = yf.Ticker(YAHOO_SYMBOL).history(period="max", auto_adjust=False)
    if frame is None or frame.empty:
        raise RuntimeError(f"yfinance returned no data for {YAHOO_SYMBOL!r}")
    return _normalize_date_index(frame)


def _existing_archive_frame(out_path: Path):
    """Best-effort read of `out_path`'s current contents, or None if unusable.

    A missing, empty, or unparseable existing file is treated as "nothing to
    protect" -- there's no good data to lose by overwriting it. This mirrors
    ticker-advisor/pricing.py's own archive-read tolerance (a malformed
    archive degrades to "unavailable" rather than raising).
    """
    if not out_path.is_file():
        return None
    try:
        import pandas as pd

        # Read the Date column as plain strings (no parse_dates) --
        # _normalize_date_index()'s own pd.to_datetime(..., utc=True) call
        # tolerates a mixed-offset column that parse_dates=True would
        # otherwise silently leave unparsed on pandas 3.x (see its
        # docstring).
        frame = pd.read_csv(out_path, index_col="Date")
        frame = _normalize_date_index(frame)
    except Exception:
        return None
    return frame if not frame.empty else None


def _merge_with_existing(frame, existing_path: Path):
    """Union `frame` with whatever is already at `existing_path`, keyed by date.

    A Yahoo response that's nonempty but degraded in some way (a recent-only
    slice, a missing interior date, duplicated rows disguising a gap, ...)
    must never cause a plain overwrite to silently drop history the archive
    already has. Three successive review rounds each found a new shape of
    "looks fine in aggregate but drops real dates" that a row-count/min-date/
    max-date comparison-based reject-if-truncated check missed (Codex
    review: "Reject truncated nonempty history responses" / "Reject
    refreshes that end before the archive" / "Require refreshed history to
    retain every archived date") -- rather than continue chasing individual
    truncation shapes, this merges by date instead of ever fully replacing:
    every date the existing archive has is kept unless the new download also
    covers that same date WITH USABLE VALUES (in which case the fresh value
    wins, matching this repo's own established per-ticker-CSV update
    convention -- see scripts/run_tickers_daily_update.py's module docstring
    on the external updater's "self-healing" merge behavior). The result's
    row count can therefore only stay the same or grow, never shrink,
    regardless of what shape a degraded response takes.

    A degraded-but-nonempty response can still have a malformed row for a
    date it shares with the archive -- a null/non-finite OHLC value, not a
    missing date -- and blindly letting "later write wins" apply would
    silently corrupt that date's otherwise-good archived values (Codex
    review: "Validate fresh rows before replacing archived values"). Rows
    failing _has_usable_prices() are dropped from `frame` BEFORE the merge,
    so they can neither override a good archived row nor be admitted as a
    new date on their own.

    Raises RuntimeError if that filtering leaves nothing usable at all --
    regardless of whether an existing archive is present -- otherwise a
    nonempty-but-entirely-malformed download would rewrite the (unchanged)
    archive and let write_tick_csv()/main() report success for a refresh
    that fetched nothing usable (Codex review: "Fail refreshes with no
    usable downloaded rows"; this subsumes the initial-refresh-only case an
    earlier fix here handled -- "Reject an initial refresh with no usable
    rows" -- since that was only checking the `existing is None` branch).
    """
    import pandas as pd

    existing = _existing_archive_frame(existing_path)
    frame = frame[_has_usable_prices(frame)]
    if frame.empty:
        raise RuntimeError(
            f"yfinance returned no usable rows for {YAHOO_SYMBOL!r} (every row was missing "
            "or had a null/non-finite OHLC value)"
            + (", and no existing archive to fall back to" if existing is None else "")
        )
    if existing is None:
        return frame
    merged = pd.concat([existing, frame])
    # keep="last": `frame` (the fresh download, now filtered to usable rows
    # only) is concatenated second, so a date present in both wins with the
    # fresh value -- the same "later write wins" convention
    # _existing_archive_frame()'s sibling modules (e.g.
    # ticker-advisor/pricing.py) already use for duplicate-date rows.
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged.sort_index()


#: OHLC columns that must all be present, non-null, and finite for a row to
#: be trusted -- see _has_usable_prices().
_PRICE_COLUMNS = ("Open", "High", "Low", "Close")


def _has_usable_prices(frame):
    """Boolean mask: which rows of `frame` have a usable value in every OHLC column.

    A row missing, or with a null/non-finite (NaN/inf), Open/High/Low/Close
    is not safely usable -- letting it through would either corrupt a good
    archived row it overlaps with, or admit a garbage row for a brand-new
    date (Codex review: "Validate fresh rows before replacing archived
    values"). Negative values are NOT rejected: NYSE TICK is a signed
    breadth count, so a negative Close is a normal, valid observation here
    -- unlike an ordinary equity price series.
    """
    import numpy as np
    import pandas as pd

    mask = pd.Series(True, index=frame.index)
    for column in _PRICE_COLUMNS:
        if column not in frame.columns:
            return pd.Series(False, index=frame.index)
        values = pd.to_numeric(frame[column], errors="coerce")
        mask &= values.notna() & np.isfinite(values)
    return mask


#: Source label recorded in the archive's provenance sidecar.
PROVENANCE_SOURCE = "yfinance:^TICK"


def write_tick_csv(
    frame,
    history_dir: Path,
    *,
    require_existing_history_dir: bool = False,
    fetched_at: datetime | None = None,
) -> Path:
    """Atomically merge `frame` into `<history_dir>/ARCHIVE_SUBDIRECTORY/ARCHIVE_FILENAME`.

    Kept separate from fetch_tick_history() so tests can exercise the
    write/overwrite behavior with a synthetic frame, with no network call.
    Merges by date (see _merge_with_existing) rather than replacing the file
    outright, so a degraded download can never drop history the archive
    already has, and never leaves a partially-written file on disk:
    atomic_write_text() writes to a sibling temp file and swaps it in with
    os.replace() only after the full CSV has been serialized (Codex review:
    "Replace the archive atomically").

    The read-merge-write sequence is held under a cross-process lock on a
    sidecar ``TICK.csv.lock`` file (the same scripts/file_lock.py primitive
    ticker-advisor and scripts/run_tickers_daily_update.py already use):
    without it, two overlapping invocations could each merge against the
    SAME pre-refresh archive and then race each other's os.replace(), with
    whichever merge started from staler data winning and silently discarding
    the other's rows -- atomic_write_text() alone only guarantees the write
    itself is torn-free, not that the two read-merge-write sequences are
    serialized (Codex review: "Serialize validation with the archive
    replacement").

    When ``fetched_at`` (timezone-aware) is given, a ``TICK.csv.provenance.json``
    sidecar (dca_provenance) is written inside the same lock, right after the
    swap, so the recorded SHA-256 always describes the bytes this call left on
    disk. The time is a parameter, not read here, so the record stays
    reproducible in tests; callers that omit it (existing tests) get the old
    behavior unchanged.
    """
    # Normalized here too, not just in fetch_tick_history() -- this function
    # is its own tested unit and must not assume every caller already ran
    # its input through that normalization (Codex review: "Normalize
    # archive dates before comparing refreshes").
    frame = _normalize_date_index(frame)
    out_path = history_dir / ARCHIVE_SUBDIRECTORY / ARCHIVE_FILENAME
    if require_existing_history_dir and not history_dir.is_dir():
        raise FileNotFoundError(
            f"ticker history archive root {history_dir} disappeared before the breadth archive write; "
            "refusing to create a local stand-in"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(out_path.with_suffix(".csv.lock")):
        merged = _merge_with_existing(frame, out_path)
        buffer = io.StringIO()
        merged.to_csv(buffer)
        atomic_write_text(out_path, buffer.getvalue())
        if fetched_at is not None:
            has_rows = len(merged) > 0
            write_sidecar(
                out_path,
                source=PROVENANCE_SOURCE,
                fetched_at=fetched_at,
                rows=len(merged),
                first_date=merged.index.min().date() if has_rows else None,
                last_date=merged.index.max().date() if has_rows else None,
            )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Ticker archive directory (default: dca_ticker_paths.configured_ticker_history_dir())",
    )
    parser.add_argument(
        "--require-existing-history-dir",
        action="store_true",
        help="Refuse to create --history-dir when it is missing (for automatic managed-archive refreshes)",
    )
    args = parser.parse_args(argv)
    using_default_history_dir = args.history_dir is None
    history_dir = args.history_dir if args.history_dir is not None else configured_ticker_history_dir()
    if (using_default_history_dir or args.require_existing_history_dir) and not history_dir.is_dir():
        # ticker_data/ is a gitignored symlink/mount to the managed Google
        # Drive archive (CLAUDE.md: "Do not modify"). If that link is
        # missing, unmounted, or broken, mkdir(parents=True) in
        # write_tick_csv() would silently materialize a real local
        # ticker_data/ directory containing nothing but _breadth/TICK.csv -- and a
        # later daily-update/calibration job checking
        # configured_ticker_history_dir().is_dir() would then see that
        # freshly-created stub and treat the real, managed archive as
        # available when it never was. Mirrors dca_intraday_archive.py's and
        # scripts/refresh_active_ticker_histories.py's identical guard
        # (Codex review: "Refuse to create a missing default archive"). An
        # explicitly-passed --history-dir is normally exempt: the caller
        # stated their intent, so creating it is expected. Automatic callers
        # pass --require-existing-history-dir because their explicit value can
        # still be the managed default and must retain this fail-closed guard.
        if args.history_dir is not None:
            # An explicitly-passed --history-dir is the one being rejected
            # here -- this is exactly the automatic wrapper's own invocation
            # pattern (scripts/run_tickers_daily_update.py always passes
            # --history-dir together with --require-existing-history-dir).
            # Telling the operator to "pass --history-dir explicitly" would
            # point at the very flag that was just rejected and give them no
            # way forward (Codex review: "Report a usable remedy for guarded
            # explicit paths").
            raise FileNotFoundError(
                f"ticker history archive root {history_dir} does not exist -- refusing to create "
                "a local stand-in because --require-existing-history-dir was set. Restore the "
                "archive directory (e.g. remount the Drive link), or point --history-dir at a "
                "different, existing archive."
            )
        raise FileNotFoundError(
            f"ticker history archive root {history_dir} does not exist (missing or unmounted "
            "Drive link, or this package is not running from a repository checkout?) -- refusing "
            "to create a local stand-in. Set DCA_TICKER_HISTORY_DIR to the shared archive location, "
            "or pass --history-dir explicitly."
        )
    frame = fetch_tick_history()
    out_path = write_tick_csv(
        frame,
        history_dir,
        require_existing_history_dir=using_default_history_dir or args.require_existing_history_dir,
        fetched_at=datetime.now(timezone.utc),
    )
    # Re-read the actually-written file rather than reporting on `frame`
    # directly: write_tick_csv() merges the download with whatever was
    # already archived, so the file's real row count/date range can exceed
    # the downloaded frame's own.
    written = _existing_archive_frame(out_path)
    if written is not None:
        print(f"wrote {len(written)} rows ({written.index.min().date()} to {written.index.max().date()}) to {out_path}")
    else:
        print(f"wrote to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
