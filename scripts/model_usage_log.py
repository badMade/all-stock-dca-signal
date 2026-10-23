#!/usr/bin/env python3
"""Writer for the model usage audit log (schemas/model_usage_log.schema.json).

Implements the ``model_usage_log`` contract declared in
``config/scheduled_tasks.json``: one append-only JSONL entry per run of a
covered daily DCA task, recording which model / routing mode it used so
cost-conscious model routing can be audited over time. See
``docs/cost_tracking.md`` (Model usage log).

This module has no knowledge of which LLM/agent is actually driving a given
run -- Python code cannot introspect that. Per the manifest's
``model_actual_not_exposed_rule``, callers that don't have that information
should leave ``model_actual`` at its default (``'not_exposed'``) and rely on
the ``model_requested``/``model_routing_mode`` defaults (cost-conscious),
overriding them only when a caller genuinely knows better (e.g. an explicit
premium escalation).

Standard library only. Writing this log is always best-effort: a task's own
scheduled run (safety-critical trading logic) must never fail or block
because an audit-log append failed, so ``log_model_usage`` catches and logs
every exception rather than raising into its caller.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

# This module is directly executable (``python scripts/model_usage_log.py
# --run-id ...``, and its test is documented as runnable the same way), which
# puts only ``scripts/`` on sys.path -- so the repo root is added here before
# the shared helper below is imported, or the CLI dies with ModuleNotFoundError
# before it can parse an argument (Codex review, PR #219: "Preserve direct
# execution of the model-usage CLI"). Same bootstrap as
# scripts/drift_detection.py.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dca_paths import stream_root  # noqa: E402 - needs the sys.path setup above

logger = logging.getLogger(__name__)

# Resolved at IMPORT time and kept only as the module's advertised default for
# callers that read the constant. The functions below deliberately do NOT default
# to it: $DCA_CRON_TRACKING_ROOT set after this module was imported would leave
# model-usage evidence appended under the old tree while the credit-usage
# report, which resolves the shared root per call, scans the relocated one -- and
# the run's usage then reads as MISSING (Codex review, PR #219). The bundled
# morning and midday configs omit artifact_paths.model_usage_log_root, so their
# loaders take this default path and would have inherited the same staleness.
DEFAULT_LOG_ROOT = stream_root("model_usage")
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "config" / "scheduled_tasks.json"

REQUIRED_FIELDS = (
    "run_id",
    "task_name",
    "scheduled_for_et",
    "run_origin",
    "stream",
    "model_requested",
    "model_actual",
    "model_routing_mode",
    "triggered_at_utc",
    "artifact_path",
)

MODEL_ROUTING_MODE_VALUES = ("cost_conscious", "default", "premium")
PREMIUM_REASONS = (
    "failure_recovery",
    "safety_validation",
    "connector_debug_escalation",
    "explicit_user_request",
)


class UnknownTaskError(LookupError):
    """Raised when a cron_id/task_id is not a covered task in the manifest."""


def _load_task_directory(manifest_path: str | Path) -> Dict[str, Dict[str, str]]:
    """Map task_id -> {"scheduled_for_et", "stream", "task_name"} from the
    manifest's ``model_usage_log.applies_to_daily_tasks_et`` list -- the
    single source of truth for which tasks are covered (see
    config/scheduled_tasks.json)."""
    manifest = json.loads(Path(manifest_path).read_bytes())
    entries = manifest.get("model_usage_log", {}).get("applies_to_daily_tasks_et", [])
    directory: Dict[str, Dict[str, str]] = {}
    for entry in entries:
        directory[entry["task_id"]] = {
            "scheduled_for_et": entry["et"],
            "stream": entry["stream"],
            "task_name": entry["role"],
        }
    return directory


def build_entry(
    *,
    run_id: str,
    artifact_path: str | Path,
    cron_id: Optional[str] = None,
    task_id: Optional[str] = None,
    task_name: Optional[str] = None,
    scheduled_for_et: Optional[str] = None,
    stream: Optional[str] = None,
    run_origin: str = "scheduled",
    model_requested: str = "cost_conscious_default",
    model_actual: str = "not_exposed",
    model_routing_mode: str = "cost_conscious",
    reason_for_premium_model: Optional[str] = None,
    triggered_at_utc: Optional[datetime] = None,
    notes: Optional[str] = None,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
) -> Dict[str, Any]:
    """Build one schema-valid model usage log entry.

    ``task_name``/``scheduled_for_et``/``stream`` are looked up from the
    manifest by ``task_id`` (or ``cron_id``) when not supplied directly, so
    callers only need to pass the task's identifier plus run-specific facts
    (run_id, artifact_path, run_origin). Raises ``UnknownTaskError`` if
    neither identifier resolves to a covered task, and ``ValueError`` for any
    other schema violation (missing field, bad enum, inconsistent premium
    reason).
    """
    if not cron_id and not task_id:
        raise ValueError("build_entry requires cron_id and/or task_id")
    identifier = task_id or cron_id
    assert identifier is not None

    if task_name is None or scheduled_for_et is None or stream is None:
        directory = _load_task_directory(manifest_path)
        looked_up = directory.get(identifier)
        if looked_up is None:
            raise UnknownTaskError(f"{identifier!r} is not a covered task in {manifest_path}")
        task_name = task_name or looked_up["task_name"]
        scheduled_for_et = scheduled_for_et or looked_up["scheduled_for_et"]
        stream = stream or looked_up["stream"]

    if model_routing_mode not in MODEL_ROUTING_MODE_VALUES:
        raise ValueError(f"invalid model_routing_mode {model_routing_mode!r}; expected one of {MODEL_ROUTING_MODE_VALUES}")
    if model_routing_mode == "premium":
        if reason_for_premium_model not in PREMIUM_REASONS:
            raise ValueError(
                f"reason_for_premium_model must be one of {PREMIUM_REASONS} when model_routing_mode='premium', "
                f"got {reason_for_premium_model!r}"
            )
    elif reason_for_premium_model is not None:
        raise ValueError(
            f"reason_for_premium_model must be null/omitted when model_routing_mode={model_routing_mode!r}, "
            f"got {reason_for_premium_model!r}"
        )

    triggered = triggered_at_utc or datetime.now(timezone.utc)
    triggered_str = triggered.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(triggered, datetime) else triggered

    entry: Dict[str, Any] = {
        "run_id": run_id,
        "task_name": task_name,
        "scheduled_for_et": scheduled_for_et,
        "run_origin": run_origin,
        "stream": stream,
        "model_requested": model_requested,
        "model_actual": model_actual,
        "model_routing_mode": model_routing_mode,
        "triggered_at_utc": triggered_str,
        "artifact_path": str(artifact_path),
    }
    if cron_id:
        entry["cron_id"] = cron_id
    if task_id:
        entry["task_id"] = task_id
    if reason_for_premium_model is not None:
        entry["reason_for_premium_model"] = reason_for_premium_model
    if notes:
        entry["notes"] = notes

    missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
    if missing:
        raise ValueError(f"model usage log entry missing required fields: {missing}")
    return entry


def resolve_log_path(triggered_at_utc: datetime, *, root: str | Path | None = None) -> Path:
    """.../model_usage/YYYYMM/model_usage.jsonl per config/scheduled_tasks.json
    -> model_usage_log.path_template (YYYYMM is the ET month of the run;
    ``triggered_at_utc`` is close enough for the monthly bucket in practice)."""
    yyyymm = triggered_at_utc.strftime("%Y%m")
    # stream_root() per call when the caller did not name a root, so the writer
    # and the credit-usage report resolve the shared root at the same moment.
    # See DEFAULT_LOG_ROOT's note.
    return Path(root if root is not None else stream_root("model_usage")) / yyyymm / "model_usage.jsonl"


def append_model_usage_entry(entry: Mapping[str, Any], *, root: str | Path | None = None) -> Path:
    """Append one already-validated entry as a JSONL line. Creates the monthly
    directory if needed. Raises on I/O failure -- use ``log_model_usage`` for
    a call site that must never raise."""
    triggered_raw = entry["triggered_at_utc"]
    if isinstance(triggered_raw, datetime):
        triggered = triggered_raw
    else:
        # ⚡ Bolt optimization: Use fromisoformat to bypass slow datetime.strptime overhead
        if not triggered_raw.endswith("Z"):
            raise ValueError(f"time data {triggered_raw!r} does not match format '%Y-%m-%dT%H:%M:%SZ'")
        triggered = datetime.fromisoformat(triggered_raw.replace("Z", "+00:00"))
    path = resolve_log_path(triggered, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(entry), sort_keys=True))
        f.write("\n")
    return path


def log_model_usage(
    *,
    root: Optional[str | Path],
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    **entry_kwargs: Any,
) -> Optional[Path]:
    """Best-effort build + append: never raises into the caller.

    ``root`` is opt-in -- pass ``None`` (the default at every call site in
    this repo) to skip logging entirely, e.g. for tests and demo/dry-run
    invocations that must not write to the real cron_tracking tree. Pass the
    real model_usage root (``DEFAULT_LOG_ROOT``, or an override) to enable
    it. Any failure (unknown task, bad entry, disk error) is logged and
    swallowed so it can never fail or block the run it is reporting on.
    """
    if root is None:
        return None
    try:
        entry = build_entry(manifest_path=manifest_path, **entry_kwargs)
        return append_model_usage_entry(entry, root=root)
    except UnknownTaskError:
        logger.debug("model usage log: skipping unlisted task", exc_info=True)
        return None
    except Exception:  # noqa: BLE001 - append must never break the caller's run
        logger.exception("model usage log append failed (non-fatal)")
        return None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.model_usage_log",
        description=(
            "Append one entry to the model usage audit log. For the covered tasks "
            "with no in-repo Python entry point (2ba19526, 7be82b97), "
            "the scheduler/agent "
            "invokes this directly as a shell command after the run completes. "
            "(e3e6f09e and a46bf007 were removed from the schedule entirely on "
            "2026-08-02 and are no longer covered tasks.) "
            "bb4f6ef1 is NOT in this list: its 9:45 AM watchlist scan goes through "
            "MorningDCAExecutor.run(), which already appends its own model-usage "
            "entry per call -- an additional explicit shell append here would "
            "double-log a single 9:45 AM firing."
        ),
    )
    parser.add_argument("--run-id", required=True)
    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--cron-id")
    id_group.add_argument("--task-id")
    parser.add_argument("--artifact-path", required=True)
    parser.add_argument(
        "--scheduled-for-et",
        default=None,
        help=(
            "Override the manifest-default scheduled_for_et for this run, e.g. "
            "'11:30' for a multi-slot task like 7be82b97 (see "
            "applies_to_daily_tasks_et.multi_slot_task_note in config/scheduled_tasks.json). "
            "Required to make each fired slot's audit record distinguishable; "
            "omitted, the manifest's combined et string for the task is used."
        ),
    )
    parser.add_argument("--run-origin", default="scheduled", choices=("scheduled", "manual"))
    parser.add_argument("--model-requested", default="cost_conscious_default")
    parser.add_argument("--model-actual", default="not_exposed")
    parser.add_argument("--model-routing-mode", default="cost_conscious", choices=MODEL_ROUTING_MODE_VALUES)
    parser.add_argument("--reason-for-premium-model", default=None, choices=PREMIUM_REASONS)
    parser.add_argument("--notes", default=None)
    parser.add_argument(
        "--triggered-at-utc",
        default=None,
        help="ISO 8601 UTC timestamp (e.g. 2026-07-21T15:20:05Z); defaults to now.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Log root (default: the shared cron_tracking path, resolved at run time).",
    )
    parser.add_argument("--manifest-path", default=DEFAULT_MANIFEST_PATH)
    return parser


def _main(argv: Optional[list] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.triggered_at_utc:
        # ⚡ Bolt optimization: Use fromisoformat to bypass slow datetime.strptime overhead
        if not args.triggered_at_utc.endswith("Z"):
            raise ValueError(f"time data {args.triggered_at_utc!r} does not match format '%Y-%m-%dT%H:%M:%SZ'")
        triggered_at_utc = datetime.fromisoformat(args.triggered_at_utc.replace("Z", "+00:00"))
    else:
        triggered_at_utc = None
    try:
        entry = build_entry(
            run_id=args.run_id,
            cron_id=args.cron_id,
            task_id=args.task_id,
            artifact_path=args.artifact_path,
            scheduled_for_et=args.scheduled_for_et,
            run_origin=args.run_origin,
            model_requested=args.model_requested,
            model_actual=args.model_actual,
            model_routing_mode=args.model_routing_mode,
            reason_for_premium_model=args.reason_for_premium_model,
            triggered_at_utc=triggered_at_utc,
            notes=args.notes,
            manifest_path=args.manifest_path,
        )
        path = append_model_usage_entry(entry, root=args.root)
    except (ValueError, UnknownTaskError, OSError) as exc:
        print(f"model_usage_log: append failed: {exc}", file=sys.stderr)
        return 1
    print(f"model_usage_log: appended to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
