"""3D market-structure visualization ("The Ocean") -- data layer and viewer emitter.

This is the first module in the repository that produces *pixels* rather than CSV,
Markdown, or JSON. It exists because tabular reports hide the one thing a human eye is
genuinely better at than a summary table: whether a move is broad or idiosyncratic, and
which names are moving together.

The axis mapping is the one the strategy owner asked for:

===========  ===========================================================
Axis         Channel
===========  ===========================================================
``X``        volume
``Y``        the high/low range
``Z``        the open->close body
time         the fourth channel, spatialized differently per mode
===========  ===========================================================

Two render modes share one payload:

``pulse``
    Time is the animation clock. One frame per trading day; every ticker is a box
    glyph whose wick spans ``[y_low, y_high]`` and whose body spans
    ``[z_open, z_close]``, positioned along X by volume. Recent frames persist as
    fading trails, so a sector-wide move reads as a ripple passing through the cloud.

``wave``
    Time becomes a spatial axis. The ground plane is (time x ticker) and Y is the
    high/low band, so the whole window stands still as a terrain you orbit.

Both modes read the same normalized grid, which is why the mode toggle in the viewer is
instant and why the matplotlib renderer under ``analysis/`` can reuse this module
unchanged instead of reimplementing the math and drifting away from it.

Two properties of this module are load-bearing and should survive future edits:

1.  **Standard library only.** ``CLAUDE.md``'s first hard invariant. The vendored
    three.js under ``viz/vendor/`` is browser-side only -- it is read as bytes and
    pasted into a ``<script>`` tag, never imported by Python -- so the invariant holds
    with room to spare. See ``viz/vendor/README.md``.
2.  **No network, no credentials.** Like every other module here, live market data
    arrives through injected callables (``ProviderLoader``), never through a socket
    opened in this file.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from near_close_dca import EASTERN, Bar

__all__ = [
    "BarSeries",
    "TickerGrouping",
    "MarketGraphPayload",
    "CombinedCsvLoader",
    "TickerArchiveLoader",
    "IntradayArchiveLoader",
    "ProviderLoader",
    "provider_loader_from_run_dir",
    "resolve_loader",
    "seriate_tickers",
    "grouping_from_mapping",
    "build_payload",
    "build_payload_json",
    "build_viewer_html",
    "write_market_graph",
    "viewer_asset_dir",
    # Shared front-end policy, so the HTML CLI and the matplotlib renderer cannot
    # disagree about it -- see the "Shared CLI policy" section below.
    "MAX_TRAIL",
    "MAX_SERIATION_TICKERS",
    "resolve_universe",
    "seriate_or_refuse",
    "artifact_span",
    "default_output_dir",
    "validate_trail",
    "active_tickers_from_config",
]

#: Payload schema version. Bump when a field changes meaning; ``viz/market_graph.js``
#: refuses to render a payload whose version it does not recognise, which is a much
#: better failure than silently drawing a garbage scene from misread arrays.
PAYLOAD_SCHEMA_VERSION = 1

#: Default page/window title. "The Ocean" is a display name only -- deliberately absent
#: from module, CLI, and doc filenames so the code stays greppable for "market graph".
DEFAULT_TITLE = "The Ocean"

#: Where a wheel install places the bundled viewer assets (see pyproject
#: [tool.setuptools.data-files]). Mirrors dca_config._INSTALLED_CONFIG_DIR exactly;
#: the two resolutions must stay in step or an installed wheel finds its config but
#: renders a blank page.
_INSTALLED_VIZ_DIR = Path(sys.prefix) / "share" / "all-stock-dca" / "viz"

#: Environment override for the viewer asset directory, matching the DCA_CONFIG_DIR /
#: DCA_TICKER_HISTORY_DIR convention used everywhere else in this repo.
VIZ_DIR_ENV = "DCA_VIZ_DIR"

#: Where a wheel install places the bundled combined OHLCV csv, and the env override
#: for it. Same resolution ladder as the config and viewer directories.
_INSTALLED_DATA_DIR = Path(sys.prefix) / "share" / "all-stock-dca" / "data"
COMBINED_CSV_ENV = "DCA_COMBINED_CSV"

#: Minimum overlapping trading days before a pair's correlation is trusted. Below this
#: the estimate is noise dressed up as signal -- three shared days can produce a 0.99
#: correlation between two unrelated names -- so short-overlap pairs are treated as
#: uncorrelated rather than allowed to invent cluster structure. See _correlation().
MIN_OVERLAP_DAYS = 30

#: Percentile used to set the shared Y/Z scale. Not the max: one earnings gap would
#: otherwise flatten every other ticker in the scene to an invisible sliver.
_SCALE_PERCENTILE = 98.0

#: Floor for the shared Y/Z scale, as a fraction (0.005 == 0.5%). A window in which
#: nothing moved would otherwise divide by ~0 and explode the geometry.
_MIN_SCALE = 0.005

#: Categorical cluster palette. Chosen for distinguishability against both the light
#: and dark viewer backgrounds; clusters beyond this many wrap around, which is fine
#: because the viewer also labels each band. Public because
#: ``ml-forecaster/cluster_tickers.py`` emits grouping files that render alongside
#: locally-derived ones -- a second palette there would make two charts of the same
#: universe disagree about which colour means which group.
CLUSTER_COLORS: Tuple[str, ...] = (
    "#4c9be8", "#e8734c", "#5cc27f", "#c86fd4", "#e0b64a",
    "#4fbfc4", "#e2688f", "#8f8ae0", "#8ab74a", "#d09a5c",
)

#: Synthetic series name for the whole-market centroid. Double-underscored so it can
#: never collide with a real symbol.
MARKET_SERIES = "__MARKET__"

#: Template placeholders in viz/market_graph.html.tmpl, substituted in a single pass.
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

#: The only colour form accepted from an externally supplied grouping file. Anything
#: else is replaced with a palette colour -- see _safe_color().
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

#: Upper bound on the pulse trail, matching ``MAX_TRAIL`` in ``viz/market_graph.js``.
#: Validated rather than clamped so the emitted payload and the rendered scene agree --
#: a silently clamped value leaves the JSON claiming a trail nobody ever saw.
MAX_TRAIL = 60

#: Above this many tickers, :func:`seriate_or_refuse` refuses rather than running. The
#: average-linkage step is O(n^3), so the full ~6000-symbol archive would never finish,
#: and a CLI that appears to hang is worse than one that explains itself. The
#: ml-forecaster export (scipy) handles far larger universes and is the way through.
MAX_SERIATION_TICKERS = 400

#: Characters that are legal in a frame label but not in a filename on every platform.
#: Intraday spans carry "YYYY-MM-DD HH:MM", and a colon is invalid on Windows.
_UNSAFE_IN_FILENAMES = str.maketrans({":": "", " ": "T"})

#: What may survive into a path component built from a frame label. An allowlist rather
#: than a blocklist: labels reach artifact_span() from a --payload document this process
#: did not write, and default_output_dir() hands the result to stream_root() as a path
#: component. A blocklist of "characters that look dangerous" is the wrong shape for that
#: -- `..`, a leading `/`, and a backslash are each separately capable of leaving
#: $DCA_CRON_TRACKING_ROOT entirely.
_SAFE_IN_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")

#: The documented frame-label shapes: a daily date, or an intraday UTC timestamp.
_FRAME_LABEL = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}Z)?$")

#: What an archive filename stem has to look like to be treated as a ticker. Same
#: pattern the rest of the repo uses (ml-forecaster/features.py,
#: dca_inverse_martingale.py), so the archive means the same thing to every reader of it.
_TICKER_SYMBOL = re.compile(r"^[A-Z.]{1,6}$")


# ---------------------------------------------------------------------------
# Core records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BarSeries:
    """One ticker's ordered OHLCV history, plus where it came from.

    ``near_close_dca.Bar`` is reused verbatim as the atom rather than defining a
    parallel bar type -- it already carries exactly (timestamp, open, high, low, close,
    volume) and is what every provider in ``dca_live_adapters`` returns. It has no
    ticker field, which is the only reason this wrapper exists.

    ``bars`` is always ascending by timestamp and deduplicated, because both render
    modes index positionally into a shared date axis; an out-of-order or doubled bar
    would silently misalign one ticker against the rest of the market.
    """

    ticker: str
    bars: Tuple[Bar, ...]
    source: str = ""

    def __post_init__(self) -> None:
        if not self.ticker:
            raise ValueError("BarSeries requires a non-empty ticker")


@dataclass(frozen=True)
class TickerGrouping:
    """Ticker display order plus the cluster bands drawn along the ticker axis.

    ``source`` is surfaced in the viewer rather than kept internal: the grouping you are
    looking at depends on how the graph was generated (cheap return correlation vs. the
    richer ml-forecaster behavioral export), and a chart that quietly used the weaker
    one would be actively misleading.
    """

    order: Tuple[str, ...]
    clusters: Tuple[Dict[str, Any], ...] = ()
    source: str = "none"


@dataclass(frozen=True)
class MarketGraphPayload:
    """Everything the viewer needs, already normalized. Serialized straight to JSON."""

    title: str
    dates: Tuple[str, ...]
    tickers: Tuple[str, ...]
    series: Tuple[Dict[str, Any], ...]
    scales: Dict[str, Any]
    grouping: TickerGrouping
    mode: str = "both"
    whole_market: bool = False
    source: str = ""
    generated_at: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small parsing helpers
#
# Every loader below reads text written by something else -- a Polygon export, a Yahoo
# archive, a broker JSON payload -- so all of them coerce leniently and drop rows they
# cannot make sense of rather than raising. A visualization that renders 51 of 53
# tickers is useful; one that raises on a single malformed cell is not.
# ---------------------------------------------------------------------------


def _parse_float(value: Any) -> Optional[float]:
    """Coerce to a finite float, or ``None``. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse the several date/datetime spellings these archives actually contain.

    Accepts ``YYYY-MM-DD``, full ISO-8601 with or without an offset, and the ``Z``
    suffix that ``datetime.fromisoformat`` rejects before Python 3.11 semantics. All
    results are naive UTC-equivalent, because the render only ever needs day ordering
    and mixing aware and naive timestamps would make sorting raise.

    A NAIVE value carrying a time of day is read as Eastern and converted, exactly as
    ``dca_live_adapters._to_utc_iso`` reads a naive datetime -- one convention for
    "a bare wall clock in this repository", not two. That matters because the frame
    labels advertise UTC with a ``Z``: leaving naive instants untouched made that marker
    a claim the data did not support, which is precisely the uncertainty an earlier
    round's comment described and then stamped over. Establishing the zone here is what
    earns the ``Z``.

    A naive value at exactly midnight is left alone: that is a DATE marker from a daily
    source, not an instant, and shifting it would both invent a time nobody recorded and
    flip every daily payload's ``intraday`` flag (which keys off a non-midnight time).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if parsed.time() != time(0, 0):
        # Naive instant: Eastern by the repo's convention, converted so the payload's
        # "Z" is true of every intraday frame rather than of most of them.
        return parsed.replace(tzinfo=EASTERN).astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _bar_from_values(
    timestamp: Any, open_: Any, high: Any, low: Any, close: Any, volume: Any
) -> Optional[Bar]:
    """Build a ``Bar``, or ``None`` when the row cannot support one.

    A bar is only usable if it has a timestamp and a positive close -- close is what
    every downstream channel is measured against. High/low/open fall back to close so a
    quote-derived row (``dca_live_adapters`` stamps prior-close bars with ``low=0.0``
    to mean "unknown") degrades to a zero-height glyph instead of a spike to the floor.
    """
    when = _parse_timestamp(timestamp)
    close_value = _parse_float(close)
    if when is None or close_value is None or close_value <= 0:
        return None
    # Every non-positive OHLC value is "unknown", not a real print, and each is checked
    # before it is used. `or close_value` alone is not enough: a negative sentinel is
    # both finite and truthy, so it would survive the fallback and then be adopted as
    # the low by the invariant repair below, manufacturing an enormous fake range that
    # would dominate the shared percentile scale.
    open_value = _parse_float(open_) or close_value
    if open_value <= 0:
        open_value = close_value
    high_value = _parse_float(high) or close_value
    low_value = _parse_float(low) or close_value
    if low_value <= 0:
        low_value = min(open_value, close_value)
    if high_value <= 0:
        high_value = max(open_value, close_value)
    # Defend the invariant the geometry assumes: low <= {open, close} <= high.
    low_value = min(low_value, open_value, close_value)
    high_value = max(high_value, open_value, close_value)
    volume_value = _parse_float(volume) or 0.0
    return Bar(
        timestamp=when,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=max(volume_value, 0.0),
    )


