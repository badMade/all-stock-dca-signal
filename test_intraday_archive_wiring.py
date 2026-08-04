"""End-to-end checks that scheduled polls actually land in the intraday archive.

test_dca_intraday_archive.py covers the archive module in isolation. This file
covers the wiring: that a real executor run writes real prices and real
timestamps into the shared per-ticker archive, that each stream/slot lands as
its own row, and that an archive failure degrades to a warning instead of
failing a run that priced money.
"""

from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")

import pytest

from dca_intraday_archive import read_ticker_observations
from market_open_dca import MarketOpenConfig, MarketOpenDCAExecutor
from midday_dca import MiddayConfig, MiddayDCAExecutor
from morning_dca import MorningConfig, MorningDCAExecutor, session_stats
from near_close_dca import EASTERN, Bar, SourceName


@pytest.fixture(autouse=True)
def _isolated_archive(tmp_path, monkeypatch):
    """Point the archive at a temp dir so no test writes the real Drive folder."""
    archive = tmp_path / "archive"
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(archive))
    return archive


class RichProvider:
    """Provider with distinct OHLCV per bar and a live quote at the anchor.

    Unlike the flat-price doubles in test_morning_dca.py, every bar here has a
    different open/high/low/close/volume, so a test can prove the archived row
    carries THIS bar's numbers rather than a coincidence of equal values.
    """

    source = SourceName.ROBINHOOD

    def __init__(self, previous_close=100.0, drop=0.04, quote=None):
        self._prev = previous_close
        self._drop = drop
        self._quote = quote

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        p = self._prev
        return Bar(timestamp=ts, open=p, high=p * 1.02, low=p * 0.98, close=p, volume=5_000_000.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        final = self._prev * (1.0 - self._drop)
        start_price = self._prev
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        anchor_idx = max(0, len(timestamps) - 2)
        bars = []
        for i, ts in enumerate(timestamps):
            if anchor_idx > 0 and i <= anchor_idx:
                price = start_price + (final - start_price) * i / anchor_idx
            else:
                price = final
            bars.append(
                Bar(
                    timestamp=ts,
                    open=price * 1.001,
                    high=price * 1.004,
                    low=price * 0.996,
                    close=price,
                    volume=1000.0 + i,
                )
            )
        return bars

    def get_bid_ask(self, ticker, as_of):
        price = self._prev * (1.0 - self._drop)
        return (price * 0.9995, price * 1.0005)

    def get_quote(self, ticker, as_of):
        return self._quote


def _watchlist_now():
    return datetime.combine(date(2026, 7, 8), time(9, 45), tzinfo=EASTERN)


# --- the archive is written at all -------------------------------------------


def test_scheduled_run_archives_every_polled_ticker(tmp_path):
    cfg = MorningConfig(tickers=("AMD", "NVDA"), morning_run_state_root=str(tmp_path / "runs"))
    MorningDCAExecutor(cfg).run(_watchlist_now(), RichProvider())

    for ticker in ("AMD", "NVDA"):
        rows = read_ticker_observations(ticker)
        assert len(rows) == 1, f"{ticker} was polled but not archived"
        assert rows[0]["stream"] == "morning"
        assert rows[0]["slot"] == "0945"
        assert rows[0]["trading_day"] == "2026-07-08"


def test_archived_price_matches_the_signal_the_run_acted_on(tmp_path):
    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    result = MorningDCAExecutor(cfg).run(_watchlist_now(), RichProvider())

    (signal_row,) = result["artifacts"]["signal_rows"]
    (archived,) = read_ticker_observations("AMD")
    assert float(archived["price"]) == pytest.approx(signal_row["current_price"])
    assert float(archived["previous_close"]) == pytest.approx(signal_row["previous_close"])
    assert float(archived["drop_pct"]) == pytest.approx(signal_row["drop_pct"])


def test_archived_row_carries_a_price_timestamp_and_volume(tmp_path):
    """The two things the old signals.csv could not answer."""
    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    MorningDCAExecutor(cfg).run(_watchlist_now(), RichProvider())

    (archived,) = read_ticker_observations("AMD")
    assert archived["price_source"] == "completed_bar"
    # The price is as of the completed bar, which is strictly BEFORE the 09:45
    # decision anchor -- that gap is exactly what a bare run timestamp hid.
    price_as_of = datetime.fromisoformat(archived["price_as_of"])
    observed_at = datetime.fromisoformat(archived["observed_at_et"])
    assert price_as_of < observed_at
    assert archived["bar_timestamp"] == archived["price_as_of"]
    assert float(archived["bar_volume"]) > 0
    assert float(archived["session_volume"]) > float(archived["bar_volume"])
    assert int(archived["interval_minutes"]) == cfg.interval_minutes


def test_quote_sourced_price_is_labelled_as_a_quote(tmp_path):
    anchor = datetime.combine(date(2026, 7, 8), time(9, 44), tzinfo=EASTERN)
    provider = RichProvider(quote=(93.75, anchor))
    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    MorningDCAExecutor(cfg).run(_watchlist_now(), provider)

    (archived,) = read_ticker_observations("AMD")
    assert archived["price_source"] == "quote"
    assert float(archived["price"]) == pytest.approx(93.75)
    assert archived["price_as_of"] == anchor.isoformat()
    assert float(archived["quote_price"]) == pytest.approx(93.75)


def test_rejected_quote_is_still_archived_beside_the_bar_price(tmp_path):
    """A stale quote is exactly when knowing what the feed returned matters."""
    stale = datetime.combine(date(2026, 7, 8), time(9, 5), tzinfo=EASTERN)
    provider = RichProvider(quote=(88.0, stale))
    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    MorningDCAExecutor(cfg).run(_watchlist_now(), provider)

    (archived,) = read_ticker_observations("AMD")
    assert archived["price_source"] == "completed_bar"
    assert float(archived["quote_price"]) == pytest.approx(88.0)
    assert archived["quote_time"] == stale.isoformat()
    assert float(archived["price"]) != pytest.approx(88.0)


def test_ticker_with_no_data_is_archived_as_a_polled_row(tmp_path):
    class NoDataProvider(RichProvider):
        def get_previous_close_bar(self, ticker, as_of):
            return None

    cfg = MorningConfig(tickers=("GFUZ",), morning_run_state_root=str(tmp_path / "runs"))
    MorningDCAExecutor(cfg).run(_watchlist_now(), NoDataProvider())

    (archived,) = read_ticker_observations("GFUZ")
    assert archived["action"] == "NO_DATA"
    assert archived["previous_close"] == ""
    assert archived["observed_at_et"].startswith("2026-07-08T09:45")
    assert "missing previous close" in archived["warnings"]


# --- streams accumulate across the day ---------------------------------------


def test_successive_streams_append_rows_for_the_same_ticker(tmp_path):
    """market-open, morning, and midday each add their own row, not overwrite."""
    provider = RichProvider()
    MarketOpenDCAExecutor(
        MarketOpenConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "mo"))
    ).run(datetime.combine(date(2026, 7, 8), time(9, 35), tzinfo=EASTERN), provider)
    MorningDCAExecutor(
        MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "m"))
    ).run(_watchlist_now(), provider)
    MiddayDCAExecutor(
        MiddayConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "md"))
    ).run(datetime.combine(date(2026, 7, 8), time(12, 30), tzinfo=EASTERN), provider)

    rows = read_ticker_observations("AMD")
    assert [r["slot"] for r in rows] == ["0935", "0945", "1230"]
    assert {r["stream"] for r in rows} == {"market_open", "morning", "midday"}
    # Sorted by observation time, so the file reads as a day's price path.
    times = [datetime.fromisoformat(r["observed_at_utc"]) for r in rows]
    assert times == sorted(times)


