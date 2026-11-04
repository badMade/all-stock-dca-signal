"""Read-only daily views of local DCA evidence and the configured task timetable.

The timetable describes repository configuration, not a connected scheduler.
Confirmed purchases are obtained from the existing purchase-history reader; this
module never estimates fills from requested order amounts or contacts a broker.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dca_cost_tracker import aggregate_usage
from dca_risk import find_run_state_files, run_timestamp_et
from scripts.purchase_history import PurchaseRecord, collect_purchase_records
from scripts.scheduled_artifacts import submitted_order_rows

ET = ZoneInfo("America/New_York")
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _load_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _day_of(state: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(state.get("trading_day", ""))
    except (ValueError, TypeError):
        stamp = run_timestamp_et(state)
        return stamp.date() if stamp else None


def _mode(state: dict[str, Any]) -> str:
    if state.get("demo") is True or str(state.get("mode", "")).lower() == "demo":
        return "demo"
    if state.get("dry_run") is True:
        return "dry_run"
    if state.get("dry_run") is False:
        return "live"
    return "unknown"


def _stream(state: dict[str, Any]) -> str:
    classification = _mapping(state.get("run_classification"))
    source = str(classification.get("stream") or state.get("stream") or state.get("mode") or "").lower()
    for name in ("market_open", "market-open", "preflight", "watchlist", "morning", "midday", "intraday", "afternoon"):
        if name in source:
            return name.replace("_", "-")
    return "unknown"


def _status(state: dict[str, Any]) -> str:
    raw = str(state.get("status") or state.get("order_submit_status") or "unknown").lower()
    if any(token in raw for token in ("block", "abort", "fail", "error")):
        return "blocked" if "block" in raw or "abort" in raw else "failed"
    if raw in {"completed", "complete", "success", "dry_run", "no_orders", "no_qualifying_signals"}:
        return "completed"
    if raw in {"submitted", "pending", "running", "skipped", "partial", "filled", "cancelled", "not_submitted"}:
        return raw
    return "unknown"


def _sum_fills(records: list[PurchaseRecord]) -> float | None:
    if not records or any(_number(record.amount_usd) is None for record in records):
        return None
    total = _number(sum(record.amount_usd for record in records if record.amount_usd is not None))
    return round(total, 2) if total is not None else None


def _run_row(path: Path, root: Path, state: dict[str, Any], records: list[PurchaseRecord]) -> dict[str, Any]:
    counts = _mapping(state.get("counts"))
    candidates = _count(counts.get("buy_candidates"))
    if candidates is None and isinstance(state.get("buy_candidates"), list):
        candidates = len(state["buy_candidates"])
    submitted = _count(state.get("orders_submitted_count", counts.get("submitted")))
    outcomes = state.get("broker_outcomes")
    if submitted is None and isinstance(outcomes, dict):
        valid_outcomes = {key: value for key, value in outcomes.items()
                          if isinstance(value, dict) and isinstance(value.get("status"), (str, type(None)))}
        submitted = len(submitted_order_rows(valid_outcomes)) if len(valid_outcomes) == len(outcomes) else None
    classification = _mapping(state.get("run_classification"))
    origin = classification.get("run_origin")
    if classification.get("manual_run") is True:
        origin = "manual"
    if origin not in {"scheduled", "manual", "scheduled_recovery"}:
        origin = "unknown"
    stamp = run_timestamp_et(state)
    # Opaque presentation identities avoid returning arbitrary artifact strings,
    # broker identifiers, or absolute paths in a browser response.
    identity = str(path.relative_to(root)) if path.is_relative_to(root) else path.name
    return {
        "id": "run-" + hashlib.sha256(identity.encode()).hexdigest()[:12],
        "stream": _stream(state),
        "time": stamp.isoformat() if stamp else None,
        "status": _status(state),
        "mode": _mode(state),
        "origin": origin,
        "candidates": candidates,
        "submitted": submitted,
        "confirmed_spend_usd": _sum_fills(records) if _mode(state) not in {"dry_run", "demo"} else None,
    }


def _calendar(config_dir: Path):
    # Reuse the executor's calendar parsing and early-close slot decision.
    from dca_runner import MarketDayStatus, _calendar_dates

    calendar = _load_object(config_dir / "market_calendar.json")
    if calendar is None:
        raise ValueError("Calendar missing or malformed")
    years = calendar.get("years_covered")
    if not isinstance(years, list) or any(type(year) is not int for year in years):
        raise ValueError("Calendar coverage missing or malformed")
    holidays = _calendar_dates(calendar, "holidays_closed")
    early_closes = _calendar_dates(calendar, "early_close_1300")

    def lookup(day: date):
        if day.year not in years:
            raise ValueError("Calendar does not cover this year")
        return MarketDayStatus(day, day.weekday() >= 5 or day in holidays, day in early_closes)

    return lookup


def _cron_values(field: str, low: int, high: int) -> set[int]:
    """Interpret the simple numeric cron fields used by the checked-in manifest."""
    values: set[int] = set()
    for section in field.split(","):
        if section == "*":
            values.update(range(low, high + 1))
        elif "-" in section:
            first, last = (int(part) for part in section.split("-"))
            if first > last:
                raise ValueError("Invalid cron range")
            values.update(range(first, last + 1))
        else:
            values.add(int(section))
    if not values or min(values) < low or max(values) > high:
        raise ValueError("Invalid cron field")
    return values


_TIME_PATTERN = re.compile(r"\b(\d{1,2}):(\d{2})\s*(AM|PM)\b", re.I)

def _task_times(task: dict[str, Any]) -> list[time]:
    slots = task.get("et_slots")
    if isinstance(slots, list) and slots:
        return [time.fromisoformat(slot) for slot in slots]
    label = str(task.get("et_equivalent", ""))
    # ⚡ Bolt optimization: Use compiled regex to bypass re._cache lookup overhead
    found = _TIME_PATTERN.search(label)
    if found:
        hour, minute = int(found[1]) % 12 + (12 if found[3].upper() == "PM" else 0), int(found[2])
        return [time(hour, minute)]
    return []


def _one_time(task: dict[str, Any]) -> datetime | None:
    raw = task.get("fires_at_et")
    if not isinstance(raw, str):
        return None

    # ⚡ Bolt optimization: Use fromisoformat for strict ISO dates
    try:
        if len(raw) == 19 and raw[10] == "T":
            return datetime.fromisoformat(raw).replace(tzinfo=ET)
        elif len(raw) == 16 and raw[10] == " ":
            return datetime.fromisoformat(raw).replace(tzinfo=ET)
    except ValueError:
        pass

    if len(raw) == 22 and raw.endswith(" ET") and raw[10] == " " and raw[16] == " " and raw[19] == " ":
        try:
            h, m = int(raw[11:13]), int(raw[14:16])
            ampm = raw[17:19]
            if 1 <= h <= 12 and ampm in ("AM", "PM"):
                if ampm == "PM" and h != 12:
                    h += 12
                elif ampm == "AM" and h == 12:
                    h = 0
                iso_str = f"{raw[:10]}T{h:02d}:{m:02d}:00"
                return datetime.fromisoformat(iso_str).replace(tzinfo=ET)
        except ValueError:
            pass

    for pattern in ("%Y-%m-%d %I:%M %p ET", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=ET)
        except ValueError:
            continue
    return None


def _eligible_day(day: date, task: dict[str, Any], calendar) -> bool:
    parts = str(task.get("cron_utc", "")).split()
    if len(parts) != 5:
        return False
    _, _, dom, month, dow = parts
    if day.month not in _cron_values(month, 1, 12):
        return False
    if day.day not in _cron_values(dom, 1, 31) or (day.weekday() + 1) % 7 not in _cron_values(dow, 0, 7):
        return False
    label = str(task.get("et_equivalent", "")).lower()
    if "first weekday" in label:
        first = day.replace(day=1)
        while first.weekday() >= 5:
            first += timedelta(days=1)
        return first == day
    if "final eligible market day" in label:
        if calendar(day).is_closed:
            return False
        candidate = day + timedelta(days=1)
        while candidate.month == day.month:
            if not calendar(candidate).is_closed:
                return False
            candidate += timedelta(days=1)
    return not task.get("trades") or not calendar(day).is_closed


def _slot_closed(day: date, clock: time, task: dict[str, Any], calendar) -> bool:
    if not task.get("trades"):
        return False
    from dca_runner import SLOTS

    status = calendar(day)
    slot = SLOTS.get(clock.strftime("%H%M"))
    return status.slot_should_end_silently(slot) if slot else status.is_closed


def _safe_label(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    # Names are the only manifest text exposed; do not forward notes/commands.
    return re.sub(r"\b\d{9,}\b", "[redacted]", value[:180]).replace("\n", " ")


def _schedule(day: date, now: datetime, config_dir: Path, warnings: list[str]) -> list[dict[str, Any]]:
    manifest = _load_object(config_dir / "scheduled_tasks.json")
    if not manifest or not isinstance(manifest.get("tasks"), list):
        warnings.append("The local schedule manifest is missing or malformed.")
        return []
    try:
        calendar = _calendar(config_dir)
        calendar(day)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        calendar = None
        warnings.append("The market calendar is unavailable or does not cover the selected year; timing is unverified.")
    rows: list[dict[str, Any]] = []
    for index, task in enumerate(manifest["tasks"]):
        if not isinstance(task, dict):
            warnings.append("An invalid schedule entry was skipped.")
            continue
        try:
            clocks = _task_times(task)
            one_time = _one_time(task)
            if one_time:
                clocks = [one_time.timetz().replace(tzinfo=None)]
            for clock in clocks or [None]:
                row: dict[str, Any] = {
                    "id": _safe_label(task.get("id"), f"task-{index}"),
                    "name": _safe_label(task.get("name"), "Unnamed task"),
                    "time": clock.strftime("%H:%M") if clock else "unspecified",
                    "timezone": "America/New_York",
                    "status": "timing_unverified",
                    "next_at": None,
                    "type": task.get("type") if task.get("type") in {"trading", "analytics", "monitoring", "reminder"}
                    else "other",
                }
                if task.get("status") == "disabled" or task.get("enabled") is False:
                    row["status"] = "disabled"
                elif task.get("scheduler_registered") is False:
                    row["status"] = "not_registered"
                elif one_time:
                    row["status"] = "elapsed_one_time" if one_time <= now else "planned"
                    row["next_at"] = one_time.isoformat() if one_time > now else None
                elif clock is not None and calendar is not None:
                    eligible = _eligible_day(day, task, calendar)
                    closed = _slot_closed(day, clock, task, calendar)
                    stamp = datetime.combine(day, clock, tzinfo=ET)
                    row["status"] = (
                        "market_closed" if closed else "not_scheduled_today" if not eligible
                        else "elapsed_unverified" if stamp <= now else "planned"
                    )
                    for offset in range(63):
                        candidate = now.date() + timedelta(days=offset)
                        next_stamp = datetime.combine(candidate, clock, tzinfo=ET)
                        if next_stamp > now and _eligible_day(candidate, task, calendar):
                            if not _slot_closed(candidate, clock, task, calendar):
                                row["next_at"] = next_stamp.isoformat()
                                break
                rows.append(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            rows.append({
                "id": f"task-{index}", "name": _safe_label(task.get("name"), "Unnamed task"),
                "time": "unspecified", "timezone": "America/New_York", "status": "timing_unverified",
                "next_at": None, "type": "other",
            })
            warnings.append("A task has timing information that could not be verified.")
    return sorted(rows, key=lambda row: (row["time"], row["name"]))


def build_schedule_rows(day: date, now: datetime, config_dir: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Public entry for the Schedule page (GUI expansion, Phase 2).

    Returns :func:`_schedule` unchanged: one row per manifest task (or per ET
    slot of a multi-slot task) with the same ``timing_unverified`` /
    ``not_scheduled_today`` / ``elapsed_unverified`` vocabulary the daily
    snapshot shows, so the two pages can never disagree about a task's timing.
    ``warnings`` is appended to in place, exactly as the private function does.
    """
    return _schedule(day, now, config_dir, warnings)


