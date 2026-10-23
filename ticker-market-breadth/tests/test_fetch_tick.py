"""Tests for fetch_tick.py -- no real network calls.

fetch_tick_history()'s yfinance download is exercised only via a monkeypatched
`yfinance` module (sys.modules substitution); write_tick_csv() is tested
directly against a synthetic in-memory frame.
"""

from __future__ import annotations

import json
import re
import sys
import types
from datetime import datetime, timezone

import pandas as pd
import pytest

from dca_provenance import matches_data_file, sidecar_path
from fetch_tick import (
    ARCHIVE_FILENAME,
    ARCHIVE_SUBDIRECTORY,
    PROVENANCE_SOURCE,
    YAHOO_SYMBOL,
    fetch_tick_history,
    main,
    write_tick_csv,
)

# Mirrors ml-forecaster/features.py's and wondrous-octopus/data.py's own
# _TICKER_SYMBOL_PATTERN exactly (duplicated, not imported: sibling
# subprojects deliberately never import each other -- see
# dca-sizing-signals/tests/test_architecture_boundaries.py for the same
# no-cross-import posture enforced elsewhere in this repo).
_TRAINING_PIPELINE_TICKER_SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,6}$")


def test_archive_path_is_a_subdirectory_not_a_top_level_file(tmp_path):
    """The breadth series must be structurally invisible to any top-level
    "*.csv" glob over the archive root -- not just filename-convention-aware
    ones (Codex review: "Move the breadth CSV outside the updater glob").

    Two independent consumers would otherwise mistreat this series as a real
    equity ticker if it sat directly in the archive root:

      * the documented daily archive updater enumerates every "{TICKER}.csv"
        DIRECTLY under the archive root and re-fetches the stem AS A YAHOO
        SYMBOL -- that updater is external to this repo, so whether it
        skips underscore-prefixed stems (an earlier cut of this module
        assumed it did) can't be verified from here;
      * ml-forecaster's/wondrous-octopus's discover_available_tickers()
        helpers glob "*.csv" directly under the archive root too, feeding
        the result into asset-price-only feature engineering -- NYSE TICK
        is a signed breadth count, not a price.

    A subdirectory (matching the archive's existing `_intraday/` subtree)
    is invisible to a non-recursive `Path.glob("*.csv")` regardless of any
    filename convention, which is why ARCHIVE_SUBDIRECTORY exists at all.
    """
    assert ARCHIVE_SUBDIRECTORY.startswith("_")
    history_dir = tmp_path / "archive"
    out_path = history_dir / ARCHIVE_SUBDIRECTORY / ARCHIVE_FILENAME
    out_path.parent.mkdir(parents=True)
    out_path.write_text("Date,Close\n2024-01-02,1\n", encoding="utf-8")

    # The exact glob both the external daily updater (per its own
    # documentation) and the training pipelines' discover_available_tickers()
    # use: non-recursive, directly under the archive root.
    assert list(history_dir.glob("*.csv")) == []


def _archive_path(history_dir):
    """Where write_tick_csv() actually puts the file -- under ARCHIVE_SUBDIRECTORY."""
    return history_dir / ARCHIVE_SUBDIRECTORY / ARCHIVE_FILENAME


def _synthetic_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="Date")
    return pd.DataFrame(
        {
            "Open": [500.0, -120.0],
            "High": [980.0, 640.0],
            "Low": [-610.0, -450.0],
            "Close": [210.0, -80.0],
            "Volume": [0, 0],
        },
        index=index,
    )


def _install_fake_yfinance(monkeypatch, frame: pd.DataFrame | None, *, requested_symbol_holder: list[str]) -> None:
    fake_module = types.ModuleType("yfinance")

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            requested_symbol_holder.append(symbol)

        def history(self, *, period: str, auto_adjust: bool):
            assert period == "max"
            assert auto_adjust is False
            return frame

    fake_module.Ticker = _FakeTicker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)


