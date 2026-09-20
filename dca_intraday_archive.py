"""Durable intraday observation archive for every scheduled DCA poll.

Each scheduled slot (9:35 market-open, 9:45 pre-flight, 10:30 morning,
11:30/13:30/14:30 intraday, 12:30 midday, 15:30 near-close) already prices every
ticker in the active universe in order to grade it. Before this module those
prices lived only inside that one run's ``*_signals.csv`` -- scattered across
``cron_tracking/<stream>/YYYYMMDD_<slot>ET/``, keyed by nothing, and carrying no
timestamp for the price itself (only the run's ``generated_at``). Reconstructing
"what did AMD trade at, at each poll, over the last month" meant walking dozens
of run directories and inferring the observation time from the folder name.

This module writes the same numbers a second time in a shape built for later
analysis: one append-only CSV per ticker, mirroring the daily OHLCV archive in
``ticker_data/`` and sitting alongside it under ``ticker_data/_intraday/``. The
daily archive answers "what did this ticker do each day"; this one answers "what
did it do at each poll within the day", including the intraday bar OHLCV and
volume the daily file cannot carry.

    ticker_data/AMD.csv            <- daily OHLCV (one row per trading day)
    ticker_data/_intraday/AMD.csv  <- this archive (one row per poll per day)

**Write semantics.** ``append_observations`` is idempotent on the natural key
``(trading_day, stream, slot, ticker)`` for a scheduled row -- ``source`` is
deliberately excluded so a retry that resolves its price from an alternate
provider still REPLACES that slot's prior row rather than appending a
duplicate -- plus ``run_id`` for a manual row, since manual polls have no
fixed slot to own and every legitimate manual rerun must land as its own row.
The archive stays one-row-per-scheduled-poll even when a run is repaired.
Rows are kept sorted by ``(trading_day, observed_at_utc, stream, slot, source)``.

**Never fatal.** Archiving is observability, not execution. Callers are expected
to use :func:`archive_observations_safely`, which converts any failure into a
returned warning string instead of an exception -- a Drive sync hiccup or a full
disk must never block or fail a live trading run.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from dca_fileio import atomic_write_text
from dca_ticker_paths import configured_ticker_history_dir
from scripts.file_lock import cross_process_lock

EASTERN = ZoneInfo("America/New_York")

#: Subdirectory of the ticker-history archive that holds the intraday files.
#: Underscore-prefixed to match the archive's existing convention for entries
#: that are not themselves a ``<TICKER>.csv`` daily history (``_delisted.txt``,
#: ``_update_progress.json``, ...), so the daily updater's ``*.csv`` scan of the
#: archive root never mistakes an intraday file for a daily one.
INTRADAY_SUBDIR = "_intraday"

#: Stable column order. Append-only: new columns go at the END so a file written
#: by an older revision still parses, and readers key by name rather than index.
OBSERVATION_FIELDS: Tuple[str, ...] = (
    # --- identity: which poll produced this row -----------------------------
    "trading_day",
    "stream",
    "slot",
    "observed_at_et",
    "observed_at_utc",
    "run_id",
    "run_type",
    "manual_run",
    "source",
    # --- the price this poll decided on -------------------------------------
    "price",
    "price_source",
    "price_as_of",
    # --- reference levels it was graded against -----------------------------
    "previous_close",
    "previous_close_as_of",
    "previous_low",
    "drop_pct",
    # --- intraday bar OHLCV behind the price (what daily OHLCV cannot show) --
    "bar_timestamp",
    "bar_open",
    "bar_high",
    "bar_low",
    "bar_close",
    "bar_volume",
    "session_open",
    "session_high",
    "session_low",
    "session_volume",
    "bar_count",
    "interval_minutes",
    # --- live quote, when the provider supplied one distinct from the bar ----
    "quote_price",
    "quote_time",
    # --- what the strategy concluded (context for later calibration) --------
    "action",
    "multiplier",
    "data_source",
    "warnings",
)

#: Natural key. A second write of the same key replaces the first.
#:
#: ``source`` is deliberately NOT part of scheduled identity: a retry or
#: recovery of the same scheduled slot can resolve its price from an
#: alternate provider, which would otherwise make the natural key differ and
#: the retry get appended as a SECOND row instead of replacing the original
#: -- violating the module's one-row-per-poll contract and double-weighting
#: a repaired slot in downstream time-series analysis (Codex review:
#: "Replace scheduled observations when the source changes"). ``source``
#: stays present as ordinary row data; manual identity folds in ``run_id``
#: separately in :func:`_natural_key` for the opposite reason -- see there.
#:
#: ``run_type`` IS part of scheduled identity: the 9:45 ET scheduler invokes
#: MorningDCAExecutor twice back to back -- once in preflight mode, once in
#: watchlist mode -- and both produce scheduled (manual_run=false) rows with
#: the SAME (trading_day, stream='morning', slot='0945', ticker), so without
#: run_type one sub-invocation's archive write silently replaces the other's
#: distinct action/context (Codex review: "Keep 9:45 archive rows distinct
#: by run type"). run_type is stable across a genuine retry of the SAME
#: sub-invocation, so including it does not weaken the retry-replaces
#: contract for every other scheduled poll (each has exactly one run_type).
_KEY_FIELDS: Tuple[str, ...] = ("trading_day", "stream", "slot", "ticker", "run_type")

#: Sort order within a ticker file.
_SORT_FIELDS: Tuple[str, ...] = (
    "trading_day",
    "observed_at_utc",
    "stream",
    "slot",
    "source",
)


def configured_intraday_archive_dir() -> Path:
    """Return the intraday archive directory.

    ``DCA_INTRADAY_ARCHIVE_DIR`` overrides it outright; otherwise it is the
    ``_intraday`` subdirectory of whatever :func:`configured_ticker_history_dir`
    resolves to, so the intraday rows follow the daily archive wherever
    ``DCA_TICKER_HISTORY_DIR`` points it.
    """
    override = os.environ.get("DCA_INTRADAY_ARCHIVE_DIR")
    if override:
        return Path(override).expanduser()
    return configured_ticker_history_dir() / INTRADAY_SUBDIR


@dataclass(frozen=True)
class IntradayObservation:
    """One ticker's price at one scheduled poll."""

    ticker: str
    trading_day: str
    stream: str
    slot: str
    observed_at_et: str
    observed_at_utc: str
    run_id: str = ""
    run_type: str = ""
    manual_run: Optional[bool] = None
    source: str = ""
    price: Optional[float] = None
    price_source: str = ""
    price_as_of: str = ""
    previous_close: Optional[float] = None
    previous_close_as_of: str = ""
    previous_low: Optional[float] = None
    drop_pct: Optional[float] = None
    bar_timestamp: str = ""
    bar_open: Optional[float] = None
    bar_high: Optional[float] = None
    bar_low: Optional[float] = None
    bar_close: Optional[float] = None
    bar_volume: Optional[float] = None
    session_open: Optional[float] = None
    session_high: Optional[float] = None
    session_low: Optional[float] = None
    session_volume: Optional[float] = None
    bar_count: Optional[int] = None
    interval_minutes: Optional[int] = None
    quote_price: Optional[float] = None
    quote_time: str = ""
    action: str = ""
    multiplier: Optional[float] = None
    data_source: str = ""
    warnings: str = ""

    def to_row(self) -> Dict[str, Any]:
        row = {field: getattr(self, field) for field in OBSERVATION_FIELDS}
        row["ticker"] = self.ticker
        return row


