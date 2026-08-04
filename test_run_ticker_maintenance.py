"""Tests for the executable ticker-maintenance dispatcher."""

from datetime import datetime, timezone

from scripts.run_ticker_maintenance import run_calibration, run_update


def test_update_execution_emits_the_event_consumed_by_calibration(tmp_path):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    (history_dir / "ABC.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n2026-08-01,10,11,9,10,10,1\n",
        encoding="utf-8",
    )
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": ["ABC"]}', encoding="utf-8")
    updater = tmp_path / "incremental_update.py"
    updater.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "path = Path(os.environ['TICKERS_DIR']) / 'ABC.csv'\n"
        "path.write_text('Date,Open,High,Low,Close,Adj Close,Volume\\n2026-08-02,10,11,9,10,10,1\\n')\n"
        "print('ALL_DONE')\n",
        encoding="utf-8",
    )
    update_dir = tmp_path / "update"
    update = run_update(
        updater_script=updater,
        history_dir=history_dir,
        ticker_config=ticker_config,
        artifacts_dir=update_dir,
    )
    assert update["status"] == "completed"
    assert update["changed_active_tickers"] == ["ABC"]

    calibration = run_calibration(
        history_dir=history_dir,
        ticker_config=ticker_config,
        output=tmp_path / "per_ticker_sizing.json",
        artifacts_dir=tmp_path / "calibration",
        ticker_update_event=update_dir / "ticker_update_event.json",
        force=False,
        now=datetime(2026, 8, 3, 22, 0, tzinfo=timezone.utc),
    )
    assert calibration["status"] == "completed"
    assert calibration["trigger_source"] == "ticker_update_event"
