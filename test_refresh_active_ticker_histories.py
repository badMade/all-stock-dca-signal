"""Tests for the ticker-history refresh utility's archive-availability guard."""

import json

import pytest

from scripts import refresh_active_ticker_histories as refresh


def _ticker_config(path):
    path.write_text(json.dumps({"active": ["ABC"]}), encoding="utf-8")


def test_default_history_dir_refuses_to_run_when_the_archive_is_missing(tmp_path, monkeypatch):
    # Codex review ("Do not create a replacement ticker_data tree"): ticker_data/
    # is a gitignored symlink/mount to the managed Google Drive archive.
    # Running this utility without --history-dir while that mount is absent
    # must refuse outright rather than silently writing into (and thereby
    # creating) a local ticker_data stand-in that a later updater or
    # calibration run would mistake for the real archive.
    missing_root = tmp_path / "ticker_data_not_mounted"
    monkeypatch.setattr(refresh, "DEFAULT_HISTORY_DIR", missing_root)
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)
    monkeypatch.setattr(
        "sys.argv",
        ["refresh_active_ticker_histories.py", "--config", str(config_path), "--dry-run"],
    )

    with pytest.raises(SystemExit) as excinfo:
        refresh.main()

    assert excinfo.value.code == 2
    assert not missing_root.exists()


def test_an_explicit_history_dir_is_exempt_from_the_default_archive_guard(tmp_path, monkeypatch):
    # An operator who passes --history-dir explicitly is opting into that
    # location on purpose; the guard only protects the unattended default.
    explicit_dir = tmp_path / "explicit_archive"
    config_path = tmp_path / "tickers.json"
    _ticker_config(config_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "refresh_active_ticker_histories.py",
            "--config",
            str(config_path),
            "--history-dir",
            str(explicit_dir),
            "--dry-run",
        ],
    )

    exit_code = refresh.main()

    assert exit_code == 0