def build_daily_snapshot(
    root: Path, day: date, now: datetime | None = None, config_dir: Path | None = None,
    *, excluded_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """Return a JSON-safe daily summary from a local artifact tree, without writes.

    ``confirmed_spend_usd=None`` means priced confirmed fills were not available,
    rather than zero spending. ``recent_days`` lists dates with readable states.
    ``excluded_roots`` removes subtrees such as the console's synthetic demo
    archive from the observed runs, recent dates, costs, and purchase totals.
    Schedule statuses concern documented timing only and never grant authority to
    trade or claim that an external scheduler actually ran a task.
    """
    root = Path(root).resolve()
    exclusions = tuple(Path(path).resolve() for path in excluded_roots)

    _selected_cache = {}
    def selected(path: Path) -> bool:
        if path not in _selected_cache:
            resolved = path.resolve()
            _selected_cache[path] = resolved.is_relative_to(root) and not any(resolved.is_relative_to(item) for item in exclusions)
        return _selected_cache[path]

    config_dir = Path(config_dir) if config_dir is not None else DEFAULT_CONFIG_DIR
    now = now or datetime.now(ET)
    now = now.replace(tzinfo=ET) if now.tzinfo is None else now.astimezone(ET)
    if isinstance(day, datetime):
        raise ValueError("day must be a date, not a datetime")
    warnings: list[str] = []
    states: list[tuple[Path, dict[str, Any]]] = []
    recent_days: set[str] = set()
    malformed = 0
    try:
        paths = find_run_state_files(root)
    except OSError:
        paths = []
        warnings.append("The local artifact tree could not be read.")
    for path in paths:
        if not selected(path):
            continue
        state = _load_object(path)
        if state is None:
            malformed += 1
            continue
        state_day = _day_of(state)
        if state_day is not None:
            recent_days.add(state_day.isoformat())
        if state_day == day:
            states.append((path, state))
    if malformed:
        warnings.append(f"{malformed} unreadable or malformed run-state file(s) were skipped; totals may be incomplete.")
    # Multiple state variants are snapshots of one directory. Keep the latest
    # timestamp for presentation; the purchase reader performs its own exact fill
    # deduplication across variants, streams, and recovery directories.
    by_dir: dict[Path, tuple[Path, dict[str, Any]]] = {}
    for path, state in sorted(states, key=lambda item: (str(run_timestamp_et(item[1]) or ""), item[0].name)):
        by_dir[path.parent] = (path, state)
    states = list(by_dir.values())
    records: list[PurchaseRecord] = []
    try:
        all_records, fill_warnings = collect_purchase_records(root)
        from collections import defaultdict

        # ⚡ Bolt Optimization: Deduplicate `path.parent.resolve()` syscalls
        # Multiple run states often exist in the same run directory (e.g. variants, recoveries).
        # Deduplicating the parents via a set before resolving eliminates redundant I/O overhead.
        parents = {path.parent for path, _ in states}
        resolved_state_dirs = {p: str(p.resolve()) for p in parents}
        excluded = {resolved_state_dirs[path.parent] for path, state in states if _mode(state) in {"dry_run", "demo"}}
        records_by_dir = defaultdict(list)
        records = []

        resolved_source_cache = {}
        for record in all_records:
            if record.trading_day == day.isoformat():
                source_run_dir = record.source_run_dir
                if source_run_dir not in resolved_source_cache:
                    source_dir_path = Path(source_run_dir)
                    if selected(source_dir_path):
                        resolved_source_cache[source_run_dir] = str(source_dir_path.resolve())
                    else:
                        resolved_source_cache[source_run_dir] = None

                resolved_dir = resolved_source_cache[source_run_dir]
                if resolved_dir and resolved_dir not in excluded:
                    records.append(record)
                    records_by_dir[resolved_dir].append(record)

        if fill_warnings:
            warnings.append(f"The purchase-history reader reported {len(fill_warnings)} evidence warning(s); "
                            "confirmed-fill coverage may be incomplete. Run the purchase-history CLI for details.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        warnings.append("Confirmed purchases could not be read; spend remains unverified.")
        records_by_dir = {}
        resolved_state_dirs = {}

    runs = [_run_row(path, root, state, records_by_dir.get(resolved_state_dirs.get(path.parent) or str(path.parent.resolve()), []))
            for path, state in states]
    runs.sort(key=lambda row: row["time"] or "", reverse=True)
    spend = _sum_fills(records)
    summary = [f"{len(runs)} recorded run(s) for {day.isoformat()}." if runs else
               "No readable run artifacts were found for this day; activity is unverified."]
    completed = sum(row["status"] == "completed" for row in runs)
    blocked = sum(row["status"] in {"blocked", "failed"} for row in runs)
    if runs:
        summary.append(f"{completed} completed run(s); {blocked} blocked or failed run(s).")
    simulated = sum(row["mode"] in {"demo", "dry_run"} for row in runs)
    if simulated:
        summary.append(f"{simulated} run(s) are simulations and do not establish live purchases.")
    summary.append(f"Confirmed priced fills total ${spend:,.2f}." if spend is not None else
                   "Confirmed spending is unavailable; requested or simulated amounts are not counted as fills.")
    if not root.exists():
        warnings.append("The selected artifact root does not exist yet.")
    summary.append("Task timing comes from the local manifest; live scheduler execution is unverified.")
    schedule = _schedule(day, now, config_dir, warnings)
    # The existing tracker returns activity proxy units, never dollar costs. Its
    # numeric reader accepts nonfinite JSON values, so isolate and sanitize only
    # the known numeric inputs before using that public aggregation API.
    usage_states = []
    for _, state in states:
        usage = state.get("usage_proxy")
        if isinstance(usage, dict):
            safe_usage = {key: _number(value) or 0 for key, value in usage.items()}
            safe_usage["manual_recovery_required"] = usage.get("manual_recovery_required") is True
            usage_states.append({"usage_proxy": safe_usage})
    costs = aggregate_usage(usage_states, month_label=day.isoformat()) if usage_states else None
    return {
        "date": day.isoformat(), "generated_at": now.isoformat(), "summary": summary,
        "metrics": {"run_count": len(runs), "completed_count": completed, "blocked_count": blocked,
                    "confirmed_spend_usd": spend, "confirmed_fill_count": len(records),
                    "confirmed_spend_coverage": "priced_confirmed_fills" if spend is not None else "unavailable"},
        "runs": runs, "schedule": schedule, "warnings": list(dict.fromkeys(warnings)),
        "costs": costs, "recent_days": sorted(recent_days, reverse=True)[:90],
    }