# --- serialization helpers ---------------------------------------------------


def _iso_or_blank(value: Any) -> str:
    """Render a datetime (or an already-ISO string) as ISO 8601, else ``""``."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _bool_or_none(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _cell(value: Any) -> str:
    """Render one value for the CSV. ``None`` becomes an empty cell, not "None"."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# Matches everything except ASCII letters/digits and the literal separators we
# allow through unsanitized. Anything caught here still gets a second look in
# `_replace_non_filename_char` below, because `str.isalnum()` is Unicode-aware
# (e.g. "É" or "東") and the ASCII-only regex class can't express that — a
# naive `sub('_', ...)` here would silently rewrite Unicode tickers to `_`
# and collapse distinct symbols onto the same archive file.
_TICKER_FILENAME_PATTERN = re.compile(r"[^a-zA-Z0-9\-_.^]")


def _replace_non_filename_char(match: "re.Match[str]") -> str:
    char = match.group(0)
    return char if char.isalnum() else "_"


def _safe_ticker_filename(ticker: str) -> str:
    """Map a ticker to a filename, refusing anything that could escape the dir.

    Tickers reach this module from config and from parsed artifacts, so a
    malformed or hostile symbol must not be able to write outside the archive.
    """
    cleaned = _TICKER_FILENAME_PATTERN.sub(_replace_non_filename_char, str(ticker).strip())
    cleaned = cleaned.strip(".")
    if not cleaned:
        raise ValueError(f"ticker {ticker!r} has no filename-safe characters")
    return f"{cleaned.upper()}.csv"