def test_intraday_three_slots_share_a_stream_but_not_a_row(tmp_path):
    """11:30/1:30/2:30 are one stream with one cron id; each must still archive."""
    from intraday_dca import IntradayConfig, IntradayDCAExecutor

    provider = RichProvider()
    for slot, hour, minute in (("1130", 11, 30), ("1330", 13, 30), ("1430", 14, 30)):
        IntradayDCAExecutor(
            IntradayConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / slot)),
            slot=slot,
        ).run(datetime.combine(date(2026, 7, 8), time(hour, minute), tzinfo=EASTERN), provider)

    rows = read_ticker_observations("AMD")
    assert [r["slot"] for r in rows] == ["1130", "1330", "1430"]
    assert {r["stream"] for r in rows} == {"intraday"}


def test_rerunning_one_slot_does_not_duplicate_its_row(tmp_path):
    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    provider = RichProvider()
    MorningDCAExecutor(cfg).run(_watchlist_now(), provider)
    MorningDCAExecutor(cfg).run(_watchlist_now(), provider)

    assert len(read_ticker_observations("AMD")) == 1


# --- per-run snapshot + run_state summary ------------------------------------


def test_run_writes_an_observations_csv_beside_its_other_artifacts(tmp_path):
    cfg = MorningConfig(tickers=("AMD", "NVDA"), morning_run_state_root=str(tmp_path / "runs"))
    result = MorningDCAExecutor(cfg).run(_watchlist_now(), RichProvider())

    snapshot = Path(result["paths"]["observations"])
    assert snapshot.name == "observations.csv"
    assert snapshot.parent == Path(result["paths"]["run_state"]).parent
    with snapshot.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert sorted(r["ticker"] for r in rows) == ["AMD", "NVDA"]


def test_run_state_on_disk_records_the_archive_summary(tmp_path):
    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    result = MorningDCAExecutor(cfg).run(_watchlist_now(), RichProvider())

    persisted = json.loads(Path(result["paths"]["run_state"]).read_bytes())
    summary = persisted["intraday_archive"]
    assert summary["ok"] is True
    assert summary["observations"] == 1
    assert summary["rows_added"] == 1


# --- failure containment -----------------------------------------------------


