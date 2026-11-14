"""Console Goals page: read the goals, validate a proposed change, save it as a proposal file.

docs/adr-goals-and-loss-hedge-shadow.md, D5. This module is the console's first write path
and it is deliberately narrow (tests/test_dca_console_goals.py):

* It writes ONE kind of file, ``<console-output>/goal-proposals/<goal_id>-v<N>.json``, and
  nothing else. It never writes under ``config/``: the only way a goal reaches
  ``config/goals.json`` is the operator merging the PR that the allowlisted ``propose-goal``
  job opens (``scripts/propose_goal_pr.py``).
* It imports no process-spawning, network or broker module. The git and gh steps live in that job,
  which runs through the console's existing job runner like every other allowlisted action.
* Validation is ``dca_goals.validate_goal_proposal``: the same rules the schema states, so the
  form cannot propose a hedge ``mode`` other than off or shadow.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import dca_goals

PROPOSALS_DIRNAME = "goal-proposals"
_MAX_LISTED = 20


def proposals_dir(output: Path) -> Path:
    return Path(output) / PROPOSALS_DIRNAME


def _current_document(config_dir: Path) -> Dict[str, Any]:
    path = Path(config_dir) / dca_goals.GOALS_FILENAME
    if not path.is_file():
        return dca_goals.empty_document()
    try:
        return dca_goals.parse_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # Proposing against a document nobody can read would version blind; refuse instead.
        raise ValueError("config/goals.json is unreadable; fix it before proposing a change") from exc


def _next_version(document: Dict[str, Any], goal_id: str) -> int:
    for goal in document.get("goals", []):
        if isinstance(goal, dict) and goal.get("goal_id") == goal_id:
            version = goal.get("goal_version")
            return (version if isinstance(version, int) and not isinstance(version, bool) else 0) + 1
    return 1


def save_proposal(output: Path, config_dir: Path, payload: Any, *, now: datetime) -> str:
    """Validate ``payload`` and save it as the next version's proposal. Returns the file name.

    Raises ValueError (shown to the operator as a 400) when the proposal is invalid; nothing is
    written in that case. The write is atomic (temp file + rename inside the proposals folder),
    so the propose-goal job never reads a half-written proposal.
    """
    entry = dca_goals.validate_goal_proposal(payload, now=now)
    version = _next_version(_current_document(config_dir), entry["goal_id"])
    # goal_id is already constrained to [a-z0-9-] by validation, so the name cannot traverse.
    name = f"{entry['goal_id']}-v{version}.json"
    folder = proposals_dir(output)
    folder.mkdir(parents=True, exist_ok=True)
    record = {"goal": entry, "goal_version": version, "proposed_at": now.isoformat()}
    handle, temp_name = tempfile.mkstemp(prefix=".proposal-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=False)
            stream.write("\n")
        os.replace(temp_name, folder / name)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return name


def list_proposals(output: Path) -> List[Path]:
    """Saved proposals, newest first (by modification time, then name)."""
    folder = proposals_dir(output)
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob("*.json") if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


def newest_proposal(output: Path) -> Optional[Path]:
    proposals = list_proposals(output)
    return proposals[0] if proposals else None


def _active_summary(goals: dca_goals.GoalSet) -> Dict[str, Any]:
    growth = goals.growth
    return {
        "growth": None if growth is None else {
            "goal_id": growth.goal_id, "goal_version": growth.goal_version, "metric": growth.metric,
            "horizon_years": growth.horizon_years, "target": growth.target, "benchmark": growth.benchmark,
            "effective_from": growth.effective_from.isoformat(),
        },
        "hedge": {
            "goal_id": goals.hedge.goal_id, "goal_version": goals.hedge.goal_version, "mode": goals.hedge.mode,
            "max_drawdown": goals.hedge.max_drawdown, "hedge_budget_pct": goals.hedge.hedge_budget_pct,
            "instruments": [{"instrument": i.instrument, "mode": i.mode, "underlying": i.underlying,
                             "horizon_days": i.horizon_days, "assets": list(i.assets),
                             "max_alloc_pct": i.max_alloc_pct, "hold_days": i.hold_days}
                            for i in goals.hedge.instruments],
        },
    }


def goals_view(config_dir: Path, output: Path, *, now: datetime) -> Dict[str, Any]:
    """Everything the Goals page shows. Read-only; never raises on bad goal data."""
    warnings: List[str] = []
    try:
        document: Optional[Dict[str, Any]] = _current_document(config_dir)
    except ValueError as exc:
        document = None
        warnings.append(str(exc))
    goals = dca_goals.load_goals(config_dir, now=now)
    if document is not None and goals == dca_goals.NEUTRAL_GOALS and document.get("goals"):
        warnings.append("No goal is in effect right now (not yet effective, inactive, or invalid): "
                        "goals are neutral and the loss hedge is off.")
    return {
        "document": document,
        "active": _active_summary(goals),
        "hedge_execution": "disabled",
        "hedge_modes": list(dca_goals.HEDGE_MODES),
        "crypto_assets": list(dca_goals.CRYPTO_ASSETS),
        "growth_metrics": list(dca_goals.GROWTH_METRICS),
        "proposals": [p.name for p in list_proposals(output)[:_MAX_LISTED]],
        "warnings": warnings,
    }
