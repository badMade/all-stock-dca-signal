"""Tests for pricing.py's archive-first lookup -- no network calls.

Only exercises the local-archive path (a synthetic CSV under a tmp_path
history_dir); the yfinance-fallback path needs yfinance installed and a real
network call, so it isn't covered here -- fetch_price_history's ImportError
handling (yfinance not installed) is covered directly, without a real
download.
"""

import math
import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pricing import PriceHistoryUnavailable, _load_from_yfinance, _tz_naive, fetch_price_history


def _write_archive_csv(history_dir, ticker, rows):
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{ticker}.csv"
    header = "Date,Open,High,Low,Close,Adj Close,Volume"
    lines = [header] + [
        f"{date},{o},{h},{lo},{c},{ac},{v}" for date, o, h, lo, c, ac, v in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _long_live_frame(periods=400, overrides=None, tz=None):
    """A live-fetch frame with enough rows to clear fetch_price_history's
    live-history coverage floor (half the DEFAULT 3y lookback's ~756
    trading days -- see _TRADING_DAYS_PER_YEAR/_MIN_ARCHIVE_COVERAGE_FRACTION
    in pricing.py). A 1-2 row fixture, the convention used before that
    check existed, now gets rejected as insufficient coverage and routed
    to the archive fallback instead of exercising the LIVE path a test
    actually wants to isolate (Antigravity review on PR #403: "Validate
    live history coverage before returning it"). `tz` lets a caller build
    a tz-AWARE index (yfinance commonly returns one)."""
    index = pd.date_range(end=datetime.now(timezone.utc).date().isoformat(), periods=periods, freq="D", tz=tz)
    columns = {
        "Open": [1.0] * periods,
        "High": [1.0] * periods,
        "Low": [1.0] * periods,
        "Close": [1.0] * periods,
        "Adj Close": [1.0] * periods,
        "Volume": [1] * periods,
    }
    if overrides:
        columns.update(overrides)
    return pd.DataFrame(columns, index=index)


def _recent_date(days_ago: int) -> str:
    """A date `days_ago` before now -- keeps archive fixtures fresh regardless
    of when the test suite runs, so fetch_price_history's staleness check
    never routes an archive-only test through the live yfinance path (Codex
    review on PR #403: "Keep archive tests off the live downloader" --
    fixed-2024 dates went stale relative to "now" and triggered real,
    slow, network-dependent requests once more than 10 days had passed)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _stub_yfinance_unavailable(monkeypatch):
    """Make any live-fetch attempt fail deterministically (no install, no network).

    Archive-only tests use this so that, regardless of whether pricing.py's
    staleness/coverage checks route a given fixture through the fallback
    path, the result deterministically comes from the archive rather than
    a real yfinance call -- these tests are about archive behavior, not
    live-fetch behavior (Codex review on PR #403: "Keep archive tests off
    the live downloader").
    """
    import pricing

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)


def test_now_utc_naive_is_utc_regardless_of_host_timezone():
    # A bare pd.Timestamp.now() uses the HOST's local system time -- on a
    # runner/container/dev machine whose configured timezone sits behind
    # UTC, that could differ from the UTC instant this archive's own
    # downloader stamps its dates with by up to a day, purely as a
    # function of host configuration rather than anything about the data
    # (Antigravity review on PR #403: "System Timezone Edge Case in
    # Archive Staleness Check").
    from pricing import _now_utc_naive

    result = _now_utc_naive()

    assert result.tzinfo is None  # tz-naive, so it compares against naive archive timestamps
    expected = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((result.to_pydatetime() - expected).total_seconds()) < 5


def test_resolve_now_converts_aware_timestamp_to_utc():
    # tz_localize(None) on an aware Timestamp discards the offset and keeps
    # the LOCAL wall-clock value instead of converting the instant to UTC --
    # 2026-08-10T00:30+14:00 is actually 2026-08-09T10:30 UTC, so a naive
    # tz_localize(None) would wrongly resolve this to August 10 (Codex
    # review on PR #403: "Convert aware run cutoffs to UTC before stripping
    # timezone").
    from pricing import _resolve_now

    aware = pd.Timestamp("2026-08-10T00:30:00+14:00")

    result = _resolve_now(aware)

    assert result.tzinfo is None
    assert result == pd.Timestamp("2026-08-09T10:30:00")


def test_fetch_price_history_normalizes_intraday_archive_timestamps(tmp_path, monkeypatch):
    # An archived row's Date can retain a nonzero intraday time component
    # (e.g. a market-close timestamp) without being rejected as invalid --
    # but a candidate's own frame and its benchmark's frame must still
    # compare as the SAME trading session, not two different index labels,
    # or advise_tickers.py's benchmark-calendar reindex inserts a spurious
    # null row for every such day (Codex review on PR #403: "Normalize
    # daily timestamps before calendar reindexing").
    _stub_yfinance_unavailable(monkeypatch)
    rows = [
        (f"{_recent_date(days_ago)} 16:00:00", 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)
    ]
    _write_archive_csv(tmp_path, "ACME", rows)

    frame = fetch_price_history("ACME", lookback_years=3, history_dir=tmp_path)

    assert (frame.index == frame.index.normalize()).all()


def test_normalize_session_index_deduplicates_after_truncating_time_of_day():
    # The archive path's own duplicate-Date removal (_load_from_archive's
    # `indexed.index.duplicated(keep="last")`) runs on exact, still-
    # intraday-timestamped labels, before this normalization -- two rows
    # for the same trading date at different times of day (e.g. midnight
    # and 16:00) are distinct labels at that point and survive it
    # untouched. Truncating both onto the same date label without
    # deduplicating again afterward would silently reintroduce the
    # duplicate-label row _load_from_archive() already guards against
    # (Codex review on PR #403: "Deduplicate sessions after normalizing
    # timestamps").
    from pricing import _normalize_session_index

    index = pd.DatetimeIndex(["2026-08-05 00:00:00", "2026-08-05 16:00:00", "2026-08-06 00:00:00"])
    frame = pd.DataFrame({"Close": [10.0, 20.0, 30.0]}, index=index)

    result = _normalize_session_index(frame)

    assert list(result.index) == [pd.Timestamp("2026-08-05"), pd.Timestamp("2026-08-06")]
    # keep="last" -- matches every other dedup in this module: the later row wins.
    assert result.loc[pd.Timestamp("2026-08-05"), "Close"] == 20.0


def test_fetch_price_history_prefers_local_archive(tmp_path, monkeypatch):
    _stub_yfinance_unavailable(monkeypatch)
    # 1..10 days ago, not 0..9 -- fetch_price_history() now unconditionally
    # drops "today"'s row (Antigravity review on PR #403: "Exclude today's
    # bar from archived histories"), which would otherwise eat one of
    # these 10 rows.
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)]
    _write_archive_csv(tmp_path, "ACME", rows)

    frame = fetch_price_history("ACME", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 10
    assert "Close" in frame.columns


def test_fetch_price_history_applies_lookback_window(tmp_path, monkeypatch):
    _stub_yfinance_unavailable(monkeypatch)
    # 3 years of recent daily rows plus one row 5 years old that must be excluded.
    rows = [((datetime.now(timezone.utc) - timedelta(days=365 * 5)).strftime("%Y-%m-%d"), 50, 51, 49, 50, 50, 1000)]
    # 1..10 days ago, not 0..9 -- see the same "today" note above.
    rows += [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)]
    _write_archive_csv(tmp_path, "ACME", rows)

    frame = fetch_price_history("ACME", lookback_years=3, history_dir=tmp_path)

    old_year = str(datetime.now(timezone.utc).year - 5)
    assert old_year not in "".join(str(idx) for idx in frame.index)
    assert len(frame) == 10


def test_fetch_price_history_raises_when_no_archive_and_no_yfinance(tmp_path, monkeypatch):
    import pricing

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("NOPE", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_wraps_network_errors_not_just_import_error(tmp_path, monkeypatch):
    import pricing

    def _raise_connection_error(ticker, lookback_years):
        raise ConnectionError("timed out talking to yfinance")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_connection_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("NOPE", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_missing_required_column_falls_back(tmp_path, monkeypatch):
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / "BADCOLS.csv"
    path.write_text("Date,Close\n2024-01-01,100\n", encoding="utf-8")

    import pricing

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADCOLS", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_falls_through_on_malformed_archive_csv(tmp_path, monkeypatch):
    import pricing

    tmp_path.mkdir(exist_ok=True)
    # Unterminated quote -- pandas' C parser raises on this rather than
    # silently producing a usable frame.
    (tmp_path / "MALFORMED.csv").write_text(
        'Date,Open,High,Low,Close,Adj Close,Volume\n2024-01-01,"unterminated,101,99,100,100,1000\n',
        encoding="utf-8",
    )

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("MALFORMED", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_falls_through_on_unparseable_date_value(tmp_path, monkeypatch):
    # pd.read_csv(parse_dates=["Date"]) does NOT raise on an unparseable
    # date value -- it silently leaves the column as a plain string/object
    # Index rather than a DatetimeIndex (verified empirically). Without an
    # explicit check, that frame would be accepted and returned, and
    # _tz_naive()'s `frame.index.tz` access would then raise AttributeError
    # downstream, escaping both the yfinance fallback and
    # PriceHistoryUnavailable (Codex review on PR #403: "Reject invalid
    # archive dates before accepting the frame").
    import pricing

    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "BADDATE.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\nnot-a-date,100,101,99,100,100,1000\n",
        encoding="utf-8",
    )

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADDATE", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_falls_through_on_non_numeric_ohlcv_cell(tmp_path, monkeypatch):
    # read_csv() doesn't raise on a non-numeric OHLCV cell -- it silently
    # accepts an object-dtype column, and _price_returns()'s pct_change()
    # then raises TypeError on it BEFORE score_candidate()'s per-signal
    # exception guards, aborting the whole candidate batch instead of
    # degrading just this archive to "unavailable" (Codex review on PR
    # #403: "Validate numeric archive columns before accepting them").
    import pricing

    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "BADCELL.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n2024-01-01,100,101,99,oops,100,1000\n",
        encoding="utf-8",
    )

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADCELL", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_drops_nat_rows_and_keeps_valid_ones(tmp_path, monkeypatch):
    # An empty Date cell parses to NaT but the column overall still comes
    # back as a DatetimeIndex (the earlier "reject invalid archive dates"
    # check only catches a non-DatetimeIndex, not individual NaT entries
    # within a genuinely-typed one) -- _archive_deficiency()'s
    # `last_row.normalize()` raises AttributeError on a NaT last_row, and
    # score_candidate() doesn't catch that (only PriceHistoryUnavailable),
    # aborting the whole advisory batch (Codex review on PR #403: "Reject
    # archives whose date index contains NaT").
    _stub_yfinance_unavailable(monkeypatch)
    path = tmp_path
    path.mkdir(exist_ok=True)
    # 1..10 days ago, not 0..9 -- see the "today" note in
    # test_fetch_price_history_prefers_local_archive.
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)]
    header = "Date,Open,High,Low,Close,Adj Close,Volume"
    lines = [header, ",100,101,99,100,100,1000"] + [
        f"{date},{o},{h},{lo},{c},{ac},{v}" for date, o, h, lo, c, ac, v in rows
    ]
    (path / "PARTIALNAT.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    frame = fetch_price_history("PARTIALNAT", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 10  # the NaT row is dropped, the 10 valid rows survive
    assert frame.index.notna().all()


def test_fetch_price_history_falls_through_when_every_date_is_nat(tmp_path, monkeypatch):
    import pricing

    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "ALLNAT.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n,100,101,99,100,100,1000\n,101,102,100,101,101,1000\n",
        encoding="utf-8",
    )

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("ALLNAT", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_drops_future_dated_rows_and_keeps_valid_ones(tmp_path, monkeypatch):
    # A mistyped future date would otherwise become `last_row` in
    # _archive_deficiency(), making `staleness` NEGATIVE and so falsely
    # "fresh" -- _windowed() then anchors the lookback to that bogus future
    # timestamp and typically returns just the one bad row, silently
    # eliminating health/backtest signals even though a live refresh was
    # available (Codex review on PR #403: "Reject future-dated archive
    # rows").
    _stub_yfinance_unavailable(monkeypatch)
    future_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    # 1..10 days ago, not 0..9 -- see the "today" note in
    # test_fetch_price_history_prefers_local_archive.
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)]
    rows.append((future_date, 999, 999, 999, 999, 999, 9999))
    _write_archive_csv(tmp_path, "FUTUREDATE", rows)

    frame = fetch_price_history("FUTUREDATE", lookback_years=3, history_dir=tmp_path)

    assert future_date not in "".join(str(idx) for idx in frame.index)
    assert len(frame) == 10  # the future row is dropped, the 10 valid rows survive


def test_fetch_price_history_excludes_todays_bar_from_archive(tmp_path, monkeypatch):
    # An archive refreshed before market close could have written an
    # evolving/non-final "today" bar the same way a live yfinance download
    # can -- the live path already drops it for that reason (Antigravity
    # review on PR #403: "Exclude an unfinished live daily bar"). Without
    # the same exclusion on the archive path, a mixed archive/live batch
    # (or an archive vs. its own benchmark, fetched via a different path)
    # would be scored through DIFFERENT effective closing dates
    # (Antigravity review on PR #403: "Exclude today's bar from archived
    # histories").
    _stub_yfinance_unavailable(monkeypatch)
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)]
    rows.append((_recent_date(0), 999, 999, 999, 999, 999, 9999))
    _write_archive_csv(tmp_path, "TODAYARCHIVE", rows)

    frame = fetch_price_history("TODAYARCHIVE", lookback_years=3, history_dir=tmp_path)

    assert today not in frame.index
    assert len(frame) == 10  # today's row is dropped, the 10 prior rows survive


def test_fetch_price_history_falls_through_when_every_date_is_future(tmp_path, monkeypatch):
    import pricing

    future_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "ALLFUTURE.csv").write_text(
        f"Date,Open,High,Low,Close,Adj Close,Volume\n{future_date},100,101,99,100,100,1000\n",
        encoding="utf-8",
    )

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("ALLFUTURE", lookback_years=3, history_dir=tmp_path)


def test_tz_naive_strips_tz_awareness():
    tz_aware = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.date_range("2024-01-01", periods=2, tz="UTC"),
    )

    result = _tz_naive(tz_aware)

    assert result.index.tz is None


def test_tz_naive_is_a_noop_on_already_naive_frame():
    naive = pd.DataFrame({"Close": [100.0]}, index=pd.date_range("2024-01-01", periods=1))

    result = _tz_naive(naive)

    assert result.index.tz is None
    assert list(result["Close"]) == [100.0]


def test_fetch_price_history_returns_tz_naive_index_from_archive(tmp_path, monkeypatch):
    _stub_yfinance_unavailable(monkeypatch)
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(9, -1, -1)]
    _write_archive_csv(tmp_path, "ACME", rows)

    frame = fetch_price_history("ACME", lookback_years=3, history_dir=tmp_path)

    assert frame.index.tz is None


def test_fetch_price_history_accepts_archive_with_tz_aware_dates(tmp_path, monkeypatch):
    # A Date column with a consistent UTC offset (e.g.
    # "2024-01-01T00:00:00+00:00") parses to a tz-AWARE DatetimeIndex --
    # comparing that against the tz-naive pd.Timestamp.now().normalize()
    # in the future-date-rejection check raises TypeError, which this
    # function's own broad except then silently degrades to "archive
    # unavailable," unnecessarily forcing a live yfinance fetch for an
    # otherwise perfectly usable archive (Antigravity review on PR #403:
    # "Potential TypeError on Tz-Aware Timestamps in CSV Archive
    # Loading").
    _stub_yfinance_unavailable(monkeypatch)
    # 1..10 days ago, not 0..9 -- see the "today" note in
    # test_fetch_price_history_prefers_local_archive.
    rows = [
        (f"{_recent_date(days_ago)}T00:00:00+00:00", 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)
    ]
    _write_archive_csv(tmp_path, "TZDATES", rows)

    frame = fetch_price_history("TZDATES", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 10
    assert frame.index.tz is None


def test_fetch_price_history_falls_back_to_stale_archive_when_live_fetch_fails(tmp_path, capsys, monkeypatch):
    # A stale archive is used only as a last resort once a live refresh
    # attempt has genuinely failed. Stubbed deterministically (ImportError)
    # rather than relying on a real network call for a nonexistent "STALE"
    # ticker to fail -- that would be slow and provider-dependent even
    # though this test has nothing to do with live-fetch behavior itself
    # (Codex review on PR #403: "Keep archive tests off the live
    # downloader").
    import pricing

    def _raise_import_error(ticker, lookback_years):
        raise ImportError("yfinance not installed")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_import_error)

    stale_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    _write_archive_csv(tmp_path, "STALE", [(stale_date, 100, 101, 99, 100, 100, 1000)])

    frame = fetch_price_history("STALE", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 1
    assert "stale" in capsys.readouterr().err.lower()
    # A stderr-only warning is invisible to anyone who only reads
    # scores.json/scores.md/the proposed diff later -- attaching the reason
    # via .attrs lets the caller (score_candidate()) carry it into the
    # candidate's own persisted `errors` list (Codex review on PR #403:
    # "Persist stale-archive fallback warnings in result artifacts").
    assert "stale" in frame.attrs.get("stale_archive_fallback_reason", "").lower()


def test_fetch_price_history_redacts_url_from_live_download_failure(tmp_path, monkeypatch):
    # A yfinance failure through an authenticated proxy, or one whose
    # message otherwise embeds a signed/request URL, would otherwise carry
    # that URL into the persisted stale-archive fallback reason --
    # score_candidate() writes this into scores.json/scores.md/the proposed
    # diff (Codex review on PR #403: "Sanitize live price-download failures
    # before persistence").
    import pricing

    def _raise_with_url(ticker, lookback_years):
        raise ConnectionError("failed for url: https://query1.finance.yahoo.com/v8/finance?crumb=SECRET123")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_with_url)

    stale_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    _write_archive_csv(tmp_path, "STALE", [(stale_date, 100, 101, 99, 100, 100, 1000)])

    frame = fetch_price_history("STALE", lookback_years=3, history_dir=tmp_path)

    fallback_reason = frame.attrs.get("stale_archive_fallback_reason", "")
    assert "SECRET123" not in fallback_reason
    assert "https://" not in fallback_reason


def test_fetch_price_history_redacts_url_from_unavailable_error(tmp_path, monkeypatch):
    import pricing

    def _raise_with_url(ticker, lookback_years):
        raise ConnectionError("failed for url: https://query1.finance.yahoo.com/v8/finance?crumb=SECRET123")

    monkeypatch.setattr(pricing, "_load_from_yfinance", _raise_with_url)

    with pytest.raises(PriceHistoryUnavailable) as exc_info:
        fetch_price_history("NOARCHIVE", lookback_years=3, history_dir=tmp_path)

    assert "SECRET123" not in str(exc_info.value)
    assert "https://" not in str(exc_info.value)


def test_fetch_price_history_falls_back_when_live_frame_has_no_close_column(tmp_path, monkeypatch):
    # yfinance can return a nonempty but malformed frame the archive path
    # would already reject/normalize -- without the same validation on the
    # live path, score_candidate() would raise KeyError on the missing
    # "Close" column BEFORE its own per-signal exception guards, aborting
    # the whole advisory batch instead of degrading just this one
    # candidate (Codex review on PR #403: "Validate live price frames
    # before returning them").
    import pricing

    malformed = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Volume": [1]},
        index=pd.date_range(datetime.now(timezone.utc).date().isoformat(), periods=1),
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: malformed)

    stale_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    _write_archive_csv(tmp_path, "NOCLOSE", [(stale_date, 100, 101, 99, 100, 100, 1000)])

    frame = fetch_price_history("NOCLOSE", lookback_years=3, history_dir=tmp_path)

    assert list(frame["Close"]) == [100]  # fell back to the archive, not the malformed live frame


def test_fetch_price_history_falls_back_when_live_frame_has_non_datetime_index(tmp_path, monkeypatch):
    import pricing

    malformed = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Adj Close": [1.0], "Volume": [1]},
        index=["not-a-date"],
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: malformed)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADINDEX", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_falls_back_to_close_when_live_adj_close_unusable(tmp_path, monkeypatch):
    # Mirrors the archive-side "Reject archives with unusable adjusted
    # prices" fix -- a live frame with a blank/NaN Adj Close alongside a
    # valid Close would otherwise silently lose health/backtest data since
    # Adj Close is preferred whenever the column exists.
    import pricing

    live_frame = _long_live_frame(overrides={"Adj Close": [float("nan")] * 400})
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("BADLIVEADJ", lookback_years=3, history_dir=tmp_path)

    assert frame["Adj Close"].equals(frame["Close"])


def test_fetch_price_history_falls_back_when_live_close_is_nonnumeric(tmp_path, monkeypatch):
    # notna()-only validation still accepts a string/object-dtype column --
    # a nonempty, correctly-indexed live frame with nonnumeric prices
    # previously sailed through _validate_live_frame(), and
    # _price_returns()'s pct_change() then raised TypeError BEFORE
    # score_candidate() enters either per-signal exception guard, aborting
    # the whole advisory batch instead of falling back or degrading just
    # this one candidate (Codex review on PR #403: "Reject nonnumeric live
    # price columns").
    import pricing

    malformed = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [1.0, 2.0],
            "Low": [1.0, 2.0],
            "Close": ["oops", "oops"],
            "Adj Close": ["oops", "oops"],
            "Volume": [1, 1],
        },
        index=pd.date_range(datetime.now(timezone.utc).date().isoformat(), periods=2),
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: malformed)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADLIVENUM", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_deduplicates_live_frame_dates(tmp_path, monkeypatch):
    # A duplicate yfinance timestamp left in place makes _price_returns()
    # treat the movement across two rows for the SAME session as a
    # separate daily return, the vectorbt backtest treats both rows as
    # distinct freq="1D" bars, and the benchmark-alignment join can
    # duplicate observations on the shared label -- the archive path
    # already guards all three; nothing previously did for the live-
    # download path (Antigravity review on PR #403: "Deduplicate live
    # price dates before scoring").
    import pricing

    # 399 distinct dates plus one more row duplicating the LAST date --
    # enough total rows to also clear the live-history coverage floor
    # (Antigravity review on PR #403: "Validate live history coverage
    # before returning it") while still exercising the dedup behavior.
    # Ends YESTERDAY, not today -- fetch_price_history() now
    # unconditionally drops today's row as a possibly-still-evolving
    # intraday bar (Antigravity review on PR #403: "Exclude an unfinished
    # live daily bar"), which would otherwise eat the very row this test
    # is trying to exercise dedup on.
    base_index = pd.date_range(
        end=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(), periods=399, freq="D"
    )
    duplicated_index = base_index.append(pd.DatetimeIndex([base_index[-1]]))
    close_values = [1.0] * 399 + [20.0]  # the appended duplicate must win
    live_frame = pd.DataFrame(
        {
            "Open": [1.0] * 400,
            "High": [1.0] * 400,
            "Low": [1.0] * 400,
            "Close": close_values,
            "Adj Close": close_values,
            "Volume": [1] * 400,
        },
        index=duplicated_index,
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("DUPLIVE", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 399
    assert frame["Close"].iloc[-1] == 20.0


def test_fetch_price_history_prefers_live_data_over_stale_archive(tmp_path, monkeypatch):
    # A stale archive must NOT be returned directly -- a working live fetch
    # takes priority (Codex review on PR #403: "Refuse stale archives
    # instead of ranking old data").
    import pricing

    stale_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    _write_archive_csv(tmp_path, "STALE", [(stale_date, 999, 999, 999, 999, 999, 1000)])

    live_frame = _long_live_frame()
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("STALE", lookback_years=3, history_dir=tmp_path)

    assert (frame["Close"] == 1.0).all()  # live data, not the stale archive's 999


def test_fetch_price_history_rejects_stale_live_data_and_falls_back_to_archive(tmp_path, monkeypatch):
    # _validate_live_frame() only checks shape and coverage, never
    # freshness -- a delisted/inactive symbol can return structurally
    # valid, well-covered data whose last row is just old (e.g. ending at
    # delisting). Previously that sailed straight through even though an
    # equally stale ARCHIVE would have been rejected by
    # _archive_deficiency()'s identical staleness check and either
    # refreshed or fallen back with a visible warning (Antigravity review
    # on PR #403: "Reject stale live histories before scoring").
    import pricing

    stale_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    _write_archive_csv(tmp_path, "STALEBOTH", [(stale_date, 999, 999, 999, 999, 999, 1000)])

    stale_live_frame = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Adj Close": [1.0], "Volume": [1]},
        index=pd.date_range(stale_date, periods=1),
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: stale_live_frame)

    frame = fetch_price_history("STALEBOTH", lookback_years=3, history_dir=tmp_path)

    assert list(frame["Close"]) == [999]  # archive fallback, not the stale live data's 1.0


def test_fetch_price_history_rejects_short_live_data_and_falls_back_to_archive(tmp_path, monkeypatch):
    # A newly-listed ticker can return a short but otherwise fresh,
    # fully-covered history from yfinance -- e.g. 40 sessions when a 3y
    # lookback was requested. That already clears strategy_health_score()'s
    # own minimum-observation floor, so the candidate would get annualized
    # health metrics and a partial backtest computed from a tiny fraction
    # of the requested period, skewing cross-candidate z-score
    # normalization against peers scored on their full requested window
    # (Antigravity review on PR #403: "Validate live history coverage
    # before returning it").
    import pricing

    # 1 day ago, not today -- fetch_price_history() now unconditionally
    # drops "today"'s row from archives too (Antigravity review on PR
    # #403: "Exclude today's bar from archived histories"), which would
    # otherwise eat this single-row archive entirely and leave no
    # fallback at all.
    _write_archive_csv(tmp_path, "SHORTLIVE", [(_recent_date(1), 999, 999, 999, 999, 999, 1000)])

    short_live_frame = pd.DataFrame(
        {
            "Open": [1.0] * 40,
            "High": [1.0] * 40,
            "Low": [1.0] * 40,
            "Close": [1.0] * 40,
            "Adj Close": [1.0] * 40,
            "Volume": [1] * 40,
        },
        index=pd.date_range(end=datetime.now(timezone.utc).date().isoformat(), periods=40, freq="D"),
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: short_live_frame)

    frame = fetch_price_history("SHORTLIVE", lookback_years=3, history_dir=tmp_path)

    assert list(frame["Close"]) == [999]  # archive fallback, not the short live data's 1.0


def test_fetch_price_history_rejects_future_dated_live_rows(tmp_path, monkeypatch):
    # A single corrupt future-dated row would otherwise become `last_row`,
    # making the freshness check falsely see the frame as "fresh" no
    # matter how stale the rest of it actually is, and would feed a
    # fabricated future price transition into health/backtest scoring --
    # the same failure mode _load_from_archive()'s own future-date
    # rejection already guards against for the archive path (Antigravity
    # review on PR #403: "Reject future-dated live price rows").
    import pricing

    base = _long_live_frame()
    future_date = pd.Timestamp(datetime.now(timezone.utc).date()) + timedelta(days=365)
    future_row = pd.DataFrame(
        {"Open": [999.0], "High": [999.0], "Low": [999.0], "Close": [999.0], "Adj Close": [999.0], "Volume": [1]},
        index=pd.DatetimeIndex([future_date]),
    )
    live_frame = pd.concat([base, future_row])
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("FUTUREROW", lookback_years=3, history_dir=tmp_path)

    assert future_date not in frame.index
    assert frame.index.max() < future_date


def test_fetch_price_history_excludes_todays_live_bar(tmp_path, monkeypatch):
    # Run before the candidate's regular session has closed, yfinance can
    # include the CURRENT date's still-evolving "1d" bar -- it isn't in
    # the future, so the future-date filter doesn't catch it, and this
    # module has no market-hours/holiday-calendar knowledge to tell a
    # genuinely final bar from a partial one. Candidates are also
    # downloaded sequentially after the benchmark, so an in-progress bar
    # wouldn't even be a consistent snapshot across candidates -- always
    # treat "today" as provisional (Antigravity review on PR #403:
    # "Exclude an unfinished live daily bar").
    import pricing

    today = pd.Timestamp(datetime.now(timezone.utc).date())
    live_frame = _long_live_frame()  # already ends on "today" by construction

    assert today in live_frame.index
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("TODAYBAR", lookback_years=3, history_dir=tmp_path)

    assert today not in frame.index
    assert frame.index.max() == today - timedelta(days=1)


def test_fetch_price_history_handles_tz_aware_live_frame_without_crashing(tmp_path, monkeypatch):
    # _validate_live_frame() used to call _ensure_usable_price_columns()
    # (which now includes _column_is_fresh()'s freshness check) BEFORE
    # normalizing the frame to tz-naive -- yfinance commonly returns a
    # tz-AWARE DatetimeIndex, and comparing that against a tz-naive
    # timestamp inside _column_is_fresh() raised TypeError OUTSIDE any
    # try/except score_candidate() catches (only PriceHistoryUnavailable),
    # aborting the whole advisory batch instead of falling back or
    # degrading just this one candidate (Antigravity review on PR #403:
    # "Normalize live indices before validating price freshness").
    import pricing

    live_frame = _long_live_frame(tz="UTC")
    assert live_frame.index.tz is not None
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("TZLIVE", lookback_years=3, history_dir=tmp_path)  # must not raise

    assert frame.index.tz is None
    assert (frame["Close"] == 1.0).all()


def test_fetch_price_history_uses_supplied_now_for_future_date_cutoff(tmp_path, monkeypatch):
    # `now` anchors every future-dated-row/current-date-row/staleness
    # check in this call to a single caller-supplied instant instead of
    # each independently calling the real "now" -- proven here by
    # supplying a `now` from BEFORE the live frame's actual last rows,
    # which must then be treated as future-dated relative to that
    # SUPPLIED instant, not the real current time (Antigravity review on
    # PR #403: "Anchor price cutoffs to the run timestamp").
    import pricing

    live_frame = _long_live_frame()  # ends on the REAL today
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    stale_now = pd.Timestamp(datetime.now(timezone.utc).date()) - timedelta(days=5)

    frame = fetch_price_history("ANCHOREDNOW", lookback_years=3, history_dir=tmp_path, now=stale_now)

    # The frame's last 5 real-today-relative rows are future-dated
    # relative to the supplied (older) `now`, so the returned frame's
    # last row must not exceed it.
    assert frame.index.max() <= stale_now


def test_fetch_price_history_warns_on_sparse_archive_despite_wide_calendar_span(tmp_path, capsys, monkeypatch):
    # Two rows far apart in calendar time clear the span check (they cover
    # more than half the requested window) while holding almost no actual
    # observations -- health would compute on effectively no data and the
    # backtest would have nothing to work with, even though the calendar
    # span alone looks "complete" (Codex review on PR #403: "Count
    # observations when judging archive completeness").
    _stub_yfinance_unavailable(monkeypatch)
    rows = [
        (_recent_date(200), 100, 101, 99, 100, 100, 1000),
        (_recent_date(0), 100, 101, 99, 100, 100, 1000),
    ]
    _write_archive_csv(tmp_path, "SPARSE", rows)

    fetch_price_history("SPARSE", lookback_years=1, history_dir=tmp_path)

    stderr = capsys.readouterr().err.lower()
    assert "rows" in stderr


def test_fetch_price_history_warns_when_archive_has_old_padding_but_thin_recent_window(tmp_path, capsys, monkeypatch):
    # Hundreds of rows well outside the requested lookback window can clear
    # a whole-archive row-count check while the windowed frame that's
    # actually scored (_windowed() anchored at the archive's last row)
    # holds almost nothing -- a dense archive from years ago plus one fresh
    # row would otherwise look "complete" by total row count alone (Codex
    # review on PR #403: "Count rows inside the requested archive window").
    _stub_yfinance_unavailable(monkeypatch)
    old_business_days = pd.bdate_range(end=(datetime.now(timezone.utc).date() - timedelta(days=800)), periods=200)
    rows = [(day.strftime("%Y-%m-%d"), 100, 101, 99, 100, 100, 1000) for day in old_business_days]
    # 1 day ago, not today -- otherwise the new "exclude today's row"
    # filter (Antigravity review on PR #403: "Exclude today's bar from
    # archived histories") drops this fresh anchor row entirely, leaving
    # only the 800-day-old padding and triggering a STALENESS warning
    # instead of the row-count-within-window one this test checks for.
    rows.append((_recent_date(1), 100, 101, 99, 100, 100, 1000))
    _write_archive_csv(tmp_path, "OLDPADDING", rows)

    fetch_price_history("OLDPADDING", lookback_years=1, history_dir=tmp_path)

    stderr = capsys.readouterr().err.lower()
    assert "rows" in stderr and "lookback window" in stderr


def test_fetch_price_history_measures_adj_close_coverage_within_window(tmp_path, monkeypatch):
    # An archive can have valid Adj Close in OLD history (outside the
    # requested lookback) but be mostly blank in the specific recent
    # window that actually gets returned -- evaluating coverage over the
    # unwindowed archive's total row count let this pass (enough old valid
    # rows dilute the blank recent ones), silently returning an
    # all/mostly-NaN Adj Close for the window that's actually scored
    # (Codex review on PR #403: "Measure adjusted-price coverage in the
    # requested window").
    _stub_yfinance_unavailable(monkeypatch)
    old_days = pd.bdate_range(end=(datetime.now(timezone.utc).date() - timedelta(days=400)), periods=300)
    recent_days = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=250)
    rows = [(day.strftime("%Y-%m-%d"), 100, 101, 99, 50, 50, 1000) for day in old_days]
    rows += [(day.strftime("%Y-%m-%d"), 100, 101, 99, 200, "", 1000) for day in recent_days]
    _write_archive_csv(tmp_path, "WINDOWCOVERAGE", rows)

    frame = fetch_price_history("WINDOWCOVERAGE", lookback_years=1, history_dir=tmp_path)

    assert frame["Adj Close"].notna().all()
    assert frame["Adj Close"].equals(frame["Close"])


def test_ensure_usable_price_columns_accepts_valid_adj_close_with_sparse_close():
    # Both scoring paths prefer "Adj Close" whenever it exists -- requiring
    # "Close" to ALSO be usable before even checking Adj Close rejected an
    # otherwise scoreable frame (a sufficient Adj Close, a sparse/blank
    # Close) unnecessarily, falling through to another download, a stale
    # fallback, or PriceHistoryUnavailable and dropping health/backtest
    # results that were available all along (Codex review on PR #403:
    # "Accept valid adjusted prices without requiring Close").
    from pricing import _ensure_usable_price_columns

    frame = pd.DataFrame(
        {
            "Open": [1.0] * 10,
            "High": [1.0] * 10,
            "Low": [1.0] * 10,
            "Close": [float("nan")] * 9 + [1.0],  # only 1/10 valid -- well under the coverage threshold
            "Adj Close": [10.0] * 10,  # fully valid
        }
    )

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    assert list(result["Adj Close"]) == [10.0] * 10  # untouched -- no fallback needed


def test_ensure_usable_price_columns_prefers_gap_free_close_over_gappy_adj_close():
    # A column that clears the coverage/freshness bar can still have an
    # INTERNAL gap (a hole between its first and last valid observation)
    # -- score_candidate() skips BOTH health and backtest for whichever
    # column ends up selected if it has one. Adj Close was always
    # preferred once merely "usable," even when it had an internal gap
    # and Close (fully contiguous) was sitting right there as a gap-free
    # alternative, unnecessarily discarding two weighted signals despite
    # having a usable substitute (Antigravity review on PR #403: "Fall
    # back when adjusted prices contain internal gaps").
    from pricing import _ensure_usable_price_columns

    adj_close = [10.0, 10.0, 10.0, float("nan"), float("nan"), float("nan"), float("nan"), 10.0, 10.0, 10.0]
    close = [1.0] * 10  # fully valid, no gap
    frame = pd.DataFrame({"Close": close, "Adj Close": adj_close})

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    assert list(result["Adj Close"]) == close  # Close's gap-free series wins, not the gappy Adj Close


def test_ensure_usable_price_columns_returns_gappy_adj_close_when_no_gap_free_alternative():
    # When NEITHER usable column is gap-free, there's no better
    # alternative -- must still return the usable (if gappy) Adj Close
    # rather than rejecting the frame outright.
    from pricing import _ensure_usable_price_columns

    adj_close = [10.0, 10.0, 10.0, float("nan"), float("nan"), 10.0, 10.0, 10.0, 10.0, 10.0]
    close = [1.0, 1.0, 1.0, float("nan"), float("nan"), 1.0, 1.0, 1.0, 1.0, 1.0]  # same gap
    frame = pd.DataFrame({"Close": close, "Adj Close": adj_close})

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    # .equals(), not == -- NaN != NaN under plain equality, and both
    # series have one at the same position.
    assert result["Adj Close"].equals(pd.Series(adj_close))  # no gap-free alternative -- Adj Close still wins


def test_ensure_usable_price_columns_prefers_complete_close_over_truncated_adj_close():
    # "Gap-free" only means no hole BETWEEN the first and last valid
    # observation -- a column whose valid values are clustered at one EDGE
    # of the window (e.g. Adj Close populated only for the most recent
    # half) still clears coverage/freshness and _has_internal_gap alike,
    # even though a fully-populated Close sitting right there covers
    # materially more of the requested window. Previously this branch
    # returned the truncated Adj Close immediately because leading nulls
    # aren't an "internal" gap, silently scoring this candidate over a
    # shorter effective lookback than peers scored on their full window
    # (Codex review on PR #403: "Prefer complete Close history over
    # truncated adjusted prices").
    from pricing import _ensure_usable_price_columns

    adj_close = [float("nan")] * 10 + [10.0] * 10  # exactly 50% coverage -- clears the threshold, no internal gap
    close = [1.0] * 20  # fully valid
    frame = pd.DataFrame({"Close": close, "Adj Close": adj_close})

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    assert list(result["Adj Close"]) == close  # the complete Close series wins, not the truncated Adj Close


def test_ensure_usable_price_columns_keeps_adj_close_when_coverage_is_equal():
    # A tie in coverage keeps the existing Adj Close preference --
    # dividend-adjusted prices are financially more correct when both
    # columns cover the same span (Codex review on PR #403: "Prefer
    # complete Close history over truncated adjusted prices").
    from pricing import _ensure_usable_price_columns

    adj_close = [20.0] * 20
    close = [1.0] * 20
    frame = pd.DataFrame({"Close": close, "Adj Close": adj_close})

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    assert list(result["Adj Close"]) == adj_close


def test_ensure_usable_price_columns_handles_duplicate_column_labels():
    # A duplicate-labeled column ("Adj Close" appearing twice, from an
    # unflattened MultiIndex or a malformed CSV) makes
    # frame["Adj Close"] return a DataFrame instead of a Series --
    # .notna().sum() then returns a per-column Series, and the boolean
    # usability check raises "ValueError: The truth value of a Series is
    # ambiguous" instead of degrading this frame to unusable like any
    # other malformed input (Antigravity review on PR #403: "Duplicate
    # Column Handling in _ensure_usable_price_columns").
    from pricing import _ensure_usable_price_columns

    frame = pd.DataFrame(
        [[1.0, 1.0, 10.0, 10.5]] * 5,
        columns=["Open", "Close", "Adj Close", "Adj Close"],
    )

    result = _ensure_usable_price_columns(frame)  # must not raise

    assert result is not None
    assert not result.columns.duplicated().any()


def test_ensure_usable_price_columns_handles_missing_close_column():
    # Both current callers already guarantee "Close" is present before
    # reaching here, but this function is a shared, independently-callable
    # helper -- indexing frame["Close"] without checking first would raise
    # an unhandled KeyError for any other/future caller that doesn't carry
    # that guarantee (Antigravity review on PR #403: "Potential KeyError
    # Guard in _ensure_usable_price_columns").
    from pricing import _ensure_usable_price_columns

    frame = pd.DataFrame({"Open": [1.0, 2.0], "High": [1.0, 2.0]})  # no "Close" at all

    result = _ensure_usable_price_columns(frame)  # must not raise

    assert result is None


def test_ensure_usable_price_columns_replaces_infinite_prices_with_nan():
    # notna() alone treats +/-inf as "present," so a frame with a stray
    # inf price could pass the coverage check unchanged and then feed
    # _price_returns()'s pct_change() a transition FROM inf that computes
    # to a FINITE -1.0 (not inf) -- a fabricated "100% loss" that
    # survives that function's own np.isfinite() filter untouched, since
    # -1.0 IS finite (Antigravity review on PR #403: "Reject non-finite
    # price observations before scoring").
    from pricing import _ensure_usable_price_columns

    frame = pd.DataFrame({"Adj Close": [float("inf"), 100.0, 110.0, 111.0]})

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    assert math.isnan(result["Adj Close"].iloc[0])  # inf replaced with NaN, not left as a "valid" price


def test_ensure_usable_price_columns_replaces_non_positive_prices_with_nan():
    # A zero or negative price isn't a valid observation (a stock/ETF
    # doesn't legitimately trade at $0 or below; 0 is far more likely a
    # corrupt/missing-data sentinel) -- left in place, notna() alone
    # still counts it as "present" toward coverage, and the FINITE
    # transition INTO it (pct_change() computing exactly -1.0) fabricates
    # a "100% loss" that survives _price_returns()'s own np.isfinite()
    # filter untouched (Antigravity review on PR #403: "Reject zero and
    # negative prices before scoring").
    from pricing import _ensure_usable_price_columns

    frame = pd.DataFrame({"Adj Close": [100.0, 0.0, -5.0, 100.0]})

    result = _ensure_usable_price_columns(frame)

    assert result is not None
    assert math.isnan(result["Adj Close"].iloc[1])
    assert math.isnan(result["Adj Close"].iloc[2])


def test_ensure_usable_price_columns_rejects_stale_last_valid_price():
    # >= 50% populated overall clears the coverage check, but if every
    # valid value is clustered at the START of the window (the recent
    # portion entirely blank), the column's EFFECTIVE last price could be
    # weeks old. The frame-level staleness checks elsewhere only look at
    # the INDEX's last row, not the last VALID PRICE -- a frame can be
    # "index-fresh" while being "price-stale," and score_candidate()
    # deliberately ignores TRAILING gaps, so nothing else in the pipeline
    # catches this (Antigravity review on PR #403: "Check freshness of the
    # selected price series").
    from pricing import _ensure_usable_price_columns

    index = pd.date_range(end=datetime.now(timezone.utc).date().isoformat(), periods=30, freq="D")
    values = [100.0] * 15 + [float("nan")] * 15  # last valid price is 15 days old
    frame = pd.DataFrame({"Adj Close": values}, index=index)

    result = _ensure_usable_price_columns(frame)

    assert result is None


def test_ensure_usable_price_columns_accepts_fresh_last_valid_price():
    # The positive counterpart to the staleness test above -- a column
    # whose valid values extend up to (or near) the present must not be
    # rejected.
    from pricing import _ensure_usable_price_columns

    index = pd.date_range(end=datetime.now(timezone.utc).date().isoformat(), periods=30, freq="D")
    values = [100.0] * 25 + [float("nan")] * 5  # last valid price is 5 days old
    frame = pd.DataFrame({"Adj Close": values}, index=index)

    result = _ensure_usable_price_columns(frame)

    assert result is not None


def test_fetch_price_history_rejects_live_frame_with_unusable_close(tmp_path, monkeypatch):
    # Previously the Adj-Close fallback assignment happened unconditionally
    # -- if "Close" itself was unusable (and "Adj Close" absent/unusable
    # too), the fallback just copied one unusable series into the other
    # and _validate_live_frame() still returned the frame as "usable",
    # bypassing both an available stale-archive fallback and
    # PriceHistoryUnavailable and leaving the candidate with no
    # health/backtest signal at all (Codex review on PR #403: "Reject an
    # unusable live Close fallback").
    import pricing

    malformed = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [1.0, 2.0],
            "Low": [1.0, 2.0],
            "Close": [float("nan"), float("nan")],
            "Volume": [1, 1],
        },
        index=pd.date_range(datetime.now(timezone.utc).date().isoformat(), periods=2),
    )
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: malformed)

    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADLIVECLOSE", lookback_years=3, history_dir=tmp_path)


def test_fetch_price_history_does_not_warn_on_fresh_complete_archive(tmp_path, capsys, monkeypatch):
    # lookback_years=1 with a genuinely dense ~150-business-day daily
    # archive clears every completeness check: recent last row (not
    # stale), >=50% calendar-span coverage, AND >=50% row-count coverage of
    # the requested window's expected trading days -- so no fallback path,
    # no warning of any kind. A 2-row fixture with the same calendar span
    # would pass the span check while being wildly sparse, which the
    # row-count check now also guards against (Codex review on PR #403:
    # "Count observations when judging archive completeness").
    _stub_yfinance_unavailable(monkeypatch)
    business_days = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=150)
    rows = [(day.strftime("%Y-%m-%d"), 100, 101, 99, 100, 100, 1000) for day in business_days]
    _write_archive_csv(tmp_path, "FRESH", rows)

    fetch_price_history("FRESH", lookback_years=1, history_dir=tmp_path)

    assert capsys.readouterr().err == ""


def test_fetch_price_history_warns_on_incomplete_but_not_stale_archive(tmp_path, capsys, monkeypatch):
    # Last row is YESTERDAY (not stale by date) but the archive only spans
    # a few days -- far short of the requested 3-year lookback -- so it
    # must still route through the fallback path (Codex review on PR
    # #403: "Refresh archives that lack the requested history"). 1..3
    # days ago, not 0..2 -- fetch_price_history() now unconditionally
    # drops "today"'s row (Antigravity review on PR #403: "Exclude
    # today's bar from archived histories").
    _stub_yfinance_unavailable(monkeypatch)
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(3, 0, -1)]
    _write_archive_csv(tmp_path, "THIN", rows)

    frame = fetch_price_history("THIN", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 3
    stderr = capsys.readouterr().err.lower()
    assert "only" in stderr and "lookback" in stderr


def test_fetch_price_history_deduplicates_archive_dates(tmp_path, monkeypatch):
    # A duplicate Date row (a source anomaly, or a prior interrupted/corrupted
    # merge) must not reach _beta_alpha's inner join and inflate the aligned
    # series via a many-to-many match on the duplicated label (Antigravity
    # review on PR #403: "Deduplicate Dates in Price History to Prevent
    # Cartesian Join Skew"). keep="last" matches this repo's own merge
    # convention: a later-written row wins.
    _stub_yfinance_unavailable(monkeypatch)
    dupe_date = _recent_date(5)
    # 1..10 days ago, not 0..9 -- see the "today" note in
    # test_fetch_price_history_prefers_local_archive.
    rows = [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 0, -1)]
    rows.append((dupe_date, 999, 999, 999, 999, 999, 9999))  # later-written row for the same date
    _write_archive_csv(tmp_path, "DUPES", rows)

    frame = fetch_price_history("DUPES", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 10  # 10 distinct dates, not 11 rows
    assert frame.index.is_unique
    dupe_row = frame.loc[pd.Timestamp(dupe_date)]
    assert dupe_row["Close"] == 999  # the later-written duplicate wins, not the first


def test_fetch_price_history_deduplicate_keeps_file_order_with_interleaved_duplicates(tmp_path, monkeypatch):
    # sort_index()'s default quicksort is not stable -- for equal-Date rows
    # it can reorder them relative to their original CSV order, so
    # duplicated(keep="last") is not reliably keeping the file's LATER row
    # once duplicate dates are interleaved with other dates rather than
    # sitting adjacent at the end of the file (Codex review on PR #403:
    # "Preserve file order when resolving duplicate dates").
    _stub_yfinance_unavailable(monkeypatch)
    d0, d1, d2 = _recent_date(9), _recent_date(8), _recent_date(7)
    rows = [
        (d0, 100, 101, 99, 100, 100, 1000),  # d0, first write: stale
        (d1, 100, 101, 99, 200, 200, 1000),  # d1, first write: stale
        (d0, 100, 101, 99, 111, 111, 1000),  # d0, later write: should win
        (d2, 100, 101, 99, 300, 300, 1000),
        (d1, 100, 101, 99, 222, 222, 1000),  # d1, later write: should win
    ]
    rows += [(_recent_date(days_ago), 100, 101, 99, 100, 100, 1000) for days_ago in range(6, -1, -1)]
    _write_archive_csv(tmp_path, "INTERLEAVED", rows)

    frame = fetch_price_history("INTERLEAVED", lookback_years=3, history_dir=tmp_path)

    assert frame.loc[pd.Timestamp(d0)]["Close"] == 111
    assert frame.loc[pd.Timestamp(d1)]["Close"] == 222


def test_fetch_price_history_does_not_wrongly_reject_intraday_timestamped_today_as_future(tmp_path, monkeypatch):
    # Comparing the future-date-rejection THRESHOLD against
    # pd.Timestamp.now().normalize() (midnight) instead of the actual
    # current moment would wrongly reject a legitimately same-day row
    # that happens to carry ANY nonzero intraday time component (e.g. a
    # market-close timestamp) as "future," just because it's later in the
    # day than midnight (Antigravity review on PR #403: "Potential Edge
    # Case in pricing.py Date Filtering") -- that specific miscomparison
    # would silently drop the row. The row is separately,
    # deliberately excluded by the UNCONDITIONAL "exclude today" filter
    # added later (Antigravity review on PR #403: "Exclude today's bar
    # from archived histories"), so the end-to-end row count reflects
    # THAT exclusion -- this test only confirms the two filters compose
    # without error and the other 9 rows are unaffected. A tiny
    # "00:00:01" offset is enough to distinguish the future-date
    # threshold from a midnight-normalized one, without the test
    # depending on what time of day it happens to run.
    _stub_yfinance_unavailable(monkeypatch)
    rows = [(f"{_recent_date(days_ago)}T00:00:00", 100, 101, 99, 100, 100, 1000) for days_ago in range(10, 1, -1)]
    rows.append((f"{_recent_date(0)}T00:00:01", 100, 101, 99, 100, 100, 1000))
    _write_archive_csv(tmp_path, "INTRADAY", rows)

    frame = fetch_price_history("INTRADAY", lookback_years=3, history_dir=tmp_path)

    assert len(frame) == 9  # the other 9 rows survive; today's intraday row is excluded
    assert pd.Timestamp(datetime.now(timezone.utc).date()) not in frame.index


def test_fetch_price_history_falls_back_to_close_when_adj_close_unusable(tmp_path, monkeypatch):
    # An archive whose "Adj Close" is blank/NaN for (almost) every row while
    # "Close" stays valid clears every completeness check (they count
    # rows/span, not per-column NaN-ness) -- without a fallback, the
    # candidate would silently lose health/backtest data since Adj Close is
    # preferred whenever the column exists (Codex review on PR #403:
    # "Reject archives with unusable adjusted prices").
    _stub_yfinance_unavailable(monkeypatch)
    rows = [(_recent_date(days_ago), 100, 101, 99, 100 + days_ago, "", 1000) for days_ago in range(9, -1, -1)]
    _write_archive_csv(tmp_path, "BADADJ", rows)

    frame = fetch_price_history("BADADJ", lookback_years=3, history_dir=tmp_path)

    assert frame["Adj Close"].notna().all()
    assert frame["Adj Close"].equals(frame["Close"])


def test_fetch_price_history_treats_archive_as_unavailable_when_close_also_unusable(tmp_path, monkeypatch):
    # When "Close" itself is mostly/entirely blank, there's no usable price
    # series left to fall back TO -- the date-span/row-count completeness
    # checks only look at row presence, not price validity, so this
    # archive would otherwise still be accepted and the Adj-Close fallback
    # would just replace one unusable series with another (Codex review on
    # PR #403: "Validate Close before using it as the adjusted-price
    # fallback").
    import pricing

    rows = [(_recent_date(days_ago), 100, 101, 99, "", "", 1000) for days_ago in range(9, -1, -1)]
    _write_archive_csv(tmp_path, "BADBOTH", rows)

    live_frame = _long_live_frame()
    monkeypatch.setattr(pricing, "_load_from_yfinance", lambda ticker, lookback_years: live_frame)

    frame = fetch_price_history("BADBOTH", lookback_years=3, history_dir=tmp_path)

    assert (frame["Close"] == 1.0).all()  # live data, since the unusable archive offered no fallback

    monkeypatch.setattr(
        pricing, "_load_from_yfinance", lambda ticker, lookback_years: (_ for _ in ()).throw(ImportError("no net"))
    )
    with pytest.raises(PriceHistoryUnavailable):
        fetch_price_history("BADBOTH", lookback_years=3, history_dir=tmp_path)


def _fake_yfinance_module(*, raise_on_kwarg, columns_are_multiindex):
    """A minimal stand-in for the `yfinance` module, no real install needed."""
    calls = []

    def download(ticker, **kwargs):
        calls.append(kwargs)
        if raise_on_kwarg and "multi_level_index" in kwargs:
            raise TypeError("download() got an unexpected keyword argument 'multi_level_index'")
        index = pd.date_range("2024-01-01", periods=5)
        if columns_are_multiindex:
            columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Adj Close", "Volume"], [ticker]])
        else:
            columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        return pd.DataFrame(1.0, index=index, columns=columns)

    module = types.SimpleNamespace(download=download)
    return module, calls


def test_load_from_yfinance_retries_without_unsupported_kwarg(monkeypatch):
    fake_module, calls = _fake_yfinance_module(raise_on_kwarg=True, columns_are_multiindex=True)
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)

    frame = _load_from_yfinance("ACME", lookback_years=3)

    assert len(calls) == 2  # first call (with the kwarg) raised, second succeeded
    assert "multi_level_index" not in calls[1]
    assert not isinstance(frame.columns, pd.MultiIndex)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def test_load_from_yfinance_translates_dotted_share_class_symbol(monkeypatch):
    # Yahoo Finance addresses share classes with a hyphen (BRK-B), not the
    # dot config/tickers.json's own schema requires (BRK.B,
    # ^[A-Z.]{1,6}$) -- passing the dotted form straight through silently
    # loses health/backtest data for a security that actually has full
    # price history available (Codex review on PR #403: "Translate dotted
    # share-class symbols for Yahoo").
    requested_tickers = []

    def download(ticker, **kwargs):
        requested_tickers.append(ticker)
        index = pd.date_range("2024-01-01", periods=5)
        columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        return pd.DataFrame(1.0, index=index, columns=columns)

    module = types.SimpleNamespace(download=download)
    monkeypatch.setitem(sys.modules, "yfinance", module)

    _load_from_yfinance("BRK.B", lookback_years=3)

    assert requested_tickers == ["BRK-B"]


def test_load_from_yfinance_normal_path_unaffected(monkeypatch):
    fake_module, calls = _fake_yfinance_module(raise_on_kwarg=False, columns_are_multiindex=False)
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)

    frame = _load_from_yfinance("ACME", lookback_years=3)

    assert len(calls) == 1
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def test_load_from_yfinance_flattens_multiindex_even_when_kwarg_accepted(monkeypatch):
    # Some yfinance versions/conditions can still return MultiIndex columns
    # even when multi_level_index=False is accepted without raising -- the
    # flattening previously only ran inside the TypeError-retry branch, so
    # this combination (kwarg accepted, columns still a MultiIndex) escaped
    # it entirely (Antigravity review on PR #403: "Always Flatten MultiIndex
    # Columns").
    fake_module, calls = _fake_yfinance_module(raise_on_kwarg=False, columns_are_multiindex=True)
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)

    frame = _load_from_yfinance("ACME", lookback_years=3)

    assert len(calls) == 1  # the kwarg call succeeded -- no retry needed
    assert not isinstance(frame.columns, pd.MultiIndex)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def test_load_from_yfinance_resolves_multiindex_when_ticker_is_level_zero(monkeypatch):
    # Which level holds the field name ("Close", "Open", ...) vs. the
    # ticker symbol has flipped across yfinance versions/flags -- blindly
    # taking level 0 would flatten a (ticker, field)-ordered frame into
    # duplicate ticker-symbol column names, silently discarding "Close"
    # and making _validate_live_frame() reject perfectly good downloaded
    # data (Antigravity review on PR #403: "MultiIndex Level Resolution
    # Risk in _load_from_yfinance").
    def download(ticker, **kwargs):
        index = pd.date_range("2024-01-01", periods=5)
        # (ticker, field) order -- level 0 is the ticker symbol repeated,
        # not the field name, the reverse of the existing fixture's order.
        columns = pd.MultiIndex.from_product([[ticker], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
        return pd.DataFrame(1.0, index=index, columns=columns)

    module = types.SimpleNamespace(download=download)
    monkeypatch.setitem(sys.modules, "yfinance", module)

    frame = _load_from_yfinance("ACME", lookback_years=3)

    assert not isinstance(frame.columns, pd.MultiIndex)
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
