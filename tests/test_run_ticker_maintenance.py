"""Tests for the executable ticker-maintenance dispatcher."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from scripts.run_ticker_maintenance import main, run_calibration, run_update


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





def test_main_update_command(tmp_path, monkeypatch, capsys):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": []}', encoding="utf-8")

    updater = tmp_path / "incremental_update.py"
    updater.write_text("print('ALL_DONE')", encoding="utf-8")

    update_dir = tmp_path / "update"

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ticker_maintenance",
            "update",
            "--updater-script", str(updater),
            "--history-dir", str(history_dir),
            "--ticker-config", str(ticker_config),
            "--artifacts-dir", str(update_dir),
            "--max-invocations", "10",
        ],
    )

    assert main() == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "completed"

def test_main_calibrate_command(tmp_path, monkeypatch, capsys):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": []}', encoding="utf-8")

    artifacts_dir = tmp_path / "calibration"

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ticker_maintenance",
            "calibrate",
            "--history-dir", str(history_dir),
            "--ticker-config", str(ticker_config),
            "--output", str(tmp_path / "per_ticker_sizing.json"),
            "--artifacts-dir", str(artifacts_dir),
        ],
    )

    assert main() == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "completed"
    assert output["calibrated_tickers"] == 0

def test_main_update_command_missing_script(monkeypatch, capsys):
    monkeypatch.delenv("DCA_TICKER_DAILY_UPDATE_SCRIPT", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["run_ticker_maintenance", "update"],
    )
    with pytest.raises(SystemExit, match="set DCA_TICKER_DAILY_UPDATE_SCRIPT"):
        main()

def test_main_event_calibrate_command_with_default_artifacts(tmp_path, monkeypatch, capsys):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": []}', encoding="utf-8")

    update_dir = tmp_path / "update"
    update_dir.mkdir()
    event_file = update_dir / "ticker_update_event.json"
    event_file.write_text('{"status": "completed", "changed_active_tickers": []}', encoding="utf-8")

    # No --artifacts-dir is passed below so main() resolves the default
    # artifact root via stream_root()/DCA_CRON_TRACKING_ROOT. Redirect that
    # env var under tmp_path or this test overwrites the real operational
    # per_ticker_calibration/<today>/calibration_run.json artifact.
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path / "cron_tracking"))

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ticker_maintenance",
            "event-calibrate",
            "--history-dir", str(history_dir),
            "--ticker-config", str(ticker_config),
            "--output", str(tmp_path / "per_ticker_sizing.json"),
            "--ticker-update-event", str(event_file)
        ],
    )

    assert main() == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "skipped_no_active_ticker_change"

def test_main_update_command_failed(tmp_path, monkeypatch, capsys):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": []}', encoding="utf-8")

    # an updater script that fails
    updater = tmp_path / "incremental_update.py"
    updater.write_text("import sys\nsys.exit(1)", encoding="utf-8")

    update_dir = tmp_path / "update"

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ticker_maintenance",
            "update",
            "--updater-script", str(updater),
            "--history-dir", str(history_dir),
            "--ticker-config", str(ticker_config),
            "--artifacts-dir", str(update_dir),
            "--max-invocations", "10",
        ],
    )

    assert main() == 1
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] in ["failed", "partial"]

def test_main_calibrate_command_failed(tmp_path, monkeypatch, capsys):
    history_dir = tmp_path / "histories"
    history_dir.mkdir()
    ticker_config = tmp_path / "tickers.json"
    ticker_config.write_text('{"active": []}', encoding="utf-8")

    artifacts_dir = tmp_path / "calibration"

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ticker_maintenance",
            "calibrate",
            "--history-dir", str(history_dir),
            "--ticker-config", str(ticker_config),
            "--output", str(tmp_path / "per_ticker_sizing.json"),
            "--artifacts-dir", str(artifacts_dir),
        ],
    )

    with patch("scripts.run_ticker_maintenance.run_calibration", return_value={"status": "failed"}):
        assert main() == 1
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "failed"