def test_fetch_tick_history_requests_the_caret_tick_symbol_and_max_period(monkeypatch):
    requested: list[str] = []
    _install_fake_yfinance(monkeypatch, _synthetic_frame(), requested_symbol_holder=requested)

    frame = fetch_tick_history()

    assert requested == [YAHOO_SYMBOL]
    assert frame.index.name == "Date"
    assert list(frame["Close"]) == [210.0, -80.0]


def test_fetch_tick_history_raises_on_empty_download(monkeypatch):
    _install_fake_yfinance(monkeypatch, pd.DataFrame(), requested_symbol_holder=[])

    with pytest.raises(RuntimeError, match="no data"):
        fetch_tick_history()


def test_fetch_tick_history_raises_when_yfinance_returns_none(monkeypatch):
    _install_fake_yfinance(monkeypatch, None, requested_symbol_holder=[])

    with pytest.raises(RuntimeError, match="no data"):
        fetch_tick_history()


def test_write_tick_csv_creates_the_archive_directory_and_file(tmp_path):
    history_dir = tmp_path / "does-not-exist-yet"
    frame = _synthetic_frame()

    out_path = write_tick_csv(frame, history_dir)

    assert out_path == _archive_path(history_dir)
    assert out_path.is_file()
    written = pd.read_csv(out_path, index_col="Date", parse_dates=True)
    assert list(written["Close"]) == [210.0, -80.0]


def test_write_tick_csv_writes_no_sidecar_when_fetched_at_is_omitted(tmp_path):
    out_path = write_tick_csv(_synthetic_frame(), tmp_path / "histories")
    assert not sidecar_path(out_path).exists()


def test_write_tick_csv_writes_a_provenance_sidecar_matching_the_written_bytes(tmp_path):
    fetched_at = datetime(2026, 10, 22, 21, 0, tzinfo=timezone.utc)
    out_path = write_tick_csv(_synthetic_frame(), tmp_path / "histories", fetched_at=fetched_at)

    record = json.loads(sidecar_path(out_path).read_text(encoding="utf-8"))
    assert record["source"] == PROVENANCE_SOURCE
    assert record["fetched_at"] == fetched_at.isoformat()
    assert record["rows"] == 2
    assert record["first_date"] <= record["last_date"]
    assert matches_data_file(out_path)
    # The sidecar must not look like an archive CSV to a "*.csv" glob.
    assert not sidecar_path(out_path).name.endswith(".csv")


def test_write_tick_csv_overwrites_an_existing_file(tmp_path):
    history_dir = tmp_path / "histories"
    _archive_path(history_dir).parent.mkdir(parents=True)
    _archive_path(history_dir).write_text("stale contents\n", encoding="utf-8")

    write_tick_csv(_synthetic_frame(), history_dir)

    written = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(written) == 2


def test_main_writes_to_the_configured_archive_by_default(tmp_path, monkeypatch):
    # The default archive must already exist (mirrors dca_intraday_archive.py's
    # and scripts/refresh_active_ticker_histories.py's identical convention:
    # a MISSING default is a mount failure, not something to silently
    # materialize -- see test_main_refuses_to_create_a_missing_default_archive
    # below).
    default_dir = tmp_path / "default-archive"
    default_dir.mkdir()
    monkeypatch.setattr("fetch_tick.configured_ticker_history_dir", lambda: default_dir)
    _install_fake_yfinance(monkeypatch, _synthetic_frame(), requested_symbol_holder=[])

    exit_code = main([])

    assert exit_code == 0
    assert _archive_path(default_dir).is_file()


def test_main_refuses_to_create_a_missing_default_archive(tmp_path, monkeypatch):
    """Codex review ("Refuse to create a missing default archive"): a missing
    DEFAULT ticker_data/ (the gitignored, externally-managed Drive
    symlink/mount) must fail closed, not silently materialize a bogus local
    stand-in that later daily-update/calibration jobs would mistake for the
    real, managed archive.
    """
    missing_default_dir = tmp_path / "default-archive-not-mounted"
    monkeypatch.setattr("fetch_tick.configured_ticker_history_dir", lambda: missing_default_dir)
    _install_fake_yfinance(monkeypatch, _synthetic_frame(), requested_symbol_holder=[])

    with pytest.raises(FileNotFoundError, match="DCA_TICKER_HISTORY_DIR"):
        main([])

    assert not missing_default_dir.exists()


