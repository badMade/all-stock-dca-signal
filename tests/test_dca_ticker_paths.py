"""Tests for the shared repository-to-Drive ticker-history location."""

from pathlib import Path

import dca_ticker_paths


def test_default_ticker_history_dir_is_the_repository_data_link(monkeypatch):
    monkeypatch.delenv("DCA_TICKER_HISTORY_DIR", raising=False)

    assert dca_ticker_paths.configured_ticker_history_dir() == (
        Path(__file__).resolve().parent.parent / "ticker_data"
    )


def test_explicit_ticker_history_override_is_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("DCA_TICKER_HISTORY_DIR", str(tmp_path / "alternate_archive"))

    assert dca_ticker_paths.configured_ticker_history_dir() == tmp_path / "alternate_archive"
