"""Read-only Schedule page reader: configured vs deployable vs observed timing.

Three layers that must never be confused (GUI expansion, Phase 2):
**configured** (``config/scheduled_tasks.json`` through
``dca_console_data.build_schedule_rows``, keeping the daily snapshot's
``timing_unverified`` vocabulary), **deployable** (the checked-in ``deploy/``
cron/systemd examples, GitHub workflows and launchd agent -- files in the
checkout, not a running scheduler) and **observed** (what the runs module found
on disk, handed to :func:`build_schedule_layers` by the caller).

Nothing here asserts that a scheduler fired. Every builder is read-only and
lock-free: no ledger or ``strategy_state.json`` is opened, no executor, broker
or runner is constructed (executor modules are imported only for their
``*_PROFILES`` constants), file access goes through ``BoundedReader`` or one
explicitly bounded seek-and-read, and no field carries an absolute path.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dca_console_data import ET, _load_object, build_schedule_rows
from dca_console_jobs import redact, redact_value
from dca_console_reader import BoundedReader, RootSpec

MANIFEST_NAME = "scheduled_tasks.json"
TEXT_LIMIT = 500
NOTE_LIMIT = 300
LIST_LIMIT = 200
POLICY_BYTES = 64 * 1024
LOG_TAIL_LINES = 40
LOG_TAIL_BYTES = 8 * 1024
# Mirrors deploy/launchd/render_console_agent.py's LABEL; a test pins the two
# equal so the presence probe cannot drift from what the renderer installs.
LAUNCHD_LABEL = "com.all-stock-dca.console"
LAUNCHD_PLIST = Path("Library") / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
LAUNCHD_LOGS = ("launchd.out.log", "launchd.err.log")

# deploy/run-drift-check.sh takes exactly "<stream-subpath> <HHMMET-suffix>";
# crontab.example and every systemd ExecStart= call it that way, so the slot is
# the 4-digit-plus-ET token and the stream is the artifact subpath before it.
_CRON_LINE = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$")
_CRON_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SLOT_TOKEN = re.compile(r"^\d{4}ET$")
# Two or more directory components and not preceded by a word character, a
# colon or another slash: that scrubs /Users/... and /private/var/... but
# leaves ``http://host/x`` and API routes such as ``/api/bootstrap`` readable.
_ABSOLUTE_PATH = re.compile(r"(?<![\w:/])/(?:[^\s/'\"]+/){2,}([^\s/'\"]*)")
_STREAM_SUBPATHS = {
    "market_open_dca": "market-open", "morning_dca": "morning", "intraday_dca": "intraday",
    "midday_dca": "midday", "ce16ba6b/scheduled_runs": "afternoon", "scheduled_runs": "afternoon",
}
_WORKFLOW_EVENTS = ("schedule", "workflow_dispatch", "pull_request", "push", "workflow_run")
_TASK_EXECUTOR_FIELDS = ("executor", "execution_owner", "owner", "runner", "invoked_by")
# Profile keys safe to surface; ``state_root`` is deliberately absent because
# it is an absolute path baked in at import time.
_PROFILE_KEYS = ("mode", "trades", "cron_id", "scheduled_for_et", "window_start_et", "window_end_et",
                 "state_root_stream", "folder_suffix")
# The 3:30 PM canonical stream has no executor class: run_dca.run_slot owns it.
_AFTERNOON_OWNER = {"executor_class": None, "config_stem": "near_close_dca", "pointer_name": "latest_scheduled_run.json",
                    "profile": {"trades": True, "state_root_stream": "ce16ba6b/scheduled_runs"}}


def _text(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    """Bound, redact and path-scrub one free-text field copied from a repo file; None when absent.

    Order matters (adversarial review, round 2): the WHOLE value is redacted and
    path-scrubbed first and clipped last, so a secret straddling the limit cannot
    survive as a prefix and a ``[redacted]`` substitution cannot push a note
    past its cap. Manifest prose cites ``/home/user/workspace/...`` freely, so
    the scrub is not optional for notes either.
    """
    if not isinstance(value, str) or not value:
        return None
    cleaned = _scrub_paths(redact_value(value.replace("\n", " ")))
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _cron_utc(value: Any) -> str | None:
    """Keep a cron expression only when it has the five fields the manifest documents."""
    return value if isinstance(value, str) and len(value.split()) == 5 else None


def _dedupe(warnings: list[str]) -> list[str]:
    return list(dict.fromkeys(warnings))


def _scrub_paths(line: str) -> str:
    """Replace every absolute path in a log line by ``<path>/<basename>``."""
    return _ABSOLUTE_PATH.sub(lambda match: f"<path>/{match.group(1)}", line)


def _policy_keys(manifest: dict[str, Any]) -> list[str]:
    return sorted(key for key in manifest if key != "tasks")


def _task_timing(task: dict[str, Any]) -> str:
    """Name where a task's timing lives, so 'no cron_utc' never reads as 'never runs'.

    The seven standing Perplexity backup passes trade with no ``cron_utc``:
    their scheduler is a connector outside this repo, so the row must say so
    instead of showing an unspecified time next to a live-money task.
    """
    if _cron_utc(task.get("cron_utc")):
        return "cron_utc"
    if task.get("trades") is True:
        return "outside this repo"
    return "one-time" if isinstance(task.get("fires_at_et"), str) else "unspecified"


def _task_details(task: dict[str, Any]) -> dict[str, Any]:
    executor = next((task[field] for field in _TASK_EXECUTOR_FIELDS if isinstance(task.get(field), str)), None)
    slots = task.get("et_slots")
    return {
        "type": _text(task.get("type"), 40), "trades": _flag(task.get("trades")), "enabled": _flag(task.get("enabled")),
        "manifest_status": _text(task.get("status"), 40),
        "scheduler_registered": _flag(task.get("scheduler_registered")),
        "in_live_scheduler": _flag(task.get("in_live_scheduler")),
        "cron_utc": _cron_utc(task.get("cron_utc")),
        "et_slots": [_text(slot, 8) for slot in slots[:LIST_LIMIT]] if isinstance(slots, list) else None,
        "et_equivalent": _text(task.get("et_equivalent"), 120), "fires_at_et": _text(task.get("fires_at_et"), 120),
        "executor": _text(executor, 120), "notes": _text(task.get("notes"), NOTE_LIMIT), "timing": _task_timing(task),
    }


_EVIDENCE_RANK = {"verified": 3, "evidence_incomplete": 2, "awaiting_evidence": 1}


def slot_evidence(roots: Sequence[RootSpec], day: date, now: datetime) -> dict[str, dict[str, Any]]:
    """Per-slot evidence for ``day``, keyed by slot id, via ``verify_schedule.classify_slot_evidence``.

    The same verdict ``python -m scripts.verify_schedule`` prints, but every
    read goes through a ``BoundedReader`` on the resolved root (a Drive- or
    symlink-mounted tree would otherwise fail confinement, see
    ``dca_console_runs._resolve_roots``). Each slot is probed under every root;
    the primary root's row wins unless a secondary root holds strictly better
    evidence, in which case that row is used and flagged ``at_risk``. Only the
    root label is returned, never its path.
    """
    from scripts.scheduled_artifacts import SIGNAL_CSV_NAMES
    from scripts.verify_schedule import (
        SLOT_EVIDENCE_LAYOUT,
        classify_slot_evidence,
        market_closed_for_slot,
        slot_run_dir_relative,
    )

    warnings: list[str] = []
    result: dict[str, dict[str, Any]] = {}
    for slot_id in SLOT_EVIDENCE_LAYOUT:
        closed = market_closed_for_slot(day, slot_id)
        best: dict[str, Any] | None = None
        for root in roots:
            try:
                base = Path(root.path).resolve()
            except (OSError, RuntimeError):
                continue
            reader = BoundedReader(base, warnings)
            relative = slot_run_dir_relative(slot_id, day)
            run_dir = reader.confined(relative)
            state_file = reader.confined(relative / "run_state.json")
            present = state_file is not None and state_file.is_file()
            state = reader.json(relative / "run_state.json") if present else {}
            # Bounded presence check of the artifact contract's companion files
            # (the full validate_run_artifacts opens files outside BoundedReader).
            missing = [] if not state else [
                name for name, found in (
                    ("run_summary.json", _present(reader, relative / "run_summary.json")),
                    ("submitted_orders.csv", _present(reader, relative / "submitted_orders.csv")),
                    ("a signals CSV", any(_present(reader, relative / name) for name in SIGNAL_CSV_NAMES)),
                ) if not found]
            row = classify_slot_evidence(slot_id, day, now, run_dir_present=run_dir is not None and run_dir.is_dir(),
                                         run_state=state or None, run_state_present=present, market_closed=closed,
                                         artifact_errors=[f"{name} is missing" for name in missing])
            row.update(root_label=root.label, at_risk=root.kind == "secondary",
                       reasons=[_text(reason, 200) for reason in row["reasons"][:10]])
            if best is None or _EVIDENCE_RANK.get(row["status"], 0) > _EVIDENCE_RANK.get(best["status"], 0):
                best = row
        if best is not None:
            result[slot_id] = best
    return result


def _present(reader: BoundedReader, relative: Path) -> bool:
    path = reader.confined(relative)
    return path is not None and path.is_file()


def _apply_evidence(rows: list[dict[str, Any]], evidence: dict[str, dict[str, Any]],
                    slot_owners: dict[str, str]) -> None:
    """Replace ``elapsed_unverified`` on a live slot's manifest row with that slot's evidence verdict.

    A row matches a slot only when BOTH its time and its task id (the cron id)
    agree with the roster, so the 13:30 row of the multi-slot intraday task
    takes the 13:30 verdict, never the 11:30 one. Every other status
    (planned, not scheduled today, disabled, not registered) is a calendar or
    manifest fact and is left alone.
    """
    for row in rows:
        slot_id = row.get("time", "").replace(":", "")
        verdict = evidence.get(slot_id)
        if verdict is None or slot_owners.get(slot_id) != row.get("id") or row.get("status") != "elapsed_unverified":
            continue
        row["status"] = verdict["status"]
        row["evidence"] = {key: verdict.get(key) for key in ("run_dir", "reasons", "order_submit_status",
                                                              "orders_submitted_count", "root_label", "at_risk")}


def manifest_tasks(config_dir: Path, day: date, now: datetime, *,
                   evidence: dict[str, dict[str, Any]] | None = None,
                   slot_owners: dict[str, str] | None = None) -> dict[str, Any]:
    """Read ``config/scheduled_tasks.json`` once and enrich the daily schedule rows.

    ``tasks``: one row per ``build_schedule_rows`` entry (a multi-slot task
    yields one per ET slot) joined by task id to the manifest fields the daily
    snapshot omits: type, trades, enabled/status, scheduler registration,
    ``cron_utc``, executor label, a 300-char note and ``timing`` (``cron_utc`` /
    ``outside this repo`` / ``one-time`` / ``unspecified``). ``status`` keeps the
    snapshot's ``timing_unverified`` wording; disabled and unregistered tasks
    stay listed. ``task_count`` is the manifest's own total (None when
    unreadable); ``policy_keys`` are the keys :func:`policy_block` accepts.
    """
    warnings: list[str] = []
    manifest = _load_object(Path(config_dir) / MANIFEST_NAME)
    now = now.replace(tzinfo=ET) if now.tzinfo is None else now.astimezone(ET)
    rows = build_schedule_rows(day, now, Path(config_dir), warnings)
    tasks = manifest.get("tasks") if manifest else None
    by_id = {task["id"]: task for task in tasks if isinstance(task, dict) and isinstance(task.get("id"), str)} \
        if isinstance(tasks, list) else {}
    for row in rows:
        task = by_id.get(row["id"])
        row.update(_task_details(task) if task else {"timing": "unspecified"})
        # The shared rows bound ``name`` but never path-scrub it (that helper is
        # dca_console_data's and off-limits here); task names cite state dirs.
        row["name"] = _text(row["name"]) or "Unnamed task"
        if task is None:
            warnings.append("A schedule row could not be matched back to a manifest task id.")
    if evidence:
        _apply_evidence(rows, evidence, slot_owners or {})
    return {"tasks": rows, "task_count": len(tasks) if isinstance(tasks, list) else None,
            "policy_keys": _policy_keys(manifest) if manifest else [], "warnings": _dedupe(warnings)}


def _bounded_copy(value: Any, notes: list[str]) -> Any:
    """JSON-safe copy: lists/objects cut to 200 entries, strings to 500 chars, floats finite."""
    if isinstance(value, (dict, list)):
        entries = list(value.items()) if isinstance(value, dict) else value
        if len(entries) > LIST_LIMIT:
            notes.append(f"A {'object' if isinstance(value, dict) else 'list'} was truncated to {LIST_LIMIT} entries.")
        if isinstance(value, dict):
            return {_text(str(key), 120) or "": _bounded_copy(item, notes) for key, item in entries[:LIST_LIMIT]}
        return [_bounded_copy(item, notes) for item in entries[:LIST_LIMIT]]
    if isinstance(value, str):
        if len(value) > TEXT_LIMIT:
            notes.append(f"A string was truncated to {TEXT_LIMIT} characters.")
        return _text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if isinstance(value, (bool, int, float)) or value is None else str(value)[:TEXT_LIMIT]


def _fit(value: Any, notes: list[str]) -> Any:
    """Drop trailing entries until the serialised block fits POLICY_BYTES."""
    if len(json.dumps(value)) <= POLICY_BYTES or not isinstance(value, (dict, list)):
        return value
    entries = list(value.items()) if isinstance(value, dict) else list(value)
    kept: list[Any] = []
    size = 2
    for entry in entries:
        size += len(json.dumps(entry)) + 2
        if size > POLICY_BYTES:
            break
        kept.append(entry)
    notes.append(f"{len(entries) - len(kept)} entries omitted to stay under {POLICY_BYTES // 1024} KB.")
    return dict(kept) if isinstance(value, dict) else kept


def policy_block(config_dir: Path, key: str) -> dict[str, Any]:
    """Return one non-task top-level manifest block, bounded to 64 KB serialised.

    ``found`` is False (and ``value`` None) when ``key`` is not one of the
    manifest's policy keys -- ``tasks`` included -- so a route can 404 without
    revealing anything else. ``notes`` lists every truncation applied.
    """
    manifest = _load_object(Path(config_dir) / MANIFEST_NAME)
    if manifest is None:
        return {"key": key[:120], "found": False, "value": None, "notes": [],
                "warnings": ["The local schedule manifest is missing or malformed."]}
    if key not in _policy_keys(manifest):
        return {"key": key[:120], "found": False, "value": None, "notes": [], "warnings": []}
    notes: list[str] = []
    value = _fit(_bounded_copy(manifest[key], notes), notes)
    return {"key": key, "found": True, "value": value, "notes": _dedupe(notes), "warnings": []}


def _command_shape(command: str) -> dict[str, Any]:
    """Describe a scheduled command by script basename, arguments, slots and streams.

    Absolute tokens (the ``/opt/all-stock-dca`` placeholder in the examples, or
    a real install path on a host) are reduced to their basename, and EVERY
    emitted token then goes through :func:`_text` so a flag-style token such as
    ``--state-root=/Users/me/cron_tracking`` is path-scrubbed too (adversarial
    review, round 2: basename-only left embedded paths intact). The slot and
    stream tokens feed the join matrix.
    """
    tokens = command.split()
    shown = [_text(Path(token).name if token.startswith("/") else token, 80) for token in tokens[:20]]
    return {"script": shown[0] if shown else None, "arguments": [item for item in shown[1:] if item is not None],
            "slots": [token for token in tokens if _SLOT_TOKEN.fullmatch(token)][:LIST_LIMIT],
            "streams": sorted({_STREAM_SUBPATHS[token] for token in tokens if token in _STREAM_SUBPATHS})}


def _parse_crontab(text: str, warnings: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    """Line-by-line crontab parse: five fields plus a command; CRON_TZ captured separately."""
    rows: list[dict[str, Any]] = []
    cron_tz = None
    for line in text.splitlines()[:5000]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assignment = _CRON_ASSIGNMENT.match(stripped)
        if assignment:
            # Only CRON_TZ matters to the page; any other assignment in a real
            # crontab could be a secret, so only its name is recorded.
            if assignment.group(1) == "CRON_TZ":
                cron_tz = assignment.group(2).strip()[:40]
            else:
                warnings.append(f"crontab assignment {assignment.group(1)[:40]} was noted by name only.")
            continue
        match = _CRON_LINE.match(stripped)
        if not match:
            warnings.append("A crontab line did not parse as five fields plus a command.")
            continue
        if len(rows) >= LIST_LIMIT:
            warnings.append("The crontab has more entries than the page shows.")
            break
        rows.append({"schedule": _text(" ".join(match.groups()[:5]), 120), "timezone": cron_tz,
                     **_command_shape(match.group(6))})
    return rows, cron_tz


def _parse_unit(text: str) -> dict[str, str]:
    """Collect ``Key=Value`` pairs from a systemd unit, ignoring sections and comments."""
    found: dict[str, str] = {}
    for line in text.splitlines()[:2000]:
        stripped = line.strip()
        if stripped and stripped[0] not in "#;[" and "=" in stripped:
            key, value = stripped.split("=", 1)
            found.setdefault(key.strip(), value.strip())
    return found


def _systemd_rows(reader: BoundedReader, warnings: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in sorted(reader.entries("deploy/systemd", limit=LIST_LIMIT)):
        if entry.suffix not in {".timer", ".service"}:
            continue
        unit = _parse_unit(reader.text(entry) or "")
        if not unit:
            warnings.append(f"systemd unit {entry.name} is empty or unreadable.")
            continue
        row: dict[str, Any] = {"file": entry.name, "kind": entry.suffix[1:], "description": _text(unit.get("Description"), 120)}
        if entry.suffix == ".timer":
            row.update({"on_calendar": _text(unit.get("OnCalendar"), 80), "unit": _text(unit.get("Unit"), 80),
                        "persistent": (unit.get("Persistent") or "").lower() in {"true", "yes", "1", "on"}})
        else:
            # EnvironmentFile= names the secrets file; the NAME is shown, the
            # contents are never opened by any console reader.
            env_file = unit.get("EnvironmentFile")
            row.update({"user": _text(unit.get("User"), 40),
                        "environment_file": _text(Path(env_file.lstrip("-")).name, 120) if env_file else None,
                        **_command_shape(unit.get("ExecStart", ""))})
        rows.append(row)
    return rows


def deploy_layer(repo: Path) -> dict[str, Any]:
    """Parse the checked-in ``deploy/`` cron and systemd examples without executing anything.

    ``cron`` rows come from ``deploy/crontab.example`` (five fields, CRON_TZ,
    command shape with slot/stream tokens) and are None when that file is
    absent; ``systemd`` rows from ``deploy/systemd/*.timer`` and ``*.service``;
    ``env_files`` lists the ``deploy/drift-slack.env*`` file NAMES present,
    never their contents. These are example files, not any host's crontab.
    """
    warnings: list[str] = []
    reader = BoundedReader(Path(repo).resolve(), warnings)
    crontab = reader.text("deploy/crontab.example")
    cron_rows, cron_tz = _parse_crontab(crontab, warnings) if crontab is not None else (None, None)
    if crontab is None:
        warnings.append("deploy/crontab.example is absent; the cron layer is unavailable.")
    env_files = sorted(entry.name for entry in reader.entries("deploy", limit=LIST_LIMIT)
                       if entry.name.startswith("drift-slack.env"))
    return {"cron": cron_rows, "cron_tz": cron_tz, "systemd": _systemd_rows(reader, warnings),
            "env_files": env_files, "warnings": _dedupe(warnings)}


def _workflow_triggers(lines: list[str]) -> list[str]:
    """Find the ``on:`` block by indentation and list its events and cron strings.

    A line scanner, not a YAML parser: the workflows use the flat ``on:`` with
    two-space-indented event keys, and PyYAML is not a runtime dependency.
    """
    triggers: list[str] = []
    inside = False
    for line in lines:
        if not inside:
            head = re.match(r"^(?:on|\"on\"|'on'):\s*(.*)$", line)
            if head is None:
                continue
            inline = head.group(1).split("#", 1)[0].strip()
            if inline:  # ``on: push`` and ``on: [push, pull_request]`` inline forms.
                return [token for token in re.findall(r"[a-z_]+", inline) if token in _WORKFLOW_EVENTS]
            inside = True
            continue
        if line.strip() and not line.startswith(" ") and not line.lstrip().startswith("#"):
            break
        event = re.match(r"^  ([a-z_]+):", line)
        if event and event.group(1) in _WORKFLOW_EVENTS:
            triggers.append(event.group(1))
        cron = re.match(r"^\s*-\s*cron:\s*['\"]?([^'\"#]+?)['\"]?\s*(#.*)?$", line)
        if cron:
            triggers.append(cron.group(1).strip()[:40])
    return triggers


def workflows_layer(repo: Path) -> dict[str, Any]:
    """List ``.github/workflows/*.yml`` with their triggers by bounded line scanning.

    Each row: ``file``, ``name`` (top-level ``name:`` or None), ``triggers``
    (event names among schedule/workflow_dispatch/pull_request/push/workflow_run
    plus every schedule cron string) and ``has_secrets_reference`` (the text
    mentions ``secrets.``; secret names are not copied). ``workflows`` is None
    when the directory is absent.
    """
    warnings: list[str] = []
    reader = BoundedReader(Path(repo).resolve(), warnings)
    folder = reader.confined(".github/workflows")
    if folder is None or not folder.is_dir():
        return {"workflows": None, "warnings": ["No .github/workflows directory; the workflow layer is unavailable."]}
    rows = []
    for entry in sorted(reader.entries(".github/workflows", limit=LIST_LIMIT)):
        if entry.suffix not in {".yml", ".yaml"}:
            continue
        text = reader.text(entry)
        if text is None:
            warnings.append(f"Workflow {entry.name} could not be read.")
            continue
        lines = text.splitlines()[:5000]
        name = next((match.group(1) for line in lines if (match := re.match(r"^name:\s*(.+?)\s*$", line))), None)
        rows.append({"file": entry.name, "name": _text(name, 120), "triggers": _workflow_triggers(lines),
                     "has_secrets_reference": "secrets." in text})
    return {"workflows": rows, "warnings": _dedupe(warnings)}


def _log_tail(reader: BoundedReader, relative: Path, warnings: list[str]) -> dict[str, Any] | None:
    """Return size and the last 40 lines of one log, reading at most 8 KB from its end."""
    path = reader.confined(relative)
    if path is None or not path.is_file():
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            data = handle.read(LOG_TAIL_BYTES)
    except OSError:
        warnings.append(f"{relative.name} exists but could not be read.")
        return None
    # ``redact`` (the free-text boundary) rather than ``redact_value``: a log
    # line is exactly the shape where bare numbers and label=value pairs leak.
    # Neither scrubs paths, and the console's own startup banner prints the
    # summaries directory, hence the path scrub first.
    lines = data.decode("utf-8", "replace").splitlines()[-LOG_TAIL_LINES:]
    return {"name": relative.name, "size": size, "tail": [redact(_scrub_paths(line))[:TEXT_LIMIT] for line in lines]}


def launchd_status(roots: Sequence[RootSpec], home: Path | None = None) -> dict[str, Any]:
    """Report whether the console's LaunchAgent plist exists and tail its logs.

    ``plist_present`` checks ``~/Library/LaunchAgents/<LAUNCHD_LABEL>.plist``
    (``home`` is injectable for tests); presence means "rendered and copied
    into place", not "loaded" -- ``launchctl`` is never run. ``logs`` covers
    ``<primary root>/console/launchd.{out,err}.log`` when present, each tail
    path-scrubbed and redacted; absent files simply do not appear.
    """
    warnings: list[str] = []
    home = Path(home) if home is not None else Path.home()
    modified = BoundedReader(home.resolve(), warnings).modified_at(LAUNCHD_PLIST)
    primary = next((root for root in roots if root.kind == "primary"), roots[0] if roots else None)
    logs: list[dict[str, Any]] = []
    if primary is None:
        warnings.append("No artifact root was supplied; launchd logs are unavailable.")
    else:
        reader = primary.reader(warnings)
        logs = [tail for name in LAUNCHD_LOGS if (tail := _log_tail(reader, Path("console") / name, warnings))]
    return {"plist_present": modified is not None,
            "plist_modified": datetime.fromtimestamp(modified, ET).isoformat() if modified is not None else None,
            "log_root": primary.label if primary else None, "logs": logs, "warnings": _dedupe(warnings)}


def _profile_rows() -> dict[str, dict[str, Any]]:
    """Map slot id -> executor class name, allowlisted profile fields, config stem and pointer.

    Importing the executor modules is heavy but constructs nothing; only
    ``_PROFILE_KEYS`` are copied out of each profile dict and the pointer name
    is a class attribute, so no executor is instantiated.
    """
    import intraday_dca
    import market_open_dca
    import midday_dca
    import morning_dca

    def row(cls, profile: dict[str, Any], stem: str) -> dict[str, Any]:
        pointer = getattr(cls, "latest_pointer_name", None)
        return {"executor_class": cls.__name__, "config_stem": stem,
                "pointer_name": pointer if isinstance(pointer, str) else None,
                "profile": {key: profile.get(key) for key in _PROFILE_KEYS if isinstance(profile.get(key), (str, bool))}}

    rows = {"0935": row(market_open_dca.MarketOpenDCAExecutor, market_open_dca.MARKET_OPEN_PROFILES.get("live", {}),
                        "market_open_dca"),
            "1030": row(morning_dca.MorningDCAExecutor, morning_dca.MORNING_PROFILES.get("live", {}), "morning_dca"),
            "1230": row(midday_dca.MiddayDCAExecutor, midday_dca.MIDDAY_PROFILES.get("live", {}), "midday_dca")}
    for slot in ("1130", "1330", "1430"):
        rows[slot] = row(intraday_dca.IntradayDCAExecutor, intraday_dca.INTRADAY_SLOT_PROFILES.get(slot, {}), "intraday_dca")
    return rows


def roster_identity(config_dir: Path) -> dict[str, Any]:
    """One row per ``dca_runner.SLOTS`` entry naming who owns it and how it is configured.

    ``run_dca_entry`` is ``run_slot`` when ``run_dca.SLOT_MAP`` owns the slot,
    ``disabled`` when it is in ``DISABLED_SLOTS``, else ``not owned``.
    ``live_execution_enabled`` is the raw boolean from ``config/<stream>_dca.json``
    (None when the file or key is absent: morning and the 3:30 stream carry no
    such flag). ``window`` is the runner's acceptance window, ``executor_window``
    the executor profile's own, which may differ. Nothing here says a slot ran.
    """
    from dca_runner import SLOTS
    from run_dca import AFTERNOON_LIVE_CRON_ID, DISABLED_SLOTS, SLOT_MAP

    warnings: list[str] = []
    profiles = _profile_rows()
    slots = []
    for slot_id, spec in SLOTS.items():
        owner = profiles.get(slot_id) or {**_AFTERNOON_OWNER, "profile": {**_AFTERNOON_OWNER["profile"],
                                                                          "cron_id": AFTERNOON_LIVE_CRON_ID}}
        profile = owner["profile"]
        config_file = f"{owner['config_stem']}.json"
        config = _load_object(Path(config_dir) / config_file)
        if config is None:
            warnings.append(f"config/{config_file} is missing or malformed; its live flag is unknown.")
        key = f"{slot_id}ET"
        slots.append({
            "slot_id": slot_id, "time_et": "%02d:%02d" % spec.nominal_et, "stream": spec.label,
            "executor_class": owner["executor_class"], "cron_id": profile.get("cron_id"), "trades": profile.get("trades"),
            "live_execution_enabled": _flag(config.get("live_execution_enabled")) if config else None,
            "run_dca_entry": "run_slot" if key in SLOT_MAP else "disabled" if key in DISABLED_SLOTS else "not owned",
            "window": {"start": "%02d:%02d" % spec.window_start_et, "end": "%02d:%02d" % spec.window_end_et},
            "executor_window": {"start": profile.get("window_start_et"), "end": profile.get("window_end_et")},
            "artifact_dir": profile.get("state_root_stream"), "pointer_name": owner["pointer_name"],
            "config_file": config_file,
        })
    return {"slots": slots, "warnings": _dedupe(warnings)}


def _observed_rank(row: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Primary root beats secondary; then scheduled evidence; then a present run_state."""
    return (row.get("at_risk") is not True, row.get("evidence") is True, row.get("run_state_present") is True)


def observed_from_pointer_strip(strip: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Collapse ``dca_console_runs.pointer_strip`` into the stream-keyed ``observed`` dict.

    ``pointer_strip`` returns ``{"streams": [row, ...]}``: one row per root per
    stream (two probes for ``afternoon``) plus ``alias=True`` rows, each shaped
    by ``_pointer_row`` (``stream``, ``label``, ``at_risk``, ``status``,
    ``evidence`` bool, ``evidence_label``, ``run_state_present``). The matrix
    wants one entry per stream, so the primary root's canonical row wins, a
    secondary root's row is used only when no primary row exists and keeps
    ``at_risk=True`` so the cell says so, and an alias pointer never stands in
    for the canonical one (adversarial review, round 2). Keys are the runs
    module's stream names (``market_open``, ``afternoon`` ...);
    :func:`build_schedule_layers` maps the runner labels onto them. A caller
    with per-slot evidence may add ``"<slot_id>"`` keys (see ``_observed_for``).
    """
    rows = strip.get("streams") if isinstance(strip, dict) else None
    observed: dict[str, dict[str, Any]] = {}
    for row in rows[:LIST_LIMIT] if isinstance(rows, list) else []:
        stream = row.get("stream") if isinstance(row, dict) else None
        if not isinstance(stream, str) or row.get("alias") is True:
            continue
        if stream not in observed or _observed_rank(row) > _observed_rank(observed[stream]):
            observed[stream] = row
    return observed


def _observed_entry(entry: dict[str, Any], *, per_slot: bool) -> dict[str, Any]:
    # ``evidence`` is the producer's bool (True only for STATUS_VALID with
    # scheduled evidence), never coerced through _text: it is the one flag that
    # separates a scheduled fire from a manual or recovery-context run.
    return {"status": _text(entry.get("status"), 80), "evidence": _flag(entry.get("evidence")),
            "evidence_label": _text(entry.get("evidence_label"), 80),
            "run_state_present": _flag(entry.get("run_state_present")), "at_risk": _flag(entry.get("at_risk")),
            "label": _text(entry.get("label"), 80), "per_slot": per_slot}


def _observed_for(observed: dict[str, Any] | None, slot: dict[str, Any],
                  shared: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one roster slot's observed entry; second item is a note when the match is only per-stream.

    A slot-qualified key (``"1330"`` or ``"intraday@1330"``) is tried first.
    A stream key is accepted for a stream that owns one slot, but ``intraday``
    covers 1130/1330/1430 with a single pointer, so a stream-level match there
    is returned with ``per_slot=False`` and the cell stays None: one 11:30
    ``run_state.json`` must not light up the 13:30 and 14:30 rows (adversarial
    review, round 2).
    """
    if not isinstance(observed, dict):
        return None, None
    stream = str(slot.get("stream") or "")
    slot_id = str(slot.get("slot_id") or "")
    alias: str = {"final": "afternoon"}.get(stream, stream)
    for key in (slot_id, f"{stream}@{slot_id}", f"{alias}@{slot_id}"):
        if isinstance(observed.get(key), dict):
            return _observed_entry(observed[key], per_slot=True), None
    entry = next((observed[key] for key in (stream, alias, alias.replace("-", "_"))
                  if isinstance(observed.get(key), dict)), None)
    if entry is None:
        return None, None
    if stream in shared:
        return _observed_entry(entry, per_slot=False), \
            f"Evidence for '{stream}' is per-stream, not per-slot; its single pointer cannot say which slot wrote it."
    return _observed_entry(entry, per_slot=True), None


def _matrix_row(slot: dict[str, Any], manifest: dict[str, Any], deploy: dict[str, Any],
                observed: dict[str, Any] | None, shared: set[str]) -> dict[str, Any]:
    tasks = [row for row in manifest["tasks"] if row["time"] == slot["time_et"]]
    key = f"{slot['slot_id']}ET"
    cron = [row for row in deploy["cron"] or [] if key in row["slots"]]
    systemd = [row["file"] for row in deploy["systemd"] if key in row.get("slots", ())]
    entry, note = _observed_for(observed, slot, shared)
    settled = entry is not None and entry["per_slot"]
    live = [row for row in tasks if row["status"] not in {"disabled", "not_registered"}]
    return {
        "slot_id": slot["slot_id"], "time_et": slot["time_et"], "stream": slot["stream"],
        "manifest_task_ids": [row["id"] for row in tasks], "cron_entries": len(cron), "systemd_units": systemd,
        # None = that layer itself was unavailable, never "unknown whether it fired".
        "configured": None if manifest["task_count"] is None else bool(live),
        "deployable": None if deploy["cron"] is None and not deploy["systemd"] else bool(cron or systemd),
        # ``observed`` is run_state presence (manual and recovery runs count);
        # ``scheduled_evidence`` is the stricter producer flag; ``observed_at_risk``
        # says the evidence sits under a secondary (at-risk) root only.
        "observed": entry["run_state_present"] if (entry is not None and settled) else None,
        "scheduled_evidence": entry["evidence"] if (entry is not None and settled) else None,
        "observed_at_risk": entry["at_risk"] if entry else None,
        "observed_entry": entry, "observed_note": note,
    }


def build_schedule_layers(config_dir: Path, repo: Path, roots: Sequence[RootSpec], day: date, now: datetime, *,
                          observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble every layer and join them per runner slot into ``matrix``.

    ``observed`` is a dict of ``_pointer_row``-shaped entries keyed by slot id
    (``"1330"``, preferred) or by the runs module's stream name, as
    :func:`observed_from_pointer_strip` produces; when omitted every
    ``observed`` cell is None. True in ``configured``/``deployable`` means a
    file in the checkout mentions the slot; only ``observed`` reflects
    artifacts on disk, and even that never asserts the scheduler fired.
    """
    now = now.replace(tzinfo=ET) if now.tzinfo is None else now.astimezone(ET)
    roster = roster_identity(config_dir)
    slots = roster["slots"]
    # Per-slot run_state evidence (scripts/verify_schedule.py's verdict) is what
    # turns an elapsed live row from "elapsed_unverified" into verified or
    # missing; the per-stream pointer ``observed`` above cannot, because one
    # intraday pointer covers three slots (2026-09-22 schedule audit).
    evidence = slot_evidence(roots, day, now)
    owners = {slot["slot_id"]: slot["cron_id"] for slot in slots if isinstance(slot.get("cron_id"), str)}
    layers = {"manifest": manifest_tasks(config_dir, day, now, evidence=evidence, slot_owners=owners),
              "deploy": deploy_layer(repo), "workflows": workflows_layer(repo), "launchd": launchd_status(roots),
              "roster": roster}
    shared = {slot["stream"] for slot in slots if sum(other["stream"] == slot["stream"] for other in slots) > 1}
    matrix = [_matrix_row(slot, layers["manifest"], layers["deploy"], observed, shared) for slot in slots]
    for row in matrix:
        verdict = evidence.get(row["slot_id"])
        row["slot_evidence"] = verdict["status"] if verdict else None
        row["slot_evidence_reasons"] = verdict["reasons"] if verdict else []
    warnings = [warning for layer in layers.values() for warning in layer["warnings"]]
    warnings.extend(row["observed_note"] for row in matrix if row["observed_note"])
    failing = [row["slot_id"] for row in matrix if row["slot_evidence"] in {"evidence_incomplete", "no_evidence"}]
    if failing:
        warnings.append(f"Live slot(s) {', '.join(failing)} have no finalized scheduled run_state for {day.isoformat()}; "
                        "run `python -m scripts.verify_schedule` for reasons.")
    warnings.append("Every row is configuration or on-disk evidence; 'verified' means a scheduled run_state was "
                    "finalized for that slot, not that any order filled.")
    return {**layers, "matrix": matrix, "evidence": evidence, "warnings": _dedupe(warnings)}