# --- reading -----------------------------------------------------------------


def read_ticker_observations(
    ticker: str, *, archive_dir: Optional[Path | str] = None
) -> List[Dict[str, str]]:
    """Return every archived observation for one ticker, in file order.

    Returns ``[]`` when the ticker has no archive file yet.
    """
    directory = Path(archive_dir) if archive_dir is not None else configured_intraday_archive_dir()
    path = directory / _safe_ticker_filename(ticker)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        _reader = csv.reader(handle)
        try:
            _header = next(_reader)
        except StopIteration:
            return []

        _rows = []
        for _row in _reader:
            _rows.append(dict(zip(_header, _row)))  # noqa: B905
        return _rows


def _sort_key(row: Mapping[str, Any]) -> Tuple[str, ...]:
    return tuple(str(row.get(field) or "") for field in _SORT_FIELDS)


def _natural_key(row: Mapping[str, Any], ticker: str) -> Tuple[str, ...]:
    values = []
    for field in _KEY_FIELDS:
        values.append(ticker if field == "ticker" else str(row.get(field) or ""))
    if _bool_or_none(row.get("manual_run")):
        # A manual run does not own a fixed scheduled slot the way a
        # scheduled poll does: two legitimate manual polls on the same
        # trading day normally share stream/slot/ticker/source, so without
        # this the base key would treat the second poll as a retry of the
        # first and silently replace it even though the caller explicitly
        # archives every manual run (Codex review: "Preserve distinct manual
        # observations"). run_id is unique per manual invocation (see
        # MorningDCAExecutor.run_id's random suffix), so folding it in here
        # keeps each manual poll's row distinct while leaving a genuine
        # scheduled retry -- which reuses the SAME run_id/slot on purpose --
        # replacing its slot's prior row as before.
        values.append(str(row.get("run_id") or ""))
    return tuple(values)


# --- writing -----------------------------------------------------------------


