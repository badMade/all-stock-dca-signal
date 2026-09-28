"""Tests for scripts/export_octopus_ledger.py."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("scripts.export_octopus_ledger")


def test_load_scheduled_tickers() -> None:
    tickers = _module().load_scheduled_tickers()
    assert len(tickers) >= 50
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert "NVDA" in tickers


def test_import_does_not_bootstrap_octopus_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "scripts.export_octopus_ledger", raising=False)
    monkeypatch.delitem(sys.modules, "inspect_run", raising=False)

    _module()

    assert "inspect_run" not in sys.modules


def test_main_cli_missing_dir(tmp_path: Path) -> None:
    missing_dir = tmp_path / "nonexistent"
    ret = _module().main(["--run-dir", str(missing_dir)])
    assert ret == 1