def _split_adjusted(bar: Bar, raw_close: Any, adjusted_close: Any) -> Bar:
    """Rescale a raw OHLC bar onto its split/dividend-adjusted basis.

    The Yahoo-flavored archive stores RAW open/high/low/close beside an ``Adj Close``.
    Read raw, a 2-for-1 split looks like a 50% overnight collapse: the prior close sits
    near 100 and the next session's prints near 50. That single fabricated move would
    dominate the shared percentile scale, drag the market centroid, and flatly
    contradict this module's documented guarantee that a split does not move a vertex.

    ``Adj Close / Close`` is the row's own adjustment ratio, applied uniformly to all
    four prices -- the same idiom ``scripts/generate_per_ticker_sizing.py`` already uses
    (``adjusted_low = low * adjusted_close / close``). Volume is deliberately left
    alone: it is a share count, not a price, and the volume channel is normalized
    per-ticker anyway.

    Returns the bar unchanged when either value is missing or non-positive -- an
    unusable ratio must not silently rescale real prices.
    """
    close_value = _parse_float(raw_close)
    adjusted = _parse_float(adjusted_close)
    if close_value is None or adjusted is None or close_value <= 0 or adjusted <= 0:
        return bar
    ratio = adjusted / close_value
    if ratio == 1.0:
        return bar
    return Bar(
        timestamp=bar.timestamp,
        open=bar.open * ratio,
        high=bar.high * ratio,
        low=bar.low * ratio,
        close=bar.close * ratio,
        volume=bar.volume,
    )


def _finalize(ticker: str, bars: List[Bar], source: str) -> Optional[BarSeries]:
    """Sort, dedupe, and wrap -- or return ``None`` for a series too thin to draw.

    Two bars is the floor: one bar has no prior close, so every channel here (which is
    measured *against* the prior close) would be undefined. This is what excludes the
    single-row stub tickers in ``historical_stock_data.csv``.
    """
    if len(bars) < 2:
        return None
    by_timestamp: Dict[datetime, Bar] = {}
    for bar in bars:
        # Last write wins, matching ml-forecaster/features.py's duplicate-date handling.
        by_timestamp[bar.timestamp] = bar
    ordered = tuple(by_timestamp[key] for key in sorted(by_timestamp))
    if len(ordered) < 2:
        return None
    return BarSeries(ticker=ticker, bars=ordered, source=source)


def _within(bar: Bar, start: Optional[date], end: Optional[date]) -> bool:
    day = bar.timestamp.date()
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _window(bars: Sequence[Bar], start: Optional[date], end: Optional[date]) -> List[Bar]:
    """Trim to ``[start, end]``, keeping ONE bar before ``start`` as a prior-close baseline.

    Every channel here is measured against the previous bar's close, so a hard cut at
    ``start`` throws away the reference the first requested session needs -- it would
    always render null, and a single-session window would resolve to no usable series at
    all despite the archive holding the preceding close. The extra bar is a calculation
    input only; :func:`build_payload` is told the real window start and drops earlier
    frames from the emitted axis.

    ``bars`` must already be ascending.
    """
    kept = [bar for bar in bars if end is None or bar.timestamp.date() <= end]
    if start is None:
        return kept
    inside = [bar for bar in kept if bar.timestamp.date() >= start]
    before = [bar for bar in kept if bar.timestamp.date() < start]
    return ([before[-1]] + inside) if before else inside


def _wanted(tickers: Optional[Sequence[str]]) -> Optional[set]:
    """Uppercase set of requested tickers; ``None`` means "everything the source has".

    An explicitly EMPTY request stays empty rather than collapsing to ``None``. The two
    mean opposite things -- "the caller asked for nothing" versus "the caller did not
    filter" -- and conflating them let ``--tickers @config`` silently render the entire
    archive when the configured universe came back empty.
    """
    if tickers is None:
        return None
    return {t.strip().upper() for t in tickers if t.strip()}


def normalize_roster(
    tickers: Optional[Sequence[str]],
    fallback: Optional[List[str]] = None,
) -> Optional[List[str]]:
    """Ordered uppercase roster; ``None`` falls back, an explicit ``[]`` stays empty.

    The list-shaped twin of :func:`_wanted`, for the paths that carry a roster forward
    rather than filtering against one. Same distinction, and it exists as a named helper
    for a specific reason: ``tickers or self._tickers`` was written three separate times
    in this module, each one silently widening "the caller asked for nothing" into "the
    caller did not filter". Fixing the reported instance and leaving its siblings is what
    let it recur, so every optional-roster site now routes through here and the truthiness
    form is greppable by its absence.
    """
    if tickers is None:
        return fallback
    return [t.strip().upper() for t in tickers if t.strip()]


# ---------------------------------------------------------------------------
# Loaders
#
# One protocol, four implementations, covering every OHLCV store this repository has.
# They are deliberately interchangeable: the CLI picks one by name, and nothing
# downstream of load() knows or cares which one ran.
# ---------------------------------------------------------------------------


