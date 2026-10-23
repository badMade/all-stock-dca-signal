"""Append-only run registry for the ML sizing-adjustment retrain.

``publish.guarded_publish`` overwrites ``config/ml_sizing_adjustment.json`` in
place and keeps no record of what it replaced, so after a bad publish the only
way back was hand-hunting for the prior ``model.json`` under the models dir. The
registry is one JSON line per retrain run -- published, ``blocked_*`` and
``--dry-run`` alike -- appended to ``<models-dir>/registry.jsonl``. Each line
carries enough to answer "what produced this model, from which data, and what
was live before it":

* ``dataset_sha256`` -- a fingerprint of the exact archive CSVs (plus the
  ``as_of`` cutoff) the run trained on, built from :func:`dca_provenance.sha256_file`
  so it means the same thing as every other provenance hash in the repo;
* ``model_json_sha256`` -- the saved model artifact's bytes;
* ``code_git_sha`` -- the checkout's HEAD when it can be read, else ``None``;
* ``prev_model_version`` / ``prev_model_artifact_path`` -- the incumbent the run
  would have replaced, which IS the rollback target.

It deliberately lives next to the run artifacts (not under ``config/``) so the
``ml_sizing_adjustment`` schema and ``tests/test_config_invariants.py`` are
untouched, and it never raises into the retrain: a registry failure is a
warning, never a reason to lose or refuse a publish that already happened.

Standard library plus the repo's own ``dca_provenance`` only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parent
# Index 1, not 0 -- same reasoning as train.py/features.py: this subproject's own
# directory must stay ahead of the repo root on sys.path.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

from dca_provenance import sha256_file  # noqa: E402

REGISTRY_FILENAME = "registry.jsonl"

#: Bump when the feature set or target definition in features.py/model.py changes
#: in a way a reader comparing two registry lines should treat as incompatible.
FEATURE_SET_VERSION = "pooled-ridge-v1"

SCHEMA_VERSION = 1

_GIT_TIMEOUT_SECONDS = 5


def dataset_fingerprint(history_dir: Path, tickers: Iterable[str], *, as_of_iso: str) -> str:
    """SHA-256 over each ticker CSV's own hash, the ticker set and the ``as_of`` cutoff.

    Sorted by ticker so the order the tickers were discovered in never changes
    the fingerprint. A ticker whose CSV is unreadable contributes a ``missing``
    marker instead of raising: the fingerprint must still differ from a run that
    had the file, and a hash helper must not be able to crash the retrain.
    """
    outer = hashlib.sha256()
    outer.update(f"as_of={as_of_iso}\n".encode("utf-8"))
    for ticker in sorted({str(t).upper() for t in tickers}):
        try:
            digest = sha256_file(Path(history_dir) / f"{ticker}.csv")
        except OSError:
            digest = "missing"
        outer.update(f"{ticker}:{digest}\n".encode("utf-8"))
    return outer.hexdigest()


def current_git_sha(repo_root: Path = REPO_ROOT) -> Optional[str]:
    """HEAD of the checkout, or ``None`` outside a checkout / without git."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and len(sha) == 40 else None


def incumbent_reference(current_config: Optional[dict[str, Any]]) -> dict[str, Optional[str]]:
    """The currently-published model's identity, read from the live config dict."""
    config = current_config or {}
    version = config.get("model_version")
    artifact = config.get("model_artifact_path")
    return {
        "prev_model_version": version if isinstance(version, str) else None,
        "prev_model_artifact_path": artifact if isinstance(artifact, str) else None,
    }


def build_entry(
    report: dict[str, Any],
    *,
    run_dir: Path,
    context: dict[str, Any],
) -> dict[str, Any]:
    """One registry line from a finished run's eval report plus its provenance context."""
    model_json = run_dir / "model.json"
    try:
        model_sha: Optional[str] = sha256_file(model_json)
    except OSError:
        model_sha = None
    evaluation = report.get("evaluation") or {}
    overall = evaluation.get("overall") if isinstance(evaluation, dict) else None
    dataset = report.get("dataset") or {}
    status = report.get("status")
    publish_result = report.get("publish_result") or {}
    published = status == "published"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "model_version": report.get("model_version"),
        "status": status,
        "run_at": report.get("run_at"),
        "model_json_path": str(model_json),
        "model_json_sha256": model_sha,
        "dataset_sha256": context.get("dataset_sha256"),
        "code_git_sha": context.get("code_git_sha"),
        "feature_set_version": context.get("feature_set_version", FEATURE_SET_VERSION),
        "prev_model_version": context.get("prev_model_version"),
        "prev_model_artifact_path": context.get("prev_model_artifact_path"),
        # Only a real publish counts as a deployment; blocked and dry-run lines
        # record the attempt but never claim anything went live.
        "deployed_at": context.get("deployed_at") if published else None,
        "model_mae": overall.get("model_mae") if isinstance(overall, dict) else None,
        "n_rows_dip_days": dataset.get("n_rows_dip_days"),
        "n_rows_all": dataset.get("n_rows_all"),
        "new_model_mae": publish_result.get("new_model_mae"),
    }


def append_entry(models_dir: Path, entry: dict[str, Any]) -> Path:
    """Append ``entry`` as a single JSON line; never leaves a partial line behind.

    One ``os.write`` on an ``O_APPEND`` descriptor: POSIX appends of a single
    write are not interleaved with other appenders' writes, so two overlapping
    retrains cannot tear each other's lines. The whole line (newline included)
    is encoded first so a serialization error raises before anything is written.
    """
    path = Path(models_dir) / REGISTRY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def read_entries(models_dir: Path) -> list[dict[str, Any]]:
    """Every parseable registry line, oldest first; torn or foreign lines are skipped."""
    path = Path(models_dir) / REGISTRY_FILENAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict):
            entries.append(loaded)
    return entries


def latest_published(models_dir: Path) -> Optional[dict[str, Any]]:
    """The most recent line whose status is ``published`` (the live model's record)."""
    for entry in reversed(read_entries(models_dir)):
        if entry.get("status") == "published":
            return entry
    return None


def rollback_target(models_dir: Path) -> Optional[dict[str, Any]]:
    """The registry line for the model the live one replaced, if it is still on record.

    Resolved by ``prev_model_version`` against earlier lines so the caller gets
    the full record (artifact path and hash), not just the version string. ``None``
    when nothing is published yet, the live model had no predecessor, or the
    predecessor predates the registry.
    """
    live = latest_published(models_dir)
    if live is None:
        return None
    wanted = live.get("prev_model_version")
    if not wanted:
        return None
    for entry in reversed(read_entries(models_dir)):
        if entry.get("model_version") == wanted and entry.get("model_json_path"):
            return entry
    return None
