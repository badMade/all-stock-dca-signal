"""Unit tests for dca_console_training.py (models, loss history, and hardware)."""

from pathlib import Path
from unittest.mock import MagicMock

from dca_console_training import (
    _extract_loss_curves,
    _get_hardware_info,
    _scan_training_runs,
    build_training_snapshot,
)


def test_hardware_info_returns_structure():
    info = _get_hardware_info()
    assert isinstance(info, dict)
    assert "torch_available" in info
    assert "device" in info
    assert "device_label" in info
    assert "sklearn_available" in info


def test_scan_training_runs_empty(tmp_path: Path):
    runs = _scan_training_runs(tmp_path, tmp_path / "output")
    assert isinstance(runs, list)


def test_extract_loss_curves_missing(tmp_path: Path):
    curves = _extract_loss_curves(tmp_path)
    assert curves["available"] is False
    assert curves["epochs"] == []


def test_build_training_snapshot():
    mock_app = MagicMock()
    mock_app.settings.output = Path("/tmp/mock_output")
    mock_app.octopus_run = None
    mock_repo = Path(__file__).resolve().parent.parent

    snapshot = build_training_snapshot(mock_app, mock_repo)
    assert isinstance(snapshot, dict)
    assert "hardware" in snapshot
    assert "models" in snapshot
    assert len(snapshot["models"]) >= 4
    for model in snapshot["models"]:
        assert "last_updated" in model
        lu = model["last_updated"]
        assert isinstance(lu, dict)
        assert "timestamp" in lu
        assert "status" in lu
    assert any(m["id"] == "train-foundation-smoke" for m in snapshot["models"])
    assert any(m["id"] == "ml-train-dry-run" for m in snapshot["models"])
    assert "runs" in snapshot
    assert "loss_curves" in snapshot