def test_archive_failure_does_not_fail_the_run(tmp_path, monkeypatch):
    """A blocked archive path must cost a warning, never a trading run."""
    blocker = tmp_path / "blocked"
    blocker.write_text("this is a file, not a directory")
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(blocker))

    cfg = MorningConfig(tickers=("AMD",), morning_run_state_root=str(tmp_path / "runs"))
    result = MorningDCAExecutor(cfg).run(_watchlist_now(), RichProvider())

    assert Path(result["paths"]["run_state"]).exists()
    assert result["artifacts"]["signal_rows"]
    persisted = json.loads(Path(result["paths"]["run_state"]).read_bytes())
    assert persisted["intraday_archive"]["ok"] is False
    assert persisted["intraday_archive"]["error"]


# --- the 15:30 near-close stream ---------------------------------------------


def test_near_close_run_artifacts_archive_their_observations(tmp_path):
    """The 3:30 stream writes through the same archive as the morning family."""
    # write_run_artifacts() itself no longer archives observations or
    # returns paths["observations"] -- that call was deliberately moved to
    # _finalize_and_write_artifacts, invoked only AFTER the scheduled
    # contract's required run_summary.json/submitted_orders.csv are already
    # on disk, since the per-ticker archive lock can block for several
    # seconds and must never delay those required files (Codex review:
    # "Finalize scheduled artifacts before waiting on archive locks"). This
    # test exercises the two calls in that same order to match the real
    # production wiring.
    from near_close_dca import _archive_intraday_observations, write_run_artifacts

    artifacts = {
        "run_id": "afternoon_live_20260708_1530ET",
        "trading_day": "2026-07-08",
        "generated_at": "2026-07-08T15:30:00",
        "data_source": "finance",
        "run_classification": {
            "stream": "afternoon",
            "scheduled_for_et": "15:30",
            "run_id": "afternoon_live_20260708_1530ET",
            "run_type": "near_close_live",
            "manual_run": False,
            "trading_day": "2026-07-08",
        },
        "signal_rows": [
            {
                "ticker": "TSM",
                "source": "finance",
                "action": "BUY",
                "decision_bar_close": 240.75,
                "previous_close": 250.0,
                "previous_low": 239.0,
                "drop_vs_previous_close_pct": 3.7,
                "buy_multiplier": 2.0,
                "decision_bar_timestamp": "2026-07-08T15:25:00-04:00",
                "previous_close_timestamp": "2026-07-07T16:00:00-04:00",
                "quote_price": 240.71,
                "quote_time": "2026-07-08T15:29:58-04:00",
            }
        ],
        "buy_candidates": [],
    }
    write_run_artifacts(artifacts, tmp_path / "run")
    snapshot_path = _archive_intraday_observations(artifacts, tmp_path / "run")

    assert snapshot_path is not None
    assert Path(snapshot_path).name == "observations.csv"
    (archived,) = read_ticker_observations("TSM")
    assert archived["stream"] == "afternoon"
    assert archived["slot"] == "1530"
    assert float(archived["price"]) == pytest.approx(240.75)
    assert archived["bar_timestamp"] == "2026-07-08T15:25:00-04:00"
    assert archived["previous_close_as_of"] == "2026-07-07T16:00:00-04:00"
    assert float(archived["quote_price"]) == pytest.approx(240.71)


def test_near_close_archive_failure_does_not_fail_the_run(tmp_path, monkeypatch):
    from near_close_dca import _archive_intraday_observations, write_run_artifacts

    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setenv("DCA_INTRADAY_ARCHIVE_DIR", str(blocker))

    artifacts = {
        "run_id": "afternoon_live_20260708_1530ET",
        "trading_day": "2026-07-08",
        "generated_at": "2026-07-08T15:30:00",
        "run_classification": {"stream": "afternoon", "scheduled_for_et": "15:30"},
        "signal_rows": [{"ticker": "TSM", "decision_bar_close": 240.75}],
        "buy_candidates": [],
    }
    paths = write_run_artifacts(artifacts, tmp_path / "run")
    assert Path(paths["run_state"]).exists()
    assert Path(paths["signals"]).exists()

    # A blocked SHARED archive path must cost a warning, never raise past
    # this call -- the per-run observations.csv snapshot is independent of
    # the shared archive and still succeeds.
    snapshot_path = _archive_intraday_observations(artifacts, tmp_path / "run")
    assert snapshot_path is not None
    assert Path(snapshot_path).exists()


# --- session aggregation -----------------------------------------------------


def test_session_stats_aggregates_the_window():
    base = datetime.combine(date(2026, 7, 8), time(9, 30), tzinfo=EASTERN)
    bars = [
        Bar(timestamp=base, open=10.0, high=11.0, low=9.5, close=10.5, volume=100.0),
        Bar(
            timestamp=base + timedelta(minutes=5),
            open=10.5,
            high=12.0,
            low=8.0,
            close=9.0,
            volume=250.0,
        ),
    ]
    assert session_stats(bars) == {
        "session_open": 10.0,
        "session_high": 12.0,
        "session_low": 8.0,
        "session_volume": 350.0,
    }


def test_session_stats_of_an_empty_window_reports_nothing():
    """Unknown volume and zero volume must not look the same in the archive."""
    assert session_stats([]) == {}