class BarSeriesLoader(Protocol):
    """Anything that can produce ``BarSeries`` for a set of tickers."""

    name: str

    def available_tickers(self) -> List[str]:  # pragma: no cover - protocol
        ...

    def load(
        self,
        tickers: Optional[Sequence[str]] = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[BarSeries]:  # pragma: no cover - protocol
        ...


#: Column spelling of the repo-root ``historical_stock_data.csv``. This is a
#: Polygon.io ``list_aggs`` export (hence lowercase names plus ``vwap``/``n``) and
#: matches no other data store here -- the per-ticker archive is Yahoo-flavored. Both
#: spellings are supported rather than normalizing the file, because CLAUDE.md lists
#: ``historical_stock_data.csv`` as read-only operational data.
_COMBINED_CSV_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume")


def default_combined_csv() -> Path:
    """Locate ``historical_stock_data.csv`` across checkout and installed-wheel layouts.

    Resolved lazily and by search rather than as a module-relative constant: this is the
    CLI's DEFAULT source, and in a wheel the module lands in site-packages while the CSV
    ships as a data file under ``share/all-stock-dca/data``. A fixed
    ``Path(__file__).parent`` would make the documented default command work from a
    checkout and fail on every installed copy.
    """
    override = os.environ.get(COMBINED_CSV_ENV)
    if override:
        return Path(override)
    for candidate in (
        Path(__file__).resolve().parent / "historical_stock_data.csv",
        _INSTALLED_DATA_DIR / "historical_stock_data.csv",
        Path.cwd() / "historical_stock_data.csv",
    ):
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parent / "historical_stock_data.csv"


class CombinedCsvLoader:
    """Read the single combined ``historical_stock_data.csv`` at the repository root.

    This is the only OHLCV source committed to git, so it is the CLI default and the
    one path that works in a fresh checkout with no environment configured. It is thin
    -- a handful of real tickers plus single-row stubs -- which is why the CLI reports
    how many requested tickers actually resolved.
    """

    name = "csv"

    def __init__(self, csv_path: Optional[Path | str] = None) -> None:
        self.csv_path = Path(csv_path) if csv_path is not None else default_combined_csv()

    def available_tickers(self) -> List[str]:
        if not self.csv_path.is_file():
            return []
        seen = set()
        with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = str(row.get("ticker") or "").strip().upper()
                if symbol:
                    seen.add(symbol)
        return sorted(seen)

    def load(
        self,
        tickers: Optional[Sequence[str]] = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[BarSeries]:
        if not self.csv_path.is_file():
            raise FileNotFoundError(f"combined OHLCV csv not found: {self.csv_path}")
        wanted = _wanted(tickers)
        grouped: Dict[str, List[Bar]] = {}
        with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                symbol = str(row.get("ticker") or "").strip().upper()
                if not symbol or (wanted is not None and symbol not in wanted):
                    continue
                bar = _bar_from_values(
                    row.get("date"), row.get("open"), row.get("high"),
                    row.get("low"), row.get("close"), row.get("volume"),
                )
                if bar is not None:
                    grouped.setdefault(symbol, []).append(bar)
        return _collect(grouped, self.name, start, end)


class TickerArchiveLoader:
    """Read the per-ticker Yahoo-flavored archive: ``<history_dir>/<TICKER>.csv``.

    This is the repository's primary OHLCV store and the one that makes "the market as
    a whole" meaningful (it covers thousands of symbols, versus the combined CSV's
    handful). It is gitignored and resolved through
    ``dca_ticker_paths.configured_ticker_history_dir()``, so ``$DCA_TICKER_HISTORY_DIR``
    must point at it outside a populated checkout.

    Entries that are not ``<TICKER>.csv`` are underscore-prefixed by archive convention
    (``_intraday/``, ``_delisted.txt``, ``_update_progress.json``) and skipped here --
    the same rule ``ml-forecaster/features.py::discover_available_tickers`` applies.
    """

    name = "archive"

    def __init__(self, history_dir: Optional[Path | str] = None) -> None:
        if history_dir is not None:
            self.history_dir = Path(history_dir)
        else:
            from dca_ticker_paths import configured_ticker_history_dir

            self.history_dir = configured_ticker_history_dir()

    def _paths_by_symbol(self) -> Dict[str, Path]:
        """Canonical uppercase symbol -> the archive file's ACTUAL path.

        Discovery uppercases the stem, but the file on a case-sensitive filesystem may
        be ``aapl.csv``. Rebuilding the path as ``<SYMBOL>.csv`` would then advertise a
        ticker that load() silently skips, so the real name is carried through instead.
        An uppercase file wins if both spellings somehow exist.
        """
        found: Dict[str, Path] = {}
        for entry in sorted(self.history_dir.glob("*.csv")):
            if entry.name.startswith("_"):
                continue
            symbol = entry.stem.upper()
            # Stems must look like tickers, not merely "not underscore-prefixed". The
            # archive accumulates auxiliary files beside the real history, and
            # `AAPL-backup.csv` was being loaded as a ticker named AAPL-BACKUP --
            # duplicating AAPL's history into the market centroid and handing the
            # correlation grouping a pair with a perfect 1.0 that means nothing.
            # ml-forecaster/features.py::discover_available_tickers filters its
            # archive-wide branch through this same pattern; this is that rule, applied
            # to the loader that shares the directory.
            if not _TICKER_SYMBOL.match(symbol):
                continue
            if symbol not in found or entry.stem == symbol:
                found[symbol] = entry
        return found

    def available_tickers(self) -> List[str]:
        if not self.history_dir.is_dir():
            return []
        return sorted(self._paths_by_symbol())

    def load(
        self,
        tickers: Optional[Sequence[str]] = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[BarSeries]:
        if not self.history_dir.is_dir():
            raise FileNotFoundError(
                f"ticker history archive not found: {self.history_dir} "
                "(set $DCA_TICKER_HISTORY_DIR outside a populated checkout)"
            )
        wanted = _wanted(tickers)
        paths = self._paths_by_symbol()
        symbols = sorted(paths) if wanted is None else [s for s in sorted(paths) if s in wanted]
        grouped: Dict[str, List[Bar]] = {}
        for symbol in symbols:
            bars: List[Bar] = []
            with paths[symbol].open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    bar = _bar_from_values(
                        row.get("Date"), row.get("Open"), row.get("High"),
                        row.get("Low"), row.get("Close"), row.get("Volume"),
                    )
                    if bar is None:
                        continue
                    bar = _split_adjusted(bar, row.get("Close"), row.get("Adj Close"))
                    bars.append(bar)
            if bars:
                grouped[symbol] = bars
        return _collect(grouped, self.name, start, end)


class IntradayArchiveLoader:
    """Read the per-poll intraday archive under ``<history_dir>/_intraday/``.

    Each row is one scheduled poll, and the ``bar_*`` columns carry the intraday OHLCV
    behind the price the strategy decided on -- resolution the daily archive cannot
    show. Reading goes through ``dca_intraday_archive.read_ticker_observations`` rather
    than parsing the CSV here, so the archive's column contract stays owned by one
    module.
    """

    name = "intraday"

    def __init__(self, archive_dir: Optional[Path | str] = None) -> None:
        if archive_dir is not None:
            self.archive_dir = Path(archive_dir)
        else:
            from dca_intraday_archive import configured_intraday_archive_dir

            self.archive_dir = configured_intraday_archive_dir()

    def available_tickers(self) -> List[str]:
        if not self.archive_dir.is_dir():
            return []
        return [e.stem.upper() for e in sorted(self.archive_dir.glob("*.csv")) if not e.name.startswith("_")]

    def load(
        self,
        tickers: Optional[Sequence[str]] = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[BarSeries]:
        from dca_intraday_archive import read_ticker_observations

        wanted = _wanted(tickers)
        symbols = self.available_tickers()
        if wanted is not None:
            symbols = [s for s in symbols if s in wanted]
        grouped: Dict[str, List[Bar]] = {}
        for symbol in symbols:
            bars: List[Bar] = []
            for row in read_ticker_observations(symbol, archive_dir=self.archive_dir):
                # Prefer the intraday bar; fall back to the poll's decided price so a
                # row archived before the bar_* columns existed still contributes.
                timestamp = row.get("bar_timestamp") or row.get("observed_at_utc")
                bar = _bar_from_values(
                    timestamp,
                    row.get("bar_open"), row.get("bar_high"), row.get("bar_low"),
                    row.get("bar_close") or row.get("price"), row.get("bar_volume"),
                )
                if bar is not None:
                    bars.append(bar)
            if bars:
                grouped[symbol] = bars
        return _collect(grouped, self.name, start, end)


class ProviderLoader:
    """Adapt any ``near_close_dca.MarketDataProvider`` into a loader.

    This is the single most useful loader, because it is not one data source but three.
    ``RobinhoodDataProvider``, ``FinanceDataProvider``, and the frozen replay provider
    ``run_dca`` builds from persisted ``raw/historicals_batchN.json`` all satisfy the
    same protocol, so the live-injected path and the archived-run path are the same
    code here. Nothing about the MCP payload shape is reimplemented -- the provider's
    own ``_parse_bars`` already drops interpolated bars and non-positive closes.
    """

    name = "provider"

    def __init__(
        self,
        provider: Any,
        *,
        tickers: Optional[Sequence[str]] = None,
        interval_minutes: int = 5,
    ) -> None:
        self.provider = provider
        self.interval_minutes = interval_minutes
        self._tickers = normalize_roster(tickers, fallback=[]) or []

    def available_tickers(self) -> List[str]:
        # A provider cannot enumerate a universe -- it answers per-symbol queries. The
        # caller supplies the roster (from config/tickers.json, typically).
        return list(self._tickers)

    def load(
        self,
        tickers: Optional[Sequence[str]] = None,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[BarSeries]:
        symbols = normalize_roster(tickers, fallback=self._tickers) or []
        if not symbols:
            raise ValueError("ProviderLoader needs an explicit ticker list; a provider cannot enumerate one")
        # Both bounds are Eastern-aware. dca_live_adapters._to_utc_iso() documents that
        # it treats a NAIVE datetime as Eastern, so a bare datetime.now() would hand it
        # the host's wall clock and have it read as Eastern: a Pacific host silently
        # truncates three hours of history, a UTC host asks hours into the future. The
        # window a live provider sees must not depend on where the process happens to
        # run. Explicit dates are stamped in Eastern for the same reason -- a trading
        # day is an Eastern day here.
        window_start = (
            datetime(start.year, start.month, start.day, tzinfo=EASTERN)
            if start
            else datetime(1970, 1, 1, tzinfo=EASTERN)
        )
        # time.max, not 23:59: the intent is "through the end of that day", and 23:59
        # silently excludes anything stamped in the final minute. No equity feed puts a
        # bar there, but the bound should mean what it says rather than be right by
        # accident of market hours.
        window_end = (
            datetime.combine(end, time.max, tzinfo=EASTERN)
            if end
            else datetime.now(EASTERN)
        )
        grouped: Dict[str, List[Bar]] = {}
        for symbol in symbols:
            bars = self.provider.get_intraday_bars(
                symbol, window_start, window_end, self.interval_minutes
            )
            # Re-sanitize through _bar_from_values rather than trusting the provider's
            # own parsing. RobinhoodDataProvider legitimately emits bars with zero
            # open/high/low -- a quote-derived prior close stamps low=0.0 to mean
            # "unknown" -- and taken literally build_payload() reads that as a ~-100%
            # move, which then sets the shared percentile scale for the whole scene.
            # The CSV paths already get this treatment; the provider path was the only
            # one trusting its input.
            usable = []
            for bar in bars or []:
                if bar is None:
                    continue
                cleaned = _bar_from_values(
                    bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume
                )
                if cleaned is not None:
                    usable.append(cleaned)
            if usable:
                grouped[symbol] = usable
        return _collect(grouped, self.name, start, end)


def _unwrap_connector_envelope(payload: Any) -> Any:
    """Step into ``{"data": {...}}`` carriers until ``results`` is at the top level.

    Persisted connector responses are not always bare ``{"results": [...]}``:
    ``normalize_connector_payload`` resolves an ``output_path`` pointer and embeds the
    result under ``data``, and some inline adapter responses use that envelope natively.
    Either can nest more than once -- a pointer to a response that is itself
    ``{"data": {...}}`` lands on disk doubly wrapped.

    This mirrors ``run_dca._build_frozen_providers_from_raw`` deliberately, cycle guard
    included. Without it, run directories holding exactly the archived inputs this
    source exists to replay are rejected as containing no usable historicals.
    """
    seen: set = set()
    while (
        isinstance(payload, Mapping)
        and "results" not in payload
        and isinstance(payload.get("data"), Mapping)
    ):
        if id(payload) in seen:  # a cyclic/self-referential payload
            break
        seen.add(id(payload))
        payload = payload["data"]
    return payload


def provider_loader_from_run_dir(
    run_dir: Path | str,
    *,
    tickers: Optional[Sequence[str]] = None,
    interval_minutes: int = 5,
) -> ProviderLoader:
    """Replay a persisted run's archived Robinhood responses as a loader.

    Mirrors ``run_dca``'s frozen-provider construction (the ``_frozen_historicals``
    closure it builds around ``raw/historicals_batchN.json``) so a completed scheduled
    run can be visualized offline, from exactly the bytes the strategy actually decided
    on. That "persist before compute" archive is the whole reason this path exists: the
    picture is of the real inputs, not of a fresh pull that has since moved.
    """
    raw_dir = Path(run_dir) / "raw"
    results: List[Dict[str, Any]] = []
    for path in sorted(raw_dir.glob("historicals_batch*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        payload = _unwrap_connector_envelope(payload)
        entries = payload.get("results") if isinstance(payload, Mapping) else None
        if isinstance(entries, list):
            results.extend(dict(e) for e in entries if isinstance(e, Mapping))
    if not results:
        raise FileNotFoundError(
            f"no usable raw/historicals_batch*.json under {raw_dir} -- "
            "a run directory must carry persisted Robinhood responses to be replayable"
        )

    def _frozen_historicals(symbols=None, interval=None, **_kwargs):
        symbol_set = set(symbols or [])
        return {
            "results": [
                entry
                for entry in results
                if (not symbol_set or entry.get("symbol") in symbol_set)
                and (interval is None or entry.get("interval") == interval)
            ]
        }

    from dca_live_adapters import RobinhoodDataProvider

    discovered = sorted({str(e.get("symbol") or "").upper() for e in results if e.get("symbol")})
    return ProviderLoader(
        RobinhoodDataProvider(historicals_fn=_frozen_historicals),
        # normalize_roster, not truthiness: an explicitly empty request must stay empty
        # and be refused downstream, rather than widening into "replay every symbol the
        # run happens to contain". Same distinction _wanted() and resolve_universe() keep.
        tickers=normalize_roster(tickers, fallback=discovered),
        interval_minutes=interval_minutes,
    )


def _collect(
    grouped: Mapping[str, List[Bar]],
    source: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[BarSeries]:
    """Finalize a ticker -> bars mapping into sorted, windowed, drawable series.

    Windowing happens HERE rather than during the read, because :func:`_window` needs
    the bars already sorted to identify the one pre-``start`` bar it keeps as the
    prior-close baseline.
    """
    out = []
    for symbol in sorted(grouped):
        series = _finalize(symbol, grouped[symbol], source)
        if series is None:
            continue
        windowed = _window(series.bars, start, end)
        if len(windowed) < 2:
            continue
        out.append(BarSeries(ticker=symbol, bars=tuple(windowed), source=source))
    return out


def resolve_loader(
    spec: str,
    *,
    csv_path: Optional[Path | str] = None,
    history_dir: Optional[Path | str] = None,
    archive_dir: Optional[Path | str] = None,
    run_dir: Optional[Path | str] = None,
    tickers: Optional[Sequence[str]] = None,
    interval_minutes: int = 5,
) -> BarSeriesLoader:
    """Build a loader from the CLI's ``--source`` string."""
    key = (spec or "").strip().lower()
    if key == "csv":
        return CombinedCsvLoader(csv_path)
    if key == "archive":
        return TickerArchiveLoader(history_dir)
    if key == "intraday":
        return IntradayArchiveLoader(archive_dir)
    if key == "run-dir":
        if run_dir is None:
            raise ValueError("--source run-dir requires --run-dir")
        return provider_loader_from_run_dir(run_dir, tickers=tickers, interval_minutes=interval_minutes)
    raise ValueError(f"unknown source {spec!r}; expected one of: csv, archive, intraday, run-dir")


# ---------------------------------------------------------------------------
# Statistics helpers (stdlib only -- no numpy anywhere in this tree)
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile. Returns 0.0 for an empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (pct / 100.0)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    """Population standard deviation. 0.0 for fewer than two points."""
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((v - average) ** 2 for v in values) / len(values))


def bar_closes(series: BarSeries) -> Dict[datetime, float]:
    """Close price keyed by the bar's full timestamp.

    What gets precomputed once per series and reused across the O(n^2) correlation
    pairs. Returns are deliberately NOT precomputed here -- see _correlation()'s
    docstring for why a return has to be computed per PAIR, against that pair's own
    shared axis, rather than once per series against its own adjacent bar.
    """
    return {bar.timestamp: bar.close for bar in series.bars}


def _correlation(left: Mapping[datetime, float], right: Mapping[datetime, float]) -> float:
    """Pearson correlation of close-to-close returns, aligned on the two series' shared axis.

    Returns are computed HERE, from consecutive points of the shared timestamp axis --
    not once per series in isolation -- so that a return on one side and its counterpart
    on the other always span the identical interval. The earlier per-series version
    computed each return against that series' OWN previous bar: at a shared timestamp, a
    series with a gap just before it contributed a multi-session return while a fully
    populated peer contributed a one-session return at the same nominal endpoint, pairing
    two returns that covered different intervals and distorting exactly the halted,
    newly-listed, or archive-gappy names whose grouping is already least certain.
    ml-forecaster/cluster_tickers.py::_correlation_distance already avoids this on the
    scipy tier -- it pivots closes onto the shared date axis and takes
    ``pct_change(fill_method=None)`` there, so a gap produces a NaN that pandas'
    pairwise-correlation excludes rather than a return computed over the wrong interval.
    This is that same guarantee, built by hand for the stdlib tier.

    Returns 0.0 -- "no evidence of a relationship" -- when the overlap is shorter than
    ``MIN_OVERLAP_DAYS`` or either side is flat. That is deliberately conservative: a
    fabricated correlation would place unrelated tickers adjacent on the ticker axis
    and invent a "sector" that does not exist.
    """
    shared = sorted(set(left) & set(right))
    xs: List[float] = []
    ys: List[float] = []
    dates: List[date] = []
    for prev, cur in zip(shared, shared[1:], strict=False):   # shared[1:] is deliberately one shorter
        left_prev, right_prev = left[prev], right[prev]
        if left_prev <= 0 or right_prev <= 0:
            continue
        xs.append(left[cur] / left_prev - 1.0)
        ys.append(right[cur] / right_prev - 1.0)
        dates.append(cur.date())
    # Distinct calendar dates, not raw entries. Intraday sources carry many bars per
    # session, so the THRESHOLD means days, as its name says -- counting entries would
    # let 30 five-minute returns from one morning clear a gate written to reject short,
    # noisy overlap. Daily data has one bar per day, so this is identical there rather
    # than a special case.
    if len(set(dates)) < MIN_OVERLAP_DAYS:
        return 0.0
    x_mean, y_mean = _mean(xs), _mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    x_ss = sum((x - x_mean) ** 2 for x in xs)
    y_ss = sum((y - y_mean) ** 2 for y in ys)
    if x_ss <= 0 or y_ss <= 0:
        return 0.0
    value = numerator / math.sqrt(x_ss * y_ss)
    # Guard against a hair over 1.0 from floating-point error, which would make the
    # distance below take the square root of a small negative number.
    return max(-1.0, min(1.0, value))


def _return_correlation_matrix(series_list: Sequence[BarSeries]) -> List[List[float]]:
    """Full symmetric correlation matrix over close-to-close returns.

    NOTE: unrelated to ``scripts/correlation.py``, which is about incident *correlation
    IDs* and has nothing to do with statistics. Do not merge the two.
    """
    closes = [bar_closes(s) for s in series_list]
    size = len(series_list)
    matrix = [[1.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            value = _correlation(closes[i], closes[j])
            matrix[i][j] = matrix[j][i] = value
    return matrix


def _correlation_distance(matrix: Sequence[Sequence[float]]) -> List[List[float]]:
    """``sqrt(2 * (1 - corr))`` -- a true metric, unlike ``1 - corr``.

    Using a proper metric keeps average linkage well behaved: with ``1 - corr`` the
    triangle inequality can fail and merge heights stop being monotonic, which shows up
    as a visibly incoherent ticker ordering.
    """
    size = len(matrix)
    return [
        [math.sqrt(max(0.0, 2.0 * (1.0 - matrix[i][j]))) for j in range(size)]
        for i in range(size)
    ]


# ---------------------------------------------------------------------------
# Hierarchical clustering + seriation
#
# ~100 lines of average-linkage UPGMA over a distance matrix. scipy would do this in
# one call, but scipy cannot be imported from this tree -- see the stdlib-only
# invariant. n is the ticker count (53 today), so the O(n^3) worst case is irrelevant.
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    """One dendrogram node: either a leaf (``index`` set) or an internal merge."""

    members: List[int]
    left: Optional["_Node"] = None
    right: Optional["_Node"] = None
    height: float = 0.0

    @property
    def is_leaf(self) -> bool:
        return self.left is None and self.right is None


def _average_linkage(distance: Sequence[Sequence[float]]) -> Optional[_Node]:
    """Agglomerate every leaf into one dendrogram using UPGMA."""
    size = len(distance)
    if size == 0:
        return None
    nodes = [_Node(members=[i]) for i in range(size)]
    # Working copy of the distance matrix, keyed by position in `nodes`.
    working = [list(row) for row in distance]
    while len(nodes) > 1:
        best = None
        best_pair = (0, 1)
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                if best is None or working[i][j] < best:
                    best = working[i][j]
                    best_pair = (i, j)
        i, j = best_pair
        merged = _Node(
            members=nodes[i].members + nodes[j].members,
            left=nodes[i],
            right=nodes[j],
            height=best or 0.0,
        )
        # Average linkage: the merged cluster's distance to each survivor is the
        # member-count-weighted mean of its two children's distances.
        size_i, size_j = len(nodes[i].members), len(nodes[j].members)
        new_row = []
        for k in range(len(nodes)):
            if k in (i, j):
                continue
            new_row.append((working[i][k] * size_i + working[j][k] * size_j) / (size_i + size_j))
        survivors = [n for k, n in enumerate(nodes) if k not in (i, j)]
        survivor_matrix = [
            [working[a][b] for b in range(len(nodes)) if b not in (i, j)]
            for a in range(len(nodes))
            if a not in (i, j)
        ]
        # Both are exactly (len(nodes) - 2) long here: new_row gains its own
        # self-distance only after this loop.
        for row, value in zip(survivor_matrix, new_row, strict=True):
            row.append(value)
        new_row.append(0.0)
        survivor_matrix.append(new_row)
        nodes = survivors + [merged]
        working = survivor_matrix
    return nodes[0]


def _order_leaves(node: _Node, distance: Sequence[Sequence[float]]) -> List[int]:
    """Order leaves so adjacent entries are similar (a cheap optimal-leaf-ordering).

    At each merge the two subtrees can be emitted in either order, giving four possible
    boundary pairings. Picking the pairing whose touching leaves are closest is a
    greedy approximation of scipy's ``optimal_leaf_ordering`` -- it is not optimal, but
    it removes the arbitrary left/right flips that otherwise scatter a coherent cluster
    across the ticker axis, which is the whole point of seriating.
    """
    if node.is_leaf:
        return list(node.members)
    left = _order_leaves(node.left, distance)  # type: ignore[arg-type]
    right = _order_leaves(node.right, distance)  # type: ignore[arg-type]
    candidates = (
        (distance[left[-1]][right[0]], left, right),
        (distance[left[-1]][right[-1]], left, list(reversed(right))),
        (distance[left[0]][right[0]], list(reversed(left)), right),
        (distance[left[0]][right[-1]], list(reversed(left)), list(reversed(right))),
    )
    _, first, second = min(candidates, key=lambda item: item[0])
    return first + second


def _cut_clusters(node: _Node, count: int) -> List[List[int]]:
    """Cut the dendrogram into at most ``count`` clusters by descending merge height."""
    clusters = [node]
    while len(clusters) < count:
        # Split the cluster whose own merge was loosest -- that is the weakest join.
        splittable = [c for c in clusters if not c.is_leaf]
        if not splittable:
            break
        target = max(splittable, key=lambda c: c.height)
        clusters.remove(target)
        clusters.extend([target.left, target.right])  # type: ignore[list-item]
    return [sorted(c.members) for c in clusters]


def _auto_cluster_count(size: int) -> int:
    """A sane default cluster count when the caller does not pick one."""
    if size < 4:
        return 1
    return max(2, min(8, round(math.sqrt(size / 2.0))))


def seriate_tickers(
    series_list: Sequence[BarSeries],
    *,
    clusters: Optional[int] = None,
    source: str = "returns",
) -> TickerGrouping:
    """Order tickers so similar-behaving names sit adjacent, and band them into groups.

    This is what makes whole-market mode legible. Alphabetical order scatters related
    names across the ticker axis and destroys the very structure the chart exists to
    show -- a sector-wide move has to appear as one contiguous swell, not as spikes
    sprinkled through the scene.

    The similarity used here is close-to-close return correlation, which captures
    "moves together". The richer "behaves like" grouping (same volatility regime,
    drawdown character, and volume profile) comes from ``ml-forecaster/cluster_tickers.py``
    and arrives through :func:`grouping_from_mapping` instead, because that tree carries
    pandas/scipy and cannot be imported from here.
    """
    names = [s.ticker for s in series_list]
    if len(names) < 2:
        return TickerGrouping(order=tuple(names), clusters=(), source=source if names else "none")
    matrix = _return_correlation_matrix(series_list)
    distance = _correlation_distance(matrix)
    root = _average_linkage(distance)
    if root is None:  # pragma: no cover - unreachable given the len check above
        return TickerGrouping(order=tuple(names), clusters=(), source=source)
    order = _order_leaves(root, distance)
    count = clusters if clusters and clusters > 0 else _auto_cluster_count(len(names))
    groups = _cut_clusters(root, min(count, len(names)))
    # Present clusters in ticker-axis order so band 0 is leftmost, which is what a
    # reader assumes when the legend is read top-to-bottom.
    position = {index: rank for rank, index in enumerate(order)}
    groups.sort(key=lambda members: min(position[m] for m in members))
    banded = tuple(
        {
            "id": cluster_id,
            "label": f"group {cluster_id + 1}",
            "tickers": [names[m] for m in sorted(members, key=lambda m: position[m])],
            "color": CLUSTER_COLORS[cluster_id % len(CLUSTER_COLORS)],
        }
        for cluster_id, members in enumerate(groups)
    )
    return TickerGrouping(order=tuple(names[i] for i in order), clusters=banded, source=source)


def _safe_color(value: Any, fallback: str) -> str:
    """Accept only ``#RRGGBB`` from an externally supplied grouping file.

    The viewer writes this straight into an inline ``background:`` declaration. A
    grouping file is untrusted input, and CSS is not merely cosmetic there: a value like
    ``url(https://example.invalid/x)`` would make the "self-contained, zero network
    requests" guarantee false the moment the page is opened. Attribute-escaping alone
    does not help, because the payload is legal CSS. Restricting the grammar does.
    """
    text = str(value or "")
    return text if _HEX_COLOR.match(text) else fallback


def grouping_from_mapping(
    mapping: Mapping[str, Any],
    series_list: Sequence[BarSeries],
) -> TickerGrouping:
    """Build a grouping from an externally supplied file.

    Accepts either shape:

    * the full export written by ``ml-forecaster/cluster_tickers.py`` -- an object with
      ``ticker_order`` and ``clusters`` -- which is used as-is;
    * a flat ``{"AAPL": "megacap-tech", ...}`` label mapping, where members are ordered
      within each group by the same return-correlation seriation used above.

    Tickers present in the data but absent from the mapping are appended in a trailing
    "ungrouped" band rather than dropped: silently omitting a ticker from a whole-market
    picture would misrepresent the market.
    """
    names = [s.ticker for s in series_list]
    present = set(names)

    raw_clusters = mapping.get("clusters")
    if isinstance(raw_clusters, list) and raw_clusters:
        banded = []
        claimed = set()
        # ONE INVARIANT for the whole document: a ticker appears at most once in the
        # grouping -- once in `clusters`, once in `ticker_order`. Both fields are copied
        # straight onto the viewer's ticker axis, and both were written by something
        # outside this repository, so neither can be assumed well formed.
        #
        # For `clusters` specifically, a ticker claimed by two bands broke the viewer
        # asymmetrically: `clusterOf[t]` is last-write-wins, so the ticker took the
        # LATER band's colour while BOTH legend buttons still advertised it -- hiding the
        # earlier band left it on screen, in a colour matching neither button. First
        # claim wins here, matching the first-occurrence rule `ticker_order` uses below;
        # the two fields are one rule, not two.
        for cluster_id, entry in enumerate(raw_clusters):
            if not isinstance(entry, Mapping):
                continue
            members = []
            for raw in entry.get("tickers") or []:
                ticker = str(raw).upper()
                if ticker in present and ticker not in claimed:
                    claimed.add(ticker)
                    members.append(ticker)
            # An entry emptied by that filtering is dropped exactly as an entry that
            # named nothing recognizable already is -- a band with no members left is
            # not a band, and drawing an empty legend row would invite a click that
            # hides nothing.
            if not members:
                continue
            banded.append(
                {
                    "id": len(banded),
                    "label": str(entry.get("label") or f"group {cluster_id + 1}"),
                    "tickers": members,
                    "color": _safe_color(
                        entry.get("color"), CLUSTER_COLORS[len(banded) % len(CLUSTER_COLORS)]
                    ),
                }
            )
        leftovers = [n for n in names if n not in claimed]
        if leftovers:
            banded.append(
                {
                    "id": len(banded),
                    "label": "ungrouped",
                    "tickers": leftovers,
                    "color": "#8a8f98",
                }
            )
        declared = [str(t).upper() for t in (mapping.get("ticker_order") or [])]
        # The axis half of the invariant above. A repeated symbol here drew the same
        # series twice in one Pulse scene and gave it two Wave rows, while the subtitle
        # and payload ticker list still counted it once. Dropping the repeat is the
        # conservative reading -- the file asked for that symbol, just more times than
        # there is a series to place.
        placed: set = set()
        order: List[str] = []
        for ticker in declared:
            if ticker in present and ticker not in placed:
                placed.add(ticker)
                order.append(ticker)
        order.extend(n for n in names if n not in placed)
        return TickerGrouping(
            order=tuple(order),
            clusters=tuple(banded),
            source=str(mapping.get("source") or "file"),
        )

    # Flat label mapping. Seriate within each label so the ordering inside a band is
    # still driven by behavior rather than by however the JSON happened to be keyed.
    # Keys are matched case-insensitively, against a normalized index built once.
    # `names` are already uppercase (every loader uppercases), so the previous
    # `mapping.get(name) or mapping.get(name.upper())` was the same lookup twice and a
    # lowercase-keyed document -- `{"aapl": "tech"}`, which nothing forbids -- matched
    # nothing and landed every ticker in "ungrouped". Same class as the rest of the
    # supplied-grouping handling: normalize the external document, once, up front.
    by_key = {}
    for key, value in mapping.items():
        normalized = key.strip().upper()
        if normalized not in by_key:      # first spelling wins, as elsewhere
            by_key[normalized] = value
    labels: Dict[str, List[str]] = {}
    for name in names:
        label = str(by_key.get(name) or "ungrouped")
        labels.setdefault(label, []).append(name)
    by_name = {s.ticker: s for s in series_list}
    banded = []
    order: List[str] = []
    for cluster_id, label in enumerate(sorted(labels)):
        members = labels[label]
        # The same O(n^3) cap the default path enforces, applied per band. Supplying a
        # grouping file is the documented way to AVOID the correlation pass on a large
        # universe, so silently running it here defeated the escape hatch -- and a sparse
        # mapping is the worst case, because every unmentioned symbol lands in one
        # "ungrouped" band that can hold the whole archive. Over the cap the band keeps
        # the order the document (and the loader) already put it in, which is a weaker
        # ordering but an instant one; refusing outright would be wrong, since the file
        # supplied the banding and only the within-band polish is being skipped.
        if 1 < len(members) <= MAX_SERIATION_TICKERS:
            inner = seriate_tickers([by_name[m] for m in members], clusters=1).order
        else:
            inner = tuple(members)
        order.extend(inner)
        banded.append(
            {
                "id": cluster_id,
                "label": label,
                "tickers": list(inner),
                "color": "#8a8f98" if label == "ungrouped" else CLUSTER_COLORS[cluster_id % len(CLUSTER_COLORS)],
            }
        )
    return TickerGrouping(order=tuple(order), clusters=tuple(banded), source="file")


# ---------------------------------------------------------------------------
# Normalization
#
# Raw units are incommensurable: volume runs to 1e8 while prices sit near 1e2. Plotted
# raw, every price channel collapses to a flat line against the volume wall. Everything
# below maps into a shared, unitless frame, and every divisor used is reported in the
# payload so the viewer can label axes in real units.
# ---------------------------------------------------------------------------


def _prior_closes(series: BarSeries) -> List[Optional[float]]:
    """The close preceding each bar; ``None`` for the first, which has no reference."""
    out: List[Optional[float]] = [None]
    out.extend(series.bars[i - 1].close for i in range(1, len(series.bars)))
    return out


def _rolling_highs(series: BarSeries, window: int) -> List[Optional[float]]:
    """Return each bar's rolling high, including the current bar.

    Including the current high makes the chart a true drawdown view: a bar's high is
    zero drawdown and its low/open/close show how far below that observed peak price
    traded. The window is measured in bars; daily chart sources therefore use trading
    sessions, while intraday sources use their available bars.
    """
    if window <= 0:
        raise ValueError("drawdown window must be positive")
    highs: List[Optional[float]] = []
    for index, _bar in enumerate(series.bars):
        candidates = [
            candidate.high
            for candidate in series.bars[max(0, index - window + 1) : index + 1]
            if candidate.high > 0 and math.isfinite(candidate.high)
        ]
        highs.append(max(candidates) if candidates else None)
    return highs


def _volume_channel(
    series_list: Sequence[BarSeries], whole_market: bool
) -> Tuple[Dict[str, List[Optional[float]]], Dict[str, Any]]:
    """Map volume onto ``[0, 1]``.

    Two regimes, because the question changes with the mode:

    ``whole_market``
        "Is *this* volume unusual for *this* name?" -- relative volume against the
        ticker's own window mean, log-scaled, then placed on a scale shared by every
        ticker. Absolute volume would simply sort the axis by market cap and tell you
        nothing.

    single/individual
        "How does today compare to the rest of this window?" -- a plain per-ticker
        log min-max, which uses the full axis range for one name.

    Log in both cases because volume is heavy-tailed; a single capitulation day would
    otherwise compress every ordinary day into the first few percent of the axis.

    Volume of zero means "not reported" -- quote-derived bars and pre-``bar_*`` intraday
    rows both carry it -- so those entries stay ``None`` rather than becoming 0.5.
    Mapping unknown volume to the axis centre would place a ticker with no volume data
    at exactly "average relative volume", which is a claim the data does not support,
    and would drag the market centroid toward it.
    """
    per_ticker: Dict[str, List[Optional[float]]] = {}
    if whole_market:
        relatives: Dict[str, List[Optional[float]]] = {}
        pool: List[float] = []
        for series in series_list:
            volumes = [b.volume for b in series.bars if b.volume > 0]
            average = _mean(volumes) if volumes else 0.0
            row: List[Optional[float]] = []
            for bar in series.bars:
                if average <= 0 or bar.volume <= 0:
                    row.append(None)
                else:
                    value = math.log10(bar.volume / average)
                    row.append(value)
                    pool.append(abs(value))
            relatives[series.ticker] = row
        scale = max(_percentile(pool, _SCALE_PERCENTILE), 0.05)
        for ticker, row in relatives.items():
            per_ticker[ticker] = [
                None if v is None else 0.5 + 0.5 * max(-1.0, min(1.0, v / scale)) for v in row
            ]
        return per_ticker, {"mode": "relative_log", "log10_span": scale}

    spans: Dict[str, Dict[str, float]] = {}
    for series in series_list:
        known = [b.volume for b in series.bars if b.volume > 0]
        if not known:
            per_ticker[series.ticker] = [None] * len(series.bars)
            spans[series.ticker] = {"log10_min": 0.0, "log10_max": 0.0}
            continue
        logs = [math.log10(v + 1.0) for v in known]
        low, high = min(logs), max(logs)
        spread = high - low
        per_ticker[series.ticker] = [
            None if b.volume <= 0
            else (0.5 if spread <= 0 else (math.log10(b.volume + 1.0) - low) / spread)
            for b in series.bars
        ]
        spans[series.ticker] = {"log10_min": low, "log10_max": high}
    return per_ticker, {"mode": "per_ticker_log", "spans": spans}


def build_payload(
    series_list: Sequence[BarSeries],
    *,
    mode: str = "both",
    whole_market: bool = False,
    grouping: Optional[TickerGrouping] = None,
    title: str = DEFAULT_TITLE,
    source: str = "",
    generated_at: Optional[str] = None,
    frame_start: Optional[date] = None,
    drawdown_window: Optional[int] = None,
) -> MarketGraphPayload:
    """Normalize loaded series into the render-ready payload both viewers consume.

    By default, the Y and Z channels are percent moves against the bar's prior close.
    With ``drawdown_window`` they instead show distance below each ticker's rolling
    high, which is useful for visually locating recent or major dips.

    ``frame_start`` drops frames earlier than the requested window from the emitted
    axis. The loaders keep one bar before it (see :func:`_window`) purely so the first
    requested session has a prior close to measure against; that bar is an input, not
    something to draw.
    """
    if not series_list:
        raise ValueError("no usable series to plot -- every requested ticker resolved empty")

    ordered = list(series_list)
    if grouping is not None and grouping.order:
        rank = {name: i for i, name in enumerate(grouping.order)}
        ordered.sort(key=lambda s: rank.get(s.ticker, len(rank)))

    # Shared frame axis keyed by FULL timestamp, not by calendar date. The intraday and
    # run-dir sources legitimately carry many bars per day, and collapsing them to a
    # date silently overwrote every poll but the last -- while the discarded ones still
    # moved the shared scale. Daily sources stamp midnight, so they are unaffected.
    #
    # A stamp earns a frame only when at least one series has a bar there with a usable
    # PRIOR CLOSE. A bar without one cannot be drawn -- every channel is a percent move
    # against that close -- so a stamp where no series has one is a frame nobody can see:
    # a blank opening scene in Pulse, a session count larger than the data, and a span
    # that starts before the first drawable observation.
    #
    # This is the general form of a bug I fixed twice in narrower shapes: round 3 kept
    # the pre-window baseline out of volume normalization, round 6 kept every series'
    # FIRST bar out of it, and the same priorless bar was still creating a frame here.
    # Filtering on drawability rather than on one enumerated reason covers all of them,
    # including an interior stamp that only stale bars touch.
    priors_by_ticker = {s.ticker: _prior_closes(s) for s in ordered}
    drawable = {
        bar.timestamp
        for s in ordered
        for bar, prior in zip(s.bars, priors_by_ticker[s.ticker], strict=True)
        if prior is not None and prior > 0
    }
    all_stamps = sorted(drawable)
    if frame_start is not None:
        all_stamps = [s for s in all_stamps if s.date() >= frame_start]
    if not all_stamps:
        # Two different failures, and telling them apart is the difference between
        # "widen the window" and "this data can never draw".
        if drawable:
            raise ValueError("no sessions fall inside the requested window")
        raise ValueError(
            "no bar has a prior close to measure against -- every series is a single "
            "observation, so there is nothing to draw a percent move from"
        )
    frame_index = {stamp: i for i, stamp in enumerate(all_stamps)}
    # Label frames as plain dates when every bar is a daily one, and as timestamps when
    # any carries a time -- otherwise an intraday axis reads as N identical dates.
    intraday = any(stamp.time() != time(0, 0) for stamp in all_stamps)

    # Pass one: collect the selected price metric to set the shared scale.
    percent: Dict[str, List[Optional[Tuple[float, float, float, float]]]] = {}
    magnitudes: List[float] = []
    for series in ordered:
        priors = priors_by_ticker[series.ticker]   # computed once, above, for the axis
        rolling_highs = _rolling_highs(series, drawdown_window) if drawdown_window else []
        row: List[Optional[Tuple[float, float, float, float]]] = [None] * len(all_stamps)
        for index, (bar, prior) in enumerate(zip(series.bars, priors, strict=True)):
            if prior is None or prior <= 0 or bar.timestamp not in frame_index:
                continue
            if drawdown_window:
                reference = rolling_highs[index]
                if reference is None or reference <= 0:
                    continue
                values = (
                    bar.low / reference - 1.0,
                    bar.high / reference - 1.0,
                    bar.open / reference - 1.0,
                    bar.close / reference - 1.0,
                )
            else:
                values = (
                    (bar.low - prior) / prior,
                    (bar.high - prior) / prior,
                    (bar.open - prior) / prior,
                    (bar.close - prior) / prior,
                )
            row[frame_index[bar.timestamp]] = values
            magnitudes.extend(abs(v) for v in values)
        percent[series.ticker] = row

    scale = max(_percentile(magnitudes, _SCALE_PERCENTILE), _MIN_SCALE)

    # Volume is normalized over the DRAWN bars only, and "drawn" is read off the price
    # pass above rather than guessed at: a bar contributes exactly when `percent` gave it
    # a cell. The price pass legitimately needs more bars than that -- the pre-window
    # baseline exists precisely to give the first rendered session a prior close -- but
    # anything without a cell is invisible, and an invisible bar shifting the per-ticker
    # log min/max or the whole-market mean makes the axis quietly untrue: two drawn
    # sessions with identical volume land at different X positions because of a bar the
    # reader cannot see.
    #
    # Testing `percent` rather than `frame_index` is the point. There are TWO ways to be
    # invisible and they need one rule, not one rule each:
    #
    #   outside the window -- the `--start` baseline bar, excluded earlier by frame_index;
    #   no prior close -- every series' FIRST bar, which is inside the window and was
    #     therefore still feeding the normalizer while never being drawn.
    #
    # The second is not an edge case: it is one bar per ticker on every render, so a
    # first-day volume outlier was skewing the whole scene by default.
    drawn = {
        s.ticker: {
            b.timestamp
            for b in s.bars
            if b.timestamp in frame_index and percent[s.ticker][frame_index[b.timestamp]] is not None
        }
        for s in ordered
    }
    visible = [
        BarSeries(
            ticker=s.ticker,
            bars=tuple(b for b in s.bars if b.timestamp in drawn[s.ticker]),
            source=s.source,
        )
        for s in ordered
    ]
    volume_channel, volume_meta = _volume_channel(visible, whole_market)

    # Pass two: emit per-ticker arrays indexed by the shared date axis. Parallel arrays
    # rather than one object per cell -- ~5x smaller JSON, and it maps directly onto
    # both the instanced glyph buffer (pulse) and the mesh grid (wave).
    payload_series: List[Dict[str, Any]] = []
    for series in visible:
        # Not .get(..., []): _volume_channel() is fed the same `visible` list this loop
        # walks, so a missing ticker is a bug worth raising on rather than silently
        # producing a series with no volume channel.
        volumes = volume_channel[series.ticker]
        volume_by_frame: Dict[int, Optional[float]] = {}
        for bar, value in zip(series.bars, volumes, strict=True):
            volume_by_frame[frame_index[bar.timestamp]] = value
        xs: List[Optional[float]] = []
        y_low: List[Optional[float]] = []
        y_high: List[Optional[float]] = []
        z_open: List[Optional[float]] = []
        z_close: List[Optional[float]] = []
        for frame in range(len(all_stamps)):
            cell = percent[series.ticker][frame]
            if cell is None:
                xs.append(None)
                y_low.append(None)
                y_high.append(None)
                z_open.append(None)
                z_close.append(None)
                continue
            low, high, open_, close = cell
            volume = volume_by_frame.get(frame)
            xs.append(None if volume is None else round(volume, 4))
            y_low.append(round(low / scale, 4))
            y_high.append(round(high / scale, 4))
            z_open.append(round(open_ / scale, 4))
            z_close.append(round(close / scale, 4))
        payload_series.append(
            {
                "ticker": series.ticker,
                "x": xs,
                "yl": y_low,
                "yh": y_high,
                "zo": z_open,
                "zc": z_close,
            }
        )

    if whole_market and len(payload_series) > 1:
        payload_series.append(_market_centroid(payload_series, len(all_stamps)))

    tickers = tuple(entry["ticker"] for entry in payload_series)
    resolved_grouping = grouping or TickerGrouping(order=tickers, clusters=(), source="none")
    # Intraday labels carry an explicit "Z", and every intraday stamp reaching here IS
    # UTC: _parse_timestamp() converts aware inputs, and reads a naive instant as Eastern
    # per this repo's one convention for a bare wall clock. Printed without the marker
    # they read as the Eastern trading clock the operator sees everywhere else, five
    # hours wrong for most of the year.
    #
    # The marker used to be applied to naive stamps that had NOT been converted, which
    # asserted UTC for values the comment here simultaneously admitted were not provably
    # UTC. Establishing the zone at parse time is what makes the claim true, rather than
    # softening the label and leaving the ambiguity for the reader.
    #
    # "Z" is filename-safe, so artifact_span() is unaffected.
    labels = tuple(
        stamp.isoformat(sep=" ", timespec="minutes") + "Z" if intraday else stamp.date().isoformat()
        for stamp in all_stamps
    )
    return MarketGraphPayload(
        title=title,
        dates=labels,
        tickers=tickers,
        series=tuple(payload_series),
        scales={
            "pct_per_unit": round(scale, 6),
            "volume": volume_meta,
            "percentile": _SCALE_PERCENTILE,
            "price_metric": "rolling_high_drawdown" if drawdown_window else "prior_close_move",
            **({"drawdown_window": drawdown_window} if drawdown_window else {}),
        },
        grouping=resolved_grouping,
        mode=mode,
        whole_market=whole_market,
        source=source or (series_list[0].source if series_list else ""),
        generated_at=generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        meta={"ticker_count": len(tickers), "frame_count": len(all_stamps), "intraday": intraday},
    )


def _market_centroid(payload_series: Sequence[Mapping[str, Any]], frames: int) -> Dict[str, Any]:
    """Equal-weight mean of every normalized channel -- the ripple's centre of mass.

    Rendered alongside the individual tickers so a reader can tell instantly whether a
    given name is moving *with* the market or *against* it, which is exactly the
    judgement the graded ladder's dip logic depends on.
    """
    out: Dict[str, Any] = {"ticker": MARKET_SERIES}
    for key in ("x", "yl", "yh", "zo", "zc"):
        column: List[Optional[float]] = []
        for frame in range(frames):
            values = [
                entry[key][frame]
                for entry in payload_series
                if entry[key][frame] is not None
            ]
            column.append(round(_mean(values), 4) if values else None)
        out[key] = column
    return out


# ---------------------------------------------------------------------------
# Serialization + HTML emission
# ---------------------------------------------------------------------------


def payload_to_dict(payload: MarketGraphPayload) -> Dict[str, Any]:
    """Plain-dict form of the payload, matching schemas/market_graph_payload.schema.json."""
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "title": payload.title,
        "generated_at": payload.generated_at,
        "source": payload.source,
        "mode": payload.mode,
        "whole_market": payload.whole_market,
        "dates": list(payload.dates),
        "tickers": list(payload.tickers),
        "series": [dict(entry) for entry in payload.series],
        "scales": payload.scales,
        "grouping": {
            "source": payload.grouping.source,
            "order": list(payload.grouping.order),
            "clusters": [dict(c) for c in payload.grouping.clusters],
        },
        "meta": payload.meta,
    }


def build_payload_json(payload: MarketGraphPayload, *, indent: Optional[int] = None) -> str:
    """Serialize the payload. Compact by default -- it is embedded in an HTML file."""
    return json.dumps(payload_to_dict(payload), indent=indent, sort_keys=True)


def viewer_asset_dir() -> Path:
    """Resolve the viewer asset directory across checkout and installed-wheel layouts.

    Order mirrors ``dca_config.get_config_dir()`` exactly: ``$DCA_VIZ_DIR`` ->
    ``viz/`` beside this module (checkout) -> the wheel's installed
    ``share/all-stock-dca/viz`` -> ``./viz`` in the working directory.
    """
    override = os.environ.get(VIZ_DIR_ENV)
    if override:
        return Path(override)
    for candidate in (Path(__file__).resolve().parent / "viz", _INSTALLED_VIZ_DIR, Path.cwd() / "viz"):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "viz"


def _read_asset(name: str) -> str:
    path = viewer_asset_dir() / name
    if not path.is_file():
        raise FileNotFoundError(
            f"viewer asset {name!r} not found under {viewer_asset_dir()} -- "
            f"set ${VIZ_DIR_ENV}, or check that pyproject ships viz/ as data-files"
        )
    return path.read_text(encoding="utf-8")


def build_viewer_html(
    payload: MarketGraphPayload,
    *,
    title: Optional[str] = None,
    self_contained: bool = True,
) -> str:
    """Assemble the single-file viewer.

    ``self_contained`` inlines the vendored three.js so the result opens from a
    ``file://`` URL with no network access at all -- which is the point: these get
    emailed, dropped in a Drive folder, and opened on machines that have never seen
    this repository. With it off, the ``<script>`` is left empty and the page expects a
    ``THREE`` global to be supplied some other way (used only by tests that do not want
    to shuttle 600 KB around).
    """
    shell = _read_asset("market_graph.html.tmpl")
    page_title = title or payload.title or DEFAULT_TITLE
    replacements = {
        "TITLE": _escape_html(page_title),
        "CSS": _read_asset("market_graph.css"),
        "VIEWER_JS": _read_asset("market_graph.js"),
        "THREE_JS": _read_asset("vendor/three.min.js") if self_contained else "",
        "PAYLOAD": _escape_script(build_payload_json(payload)),
    }

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in replacements:
            raise KeyError(f"unknown placeholder {{{{{name}}}}} in market_graph.html.tmpl")
        return replacements[name]

    # ONE pass, never a chain of .replace() calls. Sequential replacement re-scans text
    # it has already substituted, so a --title of "{{CSS}}" would be inserted by the
    # title step and then expanded by the stylesheet step -- injecting the entire
    # stylesheet into the <title> and corrupting the page. re.sub's replacement
    # function output is never re-examined, so substituted values are inert by
    # construction, whatever a ticker name or title happens to contain.
    return _PLACEHOLDER.sub(_substitute, shell)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _escape_script(text: str) -> str:
    """Neutralize every ``<`` so nothing can start an HTML token inside <script>.

    A ticker name or cluster label is externally supplied text; ``</script>`` inside it
    would end the block early and spill the rest of the payload into the document as
    markup.

    Escaping the ``<`` itself rather than the two-character sequences is the point.
    ``\\u003c`` is a valid JSON escape for the same character, so ``JSON.parse`` still
    yields the original string, while no case variant (``</SCRIPT>``) and no comment
    opener (``<!--``) survives as literal markup. An earlier version wrote ``<\\!--``,
    which is not a valid JSON escape at all and made the whole payload unparseable the
    moment a label contained a comment opener.
    """
    return text.replace("<", "\\u003c")


def write_market_graph(
    series_list: Sequence[BarSeries],
    output_dir: Path | str,
    *,
    mode: str = "both",
    whole_market: bool = False,
    grouping: Optional[TickerGrouping] = None,
    title: str = DEFAULT_TITLE,
    source: str = "",
    self_contained: bool = True,
) -> Dict[str, str]:
    """Build and write the viewer plus its payload, returning ``{name: path}``.

    Follows the house report-writer contract (see ``dca_reporting.generate_reports``):
    pure builders above, one writer that mkdirs, writes atomically, and hands back
    logical-name -> path so a caller can record the artifacts it produced.
    """
    from dca_fileio import atomic_write_text

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = build_payload(
        series_list, mode=mode, whole_market=whole_market, grouping=grouping,
        title=title, source=source,
    )
    html_path = out / "market_graph.html"
    json_path = out / "market_graph_payload.json"
    atomic_write_text(html_path, build_viewer_html(payload, title=title, self_contained=self_contained))
    atomic_write_text(json_path, build_payload_json(payload, indent=2) + "\n")
    return {"viewer_html": str(html_path), "payload_json": str(json_path)}


def load_groups_file(path: Path | str) -> Dict[str, Any]:
    """Read a grouping export (or flat label mapping) written by another tool."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"grouping file must contain a JSON object: {path}")
    return data


# ---------------------------------------------------------------------------
# Shared CLI policy
#
# Everything below is front-end policy rather than rendering: which tickers a source
# defaults to, what counts as a valid trail, when a grouping is too expensive to
# attempt, how a span becomes a filename. It lives HERE, in the stdlib-only module both
# front ends already import, because there are two of them -- scripts/generate_market_
# graph.py and analysis/market-graph-3d/render_market_graph.py -- and a policy fixed in
# one is a policy still broken in the other. CLAUDE.md documents that exact failure mode
# for the three strategy entry points; this is the same hazard in miniature, and the
# review that prompted this extraction found four separate instances of it.
# ---------------------------------------------------------------------------


def active_tickers_from_config() -> List[str]:
    """The strategy's active, TRADABLE universe, for ``--tickers @config``.

    Read through the shared resolver so this honours ``$DCA_CONFIG_DIR`` and the
    installed-wheel layout like everything else.

    ``excluded`` is subtracted from ``active``. The ticker config permits a symbol to
    appear in both -- and when it does, the live strategy will not trade it, and
    ``ml-forecaster/train.py::_load_active_tickers`` omits it. A chart that drew a name
    the strategy refuses to buy would be describing a market the operator cannot act on,
    and would also disagree with the grouping export that skipped it.

    ``whole_share`` is INCLUDED (appended, not subtracted) -- a whole-share ticker (see
    ``docs/whole_share_accumulation.md``) is evaluated by the SAME graded ladder and
    actively purchased by the afternoon stream, just via a quantity-based order once its
    accumulated contributions cross one share's price, rather than a normal fractional
    buy. Omitting it here would silently drop a ticker the strategy genuinely trades from
    the default archive graph, contradicting this function's own "active, TRADABLE
    universe" purpose and disagreeing with every other whole-share-aware consumer
    (``ticker-advisor``, ``scripts/refresh_active_ticker_histories.py``,
    ``scripts/run_tickers_daily_update.py``) (Codex review: "Include whole-share symbols
    in market-graph config defaults").
    """
    from dca_config import get_config_dir

    path = Path(get_config_dir()) / "tickers.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    # Structure is validated, not assumed, and every failure is a ValueError so the
    # CLIs' (OSError, ValueError) boundary actually catches it. Valid JSON is not a
    # valid CONFIG: a bare `[]` makes `.get` an AttributeError and `{"excluded": null}`
    # makes the comprehension a TypeError, and neither is in that tuple -- so the
    # traceback the boundary was added to prevent came back through a different door.
    # ml-forecaster/train.py::_load_active_tickers already validated exactly this shape;
    # this is that check, applied here too rather than left as a one-sided precedent.
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object, got {type(data).__name__}")
    active = data.get("active", [])
    excluded_raw = data.get("excluded", [])
    whole_share_raw = data.get("whole_share", [])
    for key, value in (("active", active), ("excluded", excluded_raw), ("whole_share", whole_share_raw)):
        # `str` is excluded explicitly: it is iterable, so a bare "AAPL" would silently
        # become the four tickers A, A, P, L rather than raising.
        if not isinstance(value, list):
            raise ValueError(f"{path}'s {key!r} field must be a list, got {type(value).__name__}")
    excluded = {str(t).upper() for t in excluded_raw}
    tickers = [str(t).upper() for t in active if str(t).upper() not in excluded]
    seen = set(tickers)
    for t in whole_share_raw:
        symbol = str(t).upper()
        if symbol not in seen and symbol not in excluded:
            tickers.append(symbol)
            seen.add(symbol)
    return tickers


def resolve_universe(
    source: str,
    tickers: Optional[Sequence[str]],
    *,
    all_tickers_in_archive: bool = False,
) -> Optional[List[str]]:
    """Apply the per-source default ticker roster.

    Only the archive has a default, and it needs one: it is the whole US universe rather
    than this strategy's, so loading it wholesale is slow, almost never what was meant,
    and feeds an O(n^3) seriation. ``--all-tickers-in-archive`` is the explicit opt-out,
    mirroring ``ml-forecaster/train.py``'s identical choice.

    ``None`` (no filter) and ``[]`` (an empty request) are kept distinct throughout: they
    mean opposite things, and collapsing them turns "asked for nothing" into "render
    everything".
    """
    if tickers is not None:
        return list(tickers)
    if source == "archive" and not all_tickers_in_archive:
        return active_tickers_from_config()
    return None


def _as_int(value: Any, name: str) -> int:
    """Strict integer coercion for the CLI validators below.

    A non-integral float is rejected rather than truncated: ``int(1.5) == 1`` would let
    a caller ask for one thing and silently receive another, which is the exact failure
    these validators exist to prevent. Argparse always hands over a string, so this only
    bites library callers -- which is precisely who would never see it otherwise.
    """
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} must be an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def validate_trail(value: Any) -> int:
    """Coerce and range-check a pulse trail length. Raises ``ValueError`` when unusable."""
    parsed = _as_int(value, "trail")
    if not 1 <= parsed <= MAX_TRAIL:
        raise ValueError(f"trail must be between 1 and {MAX_TRAIL}, got {parsed}")
    return parsed


def validate_positive_int(value: Any, name: str) -> int:
    """Coerce and floor-check a count. Raises ``ValueError`` when unusable.

    The generalization of :func:`validate_trail` to every other numeric CLI option
    here, and it exists because the "accepted, then silently produces something other
    than what was asked for" failure kept reappearing one option at a time: ``--trail
    0`` rendered a blank pulse, ``--fps 0`` died inside PillowWriter's ``1000 / fps``
    after the expensive part, ``--stride 0`` raised from ``range``, and ``--clusters 0``
    was accepted and then quietly treated as though the flag had been omitted. Every
    count-shaped option in both CLIs and in ml-forecaster/cluster_tickers.py routes
    through here now, so the next one added inherits the refusal instead of the bug.

    ``--frame`` is the one deliberate exception: a negative index there means "counting
    back from the end", which is a real request rather than a malformed one.
    """
    parsed = _as_int(value, name)
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer, got {parsed}")
    return parsed


def validate_payload_document(document: Any) -> Dict[str, Any]:
    """Check an externally supplied payload before anything renders from it.

    The browser viewer refuses an unknown ``schema_version`` outright (see
    market_graph.js), for the good reason that the array semantics -- what ``yh``/``zo``
    index, what ``null`` means -- are versioned. An offline renderer reading a payload
    from a future version without checking would not fail; it would draw a plausible,
    confidently wrong picture. Both consumers now apply the same gate.
    """
    if not isinstance(document, Mapping):
        raise ValueError(f"payload must be a JSON object, got {type(document).__name__}")
    version = document.get("schema_version")
    if version != PAYLOAD_SCHEMA_VERSION:
        raise ValueError(
            f"payload schema_version {version!r} is not supported "
            f"(this build reads version {PAYLOAD_SCHEMA_VERSION})"
        )
    for key, kind in (("dates", list), ("tickers", list), ("series", list)):
        if not isinstance(document.get(key), kind):
            raise ValueError(f"payload is missing a usable {key!r} list")
        # Non-empty, not merely present. An empty document is well-formed JSON that
        # nothing downstream can survive: artifact_span() indexes dates[0], and the
        # matplotlib renderer slices a (0, N) array into plot_surface for a cryptic
        # shape error. Refusing here turns three obscure crashes into one clear message.
        if not document[key]:
            raise ValueError(f"payload {key!r} list is empty; there is nothing to draw")
    # Frame labels are checked against the documented shape, not merely for being
    # strings. artifact_span() sanitizes them defensively because it must hold for every
    # caller, but a document whose dates are not dates is malformed and deserves to be
    # SAID SO rather than quietly renamed into something that happens to be safe -- a
    # sanitized `../../escape` writes to a real directory under a confusing name.
    for label in document["dates"]:
        if not isinstance(label, str) or not _FRAME_LABEL.match(label):
            raise ValueError(
                f"payload has a malformed frame label {label!r}; expected YYYY-MM-DD "
                "or 'YYYY-MM-DD HH:MMZ'"
            )

    # The nested shape too, not only the three top-level containers. A `series` entry
    # missing a channel -- {"schema_version":1,"dates":["2024-01-01"],"tickers":["A"],
    # "series":[{}]} -- passed every check above and then _real_tickers() raised
    # KeyError, which is the exact traceback this function exists to turn into a
    # documented exit 1. Checked against schemas/market_graph_payload.schema.json's own
    # required fields, so this validator and the schema cannot silently drift apart.
    frame_count = len(document["dates"])
    channels = ("x", "yl", "yh", "zo", "zc")
    for entry in document["series"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("ticker"), str):
            raise ValueError(f"payload has a series entry missing a string 'ticker': {entry!r}")
        for channel in channels:
            values = entry.get(channel)
            if not isinstance(values, list) or len(values) != frame_count:
                raise ValueError(
                    f"payload series {entry.get('ticker')!r} channel {channel!r} must be a "
                    f"list of {frame_count} value(s) (one per date), got {values!r}"
                )
            if not all(v is None or isinstance(v, (int, float)) for v in values):
                raise ValueError(
                    f"payload series {entry.get('ticker')!r} channel {channel!r} must "
                    "contain only numbers and nulls"
                )
    scales = document.get("scales")
    if not isinstance(scales, Mapping) or not isinstance(scales.get("pct_per_unit"), (int, float)):
        raise ValueError("payload is missing a usable 'scales.pct_per_unit'")
    grouping = document.get("grouping")
    if not isinstance(grouping, Mapping) or not isinstance(grouping.get("order"), list):
        raise ValueError("payload is missing a usable 'grouping.order'")
    if not isinstance(document.get("title"), str):
        raise ValueError("payload is missing a usable 'title'")
    return dict(document)


def seriate_or_refuse(
    series_list: Sequence[BarSeries], *, clusters: Optional[int] = None
) -> TickerGrouping:
    """:func:`seriate_tickers`, but refuse rather than grind on an oversized universe."""
    if len(series_list) > MAX_SERIATION_TICKERS:
        raise ValueError(
            f"return-correlation grouping is O(n^3) and refuses {len(series_list)} tickers "
            f"(limit {MAX_SERIATION_TICKERS}). Narrow the universe with --tickers, pass "
            "--group-source none, or generate a grouping with "
            "ml-forecaster/cluster_tickers.py and use --group-source file."
        )
    return seriate_tickers(series_list, clusters=clusters)


def artifact_span(payload: MarketGraphPayload) -> str:
    """A filesystem-safe ``<first>_<last>`` span for default output paths.

    Taken from the payload's own frame axis rather than from any one ticker: with
    staggered histories those differ, so a per-ticker span would mislabel the artifact
    and let two genuinely different windows collide on one path.

    Intraday labels carry "YYYY-MM-DD HH:MMZ"; the colon is invalid in a Windows filename
    and the space is merely unpleasant, so both are translated out.

    The return value is a single path COMPONENT, never a path. ``default_output_dir()``
    passes it to ``stream_root()``, and a payload's ``dates`` can come from a
    ``--payload`` document this process did not write -- where ``"../../escape"`` or an
    absolute ``"/etc/passwd"`` would otherwise walk straight out of
    ``$DCA_CRON_TRACKING_ROOT`` and create directories elsewhere. Sanitizing here rather
    than at one call site is deliberate: both CLIs and the matplotlib renderer build
    paths from this, and the guarantee has to hold for all of them.
    """
    if not payload.dates:
        # Public helper, so it says what is wrong rather than raising IndexError from a
        # bare subscript. build_payload() cannot produce this, but a --payload document
        # can, and this is reachable before validate_payload_document() in any caller
        # that builds a payload object by hand.
        raise ValueError("payload has no frames; there is no span to name")
    return f"{_path_component(payload.dates[0])}_{_path_component(payload.dates[-1])}"


def _path_component(label: str) -> str:
    """One frame label, reduced to something that cannot be anything but a name."""
    cleaned = _SAFE_IN_PATH_COMPONENT.sub("-", label.translate(_UNSAFE_IN_FILENAMES))
    # "." and ".." survive the character allowlist and still traverse, and an empty
    # component silently collapses the path segment. Any of the three becomes a literal.
    if cleaned.strip(".") == "":
        return "unnamed"
    return cleaned


def default_output_dir(payload: MarketGraphPayload) -> Path:
    """Where a graph goes when ``--out`` is omitted.

    Under the configured artifact root (``$DCA_CRON_TRACKING_ROOT``), not the process's
    working directory. A relative default puts the artifact wherever the invocation
    happened to start -- unfindable for a scheduled run, and unwritable in some
    deployments -- whereas the shared root is exactly the knob operators use to relocate
    and then locate this kind of output.

    ``market_graph/`` sits beside the other non-run tool outputs already living there
    (``ml_forecaster/models/``, ``per_ticker_calibration/``, ``ticker_history_update/``),
    and beside ``market_graph_groups/``, which this feature's own grouping exporter
    writes. Run-classification and drift detection key off per-stream run directories
    and their fixed artifact filenames, so a sibling directory of HTML is invisible to
    them.
    """
    from dca_paths import stream_root

    return Path(stream_root("market_graph", artifact_span(payload)))


def summarize_resolution(requested: Optional[Sequence[str]], resolved: Iterable[BarSeries]) -> str:
    """One-line human summary of how much of the request actually produced data.

    Worth surfacing loudly: the committed CSV covers only a handful of symbols, so a
    whole-market render against it is thin, and a reader deserves to know that rather
    than concluding the market was quiet.
    """
    names = [s.ticker for s in resolved]
    if requested is None:
        return f"resolved {len(names)} ticker(s)"
    wanted = _wanted(requested) or set()
    missing = sorted(wanted - set(names))
    text = f"resolved {len(names)}/{len(wanted)} requested ticker(s)"
    if missing:
        shown = ", ".join(missing[:8])
        if len(missing) > 8:
            shown += f", +{len(missing) - 8} more"
        text += f"; no usable history for: {shown}"
    return text
