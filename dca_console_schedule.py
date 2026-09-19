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
    """Scrub, redact and bound one free-text field copied from a repo file; None when absent.

    Paths are scrubbed here as well as in log tails because manifest notes and
    policy prose quote install locations, and ``walk_redact`` (the route-level
    pass) only knows secrets and account shapes, not paths. Clipping happens
    after redaction so a ``[redacted]`` substitution cannot push the field past
    ``limit``.
    """
    if not isinstance(value, str) or not value:
        return None
    cleaned = redact_value(_scrub_paths(value.replace("\n", " ")))
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


def manifest_tasks(config_dir: Path, day: date, now: datetime) -> dict[str, Any]:
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
        if task is None:
            warnings.append("A schedule row could not be matched back to a manifest task id.")
    return {"tasks": rows, "task_count": len(tasks) if isinstance(tasks, list) else None,
            "policy_keys": _policy_keys(manifest) if manifest else [], "warnings": _dedupe(warnings)}


def _bounded_copy(value: Any, notes: list[str]) -> Any:
    """JSON-safe copy: lists/objects cut to 200 entries, strings to 500 chars, floats finite."""
    if isinstance(value, (dict, list)):
        entries = list(value.items()) if isinstance(value, dict) else value
        if len(entries) > LIST_LIMIT:
            notes.append(f"A {'object' if isinstance(value, dict) else 'list'} was truncated to {LIST_LIMIT} entries.")
        if isinstance(value, dict):
            return {str(key)[:120]: _bounded_copy(item, notes) for key, item in entries[:LIST_LIMIT]}
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
    a real install path on a host) are reduced to their basename so no path
    leaves the process; the slot and stream tokens feed the join matrix.
    """
    tokens = command.split()
    shown = [Path(token).name if token.startswith("/") else token[:80] for token in tokens[:20]]
    return {"script": shown[0] if shown else None, "arguments": [redact_value(item) for item in shown[1:]],
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
        rows.append({"schedule": " ".join(match.groups()[:5]), "timezone": cron_tz, **_command_shape(match.group(6))})
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
                        "environment_file": Path(env_file.lstrip("-")).name if env_file else None,
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


def _observed_for(observed: dict[str, Any] | None, stream: str) -> dict[str, Any] | None:
    """Look up the runs module's pointer-strip entry under either stream spelling."""
    if not isinstance(observed, dict):
        return None
    alias = {"final": "afternoon"}.get(stream, stream)
    for candidate in (stream, alias, alias.replace("-", "_")):
        entry = observed.get(candidate)
        if isinstance(entry, dict):
            return {"status": _text(entry.get("status"), 80), "evidence": _text(entry.get("evidence"), 200),
                    "run_state_present": _flag(entry.get("run_state_present"))}
    return None


def _matrix_row(slot: dict[str, Any], manifest: dict[str, Any], deploy: dict[str, Any],
                observed: dict[str, Any] | None) -> dict[str, Any]:
    tasks = [row for row in manifest["tasks"] if row["time"] == slot["time_et"]]
    key = f"{slot['slot_id']}ET"
    cron = [row for row in deploy["cron"] or [] if key in row["slots"]]
    systemd = [row["file"] for row in deploy["systemd"] if key in row.get("slots", ())]
    entry = _observed_for(observed, slot["stream"])
    live = [row for row in tasks if row["status"] not in {"disabled", "not_registered"}]
    return {
        "slot_id": slot["slot_id"], "time_et": slot["time_et"], "stream": slot["stream"],
        "manifest_task_ids": [row["id"] for row in tasks], "cron_entries": len(cron), "systemd_units": systemd,
        # None = that layer itself was unavailable, never "unknown whether it fired".
        "configured": None if manifest["task_count"] is None else bool(live),
        "deployable": None if deploy["cron"] is None and not deploy["systemd"] else bool(cron or systemd),
        "observed": None if entry is None else entry["run_state_present"], "observed_entry": entry,
    }


def build_schedule_layers(config_dir: Path, repo: Path, roots: Sequence[RootSpec], day: date, now: datetime, *,
                          observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble every layer and join them per runner slot into ``matrix``.

    ``observed`` is the runs module's pointer strip keyed by stream (``status``,
    ``evidence``, ``run_state_present``); when omitted every ``observed`` cell
    is None. True in ``configured``/``deployable`` means a file in the checkout
    mentions the slot; only ``observed`` reflects artifacts on disk, and even
    that never asserts the scheduler fired.
    """
    layers = {"manifest": manifest_tasks(config_dir, day, now), "deploy": deploy_layer(repo),
              "workflows": workflows_layer(repo), "launchd": launchd_status(roots), "roster": roster_identity(config_dir)}
    matrix = [_matrix_row(slot, layers["manifest"], layers["deploy"], observed) for slot in layers["roster"]["slots"]]
    warnings = [warning for layer in layers.values() for warning in layer["warnings"]]
    warnings.append("Every row is configuration or on-disk evidence; scheduler execution is unverified.")
    return {**layers, "matrix": matrix, "warnings": _dedupe(warnings)}
