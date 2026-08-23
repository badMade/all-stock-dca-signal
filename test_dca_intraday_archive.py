"""Tests for the intraday observation archive."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dca_intraday_archive import (
    OBSERVATION_FIELDS,
    IntradayObservation,
    _bool_or_none,
    _iso_or_blank,
    _slot_from_classification,
    append_observations,
    archive_observations_safely,
    configured_intraday_archive_dir,
    observations_from_signal_rows,
    read_ticker_observations,
    write_run_snapshot,
)

EASTERN = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (None, None),
        ("", None),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("TRUE", True),
        (" True ", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("something_else", None),
        (1, True),
        (0, False),
    ],
)
def test_bool_or_none(value, expected):
    assert _bool_or_none(value) == expected


def _classification(stream="morning", scheduled_for_et="10:30", **overrides):
    base = {
        "stream": stream,
        "scheduled_for_et": scheduled_for_et,
        "run_id": f"{stream}_live_20260804_{scheduled_for_et.replace(':', '')}ET",
        "run_type": f"{stream}_dca_live",
        "manual_run": False,
        "trading_day": "2026-08-04",
    }
    base.update(overrides)
    return base


def _morning_row(ticker="AMD", **overrides):
    row = {
        "ticker": ticker,
        "source": "yahoo",
        "action": "BUY",
        "current_price": 100.5,
        "previous_close": 110.0,
        "previous_low": 99.0,
        "drop_pct": 8.636,
        "multiplier": 3.0,
        "price_source": "completed_bar",
        "price_as_of": "2026-08-04T10:25:00-04:00",
        "bar_timestamp": "2026-08-04T10:25:00-04:00",
        "bar_open": 101.0,
        "bar_high": 101.5,
        "bar_low": 100.2,
        "bar_close": 100.5,
        "bar_volume": 12345.0,
        "session_open": 108.0,
        "session_high": 108.5,
        "session_low": 100.2,
        "session_volume": 987654.0,
        "bar_count": 12,
        "interval_minutes": 5,
        "warnings": "",
    }
    row.update(overrides)
    return row


def _observations(archive, rows, classification=None, generated_at="2026-08-04T10:30:00"):
    return observations_from_signal_rows(
        rows,
        run_classification=classification or _classification(),
        generated_at=generated_at,
        data_source="yahoo",
    )


# --- directory resolution ----------------------------------------------------


def test_archive_dir_follows_ticker_history_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("DCA_INTRADAY_ARCHIVE_DIR", raising=False)
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(tmp_path / "Tickers"))
    assert configured_intraday_archive_dir() == tmp_path / "Tickers" / "_intraday"


def test_explicit_archive_dir_overrides_ticker_history_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(tmp_path / "Tickers"))
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(tmp_path / "elsewhere"))
    assert configured_intraday_archive_dir() == tmp_path / "elsewhere"


# --- write / merge semantics -------------------------------------------------


def test_append_writes_one_file_per_ticker(tmp_path):
    observations = _observations(tmp_path, [_morning_row("AMD"), _morning_row("NVDA")])
    summary = append_observations(observations, archive_dir=tmp_path)

    assert summary["tickers_written"] == 2
    assert summary["rows_added"] == 2
    assert summary["rows_replaced"] == 0
    assert (tmp_path / "AMD.csv").exists()
    assert (tmp_path / "NVDA.csv").exists()


def test_header_is_the_declared_field_order(tmp_path):
    append_observations(_observations(tmp_path, [_morning_row()]), archive_dir=tmp_path)
    with (tmp_path / "AMD.csv").open(newline="") as handle:
        header = next(csv.reader(handle))
    assert tuple(header) == OBSERVATION_FIELDS


def test_prices_and_timestamps_round_trip(tmp_path):
    append_observations(_observations(tmp_path, [_morning_row()]), archive_dir=tmp_path)
    (row,) = read_ticker_observations("AMD", archive_dir=tmp_path)

    assert row["price"] == "100.5"
    assert row["price_source"] == "completed_bar"
    assert row["price_as_of"] == "2026-08-04T10:25:00-04:00"
    assert row["previous_close"] == "110.0"
    assert row["bar_volume"] == "12345.0"
    assert row["session_volume"] == "987654.0"
    assert row["stream"] == "morning"
    assert row["slot"] == "1030"
    assert row["trading_day"] == "2026-08-04"


def test_rerunning_the_same_slot_replaces_rather_than_duplicates(tmp_path):
    """A recovery rerun of one slot must leave one row, not two."""
    first = _observations(tmp_path, [_morning_row(current_price=100.5)])
    append_observations(first, archive_dir=tmp_path)

    second = _observations(tmp_path, [_morning_row(current_price=99.25)])
    summary = append_observations(second, archive_dir=tmp_path)

    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["price"] == "99.25"
    assert summary["rows_replaced"] == 1
    assert summary["rows_added"] == 0


def test_different_slots_accumulate(tmp_path):
    for stream, slot in (("market_open", "09:35"), ("morning", "10:30"), ("midday", "12:30")):
        append_observations(
            _observations(
                tmp_path,
                [_morning_row()],
                classification=_classification(stream=stream, scheduled_for_et=slot),
                generated_at=f"2026-08-04T{slot}:00",
            ),
            archive_dir=tmp_path,
        )
    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert [r["slot"] for r in rows] == ["0935", "1030", "1230"]
    assert [r["stream"] for r in rows] == ["market_open", "morning", "midday"]


def test_rows_stay_sorted_when_slots_arrive_out_of_order(tmp_path):
    for slot in ("14:30", "09:35", "12:30"):
        append_observations(
            _observations(
                tmp_path,
                [_morning_row()],
                classification=_classification(stream="intraday", scheduled_for_et=slot),
                generated_at=f"2026-08-04T{slot}:00",
            ),
            archive_dir=tmp_path,
        )
    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert [r["slot"] for r in rows] == ["0935", "1230", "1430"]


def test_duplicate_tickers_within_one_batch_collapse(tmp_path):
    """Two rows for the same ticker/slot in one run must not both land."""
    batch = _observations(
        tmp_path, [_morning_row(current_price=1.0), _morning_row(current_price=2.0)]
    )
    append_observations(batch, archive_dir=tmp_path)
    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["price"] == "2.0"


def test_ticker_with_path_separators_cannot_escape_the_archive(tmp_path):
    observation = IntradayObservation(
        ticker="../../etc/passwd",
        trading_day="2026-08-04",
        stream="morning",
        slot="1030",
        observed_at_et="2026-08-04T10:30:00-04:00",
        observed_at_utc="2026-08-04T14:30:00+00:00",
    )
    append_observations([observation], archive_dir=tmp_path)
    written = list(tmp_path.glob("*.csv"))
    assert [p.name for p in written] == ["_.._ETC_PASSWD.csv"]
    assert written[0].parent == tmp_path
    assert not (tmp_path.parent / "etc").exists()


# --- timestamp handling ------------------------------------------------------


def test_naive_generated_at_is_read_as_eastern_not_local(tmp_path):
    """run_state writes naive ET wall-clock; a UTC host must not shift it."""
    observations = _observations(tmp_path, [_morning_row()], generated_at="2026-08-04T10:30:00")
    assert observations[0].observed_at_et == "2026-08-04T10:30:00-04:00"
    assert observations[0].observed_at_utc == "2026-08-04T14:30:00+00:00"


def test_aware_generated_at_is_preserved(tmp_path):
    aware = datetime(2026, 8, 4, 14, 30, tzinfo=timezone.utc)
    observations = _observations(tmp_path, [_morning_row()], generated_at=aware)
    assert observations[0].observed_at_utc == "2026-08-04T14:30:00+00:00"
    assert observations[0].observed_at_et == "2026-08-04T10:30:00-04:00"


def test_unparseable_generated_at_degrades_to_empty_timestamps(tmp_path):
    observations = _observations(tmp_path, [_morning_row()], generated_at="not-a-time")
    assert observations[0].observed_at_et == ""
    assert observations[0].observed_at_utc == ""
    # The trading day still comes from the classification, so the row is keyed.
    assert observations[0].trading_day == "2026-08-04"


# --- near-close row shape ----------------------------------------------------


def test_near_close_row_shape_is_understood(tmp_path):
    """near_close_dca._signal_row uses different column names for the same data."""
    row = {
        "ticker": "TSM",
        "source": "finance",
        "action": "BUY",
        "decision_bar_close": 240.75,
        "previous_close": 250.0,
        "previous_low": 239.0,
        "drop_vs_previous_close_pct": 3.7,
        "buy_multiplier": 2.0,
        "decision_bar_timestamp": "2026-08-04T15:25:00-04:00",
        "previous_close_timestamp": "2026-08-03T16:00:00-04:00",
        "quote_price": 240.71,
        "quote_time": "2026-08-04T15:29:58-04:00",
    }
    (observation,) = observations_from_signal_rows(
        [row],
        run_classification=_classification(stream="afternoon", scheduled_for_et="15:30"),
        generated_at="2026-08-04T15:30:00",
        data_source="finance",
    )
    assert observation.price == 240.75
    assert observation.bar_close == 240.75
    assert observation.drop_pct == 3.7
    assert observation.multiplier == 2.0
    assert observation.bar_timestamp == "2026-08-04T15:25:00-04:00"
    assert observation.previous_close_as_of == "2026-08-03T16:00:00-04:00"
    assert observation.quote_price == 240.71
    assert observation.slot == "1530"


def test_rows_without_a_ticker_are_dropped(tmp_path):
    observations = _observations(tmp_path, [{"source": "yahoo"}, _morning_row()])
    assert [o.ticker for o in observations] == ["AMD"]


# --- failure containment -----------------------------------------------------


def test_archive_failure_is_reported_not_raised(tmp_path):
    blocker = tmp_path / "archive"
    blocker.write_text("not a directory")
    summary = archive_observations_safely(
        _observations(tmp_path, [_morning_row()]), archive_dir=blocker
    )
    assert summary["ok"] is False
    assert summary["error"]
    assert summary["rows_added"] == 0



def test_archive_observations_safely_catches_append_exceptions(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("dca_intraday_archive.append_observations", boom)

    summary = archive_observations_safely([])
    assert summary["ok"] is False
    assert "simulated failure" in summary["error"]


def test_archive_success_reports_ok(tmp_path):
    summary = archive_observations_safely(
        _observations(tmp_path, [_morning_row()]), archive_dir=tmp_path
    )
    assert summary["ok"] is True
    assert summary["rows_added"] == 1


# --- per-run snapshot --------------------------------------------------------


def test_run_snapshot_carries_the_ticker_column(tmp_path):
    path = write_run_snapshot(tmp_path, _observations(tmp_path, [_morning_row("AMD")]))
    assert path is not None
    with Path(path).open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["ticker"] == "AMD"
    assert rows[0]["price"] == "100.5"


def test_run_snapshot_of_an_empty_run_still_writes_a_header(tmp_path):
    path = write_run_snapshot(tmp_path, [])
    with Path(path).open(newline="") as handle:
        header = next(csv.reader(handle))
    assert header[0] == "ticker"





def _observation(**overrides):
    fields = dict(
        ticker="AMD",
        trading_day="2026-08-04",
        stream="intraday",
        slot="1130",
        observed_at_et="2026-08-04T11:30:00-04:00",
        observed_at_utc="2026-08-04T15:30:00+00:00",
        run_id="intraday_live_20260804_1130",
        source="robinhood",
        manual_run=False,
        price=100.0,
    )
    fields.update(overrides)
    return IntradayObservation(**fields)


def test_scheduled_retry_with_a_different_source_replaces_the_row(tmp_path):
    # Codex review ("Replace scheduled observations when the source
    # changes"): a retry or recovery of the same scheduled slot can resolve
    # its price from an alternate provider. Because a scheduled row's
    # identity is (trading_day, stream, slot, ticker) -- source is NOT part
    # of it -- the retry must replace the original row, not duplicate it.
    first = append_observations([_observation(source="robinhood", price=100.0)], archive_dir=tmp_path)
    assert first["rows_added"] == 1
    assert first["rows_replaced"] == 0

    retry = append_observations([_observation(source="yfinance", price=101.0)], archive_dir=tmp_path)
    assert retry["rows_added"] == 0
    assert retry["rows_replaced"] == 1

    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["source"] == "yfinance"
    assert rows[0]["price"] == "101.0"


def test_two_manual_polls_on_the_same_slot_both_land_in_the_archive(tmp_path):
    # Codex review ("Preserve distinct manual observations"): unlike a
    # scheduled poll, a manual run does not own a fixed slot -- two
    # legitimate manual polls on the same trading day normally share
    # stream/slot/ticker/source, so run_id must be folded into the key for
    # manual rows or the second poll silently replaces the first.
    first = append_observations(
        [_observation(manual_run=True, run_id="manual_1", price=100.0)], archive_dir=tmp_path
    )
    second = append_observations(
        [_observation(manual_run=True, run_id="manual_2", price=99.0)], archive_dir=tmp_path
    )

    assert first["rows_added"] == 1
    assert second["rows_added"] == 1
    assert second["rows_replaced"] == 0
    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert {r["run_id"] for r in rows} == {"manual_1", "manual_2"}


def test_a_manual_retry_with_the_same_run_id_still_replaces_its_own_row(tmp_path):
    append_observations(
        [_observation(manual_run=True, run_id="manual_1", price=100.0)], archive_dir=tmp_path
    )
    retry = append_observations(
        [_observation(manual_run=True, run_id="manual_1", price=98.0)], archive_dir=tmp_path
    )

    assert retry["rows_added"] == 0
    assert retry["rows_replaced"] == 1
    rows = read_ticker_observations("AMD", archive_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["price"] == "98.0"


def test_945_preflight_and_watchlist_rows_stay_distinct(tmp_path):
    # Codex review ("Keep 9:45 archive rows distinct by run type"): the 9:45
    # ET scheduler invokes MorningDCAExecutor twice back to back -- once in
    # preflight mode, once in watchlist mode -- and both produce scheduled
    # rows sharing (trading_day, stream='morning', slot='0945', ticker).
    # Without run_type in scheduled identity, whichever mode archives last
    # would silently replace the other's distinct action/context.
    preflight = append_observations(
        [
            _observation(
                ticker="AAPL",
                stream="morning",
                slot="0945",
                run_type="morning_dca_preflight",
                action="eligible",
                price=100.0,
            )
        ],
        archive_dir=tmp_path,
    )
    watchlist = append_observations(
        [
            _observation(
                ticker="AAPL",
                stream="morning",
                slot="0945",
                run_type="morning_dca_watchlist_scan",
                action="no_action",
                price=100.0,
            )
        ],
        archive_dir=tmp_path,
    )

    assert preflight["rows_added"] == 1
    assert watchlist["rows_added"] == 1
    assert watchlist["rows_replaced"] == 0
    rows = read_ticker_observations("AAPL", archive_dir=tmp_path)
    assert {r["run_type"] for r in rows} == {"morning_dca_preflight", "morning_dca_watchlist_scan"}


def test_slot_from_classification_zero_pads_three_digit_times():
    # Antigravity review ("Inconsistent Slot Format for 3-Digit Scheduled
    # Times"): market open's "9:35" must produce "0935", matching the
    # 4-digit shape every other scheduled slot ("10:30" -> "1030") produces.
    assert _slot_from_classification({"scheduled_for_et": "9:35"}) == "0935"
    assert _slot_from_classification({"scheduled_for_et": "10:30"}) == "1030"


def test_slot_from_classification_falls_back_for_non_numeric_input():
    assert _slot_from_classification({"scheduled_for_et": ""}) == ""
    assert _slot_from_classification({}) == ""


def test_append_observations_refuses_to_create_the_archive_root_when_missing(tmp_path, monkeypatch):
    # Codex review ("Don't create a local archive when the Drive link is
    # absent"): ticker_data/ is a gitignored symlink/mount to the managed
    # Google Drive archive, not something this repo owns. When the caller
    # relies on the DEFAULT archive location (archive_dir=None) and that
    # mount is missing, append_observations must refuse rather than
    # materialize a real local stand-in directory -- a later maintenance run
    # would otherwise see that empty stub and treat the managed archive as
    # available when it never was.
    missing_root = tmp_path / "ticker_data_not_mounted"
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(missing_root))

    result = archive_observations_safely([_observation()])

    assert result["ok"] is False
    assert "does not exist" in result["error"]
    assert "DCA_TICKER_HISTORY_DIR" in result["error"]
    assert not missing_root.exists()


def test_append_observations_still_writes_to_an_explicit_archive_dir_when_the_default_root_is_missing(
    tmp_path, monkeypatch
):
    # The guard only protects the DEFAULT archive location. A caller that
    # passes an explicit archive_dir (as every test in this file, and the
    # per-run write_run_snapshot/backfill callers, do) is opting into that
    # directory on purpose and must not be blocked by an unrelated missing
    # Drive mount.
    missing_root = tmp_path / "ticker_data_not_mounted"
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(missing_root))
    explicit_dir = tmp_path / "explicit_archive"

    result = append_observations([_observation()], archive_dir=explicit_dir)

    assert result["rows_added"] == 1
    assert explicit_dir.is_dir()


def test_append_observations_respects_the_intraday_archive_dir_override_when_ticker_data_is_missing(
    tmp_path, monkeypatch
):
    # DCA_INTRADAY_ARCHIVE_DIR is configured_intraday_archive_dir()'s own
    # override, used by every wiring test in this repo (see
    # test_intraday_archive_wiring.py's autouse _isolated_archive fixture) to
    # redirect archive writes into an isolated tmp dir. That override has
    # nothing to do with ticker_data/'s own availability, so the missing-root
    # guard must not fire just because archive_dir was left as the None
    # default -- only when the call is ACTUALLY falling through to
    # ticker_data/_intraday. A CI runner with no ticker_data checkout at all
    # broke every intraday-archive-wiring test on this exact gap.
    missing_root = tmp_path / "ticker_data_not_mounted"
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(missing_root))
    override_dir = tmp_path / "isolated_archive"
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(override_dir))

    result = append_observations([_observation()])

    assert result["rows_added"] == 1
    assert override_dir.is_dir()
    assert not missing_root.exists()


def test_iso_or_blank():
    assert _iso_or_blank(None) == ""
    assert _iso_or_blank("") == ""

    dt = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert _iso_or_blank(dt) == "2023-01-01T12:00:00+00:00"

    assert _iso_or_blank("2023-01-01") == "2023-01-01"
    assert _iso_or_blank(123) == "123"