def test_main_writes_to_an_explicit_history_dir(tmp_path, monkeypatch):
    # An explicitly-passed --history-dir is exempt from the missing-default
    # guard above: the caller stated their intent, so creating a NEW
    # directory here is expected, not a silent side effect.
    explicit_dir = tmp_path / "explicit-archive"
    _install_fake_yfinance(monkeypatch, _synthetic_frame(), requested_symbol_holder=[])

    exit_code = main(["--history-dir", str(explicit_dir)])

    assert exit_code == 0
    assert _archive_path(explicit_dir).is_file()


def test_main_refuses_missing_explicit_dir_when_existing_dir_is_required(tmp_path, monkeypatch):
    """Automatic callers must preserve the managed-default missing-dir guard.

    The explicit --history-dir is the one being rejected here -- this is the
    automatic wrapper's own invocation pattern -- so the error must not
    repeat "pass --history-dir explicitly" back at the operator; it already
    was, and it was rejected (Codex review: "Report a usable remedy for
    guarded explicit paths").
    """
    missing_dir = tmp_path / "managed-archive-not-mounted"
    _install_fake_yfinance(monkeypatch, _synthetic_frame(), requested_symbol_holder=[])

    with pytest.raises(FileNotFoundError, match="point --history-dir at a different, existing archive"):
        main(["--history-dir", str(missing_dir), "--require-existing-history-dir"])

    assert not missing_dir.exists()


def test_main_refuses_to_recreate_required_dir_that_disappears_during_fetch(tmp_path, monkeypatch):
    """Keep the managed-archive guard active across the network request."""
    history_dir = tmp_path / "managed-archive"
    history_dir.mkdir()

    def fetch_after_archive_disappears():
        history_dir.rmdir()
        return _synthetic_frame()

    monkeypatch.setattr("fetch_tick.fetch_tick_history", fetch_after_archive_disappears)

    with pytest.raises(FileNotFoundError, match="disappeared before the breadth archive write"):
        main(["--history-dir", str(history_dir), "--require-existing-history-dir"])

    assert not history_dir.exists()


def _write_archive(history_dir, rows: list[tuple[str, float]]) -> None:
    _archive_path(history_dir).parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {"Open": [v for _, v in rows], "High": [v for _, v in rows], "Low": [v for _, v in rows],
         "Close": [v for _, v in rows], "Volume": [0] * len(rows)},
        index=pd.DatetimeIndex([d for d, _ in rows], name="Date"),
    )
    frame.to_csv(_archive_path(history_dir))


def test_write_tick_csv_preserves_archived_values_for_a_malformed_fresh_row(tmp_path):
    """Codex review ("Validate fresh rows before replacing archived
    values"): a degraded-but-nonempty download can have a null/non-finite
    OHLC value for a date it shares with the archive. That row must not be
    allowed to overwrite the archive's good values for that date -- merging
    by date alone (matching row) isn't enough; the fresh row's own values
    must be validated too. Mixed in with a second, usable row for a new
    date so this scenario is distinct from "the whole download was
    unusable" (see test_write_tick_csv_raises_when_a_refresh_against_an_
    existing_archive_has_no_usable_rows below, which now correctly raises
    for that all-unusable case instead of silently succeeding).
    """
    history_dir = tmp_path / "histories"
    _write_archive(history_dir, [("2020-01-02", 100.0), ("2020-01-03", 105.0)])
    mixed = pd.DataFrame(
        {
            "Open": [float("nan"), 111.0],
            "High": [float("nan"), 112.0],
            "Low": [float("nan"), 109.0],
            "Close": [float("nan"), 110.0],
            "Volume": [0, 0],
        },
        index=pd.DatetimeIndex(["2020-01-03", "2020-01-06"], name="Date"),
    )

    write_tick_csv(mixed, history_dir)

    merged = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(merged) == 3
    # The archive's own good value for 2020-01-03 survives untouched.
    assert merged.loc["2020-01-03", "Close"] == 105.0
    # The new, usable date is admitted.
    assert merged.loc["2020-01-06", "Close"] == 110.0


