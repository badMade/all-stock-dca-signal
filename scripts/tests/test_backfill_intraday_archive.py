"""Tests for the intraday-archive backfill over existing run artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from dca_intraday_archive import read_ticker_observations
from scripts.backfill_intraday_archive import (
    _classification_from_path,
    _effective_trading_day,
    iter_run_states,
    load_run_state,
    main,
    observations_for_run_state,
)


def _write_run_state(root: Path, stream: str, folder: str, payload: dict) -> Path:
    directory = root / stream / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "run_state.json"
    path.write_text(json.dumps(payload))
    return path


def _run_state(stream="morning", slot="10:30", day="2026-07-08", price=100.5, ticker="AMD"):
    return {
        "run_id": f"{stream}_live_20260708_{slot.replace(':', '')}ET",
        "trading_day": day,
        "generated_at": f"{day}T{slot}:00",
        "data_source": "yahoo",
        "run_classification": {
            "stream": stream,
            "scheduled_for_et": slot,
            "run_id": f"{stream}_live_20260708_{slot.replace(':', '')}ET",
            "run_type": f"{stream}_dca_live",
            "manual_run": False,
            "trading_day": day,
        },
        "signal_rows": [
            {
                "ticker": ticker,
                "source": "yahoo",
                "action": "BUY",
                "current_price": price,
                "previous_close": 110.0,
                "drop_pct": 8.6,
                "multiplier": 3.0,
            }
        ],
    }


def test_backfill_writes_historical_runs(tmp_path):
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    _write_run_state(root, "morning", "20260708_1030ET", _run_state())
    _write_run_state(
        root, "midday", "20260708_1230ET", _run_state(stream="midday", slot="12:30", price=98.0)
    )

    assert main(["--cron-tracking-root", str(root), "--archive-dir", str(archive)]) == 0

    rows = read_ticker_observations("AMD", archive_dir=archive)
    assert [r["slot"] for r in rows] == ["1030", "1230"]
    assert [r["price"] for r in rows] == ["100.5", "98.0"]
    assert [r["stream"] for r in rows] == ["morning", "midday"]


def test_backfill_is_idempotent(tmp_path):
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    _write_run_state(root, "morning", "20260708_1030ET", _run_state())
    argv = ["--cron-tracking-root", str(root), "--archive-dir", str(archive)]

    main(argv)
    main(argv)

    assert len(read_ticker_observations("AMD", archive_dir=archive)) == 1


def test_since_filter_skips_older_trading_days(tmp_path):
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    _write_run_state(root, "morning", "20260601_1030ET", _run_state(day="2026-06-01"))
    _write_run_state(root, "morning", "20260708_1030ET", _run_state(day="2026-07-08"))

    main(
        [
            "--cron-tracking-root",
            str(root),
            "--archive-dir",
            str(archive),
            "--since",
            "2026-07-01",
        ]
    )

    rows = read_ticker_observations("AMD", archive_dir=archive)
    assert [r["trading_day"] for r in rows] == ["2026-07-08"]


def test_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    _write_run_state(root, "morning", "20260708_1030ET", _run_state())

    assert (
        main(["--cron-tracking-root", str(root), "--archive-dir", str(archive), "--dry-run"]) == 0
    )
    assert not archive.exists()


def test_unreadable_run_state_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    broken = root / "morning" / "20260707_1030ET"
    broken.mkdir(parents=True)
    (broken / "run_state.json").write_text("{ not json")
    _write_run_state(root, "morning", "20260708_1030ET", _run_state())

    assert main(["--cron-tracking-root", str(root), "--archive-dir", str(archive)]) == 0
    assert len(read_ticker_observations("AMD", archive_dir=archive)) == 1


def test_run_state_without_signal_rows_contributes_nothing(tmp_path):
    root = tmp_path / "cron_tracking"
    path = _write_run_state(root, "morning", "20260708_1030ET", {"trading_day": "2026-07-08"})
    assert observations_for_run_state(json.loads(path.read_bytes()), path) == []


def test_missing_classification_falls_back_to_the_folder_layout(tmp_path):
    """Pre-classification artifacts still identify their slot by folder name."""
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    legacy = _run_state()
    del legacy["run_classification"]
    _write_run_state(root, "morning_dca", "20260708_1030ET", legacy)

    main(["--cron-tracking-root", str(root), "--archive-dir", str(archive)])

    (row,) = read_ticker_observations("AMD", archive_dir=archive)
    assert row["stream"] == "morning_dca"
    assert row["slot"] == "1030"


def test_classification_from_path_reads_stream_and_slot(tmp_path):
    path = tmp_path / "cron_tracking" / "intraday_dca" / "20260708_1330ET" / "run_state.json"
    derived = _classification_from_path(path)
    assert derived["stream"] == "intraday_dca"
    assert derived["scheduled_for_et"] == "1330"


def test_missing_root_is_an_error(tmp_path):
    assert main(["--cron-tracking-root", str(tmp_path / "nope")]) == 2


def test_since_filter_still_finds_a_legacy_run_missing_the_top_level_trading_day(tmp_path):
    # Codex review ("Derive the legacy day before applying --since"): a
    # legacy run_state.json lacking BOTH the top-level trading_day and
    # run_classification still names its own folder YYYYMMDD_HHMMET.
    # Comparing the empty top-level trading_day directly against --since
    # (as the old code did) would skip this run even though its folder
    # date is well within the requested window.
    root = tmp_path / "cron_tracking"
    archive = tmp_path / "archive"
    legacy = _run_state(day="2026-07-08")
    del legacy["trading_day"]
    del legacy["run_classification"]
    _write_run_state(root, "morning_dca", "20260708_1030ET", legacy)

    exit_code = main(
        ["--cron-tracking-root", str(root), "--archive-dir", str(archive), "--since", "2026-07-01"]
    )

    assert exit_code == 0
    (row,) = read_ticker_observations("AMD", archive_dir=archive)
    assert row["trading_day"] == "2026-07-08"


def test_effective_trading_day_falls_back_to_the_folder_date():
    path = Path("/cron_tracking/morning_dca/20260708_1030ET/run_state.json")
    assert _effective_trading_day({}, path) == "2026-07-08"


def test_default_archive_dir_guard_runs_for_backfills_too(tmp_path, monkeypatch):
    # Codex review ("Let the default archive guard run for backfills"): when
    # --archive-dir is omitted, this CLI must NOT pre-resolve the default
    # ticker_data-derived path and pass it to append_observations() as an
    # explicit override -- that bypasses append_observations()'s own guard
    # against materializing a local stand-in when the managed archive is
    # missing/unmounted.
    root = tmp_path / "cron_tracking"
    _write_run_state(root, "morning", "20260708_1030ET", _run_state())
    missing_ticker_data = tmp_path / "ticker_data_not_mounted"
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(missing_ticker_data))
    monkeypatch.delenv("DCA_INTRADAY_ARCHIVE_DIR", raising=False)

    exit_code = main(["--cron-tracking-root", str(root)])

    assert exit_code == 2
    assert not missing_ticker_data.exists()


def test_iter_run_states(tmp_path):
    assert list(iter_run_states(tmp_path)) == []

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "run_state.json").write_text("{}")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "run_state.json").write_text("{}")
    (tmp_path / "b" / "other.json").write_text("{}")

    paths = list(iter_run_states(tmp_path))
    assert len(paths) == 2
    assert tmp_path / "a" / "run_state.json" in paths
    assert tmp_path / "b" / "run_state.json" in paths


def test_load_run_state(tmp_path):
    path = tmp_path / "run_state.json"

    # Valid
    path.write_text('{"key": "value"}')
    assert load_run_state(path) == {"key": "value"}

    # Invalid JSON
    path.write_text('{"key": "value"')
    assert load_run_state(path) is None

    # Not a dict
    path.write_text('["list"]')
    assert load_run_state(path) is None

    # Unreadable
    path.write_bytes(b'\x80\x81')
    assert load_run_state(path) is None

    # Missing file
    assert load_run_state(tmp_path / "missing.json") is None
