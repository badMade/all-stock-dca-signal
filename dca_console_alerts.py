"""Read-only views of what Gate 9 and the reliability layer leave behind.

The Alerts & health console page (GUI expansion, Phase 2) reads five kinds of
residue and never writes any of them: the Gate 9 alert files each stream keeps
under ``<stream dir>/alerts/``, the failed-start markers in ``attempt_log/``,
the ``model_usage/YYYYMM/model_usage.jsonl`` audit log, the delivery channels
the executors *could* use (env var names, never values) and the reliability
engines that are library-only on most machines. Every builder takes explicit
``RootSpec`` roots and returns a JSON-safe dict in which ``None`` means
"unavailable", never zero; every free-text field is bounded, has absolute
paths and broker order ids scrubbed (the same "never emit an absolute path,
order id or account number" invariant the other console readers assert) and
is passed through ``redact_value``; nothing here takes a cross-process lock,
constructs an executor or broker, or posts to Slack.

Three rules from the plan's adversarial pass shape the details below:

- ``scripts.alert_digest.load_alert_files`` recurses with an unbounded
  ``rglob`` and reads files without a size ceiling, so the alert walk is done
  here over the *known* alert directories through ``BoundedReader`` and the
  records are assembled from the digest's public detectors instead.
- A secondary root (the gitignored ``artifacts/`` tree) is where 23 of the
  2026-08 alerts were lost, so every alert found only there is ``at_risk``.
- A trading day with no alert file is reported as "no alert found", never as
  healthy: seven clean runs and seven that never fired look identical here.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# The runs module owns two contracts this page must agree with byte for byte:
# the absolute-path scrubber (adversarial review of this module: a private
# lookbehind variant here let ``path:/Users/...`` through untouched, so the
# one pattern and the one ``<path>`` placeholder are shared, not copied) and
# ``run_identity``, the preimage every ``run-`` id is minted from, so a
# model-usage row can be joined to the runs index and GET /api/alerts/drift.
from dca_console_jobs import redact_value
from dca_console_reader import MAX_FILES, BoundedReader, RootSpec, iso_date
from dca_console_runs import _ABSOLUTE_PATH_RE, run_identity
from dca_paths import STREAM_ARTIFACT_DIRS

ET = ZoneInfo("America/New_York")
MAX_ALERTS = 500
MAX_WINDOW_DAYS = 120
MAX_ROWS = 200
MAX_LINES = 200
MAX_USAGE_ENTRIES = 1_000
MAX_DRIFT_RUNS = 50
MAX_STATE_FILES = 2_000
EXCERPT_CHARS = 300
TEXT_CHARS = 500
MONTH = re.compile(r"^\d{4}-\d{2}$")
DAY_DIR = re.compile(r"^\d{8}$")
# Gate 9 names alert files ``<YYYY-MM-DD>-<slot>-<gate>.txt`` (optionally
# ``<gate>.N.txt`` on a same-day collision); the same shape alert_digest parses.
ALERT_NAME = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})-(?P<slot>[A-Za-z0-9]+)-(?P<gate>[A-Za-z0-9_]+(?:\.\d+)?)\.txt$")
CRON_LINE = re.compile(r"^\s*-\s*cron:\s*['\"]([^'\"]{1,60})['\"]")
# Broker order ids. A Gate 9 body routinely quotes the order it is complaining
# about, and ``redact_value`` has no order-id rule of its own, so the same
# "no order id reaches the browser" invariant the other console readers
# assert is enforced here before an excerpt is cut. Two shapes: a bare UUID
# (Robinhood's order and instrument ids) anywhere, and a token holding a
# digit right after an ``order`` / ``order id`` / ``order ref`` label, which
# is how prose names a non-UUID reference ("by order abc-123").
UUID_TOKEN = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
ORDER_REF = re.compile(r"(?i)(\border(?:[ _-]?(?:id|uuid|ref))?\s*[:=#]?\s*)(?=[\w-]*\d)[\w-]{4,}")
# No NOTION_TOKEN or SMTP_* reader exists in this repository (dca_notion_sync
# is offline by design), so the env channel list is exactly the names
# dca_alerts reads; the page must not suggest a channel the code cannot use.
USAGE_FIELDS = ("task_name", "task_id", "cron_id", "scheduled_for_et", "run_origin", "stream", "model_requested",
                "model_actual", "model_routing_mode", "reason_for_premium_model", "triggered_at_utc", "notes")
MARKER_FIELDS = ("triggered_at_utc", "error_class", "correlation_id")
_STREAM_OF_DIR = {folder: stream for stream, folder in STREAM_ARTIFACT_DIRS.items()}


def _alert_id(label: str, relative: str) -> str:
    # Same hashing as dca_console_data._run_row (the browser sees an opaque id,
    # never the file it was minted from) but under an ``alert-`` prefix: a
    # ``run-`` id promises the runs index can resolve it, and an alert file is
    # not a run (adversarial review of this module, id-shape collision).
    return "alert-" + hashlib.sha256(f"{label}:{relative}".encode()).hexdigest()[:12]


def _run_id_for(root: RootSpec, artifact: Any) -> str | None:
    """The runs-index id of ``artifact`` (an absolute run directory), or None when it is not under ``root``."""
    if not isinstance(artifact, str) or not artifact:
        return None
    try:
        # Resolve both sides exactly as dca_console_runs._index_root does, so
        # the preimage (label + run dir relative to the resolved root) is the
        # one the runs module hashed and GET /api/alerts/drift can look up.
        base, target = root.path.resolve(), Path(artifact).resolve()
    except (OSError, RuntimeError):
        return None
    return run_identity(root.label, str(target.relative_to(base))) if target != base and target.is_relative_to(base) else None


def _text(value: Any, limit: int = TEXT_CHARS) -> str | None:
    """Bound, path-scrub, order-id-scrub and redact one free-text field copied from an artifact.

    The scrubs run before the final cut, on whole tokens: a raw cut at
    ``limit`` used to leave an excerpt of a few dozen characters once two
    home-directory paths had eaten the budget, and a cut inside a UUID or a
    path would hand the scrubbers a fragment they cannot recognise. So the
    body is pre-cut generously (paths and ids never contain whitespace, so
    dropping the one partial trailing token keeps every token whole), scrubbed,
    redacted, and only then bounded to ``limit``.
    """
    if not isinstance(value, str):
        return None
    raw = " ".join(value[: limit * 4].split())
    if len(value) > limit * 4:
        raw = raw.rsplit(" ", 1)[0] if " " in raw else ""
    text = _ABSOLUTE_PATH_RE.sub("<path>", raw)
    return redact_value(ORDER_REF.sub(r"\1[order-id]", UUID_TOKEN.sub("[order-id]", text)))[:limit]


def _window(start: date, end: date) -> None:
    if start > end or (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"The alert window must run forward and span at most {MAX_WINDOW_DAYS} days")


def _finish(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    result["warnings"] = list(dict.fromkeys(warnings))
    return result


# --- Gate 9 alert files ------------------------------------------------------


ALERT_FOLDERS = (*(f"{name}/alerts" for name in STREAM_ARTIFACT_DIRS.values()), "alerts")


def _alert_files(reader: BoundedReader) -> list[Path]:
    """Every ``.txt`` in a known alerts directory, one day-folder deep, capped at MAX_FILES."""
    found: list[Path] = []
    for folder in ALERT_FOLDERS:
        for entry in reader.entries(folder):
            full = reader.root / entry
            # Offline exports partition alerts by day beneath alerts/, exactly one
            # level; anything deeper is not a layout any writer produces.
            children = reader.entries(entry) if full.is_dir() and iso_date(entry.name) else [entry]
            found.extend(child for child in children if child.suffix == ".txt" and (reader.root / child).is_file())
            if len(found) >= MAX_FILES:
                reader.warn("The alert directories exceed the file scan limit; later alerts were not read.")
                return found[:MAX_FILES]
    return found


def _slot_from_token(token: str, slots: Sequence[str]) -> str | None:
    candidate = token.upper().removesuffix("ET")
    if len(candidate) == 3 and candidate.isdigit():
        candidate = "0" + candidate  # Gate 9 writes the 9:35 slot as a bare 935.
    return candidate if candidate in slots else None


def _alert_record(reader: BoundedReader, relative: Path, digest: Any) -> Any:
    """Build one AlertRecord the way load_alert_files does, from a bounded read."""
    body = reader.text(relative)
    modified = reader.modified_at(relative)
    if body is None or modified is None:
        return None
    match = ALERT_NAME.match(relative.name)
    named_day = None
    slot_token = ""
    gate_token = ""
    if match and iso_date(match["day"]):
        named_day = date.fromisoformat(match["day"])
        slot_token = match["slot"]
        gate_token = match["gate"]
    slot = _slot_from_token(slot_token, digest.SLOTS) if named_day else None
    slot_evidence = "filename" if slot else ""
    if slot is None:
        slot, slot_evidence = digest.detect_slot_evidence(f"{relative.name}\n{slot_token}\n{body}")
    gate, gate_evidence = digest.detect_gate_evidence(gate_token, body)
    # A contract filename is filed at ET noon, the one hour that cannot cross a
    # date boundary in UTC; an off-contract file falls back to its mtime.
    stamp = (datetime.combine(named_day, datetime.min.time(), ET).replace(hour=12).astimezone(timezone.utc)
             if named_day else datetime.fromtimestamp(modified, tz=timezone.utc))
    return digest.AlertRecord(
        alert_id=relative.as_posix(), source="alert_file", timestamp_utc=stamp, subject=relative.name, body=body,
        slot=slot, slot_evidence=slot_evidence, gate=gate, gate_evidence=gate_evidence,
        verdict=digest.detect_verdict(relative.name, body),
        # path_hint stays empty on purpose: it would hand an absolute path to a
        # detector whose detail text can end up in a response.
        warnings=digest.detect_channel_warnings(body, source="alert_file"),
        revision=str(int(modified * 1e9)), date_evidence="filename" if named_day else "",
    )


def _alert_tree_present(root: RootSpec, warnings: list[str]) -> bool:
    """Whether any known alerts directory exists under ``root``; warns when none does.

    ``BoundedReader.entries`` returns [] silently for an absent directory, which
    would let a mis-pointed root render as a clean, empty inbox. A missing tree
    is absence of evidence, not a quiet window (adversarial review of this
    module; the same rule attempt_log_view's ``root_present`` follows).
    """
    present = any((root.path / folder).is_dir() for folder in ALERT_FOLDERS)
    if not present:
        warnings.append(f"No alerts directory exists under {root.label}; an empty inbox there is not evidence "
                        "of a quiet window.")
    return present


def _collect(roots: Sequence[RootSpec], warnings: list[str]) -> list[tuple[RootSpec, Path, Any]]:
    from scripts import alert_digest

    found = []
    for root in roots:
        reader = root.reader(warnings)
        if not _alert_tree_present(root, warnings):
            continue
        for relative in _alert_files(reader):
            record = _alert_record(reader, relative, alert_digest)
            if record is not None:
                found.append((root, relative, record))
    return found


def _inbox_row(root: RootSpec, relative: Path, record: Any) -> dict[str, Any]:
    codes = sorted({code for code, _ in record.warnings})
    return {
        "id": _alert_id(root.label, relative.as_posix()), "root_label": root.label,
        # Only the primary tree is dca_paths-managed and version-safe; a
        # secondary root is the gitignored tree that has already lost alerts.
        "at_risk": root.kind != "primary",
        "stream": _STREAM_OF_DIR.get(relative.parts[0], "unknown") if len(relative.parts) > 2 else "unknown",
        "file": relative.as_posix(), "day": record.et_date.isoformat(), "slot": record.slot, "gate": record.gate,
        "verdict": record.verdict, "channel_warnings": codes, "contract_filename": record.date_evidence == "filename",
        # A file proves only the repo-file channel. A delivery contradiction the
        # digest detected proves non-delivery; its absence proves nothing.
        "delivered": False if codes else None,
        "excerpt": _text(record.body, EXCERPT_CHARS),
    }


def alert_inbox(roots: Sequence[RootSpec], start: date, end: date, *, config_dir: Path | None = None) -> dict[str, Any]:
    """List Gate 9 alert files from every root whose ET day falls in ``[start, end]``.

    Reads ``<root>/<stream dir>/alerts/*.txt`` (one day-folder deep) and
    ``<root>/alerts/*.txt``. Rows are newest first and capped at MAX_ALERTS;
    ``id`` is ``alert-`` + a hash (never ``run-``: an alert file is not a run),
    ``at_risk`` marks a secondary (gitignored) root, ``delivered`` is False when
    the digest found a delivery contradiction and None when nothing proves
    delivery either way, and ``excerpt`` has paths and order ids scrubbed.
    ``roots_present`` maps each root label to whether any alerts directory
    exists there; a False entry (also a warning) means the count for that root
    is absence of evidence, not a quiet window. Raises ValueError for a window
    longer than 120 days. ``config_dir`` is accepted for signature parity with
    the digest view.
    """
    _window(start, end)
    warnings: list[str] = []
    rows = [_inbox_row(root, relative, record) for root, relative, record in _collect(roots, warnings)
            if start <= record.et_date <= end]
    rows.sort(key=lambda row: (row["day"], row["file"]), reverse=True)
    if len(rows) > MAX_ALERTS:
        warnings.append(f"Only the newest {MAX_ALERTS} of {len(rows)} alerts are listed.")
    rows = rows[:MAX_ALERTS]
    return _finish({
        "alerts": rows, "count": len(rows),
        # Re-checked here (not taken from _collect) so the map is complete even
        # when a root was skipped; the is_dir probes are cheap and lock-free.
        "roots_present": {root.label: any((root.path / folder).is_dir() for folder in ALERT_FOLDERS) for root in roots},
        "by_gate": dict(Counter(row["gate"] or "unclassified" for row in rows)),
        "by_slot": dict(Counter(row["slot"] or "unclassified" for row in rows)),
        "at_risk_count": sum(row["at_risk"] for row in rows),
    }, warnings)


def alert_digest_view(roots: Sequence[RootSpec], start: date, end: date, *, observed_days: Sequence[date] | None = None,
                      config_dir: Path | None = None) -> dict[str, Any]:
    """Run ``scripts.alert_digest.build_digest`` + ``render_json`` over the bounded alert walk.

    ``observed_days`` are the days with valid stream pointers (supplied by the
    runs module); None means unknown, in which case the digest judges roster
    silence from the alert files alone. The digest's own ``day_classification``
    vocabulary (``roster_silent``, ``expected``, ...) is returned verbatim under
    ``digest``; ``roster_silent`` means "no alert found", not "healthy". The
    digest reads its calendar from ``dca_config.get_config_dir()``, so a
    differing ``config_dir`` is reported as a warning rather than honoured.
    """
    from dca_config import get_config_dir
    from scripts import alert_digest

    _window(start, end)
    warnings: list[str] = []
    if config_dir is not None and Path(config_dir).resolve() != Path(get_config_dir()).resolve():
        warnings.append("The digest calendar comes from the configured DCA_CONFIG_DIR, not the console's config directory.")
    records = [record for _, _, record in _collect(roots, warnings) if start <= record.et_date <= end]
    digest = alert_digest.build_digest(
        records, window_start=start, window_end=end,
        roster_observed=set(observed_days) if observed_days is not None else None,
    )
    payload = json.loads(alert_digest.render_json(digest))
    alerts = payload.get("alerts") or []
    if len(alerts) > MAX_ALERTS:
        warnings.append(f"Only the first {MAX_ALERTS} of {len(alerts)} digest alerts are listed.")
    payload["alerts"] = [
        {**row, "subject": _text(row.get("subject")),
         "warnings": [{"code": _text(item.get("code")), "detail": _text(item.get("detail"))} for item in row.get("warnings") or []]}
        for row in alerts[:MAX_ALERTS] if isinstance(row, dict)
    ]
    payload["warnings"] = [{**item, "detail": _text(item.get("detail"))} for item in payload.get("warnings") or []]
    return _finish({"digest": payload, "observed_days_supplied": observed_days is not None}, warnings)


# --- Failed-start markers -----------------------------------------------------


def _marker_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = {key: _text(value.get(key)) for key in MARKER_FIELDS}
    # dca_alerts writes ``failure_phase``; 'after_run_artifact' is an ordinary
    # late error on a run that did persist its artifact, never a failed start.
    summary["phase"] = _text(value.get("failure_phase"))
    return summary


def _jsonl(reader: BoundedReader, relative: Path | str, limit: int, warnings: list[str]) -> list[Any]:
    source = reader.text(relative)
    if source is None:
        return []
    lines = source.splitlines()
    if len(lines) > limit:
        warnings.append(f"A JSONL artifact exceeds {limit} lines; later entries were not read.")
    parsed: list[Any] = []
    for line in lines[:limit]:
        if not line.strip():
            continue
        try:
            parsed.append(json.loads(line))
        except ValueError:
            warnings.append("A JSONL artifact has a malformed line that was skipped.")
    return parsed


def attempt_log_view(roots: Sequence[RootSpec], *, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """List failed-start markers from the attempt log for the last ``days`` days.

    The log root is ``$DCA_ATTEMPT_LOG_ROOT`` read at call time when set, else
    ``<primary root>/attempt_log`` (dca_alerts.ATTEMPT_LOG_ROOT_ENV and its
    default). ``<YYYYMMDD>/<stream>.json`` is the latest marker (kind
    ``failed_start``); ``<stream>.attempts.jsonl`` holds every attempt (kind
    ``attempt``), read to 200 lines. ``root_present`` False means no log tree
    exists, which is absence of evidence, not a clean record.
    """
    from dca_alerts import ATTEMPT_LOG_HISTORY_SUFFIX, ATTEMPT_LOG_ROOT_ENV

    warnings: list[str] = []
    override = os.environ.get(ATTEMPT_LOG_ROOT_ENV)
    if override:
        # Resolved so a symlinked operator path still confines under itself.
        log_root = RootSpec.of(Path(override).resolve(), "attempt_log")
    elif roots:
        log_root = RootSpec(roots[0].label, roots[0].path / "attempt_log", roots[0].kind)
    else:
        return _finish({"markers": [], "root_present": False}, ["No artifact root is configured."])
    reader = log_root.reader(warnings)
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    earliest = (today - timedelta(days=max(days, 0))).strftime("%Y%m%d")
    markers: list[dict[str, Any]] = []
    for day_dir in sorted(reader.entries(""), key=lambda path: path.name, reverse=True):
        if not DAY_DIR.match(day_dir.name) or day_dir.name < earliest:
            continue
        for entry in reader.entries(day_dir):
            if entry.name.endswith(ATTEMPT_LOG_HISTORY_SUFFIX):
                stream, kind = entry.name[: -len(ATTEMPT_LOG_HISTORY_SUFFIX)], "attempt"
                values = _jsonl(reader, entry, MAX_LINES, warnings)
            elif entry.suffix == ".json":
                stream, kind, values = entry.stem, "failed_start", [reader.json(entry)]
            else:
                continue
            markers.extend({"day": f"{day_dir.name[:4]}-{day_dir.name[4:6]}-{day_dir.name[6:]}", "stream": _text(stream, 40),
                            "kind": kind, "summary": _marker_summary(value), "root_label": log_root.label}
                           for value in values if value)
        if len(markers) >= MAX_ROWS:
            warnings.append(f"Only the newest {MAX_ROWS} attempt-log markers are listed.")
            break
    return _finish({"markers": markers[:MAX_ROWS], "root_present": log_root.path.is_dir()}, warnings)


# --- Model usage audit log ----------------------------------------------------


def _usage_entry(root: RootSpec, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    entry = {key: _text(value.get(key)) for key in USAGE_FIELDS}
    artifact = value.get("artifact_path")
    # The writer's own ``run_id`` string is never forwarded, hashed or not: a
    # hash of it could not join the runs index (different preimage) while
    # looking exactly like an id that can. The run directory the writer
    # recorded IS the runs module's preimage, so that is what is hashed; None
    # means the directory lies outside every observed root (or was not logged).
    entry["run_id"] = _run_id_for(root, artifact)
    # The writer records an absolute artifact directory; the basename is the
    # run's own slot-stamped name and is all the page needs.
    entry["artifact"] = _text(Path(artifact).name, 120) if isinstance(artifact, str) and artifact else None
    entry["root_label"] = root.label
    return entry


def model_usage_view(roots: Sequence[RootSpec], month: str) -> dict[str, Any]:
    """Read ``<root>/model_usage/YYYYMM/model_usage.jsonl`` for one ``YYYY-MM`` month.

    Entries carry the schema's routing fields; ``run_id`` is the runs-index id
    of the logged ``artifact_path`` (``dca_console_runs.run_identity`` over the
    directory relative to its root, so it resolves on the runs page and
    GET /api/alerts/drift) or None when that directory is outside every root;
    ``artifact_path`` itself is reduced to a basename. The list is capped at
    1,000 across roots. Raises ValueError for a month outside ``^\\d{4}-\\d{2}$``.
    """
    if not MONTH.match(month):
        raise ValueError("month must be YYYY-MM")
    warnings: list[str] = []
    entries: list[dict[str, Any]] = []
    for root in roots:
        reader = root.reader(warnings)
        relative = Path("model_usage") / month.replace("-", "") / "model_usage.jsonl"
        for value in _jsonl(reader, relative, MAX_USAGE_ENTRIES, warnings):
            entry = _usage_entry(root, value)
            if entry is None:
                warnings.append("A model-usage entry has an unsupported shape and was skipped.")
            elif len(entries) < MAX_USAGE_ENTRIES:
                entries.append(entry)
            else:
                warnings.append(f"Only the first {MAX_USAGE_ENTRIES} model-usage entries are listed.")
                break
    return _finish({"entries": entries, "month": month}, warnings)


# --- Delivery channels --------------------------------------------------------


def delivery_channels(config_dir: Path) -> dict[str, Any]:
    """Report which alert channels are configured, by env var NAME and config flag only.

    ``source='env'`` rows check that the variable dca_alerts reads is set
    (non-empty) at call time: the two Slack webhooks and the heartbeat URL,
    shared or per stream. ``dca_alerts.heartbeat_url`` is deliberately NOT
    called: its safe-destination check resolves the hostname, which is a
    network call. ``source='config'`` rows come from
    ``config/notifications.json``. An unconfigured channel is a silent no-op:
    the executors skip it without error, so nothing is delivered there.
    """
    from dca_alerts import FAILURE_WEBHOOK_ENV, FALLBACK_WEBHOOK_ENV, HEARTBEAT_URL_ENV_PREFIX

    warnings: list[str] = []
    names = (FAILURE_WEBHOOK_ENV, FALLBACK_WEBHOOK_ENV, HEARTBEAT_URL_ENV_PREFIX)
    channels: list[dict[str, Any]] = [{"name": name, "configured": bool(os.environ.get(name)), "source": "env"} for name in names]
    channels.extend({"name": f"heartbeat:{stream}", "source": "env", "configured": bool(
        os.environ.get(f"{HEARTBEAT_URL_ENV_PREFIX}_{stream.upper()}") or os.environ.get(HEARTBEAT_URL_ENV_PREFIX))}
        for stream in STREAM_ARTIFACT_DIRS)
    document = BoundedReader(Path(config_dir).resolve(), warnings).json("notifications.json")
    if not document:
        warnings.append("config/notifications.json is missing or malformed; configured channels are unknown.")
    configured: dict[str, Any] = {}
    routing: dict[str, Any] = {}
    if isinstance(document, dict):
        raw_channels = document.get("channels")
        if isinstance(raw_channels, dict):
            configured = raw_channels
        raw_routing = document.get("routing")
        if isinstance(raw_routing, dict):
            routing = raw_routing
    channels.extend({"name": _text(name, 60), "configured": isinstance(value, dict) and value.get("enabled") is True,
                     "source": "config"} for name, value in list(configured.items())[:MAX_ROWS])
    for row in channels:
        row["status"] = "configured" if row["configured"] else "silent_no_op"
    return _finish({
        "channels": channels,
        "routing": {_text(key, 60): [_text(item, 60) for item in value[:20]] if isinstance(value, list) else None
                    for key, value in list(routing.items())[:MAX_ROWS]},
    }, warnings)


# --- Drift ------------------------------------------------------------------


def drift_summary(run_dirs: Sequence[tuple[str, Path]]) -> dict[str, Any]:
    """Run ``scripts.drift_detection.detect_drift`` over caller-supplied ``(id, path)`` pairs.

    The runs module mints the ids; this never posts (``send_slack_alert`` is
    not imported). ``drifted`` is None when the run directory is missing or the
    detector raised, and at most 50 runs are examined.
    """
    from scripts.drift_detection import detect_drift

    warnings: list[str] = []
    if len(run_dirs) > MAX_DRIFT_RUNS:
        warnings.append(f"Only the first {MAX_DRIFT_RUNS} runs were checked for drift.")
    rows = []
    for label, run_dir in list(run_dirs)[:MAX_DRIFT_RUNS]:
        row: dict[str, Any] = {"label": _text(label, 40), "drifted": None, "causes": [], "symbols_affected": None,
                               "recommended_action": None}
        try:
            report = detect_drift(Path(run_dir)) if Path(run_dir).is_dir() else None
        # csv.Error is its own Exception subclass, not a ValueError: the
        # detector's load_run_artifacts reads submitted_orders / buy_candidates
        # unguarded, and one field over csv's 128 KiB field limit must
        # degrade that one run to ``drifted: None``, not fail the whole batch
        # (adversarial review of this module).
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, csv.Error):
            report = None
            warnings.append("The drift detector could not read a run; its drift status is unavailable.")
        if report is not None:
            causes = {report.run_suspected_cause, *(item.suspected_cause for item in report.affected_symbols)}
            row.update(drifted=report.drift_detected, symbols_affected=len(report.affected_symbols),
                       causes=sorted(c for cause in causes if isinstance(cause, str) and cause for c in (_text(cause, 80),) if c is not None),
                       recommended_action=_text(report.recommended_action))
        rows.append(row)
    return _finish({"runs": rows}, warnings)


# --- Self-heal ----------------------------------------------------------------


def _cron_lines(repo: Path, name: str, warnings: list[str]) -> dict[str, Any]:
    reader = BoundedReader(Path(repo).resolve(), warnings, max_bytes=256 * 1024)
    source = reader.text(Path(".github/workflows") / name)
    lines = source.splitlines()[:400] if source is not None else []
    return {"file": name, "present": source is not None,
            "cron": [match[1] for match in map(CRON_LINE.match, lines) if match][:20]}


def self_heal_view(repo: Path) -> dict[str, Any]:
    """Describe the self-heal loop: its strict config, reason vocabulary and workflow cadence.

    ``config`` is ``selfheal.config.load`` as a plain dict, or None with a
    warning when the loader raises ``ConfigError`` (the message is kept, with
    the checkout path replaced by ``<repo>``). ``reasons`` are the sorted
    ``selfheal.reasons`` constants, verbatim, so the page can bind badges to
    them. Workflow cron lines come from a bounded line scan, not a YAML parser.
    """
    from selfheal import reasons
    from selfheal.config import ConfigError, load

    warnings: list[str] = []
    repo = Path(repo).resolve()
    config: dict[str, Any] | None = None
    try:
        config = asdict(load(repo / "config" / "self_heal.json"))
    except ConfigError as exc:
        warnings.append("Self-heal config rejected: " + (_text(str(exc).replace(str(repo), "<repo>"), 300) or "unknown"))
    return _finish({
        "config": config, "reasons": sorted(reasons.ALL_REASONS), "refusal_reasons": sorted(reasons.REFUSAL_REASONS),
        "workflows": [_cron_lines(repo, name, warnings) for name in ("self-heal.yml", "compute-schedule.yml")],
    }, warnings)


# --- Reliability engines ------------------------------------------------------


def _files_with_suffix(reader: BoundedReader, folders: Sequence[str], suffix: str, skip: Sequence[str] = ()) -> int:
    count = 0
    for folder in folders:
        for entry in reader.entries(folder, limit=MAX_ROWS):
            if entry.suffix == suffix and entry.name not in skip and (reader.root / entry).is_file():
                count += 1
    return count


def _correlated_states(roots: Sequence[RootSpec], warnings: list[str]) -> int | None:
    from dca_risk import find_run_state_files

    count = 0
    seen_any = False
    for root in roots:
        reader = root.reader(warnings)
        try:
            paths = find_run_state_files(root.path)
        except OSError:
            warnings.append("An artifact root could not be scanned for run states.")
            continue
        if len(paths) > MAX_STATE_FILES:
            warnings.append(f"Only the first {MAX_STATE_FILES} run-state files were checked for correlation ids.")
        for path in paths[:MAX_STATE_FILES]:
            seen_any = True
            if path.is_relative_to(root.path) and isinstance(reader.json(path.relative_to(root.path)).get("correlation_id"), str):
                count += 1
    return count if seen_any else None


def reliability_engines(roots: Sequence[RootSpec]) -> dict[str, Any]:
    """Say which reliability engines have left data under any root, with a legend each.

    ``has_data`` is True/False from a bounded scan and None where an engine is
    constants-only (the run-state machine). The run ledger has no fixed file
    name, so any ``*.jsonl`` at a root's top level or under ``run_ledger/`` or
    ``ledger/`` counts; leases are ``<day>__<stream>__<window>.lease`` files.
    """
    from scripts import run_state_machine as rsm

    warnings: list[str] = []
    ledgers = leases = 0
    for root in roots:
        reader = root.reader(warnings)
        ledgers += _files_with_suffix(reader, ("", "run_ledger", "ledger"), ".jsonl", skip=("model_usage.jsonl",))
        leases += _files_with_suffix(reader, ("", "scheduler_leases", "leases"), ".lease")
    correlated = _correlated_states(roots, warnings)
    absent = "library only, no data on this machine"
    engines = [
        {"name": "run_ledger", "has_data": ledgers > 0, "count": ledgers,
         "legend": "append-only JSONL run events (scripts/run_ledger.py)" if ledgers else absent},
        {"name": "scheduler_lease", "has_data": leases > 0, "count": leases,
         "legend": "per-window O_EXCL lease files <day>__<stream>__<window>.lease" if leases else absent},
        {"name": "run_state_machine", "has_data": None, "count": None,
         "legend": "happy path " + " > ".join(rsm.HAPPY_PATH) + "; terminal " + ", ".join(sorted(rsm.TERMINAL_STATES))},
        {"name": "correlation", "has_data": None if correlated is None else correlated > 0, "count": correlated,
         "legend": "run_state.json files carrying a correlation_id" if correlated else absent},
    ]
    return _finish({"engines": engines}, warnings)


# --- Composition --------------------------------------------------------------


def build_alerts_snapshot(roots: Sequence[RootSpec], start: date, end: date, *, config_dir: Path, repo: Path,
                          observed_days: Sequence[date] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Compose the Alerts & health page: inbox, digest, attempt log, channels, self-heal, engines.

    Drift is deliberately absent (it needs run directories from the runs
    module). Each section keeps its own ``warnings``; the top-level list is
    their deduplicated union.
    """
    sections = {
        "inbox": alert_inbox(roots, start, end, config_dir=config_dir),
        "digest": alert_digest_view(roots, start, end, observed_days=observed_days, config_dir=config_dir),
        "attempt_log": attempt_log_view(roots, now=now),
        "channels": delivery_channels(config_dir),
        "self_heal": self_heal_view(repo),
        "engines": reliability_engines(roots),
    }
    warnings = [warning for section in sections.values() for warning in section["warnings"]]
    return _finish({"window": {"from": start.isoformat(), "to": end.isoformat()}, **sections}, warnings)