def test_write_tick_csv_admits_a_negative_close_as_a_valid_breadth_reading(tmp_path):
    """NYSE TICK is a signed breadth count -- a negative Close is a normal,
    valid observation here, unlike an ordinary equity price series, and
    must not be treated as malformed."""
    history_dir = tmp_path / "histories"
    negative_reading = pd.DataFrame(
        {"Open": [-950.0], "High": [-200.0], "Low": [-1200.0], "Close": [-800.0], "Volume": [0]},
        index=pd.DatetimeIndex(["2020-01-02"], name="Date"),
    )

    write_tick_csv(negative_reading, history_dir)

    written = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert written.loc["2020-01-02", "Close"] == -800.0


def test_write_tick_csv_raises_when_an_initial_refresh_has_no_usable_rows(tmp_path):
    """Codex review ("Reject an initial refresh with no usable rows"): when
    every row of a nonempty download is unusable (missing/null/non-finite
    OHLC) AND there is no existing archive to preserve instead, silently
    writing a header-only file and reporting success would hide a genuine
    data problem.
    """
    history_dir = tmp_path / "histories"
    all_malformed = pd.DataFrame(
        {"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")], "Close": [float("nan")],
         "Volume": [0]},
        index=pd.DatetimeIndex(["2020-01-02"], name="Date"),
    )

    with pytest.raises(RuntimeError, match="no usable rows"):
        write_tick_csv(all_malformed, history_dir)

    assert not _archive_path(history_dir).exists()


def test_write_tick_csv_raises_when_a_refresh_against_an_existing_archive_has_no_usable_rows(tmp_path):
    """Codex review ("Fail refreshes with no usable downloaded rows"): the
    prior fix only rejected an all-malformed download when there was no
    existing archive. When an archive DOES already exist and the fresh
    download is nonempty but every row is malformed, the filtered frame is
    still empty and this must raise too -- rewriting the unchanged archive
    and reporting success would hide that nothing was actually refreshed.
    """
    history_dir = tmp_path / "histories"
    _write_archive(history_dir, [("2020-01-02", 100.0), ("2020-01-03", 105.0)])
    all_malformed = pd.DataFrame(
        {"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")], "Close": [float("nan")],
         "Volume": [0]},
        index=pd.DatetimeIndex(["2020-01-06"], name="Date"),
    )

    with pytest.raises(RuntimeError, match="no usable rows"):
        write_tick_csv(all_malformed, history_dir)

    # The existing archive must be left untouched, not rewritten.
    on_disk = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(on_disk) == 2


def test_write_tick_csv_merges_a_shorter_recent_slice_without_losing_older_history(tmp_path):
    """A nonempty but partial Yahoo response (e.g. only a recent slice
    during a degraded request) must not drop the archive's older rows --
    the result merges both, superseding the earlier "reject the whole
    write" approach (Codex review: "Reject truncated nonempty history
    responses" -> merge fixes this by construction instead)."""
    history_dir = tmp_path / "histories"
    _write_archive(history_dir, [("2020-01-02", 100.0), ("2020-01-03", 105.0), ("2020-01-06", 110.0)])
    recent_slice_only = pd.DataFrame(
        {"Open": [999.0], "High": [999.0], "Low": [999.0], "Close": [999.0], "Volume": [0]},
        index=pd.DatetimeIndex(["2020-01-06"], name="Date"),
    )

    write_tick_csv(recent_slice_only, history_dir)

    merged = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(merged) == 3
    # The overlapping date's value comes from the fresh download (later
    # write wins), not the stale archive.
    assert merged.loc["2020-01-06", "Close"] == 999.0