def append_observations(
    observations: Sequence[IntradayObservation],
    *,
    archive_dir: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Merge ``observations`` into the per-ticker archive files.

    Rows are grouped by ticker and merged into ``<archive>/<TICKER>.csv``,
    replacing any existing row with the same natural key (see
    :func:`_natural_key`) so a retry or rebuild of a slot never duplicates
    it, even when the retry resolved its price from a different source.

    Returns a summary dict (``tickers_written``, ``rows_added``,
    ``rows_replaced``, ``archive_dir``) for the caller to fold into run_state.
    Raises on I/O failure; use :func:`archive_observations_safely` when the
    caller must not fail.
    """
    # The default-archive guard below only protects the case where this call
    # is truly falling through to configured_ticker_history_dir()'s own
    # ticker_data-derived location: a caller that either passes archive_dir
    # explicitly OR has set DCA_INTRADAY_ARCHIVE_DIR (configured_intraday_
    # archive_dir()'s own override, used by every test in this repo to
    # isolate archive writes into a tmp dir) is deliberately pointing writes
    # somewhere else entirely, and configured_ticker_history_dir()'s
    # existence says nothing about whether THAT location is available.
    using_ticker_data_derived_default = archive_dir is None and not os.environ.get(
        "DCA_INTRADAY_ARCHIVE_DIR"
    )
    directory = Path(archive_dir) if archive_dir is not None else configured_intraday_archive_dir()
    by_ticker: Dict[str, List[IntradayObservation]] = {}
    for observation in observations:
        if not observation.ticker:
            continue
        by_ticker.setdefault(str(observation.ticker).upper(), []).append(observation)
    if not by_ticker:
        return {
            "archive_dir": str(directory),
            "tickers_written": 0,
            "rows_added": 0,
            "rows_replaced": 0,
        }

    if using_ticker_data_derived_default and not configured_ticker_history_dir().is_dir():
        # ticker_data/ is a gitignored symlink/mount to the managed Google
        # Drive archive (CLAUDE.md: "Do not modify" -- it is operational
        # data, not something this repo owns). If that link is missing,
        # unmounted, or broken, mkdir(parents=True) below would silently
        # materialize a REAL local ticker_data/_intraday directory (and
        # ticker_data/ itself) as a side effect of ordinary live archiving.
        # A later nightly updater or calibration run checking
        # configured_ticker_history_dir().is_dir() (e.g.
        # daily_per_ticker_calibration's archive-availability guard) would
        # then see that freshly-created empty stub and treat the real,
        # managed archive as available when it never was (Codex review:
        # "Don't create a local archive when the Drive link is absent").
        # Report unavailable instead of creating anything.
        raise FileNotFoundError(
            f"ticker history archive root {configured_ticker_history_dir()} does not exist "
            "(missing or unmounted Drive link, or this package is not running from a "
            "repository checkout?) -- refusing to create a local stand-in. Set "
            "DCA_TICKER_HISTORY_DIR to the shared archive location."
        )
    directory.mkdir(parents=True, exist_ok=True)
    rows_added = 0
    rows_replaced = 0
    for ticker, ticker_observations in sorted(by_ticker.items()):
        path = directory / _safe_ticker_filename(ticker)
        lock_path = path.with_suffix(".lock")
        # A scheduled retry, a manual run, and a backfill can all target the
        # same ticker file around the same time. Atomic replacement keeps any
        # single write from producing a torn file, but two processes racing
        # through read -> merge -> write independently can each read the same
        # starting rows, append their own observation, and whichever finishes
        # last silently discards the other's row -- a lost update atomic
        # replacement alone does not prevent (Codex review: "Lock each
        # archive read-modify-write"). Serializing the whole critical section
        # per ticker closes that gap.
        with cross_process_lock(lock_path):
            existing = read_ticker_observations(ticker, archive_dir=directory)
            # Keyed dict, so two observations for the same ticker/slot inside
            # ONE batch collapse to the last one rather than both landing on
            # disk.
            incoming = {
                _natural_key(obs.to_row(), ticker): obs.to_row() for obs in ticker_observations
            }
            kept: List[Dict[str, Any]] = []
            replaced_here = 0
            for row in existing:
                if _natural_key(row, ticker) in incoming:
                    replaced_here += 1
                    continue
                kept.append(row)
            rows_replaced += replaced_here
            rows_added += len(incoming) - replaced_here
            merged = kept + list(incoming.values())
            merged.sort(key=_sort_key)
            _write_ticker_file(path, merged)
    return {
        "archive_dir": str(directory),
        "tickers_written": len(by_ticker),
        "rows_added": rows_added,
        "rows_replaced": rows_replaced,
    }


def _write_ticker_file(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Atomically rewrite one ticker's archive file with ``rows``."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(OBSERVATION_FIELDS), restval="", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _cell(row.get(field)) for field in OBSERVATION_FIELDS})
    atomic_write_text(path, buffer.getvalue())


def archive_observations_safely(
    observations: Sequence[IntradayObservation],
    *,
    archive_dir: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """:func:`append_observations`, but a failure is reported, never raised.

    Every live-execution caller uses this: the archive is analysis data, and a
    Drive sync stall, a read-only mount, or a full disk must degrade to a
    warning on the run_state rather than fail a run that priced real money.
    """
    try:
        summary = append_observations(observations, archive_dir=archive_dir)
    except Exception as exc:  # noqa: BLE001 - deliberately total; see docstring
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "tickers_written": 0,
            "rows_added": 0,
            "rows_replaced": 0,
        }
    summary["ok"] = True
    return summary


def write_run_snapshot(
    output_dir: Path | str, observations: Sequence[IntradayObservation]
) -> Optional[str]:
    """Write this run's observations as ``observations.csv`` in its run directory.

    A per-run copy of exactly what the run archived, so the run's own artifact
    set stays self-describing even if the shared archive is unavailable (and so
    a backfill can be rebuilt from run directories alone). Returns the path, or
    ``None`` if it could not be written.
    """
    out = Path(output_dir)
    path = out / "observations.csv"
    try:
        out.mkdir(parents=True, exist_ok=True)
        rows = [obs.to_row() for obs in observations]
        buffer = io.StringIO()
        fields = ("ticker",) + OBSERVATION_FIELDS
        writer = csv.DictWriter(buffer, fieldnames=list(fields), restval="", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _cell(row.get(field)) for field in fields})
        atomic_write_text(path, buffer.getvalue())
    except Exception:  # noqa: BLE001 - same non-fatal contract as the archive
        return None
    return str(path)


# --- building observations from artifacts ------------------------------------


def _slot_from_classification(classification: Mapping[str, Any]) -> str:
    """Derive the slot token (``"1030"``) from ``scheduled_for_et`` (``"10:30"``).

    Zero-padded to 4 digits so a single-digit hour like market open's
    ``"9:35"`` produces ``"0935"`` rather than retaining its colon while
    ``"10:30"`` produces ``"1030"`` -- an unpadded 3-digit result would be an
    inconsistent slot identifier next to every other scheduled poll's 4-digit
    token (Antigravity review: "Inconsistent Slot Format for 3-Digit
    Scheduled Times in Intraday Archive").
    """
    scheduled = str(classification.get("scheduled_for_et") or "").strip()
    digits = scheduled.replace(":", "")
    if digits.isdigit() and 3 <= len(digits) <= 4:
        return digits.zfill(4)
    return scheduled


def observations_from_signal_rows(
    signal_rows: Iterable[Mapping[str, Any]],
    *,
    run_classification: Mapping[str, Any],
    generated_at: Any,
    data_source: str = "",
    trading_day: Optional[str] = None,
) -> List[IntradayObservation]:
    """Build observations from a run's ``signal_rows`` plus its classification.

    Works for both signal shapes in this repo: the morning-family row
    (``current_price``/``previous_close``/``drop_pct``/``multiplier``, plus the
    provenance columns this change adds) and the near-close row
    (``decision_bar_close``/``drop_vs_previous_close_pct``/``buy_multiplier``,
    which already carried ``decision_bar_timestamp``). Fields absent from a
    given shape are simply left empty rather than guessed at.
    """
    observed_dt = _coerce_datetime(generated_at)
    observed_et = observed_dt.astimezone(EASTERN) if observed_dt else None
    observed_utc = observed_dt.astimezone(timezone.utc) if observed_dt else None
    day = (
        trading_day
        or str(run_classification.get("trading_day") or "")
        or (observed_et.date().isoformat() if observed_et else "")
    )
    stream = str(run_classification.get("stream") or "")
    slot = _slot_from_classification(run_classification)
    run_id = str(run_classification.get("run_id") or "")
    run_type = str(run_classification.get("run_type") or "")
    manual_run = _bool_or_none(run_classification.get("manual_run"))

    observations: List[IntradayObservation] = []
    for row in signal_rows:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        price = _float_or_none(row.get("current_price"))
        if price is None:
            price = _float_or_none(row.get("decision_bar_close"))
        drop_pct = _float_or_none(row.get("drop_pct"))
        if drop_pct is None:
            drop_pct = _float_or_none(row.get("drop_vs_previous_close_pct"))
        multiplier = _float_or_none(row.get("multiplier"))
        if multiplier is None:
            multiplier = _float_or_none(row.get("buy_multiplier"))
        observations.append(
            IntradayObservation(
                ticker=ticker.upper(),
                trading_day=day,
                stream=stream,
                slot=slot,
                observed_at_et=observed_et.isoformat() if observed_et else "",
                observed_at_utc=observed_utc.isoformat() if observed_utc else "",
                run_id=run_id,
                run_type=run_type,
                manual_run=manual_run,
                source=str(row.get("source") or ""),
                price=price,
                price_source=str(row.get("price_source") or ""),
                price_as_of=_iso_or_blank(row.get("price_as_of")),
                previous_close=_float_or_none(row.get("previous_close")),
                previous_close_as_of=_iso_or_blank(
                    row.get("previous_close_as_of") or row.get("previous_close_timestamp")
                ),
                previous_low=_float_or_none(row.get("previous_low")),
                drop_pct=drop_pct,
                bar_timestamp=_iso_or_blank(
                    row.get("bar_timestamp") or row.get("decision_bar_timestamp")
                ),
                bar_open=_float_or_none(row.get("bar_open")),
                bar_high=_float_or_none(row.get("bar_high")),
                bar_low=_float_or_none(row.get("bar_low")),
                bar_close=_float_or_none(row.get("bar_close") or row.get("decision_bar_close")),
                bar_volume=_float_or_none(row.get("bar_volume")),
                session_open=_float_or_none(row.get("session_open")),
                session_high=_float_or_none(row.get("session_high")),
                session_low=_float_or_none(row.get("session_low")),
                session_volume=_float_or_none(row.get("session_volume")),
                bar_count=_int_or_none(row.get("bar_count")),
                interval_minutes=_int_or_none(row.get("interval_minutes")),
                quote_price=_float_or_none(row.get("quote_price")),
                quote_time=_iso_or_blank(row.get("quote_time")),
                action=str(row.get("action") or ""),
                multiplier=multiplier,
                data_source=data_source or str(row.get("source") or ""),
                warnings=str(row.get("warnings") or ""),
            )
        )
    return observations


def _int_or_none(value: Any) -> Optional[int]:
    parsed = _float_or_none(value)
    return int(parsed) if parsed is not None else None


def _coerce_datetime(value: Any) -> Optional[datetime]:
    """Parse ``generated_at`` (a datetime or ISO string), assuming ET if naive.

    Run states in this repo write ``generated_at`` as a naive ET wall-clock
    string (``"2026-07-23T10:30:00"``), so a naive value is localized to ET
    rather than to the machine's zone -- a backfill run on a UTC box must not
    shift every historical observation by four hours.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EASTERN)
    return parsed


__all__ = [
    "INTRADAY_SUBDIR",
    "OBSERVATION_FIELDS",
    "IntradayObservation",
    "append_observations",
    "archive_observations_safely",
    "configured_intraday_archive_dir",
    "observations_from_signal_rows",
    "read_ticker_observations",
    "write_run_snapshot",
]
