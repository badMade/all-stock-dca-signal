"""Model training options, history, and loss curve snapshot for the console (GUI expansion).

Inspects training engines (Octopus Neural, ml-forecaster, baseline quantile),
parses loss metrics and checkpoints, and evaluates backend compute device readiness.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dca_console_reader import root_label


def _modified_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def _get_hardware_info() -> dict[str, Any]:
    """Inspect local hardware acceleration for neural training."""
    device_name = "cpu"
    device_label = "CPU (Standard)"
    torch_version = None
    torch_available = False

    try:
        import torch

        torch_available = True
        torch_version = getattr(torch, "__version__", "unknown")
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device_name = "mps"
            device_label = "Apple Silicon GPU (Metal Performance Shaders)"
        elif torch.cuda.is_available():
            device_name = "cuda"
            device_label = f"NVIDIA CUDA ({torch.cuda.get_device_name(0)})"
    except Exception:
        pass

    sklearn_available = False
    sklearn_version = None
    try:
        import sklearn

        sklearn_available = True
        sklearn_version = getattr(sklearn, "__version__", "unknown")
    except Exception:
        pass

    return {
        "torch_available": torch_available,
        "torch_version": torch_version,
        "device": device_name,
        "device_label": device_label,
        "sklearn_available": sklearn_available,
        "sklearn_version": sklearn_version,
    }


def _scan_training_runs(repo: Path, output: Path) -> list[dict[str, Any]]:
    """Scan available training checkpoints and runs in analysis and demo trees."""
    runs: list[dict[str, Any]] = []

    # 1. Check foundation octopus runs
    foundation_dir = repo / "analysis" / "foundation-octopus-run"
    if foundation_dir.is_dir():
        for item in sorted(foundation_dir.glob("**/shard_1_metrics.json"), reverse=True):
            run_folder = item.parent
            rel_name = run_folder.relative_to(repo / "analysis")
            metrics_data = {}
            try:
                metrics_data = json.loads(item.read_bytes())
            except Exception:
                pass

            device = metrics_data.get("device", "unknown")
            stages = metrics_data.get("stages", {})
            total_epochs = 0
            for stage_info in stages.values():
                if isinstance(stage_info, dict):
                    total_epochs += len(stage_info.get("epochs", []))

            pts = [p.name for p in run_folder.glob("*.pt")]
            runs.append({
                "id": str(rel_name),
                "name": f"Octopus Foundation ({rel_name})",
                "kind": "neural_foundation",
                "device": device,
                "epochs": total_epochs,
                "checkpoints": pts,
                "has_metrics": True,
                "modified": _modified_iso(item),
            })

    # 2. Check real octopus run
    real_run = repo / "analysis" / "real-octopus-run"
    if real_run.is_dir():
        val_path = real_run / "octopus_validation_metrics.json"
        val_data = {}
        if val_path.is_file():
            try:
                val_data = json.loads(val_path.read_bytes())
            except Exception:
                pass
        runs.append({
            "id": "analysis/real-octopus-run",
            "name": "Octopus Walk-Forward Baseline",
            "kind": "walk_forward_baseline",
            "device": val_data.get("device", "cpu"),
            "epochs": val_data.get("epochs_completed", 10),
            "checkpoints": ["checkpoint", "checkpoint.good"],
            "has_metrics": bool(val_data),
            "modified": _modified_iso(real_run),
            "validation": val_data,
        })

    # 3. Check demo octopus runs
    demo_octopus = output / "demo" / "octopus"
    if demo_octopus.is_dir():
        for job_folder in sorted(demo_octopus.iterdir(), reverse=True)[:5]:
            if job_folder.is_dir() and not job_folder.name.startswith("."):
                meta_file = job_folder / "run_meta.json"
                model_sel = "demo"
                if meta_file.is_file():
                    try:
                        meta = json.loads(meta_file.read_bytes())
                        model_sel = meta.get("model_selection", "demo")
                    except Exception:
                        pass
                runs.append({
                    "id": f"demo/{job_folder.name}",
                    "name": f"Demo Replay ({job_folder.name[:12]})",
                    "kind": f"demo_{model_sel}",
                    "device": "cpu",
                    "epochs": 1,
                    "checkpoints": ["checkpoint"],
                    "has_metrics": False,
                    "modified": _modified_iso(job_folder),
                })

    return runs


def _extract_loss_curves(repo: Path) -> dict[str, Any]:
    """Extract loss curve points from the primary foundation training metrics."""
    metrics_path = repo / "analysis" / "foundation-octopus-run" / "shard_1_metrics.json"
    if not metrics_path.is_file():
        metrics_path = repo / "analysis" / "foundation-octopus-run" / "complete-2stage-0028" / "shard_1_metrics.json"

    if not metrics_path.is_file():
        return {"available": False, "epochs": [], "validation": {}}

    try:
        data = json.loads(metrics_path.read_bytes())
        points = []
        stage1_epochs = data.get("stages", {}).get("1", {}).get("epochs", [])
        for ep in stage1_epochs:
            points.append({
                "stage": 1,
                "epoch": ep.get("epoch"),
                "total_loss": round(float(ep.get("loss_total_mean", 0)), 6),
                "forward_loss": round(float(ep.get("loss_forward_mean", 0)), 6),
                "recon_loss": round(float(ep.get("loss_recon_mean", 0)), 6),
                "market_loss": round(float(ep.get("loss_market_mean", 0)), 6),
                "learning_rate": ep.get("lr_last"),
                "seconds": round(float(ep.get("seconds", 0)), 1),
                "steps": ep.get("steps"),
            })

        stage2_epochs = data.get("stages", {}).get("2", {}).get("epochs", [])
        for ep in stage2_epochs:
            points.append({
                "stage": 2,
                "epoch": ep.get("epoch"),
                "total_loss": round(float(ep.get("loss_total_mean", 0)), 6),
                "forward_loss": round(float(ep.get("loss_forward_mean", 0)), 6),
                "recon_loss": round(float(ep.get("loss_recon_mean", 0)), 6),
                "market_loss": round(float(ep.get("loss_market_mean", 0)), 6),
                "learning_rate": ep.get("lr_last"),
                "seconds": round(float(ep.get("seconds", 0)), 1),
                "steps": ep.get("steps"),
            })

        val_path = repo / "analysis" / "real-octopus-run" / "octopus_validation_metrics.json"
        val_data = {}
        if val_path.is_file():
            try:
                val_data = json.loads(val_path.read_bytes())
            except Exception:
                pass

        return {
            "available": bool(points),
            "device": data.get("device", "unknown"),
            "feature_count": data.get("feature_count", 36),
            "parameters": data.get("parameters", "1,042,839"),
            "panel_windows_total": data.get("panel_windows_total"),
            "epochs": points,
            "validation": val_data,
        }
    except Exception:
        return {"available": False, "epochs": [], "validation": {}}


def build_training_snapshot(app: Any, repo: Path) -> dict[str, Any]:
    """Compile training options, active hardware backend, loss history, and checkpoints."""
    warnings: list[str] = []
    hardware = _get_hardware_info()
    runs = _scan_training_runs(repo, app.settings.output)
    loss_curves = _extract_loss_curves(repo)

    neural_ready = hardware["torch_available"]
    sklearn_ready = hardware["sklearn_available"]

    models = [
        {
            "id": "train-foundation-smoke",
            "name": "Octopus Neural Foundation",
            "tag": "PyTorch Multimodal Trunk",
            "description": "Two-stage curriculum pre-training across historical regimes (1962 through today). Trains multimodal representation with forward, reconstruction, and market regime losses.",
            "ready": neural_ready,
            "reason": "" if neural_ready else "PyTorch is not installed in the research environment.",
            "action_id": "train-foundation-smoke",
            "action_label": "Run Foundation Smoke Train (10 steps)",
            "options": {
                "stages": ["1 (History)", "2 (Modern)", "both"],
                "default_stage": "1",
                "default_epochs": 1,
                "device": hardware["device"],
            },
        },
        {
            "id": "ml-train-dry-run",
            "name": "ml-forecaster Ridge Model",
            "tag": "Cross-Sectional Sizing",
            "description": "Cross-sectional Ridge regression sizing model. Evaluates walk-forward MAE against baseline and produces guarded sizing adjustment preview without altering config.",
            "ready": sklearn_ready,
            "reason": "" if sklearn_ready else "scikit-learn is not installed.",
            "action_id": "ml-train-dry-run",
            "action_label": "Run ML Forecaster Dry Run",
            "options": {
                "n_splits": 4,
                "alpha": 2.0,
                "dry_run": True,
            },
        },
        {
            "id": "demo-octopus",
            "name": "Quantile GBM & Persistence Baselines",
            "tag": "Probabilistic Benchmark",
            "description": "Replay synthetic bars through gradient boosting quantile regression and naive persistence baseline to establish walk-forward prediction bands.",
            "ready": sklearn_ready,
            "reason": "" if sklearn_ready else "scikit-learn is not installed.",
            "action_id": "demo-octopus",
            "action_label": "Test Octopus Baseline Pipeline",
            "options": {
                "models": ["gbm_quantile", "persistence"],
                "quantiles": [0.1, 0.5, 0.9],
            },
        },
        {
            "id": "octopus-update",
            "name": "Octopus Walk-Forward Daily Update",
            "tag": "Incremental Step",
            "description": "Advance the active research checkpoint by one trading day using the local ticker archive. Re-evaluates forecasts and updates prediction state.",
            "ready": neural_ready and app.octopus_run is not None,
            "reason": "Supply --octopus-run with an existing Octopus run directory." if app.octopus_run is None else "",
            "action_id": "octopus-update",
            "action_label": "Update Octopus Day",
            "options": {
                "active_run": root_label(app.octopus_run) if app.octopus_run else None,
            },
        },
    ]

    return {
        "hardware": hardware,
        "models": models,
        "runs": runs,
        "loss_curves": loss_curves,
        "warnings": warnings,
    }