def test_write_tick_csv_merges_a_download_missing_older_history(tmp_path):
    """A download that starts LATER than the existing archive's earliest
    row (recent-window-only data) must not erase that older history --
    it's merged in alongside the new rows instead."""
    history_dir = tmp_path / "histories"
    _write_archive(history_dir, [("2020-01-02", 100.0), ("2020-01-03", 105.0)])
    later_start = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0], "Close": [1.0, 2.0], "Volume": [0, 0]},
        index=pd.DatetimeIndex(["2020-06-01", "2020-06-02"], name="Date"),
    )

    write_tick_csv(later_start, history_dir)

    merged = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(merged) == 4
    assert pd.to_datetime(merged.index).strftime("%Y-%m-%d").tolist() == [
        "2020-01-02",
        "2020-01-03",
        "2020-06-01",
        "2020-06-02",
    ]


def test_write_tick_csv_merges_a_download_missing_recent_history(tmp_path):
    """A download whose row count and earliest date happen to match the
    archive, but whose LATEST date is older, must not silently regress the
    archive's most recent coverage (Codex review: "Reject refreshes that
    end before the archive" -> merge preserves the newer archived row
    instead of ever discarding it)."""
    history_dir = tmp_path / "histories"
    _write_archive(
        history_dir,
        [("2020-01-02", 100.0), ("2020-01-03", 105.0), ("2020-01-06", 110.0)],
    )
    missing_recent_tail = pd.DataFrame(
        {"Open": [1.0, 2.0, 3.0], "High": [1.0, 2.0, 3.0], "Low": [1.0, 2.0, 3.0],
         "Close": [1.0, 2.0, 3.0], "Volume": [0, 0, 0]},
        index=pd.DatetimeIndex(["2020-01-02", "2020-01-02", "2020-01-03"], name="Date"),
    )

    write_tick_csv(missing_recent_tail, history_dir)

    merged = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    # The archive's own 2020-01-06 row survives even though the download
    # never mentioned it.
    assert "2020-01-06" in merged.index.astype(str).tolist()
    assert len(merged) == 3


def test_write_tick_csv_merges_an_interior_gap_download(tmp_path):
    """Codex review ("Require refreshed history to retain every archived
    date"): a download whose row count matches and whose min/max dates
    match the archive can STILL be missing an interior date (an added new
    date compensating for a dropped old one) -- row-count/min/max
    comparisons alone can never catch this; only actually keeping every
    archived date does.
    """
    history_dir = tmp_path / "histories"
    _write_archive(
        history_dir,
        [("2020-01-02", 100.0), ("2020-01-03", 105.0), ("2020-01-06", 110.0)],
    )
    # Same row count (3), same min (2020-01-02) and max (2020-01-06) as the
    # archive, but 2020-01-03 is missing, replaced by a new 2020-01-07.
    interior_gap = pd.DataFrame(
        {"Open": [1.0, 2.0, 3.0], "High": [1.0, 2.0, 3.0], "Low": [1.0, 2.0, 3.0],
         "Close": [1.0, 2.0, 3.0], "Volume": [0, 0, 0]},
        index=pd.DatetimeIndex(["2020-01-02", "2020-01-06", "2020-01-07"], name="Date"),
    )

    write_tick_csv(interior_gap, history_dir)

    merged = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert pd.to_datetime(merged.index).strftime("%Y-%m-%d").tolist() == [
        "2020-01-02",
        "2020-01-03",
        "2020-01-06",
        "2020-01-07",
    ]


def test_write_tick_csv_merges_a_longer_extending_download(tmp_path):
    """A genuine refresh (later start, more total rows, extending
    coverage) must merge in cleanly on top of the existing archive."""
    history_dir = tmp_path / "histories"
    _write_archive(history_dir, [("2020-01-02", 100.0), ("2020-01-03", 105.0)])
    refreshed = _synthetic_frame()  # 2024-01-02 / 2024-01-03 -- much later, non-overlapping

    write_tick_csv(refreshed, history_dir)

    merged = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(merged) == 4


def test_fetch_tick_history_normalizes_a_tz_aware_dst_spanning_index(monkeypatch):
    """Codex review ("Normalize archive dates before comparing refreshes"):
    yfinance returns an exchange-timezone-aware index, and a full
    period="max" download commonly spans a DST transition -- rows before
    and after the boundary carry different UTC offsets (-05:00 / -04:00).
    fetch_tick_history() must hand back a plain, timezone-naive calendar-
    date index regardless.
    """
    tz_aware_index = pd.DatetimeIndex(
        ["2024-01-02", "2024-06-03"], name="Date", tz="America/New_York"
    )
    frame = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0], "Close": [1.0, 2.0], "Volume": [0, 0]},
        index=tz_aware_index,
    )
    _install_fake_yfinance(monkeypatch, frame, requested_symbol_holder=[])

    normalized = fetch_tick_history()

    assert getattr(normalized.index, "tz", None) is None
    assert pd.to_datetime(normalized.index).strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-06-03"]


def test_fetch_tick_history_keeps_the_exchange_local_date_for_a_late_ny_bar(monkeypatch):
    """Antigravity review ("Timezone Normalization Assumption"): a bar
    timestamped late in the exchange-local (America/New_York) day can
    already have rolled over to the NEXT UTC calendar date. Normalizing
    straight off the UTC instant (instead of converting back to
    America/New_York first) would silently shift such a bar's date forward
    by one day.
    """
    # 2024-01-02 23:00 America/New_York (EST, UTC-05:00) is
    # 2024-01-03 04:00 UTC -- a naive UTC-instant normalize() would report
    # this row as "2024-01-03", one day later than its real exchange-local
    # date.
    late_ny_bar = pd.DatetimeIndex(["2024-01-02 23:00:00"], name="Date", tz="America/New_York")
    frame = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [0]},
        index=late_ny_bar,
    )
    _install_fake_yfinance(monkeypatch, frame, requested_symbol_holder=[])

    normalized = fetch_tick_history()

    assert pd.to_datetime(normalized.index).strftime("%Y-%m-%d").tolist() == ["2024-01-02"]


def test_write_tick_csv_merges_a_dst_spanning_download_against_the_archive(tmp_path, monkeypatch):
    """The exact regression reported against the first cut of this guard: a
    second real refresh whose download spans a DST boundary (mixed UTC
    offsets) must not raise TypeError when merged against the existing
    archive on disk, and a genuine, longer refresh must still merge in
    cleanly.
    """
    history_dir = tmp_path / "histories"
    _write_archive(history_dir, [("2024-01-02", 100.0), ("2024-01-03", 105.0)])
    tz_aware_index = pd.DatetimeIndex(
        ["2024-01-02", "2024-01-03", "2024-06-03"], name="Date", tz="America/New_York"
    )
    dst_spanning_download = pd.DataFrame(
        {"Open": [1.0, 2.0, 3.0], "High": [1.0, 2.0, 3.0], "Low": [1.0, 2.0, 3.0],
         "Close": [1.0, 2.0, 3.0], "Volume": [0, 0, 0]},
        index=tz_aware_index,
    )

    write_tick_csv(dst_spanning_download, history_dir)

    written = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(written) == 3


def test_write_tick_csv_ignores_an_unparseable_existing_file(tmp_path):
    """A corrupt/unrecognized existing file has no good data to protect, so
    a fresh write must be allowed through rather than refused."""
    history_dir = tmp_path / "histories"
    _archive_path(history_dir).parent.mkdir(parents=True)
    _archive_path(history_dir).write_text("not,a,valid,tick,archive\n", encoding="utf-8")

    write_tick_csv(_synthetic_frame(), history_dir)

    written = pd.read_csv(_archive_path(history_dir), index_col="Date", parse_dates=True)
    assert len(written) == 2
