#!/usr/bin/env python3
"""Start a Perplexity backup ONLY when a Claude slot actually failed (credit plan step 6, 2026-10-31).

Before this, six Perplexity tasks fired on a schedule every trading day and mostly stood down, about 2,500 credits a
day (docs/cost_tracking.md). The operator's goal is zero backup credits on a normal day. This module replaces the
schedule with a trigger:

* ``run`` (the Mac, launchd, every few minutes in market hours): about 15 minutes after each of the seven slots it
  asks the local heartbeat (``scripts/local_heartbeat.status``) whether the slot may need a backup. Only when it does
  (``backup_may_act``: the slot is off or errored, with no confirmed backup buy and no live claim) does it email
  Perplexity Computer one message: that slot's filled backup prompt, with ``backup_planner.pyz`` attached. It then
  marks the slot ``backup_requested`` locally and in the public slot-status document, and never sends for it again.
* ``cloud`` (GitHub Actions, for days the Mac is down): 20 minutes after each slot it reads the public slot-status
  document and sends the same email only when today's document has no entry for the slot (the Mac never started it)
  or there is no document for today at all. Any entry means the Mac is alive and owns the decision.

No LLM runs here and no Perplexity credit is spent unless an email goes out. Every safety check stays inside the
backup procedure the email starts (broker evidence, caps, claim/confirm), so a wrong trigger costs credits but cannot
add a buy. ``config/backup_dispatch.json`` starts in ``shadow`` mode: it reports what it would send and sends nothing.

Secrets (SMTP password) come from the environment or the gitignored ``.env`` (``dca_env``) and are never printed:
output names an exception type at most. Every command prints one compact JSON line.

    python3 -m scripts.backup_dispatch run   [--now T] [--dry-run]   # Mac
    python3 -m scripts.backup_dispatch cloud [--now T] [--dry-run]   # GitHub Actions
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import dca_config
import dca_net_utils
import dca_order_plan
import dca_paths
from dca_fileio import atomic_write_text
from scripts import slot_status
from scripts.backup_procedure import BACKUPS, Backup

ET = ZoneInfo("America/New_York")
PERPLEXITY_ADDRESS = "computer@perplexity.com"
MODES = ("shadow", "live")
CLOUD_CHECK_AFTER_MINUTES = 20  # five minutes after the Mac's own check, so the Mac decides first when it is up
# A running slot (2026-11-05): scripts/slot_status_watch.py records in_progress with activity_at, the newest file in
# the slot's run folder. The 9:35 run on 2026-11-05 wrote a file every 30-40 s from 09:36 to 09:55, so 10 minutes of
# silence means it stalled; the Mac then sends. The cloud watcher waits longer, because a stale activity_at there also
# means the Mac stopped publishing (asleep or down) and nobody else will send.
RUNNING_FRESH_MINUTES = 10
CLOUD_STALE_MINUTES = 20
REPO = Path(__file__).resolve().parents[1]
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SLOT = re.compile(r"\d{4}")
_MAX_DOCUMENT_BYTES = 256 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so a remote signal host cannot redirect to internal targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# -- slots, tasks and windows -----------------------------------------------------------------------------------------


def task_for_slot(slot: str) -> Backup:
    """The backup task whose prompt backs up ``slot`` (the two-fire task covers 1330 and 1430)."""
    for backup in BACKUPS:
        if slot in backup.slots:
            return backup
    raise ValueError(f"unknown slot {slot!r}")


def _slot_time(slot: str) -> time:
    if not _SLOT.fullmatch(slot):
        raise ValueError(f"unknown slot {slot!r}")
    return time(int(slot[:2]), int(slot[2:]))


def _hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


# 2026-11-09: the final slot's backup fires at 15:40 (docs/schedule_manifest.md) and has only until 16:00, so its check
# window opens 5 minutes earlier than the 15 (Mac) or 20 (cloud) minutes every other slot waits. Both checkers shift
# together, which keeps the cloud watcher five minutes behind the Mac as CLOUD_CHECK_AFTER_MINUTES documents.
EARLIER_CHECK_MINUTES = {"1530": 5}


def check_window(slot: str, after_minutes: int = 15) -> tuple[str, str]:
    """``(start, end)`` ET: from ``after_minutes`` past the slot up to (not including) its latest placement time.

    The final slot starts ``EARLIER_CHECK_MINUTES`` earlier than that, so its backup keeps time before the close.
    """
    task_for_slot(slot)
    after_minutes -= EARLIER_CHECK_MINUTES.get(slot, 0)
    start = datetime.combine(datetime(2000, 1, 1), _slot_time(slot)) + timedelta(minutes=after_minutes)
    return _hhmm(start.time()), dca_order_plan.LATEST_PLACEMENT_ET[slot]


def _et(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the clock value has no timezone; refusing to guess one")
    return now.astimezone(ET)


def _closed(day: str, slot: str, calendar: Mapping[str, Any]) -> bool:
    """A weekend, a full holiday, or a slot at or after the early close (the backup's own gates use the same rule)."""
    if slot_status.market_for_day(day, calendar) == "closed":
        return True
    if day in set(calendar.get("early_close_1300", [])):
        early = time(*map(int, str(calendar.get("early_close_time_et", "13:00")).split(":")))
        return _slot_time(slot) >= early
    return False


def _window_reason(slot: str, now_et: datetime, after_minutes: int) -> str | None:
    start, end = check_window(slot, after_minutes)
    clock = _hhmm(now_et.time())
    if clock < start:
        return "too_early"
    if clock >= end:
        return "too_late"
    return None


def due_slot(now: datetime, after_minutes: int) -> str | None:
    """The slot whose check window contains ``now``, if any (the windows do not overlap)."""
    now_et = _et(now)
    for backup in BACKUPS:
        for slot in backup.slots:
            if _window_reason(slot, now_et, after_minutes) is None:
                return slot
    return None


# -- decisions --------------------------------------------------------------------------------------------------------


def decide(*, day: str, slot: str, now: datetime, calendar: Mapping[str, Any], probe: Callable[[str, str], dict],
           already_sent: bool, after_minutes: int = 15) -> dict[str, Any]:
    """The Mac's decision for one slot. Cheap checks first, so a closed market or a wrong time never probes."""
    now_et = _et(now)
    task_for_slot(slot)
    if _closed(day, slot, calendar):
        return {"action": "skip", "reason": "market_closed"}
    reason = _window_reason(slot, now_et, after_minutes)
    if reason:
        return {"action": "skip", "reason": reason}
    if already_sent:
        return {"action": "skip", "reason": "already_sent"}
    entry = _local_entry(day, slot)
    if entry.get("status") == "succeeded":
        return {"action": "skip", "reason": "primary_succeeded"}
    idle = _idle_minutes(entry, now_et)
    if idle is not None:
        if idle <= RUNNING_FRESH_MINUTES:
            return {"action": "skip", "reason": "primary_running"}
        return {"action": "send", "reason": "primary_stalled"}
    result = probe(day, slot)
    local_status = str(result.get("local_status"))
    if result.get("backup_may_act") is True:
        return {"action": "send", "reason": local_status}
    if local_status == "unknown":
        # The heartbeat could not be read: never trigger on that (the old backups stood down on it too), but say so.
        return {"action": "skip", "reason": "unknown", "notify": True}
    return {"action": "skip", "reason": local_status}


def _local_entry(day: str, slot: str) -> dict[str, Any]:
    """This slot's entry in today's local slot-status document, or {} (2026-11-03).

    No live prompt calls local_heartbeat start/finish, so the heartbeat probe alone sees every slot as never started.
    The Gate 9 finalizer records each accepted run here, and scripts/slot_status_watch.py marks a running one
    in_progress. An unreadable, foreign or older document gives {}, so the caller falls through to the probe.
    """
    try:
        document = json.loads(
            slot_status.state_path().read_text(encoding="utf-8"),
            object_pairs_hook=dca_config._reject_duplicate_keys,
        )
    except (OSError, ValueError):
        return {}
    if not (isinstance(document, dict) and document.get("schema") == slot_status.SCHEMA
            and document.get("trading_day") == day and isinstance(document.get("slots"), dict)):
        return {}
    entry = document["slots"].get(slot)
    return entry if isinstance(entry, dict) else {}


def _idle_minutes(entry: Mapping[str, Any], now_et: datetime) -> float | None:
    """Minutes since an in_progress slot last wrote a file, or None when there is no usable activity_at.

    None keeps the old behaviour (the heartbeat probe): an entry without activity_at is the old local_heartbeat
    start shape, and a malformed or timezone-less one is not trusted either way.
    """
    if entry.get("status") != "in_progress":
        return None
    try:
        activity = datetime.fromisoformat(str(entry.get("activity_at")))
    except ValueError:
        return None
    if activity.tzinfo is None or activity.utcoffset() is None:
        return None
    return (now_et - activity).total_seconds() / 60


def cloud_decide(document: Any, *, day: str, slot: str, now: datetime, calendar: Mapping[str, Any]) -> dict[str, Any]:
    """The GitHub watcher's decision: send only when the Mac shows no sign of having handled the slot today."""
    now_et = _et(now)
    task_for_slot(slot)
    if _closed(day, slot, calendar):
        return {"action": "skip", "reason": "market_closed"}
    reason = _window_reason(slot, now_et, CLOUD_CHECK_AFTER_MINUTES)
    if reason:
        return {"action": "skip", "reason": reason}
    today = (isinstance(document, dict) and document.get("schema") == slot_status.SCHEMA
             and document.get("trading_day") == day and isinstance(document.get("slots"), dict))
    if not today:
        return {"action": "send", "reason": "no_document_today"}
    if document.get("market") == "closed":
        return {"action": "skip", "reason": "market_closed"}
    entry = document["slots"].get(slot)
    if isinstance(entry, dict) and entry.get("status"):
        idle = _idle_minutes(entry, now_et)
        if idle is not None and idle > CLOUD_STALE_MINUTES:
            return {"action": "send", "reason": "mac_stalled"}  # the Mac stopped publishing mid-run
        return {"action": "skip", "reason": f"mac_reported_{entry['status']}"}
    return {"action": "send", "reason": "slot_never_started"}


# -- the message --------------------------------------------------------------------------------------------------------


def build_message(slot: str, *, sender_address: str, prompt_text: str, planner_bytes: bytes) -> EmailMessage:
    """One email to Perplexity Computer: the slot's filled backup prompt, with the stamped planner attached."""
    backup = task_for_slot(slot)
    if not _EMAIL.fullmatch(sender_address or ""):
        raise ValueError("sender address missing or malformed")
    if "${" in prompt_text:
        raise ValueError("the prompt still has an unfilled ${...} placeholder")
    message = EmailMessage()
    message["To"] = PERPLEXITY_ADDRESS
    message["From"] = sender_address
    message["Subject"] = f"DCA backup needed: slot {slot} (task {backup.task_id})"
    message.set_content(prompt_text)
    message.add_attachment(planner_bytes, maintype="application", subtype="zip", filename="backup_planner.pyz")
    return message


def _prompt_text(backup: Backup, environ: Mapping[str, str]) -> str:
    """The filled prompt: from the operator's private filled copies when set, else filled here from the environment."""
    filled_dir = environ.get("DCA_FILLED_PROMPTS_DIR")
    if filled_dir:
        return (Path(filled_dir) / backup.file).read_text(encoding="utf-8")
    from scripts import fill_perplexity_backup_prompts as fill  # lazy: only the fallback needs it

    last4, email = fill.resolve_values(dict(environ))
    pairs = [pair for pair in environ.get("DCA_PPLX_STATE_FOLDERS", "").split(",") if pair]
    text = (REPO / "claude" / "dca-executor-prompts" / "perplexity" / backup.file).read_text(encoding="utf-8")
    return fill.render(text, name=backup.file, last4=last4, email=email, state_root="cron_tracking",
                       folders=fill.parse_folders(pairs) or None)


def _message_for(slot: str) -> EmailMessage:
    environ = os.environ
    return build_message(slot, sender_address=environ.get("DCA_PERPLEXITY_SENDER", ""),
                         prompt_text=_prompt_text(task_for_slot(slot), environ),
                         planner_bytes=(REPO / "backup_planner.pyz").read_bytes())


def _smtp_send(message: EmailMessage) -> None:
    host, user, password = (os.environ.get(name) for name in ("DCA_SMTP_HOST", "DCA_SMTP_USER", "DCA_SMTP_PASSWORD"))
    if not (host and user and password):
        raise ValueError("smtp_not_configured")
    with smtplib.SMTP_SSL(host, 465, timeout=30) as server:
        server.login(user, password)
        server.send_message(message)


# -- dispatching (the Mac) -------------------------------------------------------------------------------------------


def _marker(day: str, slot: str) -> Path:
    return Path(dca_paths.cron_tracking_root()) / "backup_dispatch" / f"{day}_{slot}.sent"


def dispatch(*, day: str, slot: str, now: datetime, mode: str, calendar: Mapping[str, Any],
             probe: Callable[[str, str], dict], send: Callable[[EmailMessage], None],
             message_for: Callable[[str], EmailMessage], notify: Callable[[str, str], None],
             after_minutes: int = 15) -> dict[str, Any]:
    """Decide, then (live mode only) send once and mark the slot. Never raises for a send failure."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    marker = _marker(day, slot)
    decision = decide(day=day, slot=slot, now=now, calendar=calendar, probe=probe, already_sent=marker.exists(),
                      after_minutes=after_minutes)
    if decision["action"] == "skip":
        if decision.pop("notify", False):
            notify("DCA backup trigger", f"slot {slot}: heartbeat unreadable, no backup sent")
        return {**decision, "slot": slot}
    if mode == "shadow":
        return {"action": "would_send", "reason": decision["reason"], "slot": slot}
    try:
        send(message_for(slot))
    except Exception as exc:  # noqa: BLE001 - report the type only; a message can carry a host or a secret
        notify("DCA backup trigger failed", f"slot {slot}: {type(exc).__name__}")
        return {"action": "send_failed", "reason": decision["reason"], "slot": slot, "detail": type(exc).__name__}
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, _et(now).isoformat(timespec="seconds") + "\n")
    slot_status.update(day, slot, "backup_requested", now=now)  # best-effort; tells the cloud watcher it is handled
    return {"action": "sent", "reason": decision["reason"], "slot": slot}


# -- configuration and wiring -----------------------------------------------------------------------------------------


def load_settings(config_dir: Path | None = None) -> dict[str, Any]:
    path = (config_dir or dca_config.get_config_dir()) / "backup_dispatch.json"
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=dca_config._reject_duplicate_keys)


def _calendar() -> dict[str, Any]:
    return json.loads(
        (dca_config.get_config_dir() / "market_calendar.json").read_text(encoding="utf-8"),
        object_pairs_hook=dca_config._reject_duplicate_keys,
    )


def _probe(day: str, slot: str) -> dict:
    from scripts import local_heartbeat  # lazy: it carries the Mac's Python-version guard

    return local_heartbeat.status(day, slot)


def _notify(title: str, text: str) -> None:
    try:
        from scripts.schedule_watchdog import notify_macos

        notify_macos(title, text)
    except Exception:  # noqa: BLE001 - no notifier (not a Mac, or osascript failed) must not change the result
        pass


def _fetch_document(url: str) -> Any:
    # Security: enforce HTTPS, block private/link-local SSRF & DNS rebinding, refuse redirects,
    # cap response size, and reject duplicate JSON keys before parsing the signal payload.
    try:
        if not isinstance(url, str) or urlsplit(url).scheme.lower() != "https":
            return None
        dca_net_utils.assert_safe_destination(url)
        opener = urllib.request.build_opener(
            _NoRedirectHandler,
            dca_net_utils.PinnedHTTPHandler,
            dca_net_utils.PinnedHTTPSHandler,
            dca_net_utils.BlockedFileHandler,
            dca_net_utils.BlockedFTPHandler,
        )
        request = urllib.request.Request(url, method="GET")
        with opener.open(request, timeout=20) as response:  # noqa: S310 - HTTPS + assert_safe_destination enforced above
            raw = response.read(_MAX_DOCUMENT_BYTES + 1)
        if len(raw) > _MAX_DOCUMENT_BYTES:
            return None
        return json.loads(raw.decode("utf-8"), object_pairs_hook=dca_config._reject_duplicate_keys)
    except (OSError, ValueError, dca_net_utils.UnsafeDestinationError):
        return None


def _publish_requested(document: Any, day: str, slot: str, now: datetime) -> bool:
    """Cloud side: mark the slot backup_requested on the public branch so the next 5-minute run does not resend."""
    stamp = _et(now).isoformat(timespec="seconds")
    if not (isinstance(document, dict) and document.get("schema") == slot_status.SCHEMA
            and document.get("trading_day") == day and isinstance(document.get("slots"), dict)):
        document = {"schema": slot_status.SCHEMA, "trading_day": day, "market": "open", "updated_at": None,
                    "slots": {}}
    document["slots"][slot] = {"status": "backup_requested", "stamped_at": stamp}
    document["updated_at"] = stamp
    settings = slot_status.load_settings()
    publisher = slot_status.publisher_from_settings({**settings, "enabled": True}, repo_dir=REPO)
    result = publisher.publish(document) if publisher else None
    return bool(result and result.ok)


def _emit(report: dict[str, Any], code: int) -> int:
    print(json.dumps(report, separators=(",", ":")))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "cloud"):
        command = sub.add_parser(name)
        command.add_argument("--now", help="offset-aware ISO time; for tests")
        command.add_argument("--dry-run", action="store_true", help="shadow mode for this run, whatever the config says")
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        settings = load_settings()
        mode = "shadow" if args.dry_run else settings["mode"]
        if mode not in MODES:
            raise ValueError("bad mode")
        if args.command == "cloud":
            after = CLOUD_CHECK_AFTER_MINUTES
        else:
            raw_after = settings["check_after_minutes"]
            if isinstance(raw_after, bool) or not isinstance(raw_after, int):
                raise ValueError("bad check_after_minutes")
            after = raw_after
        slot = due_slot(now, after)
        if slot is None:
            return _emit({"action": "skip", "reason": "no_slot_due", "slot": None}, 0)
        day = _et(now).date().isoformat()
        if args.command == "run":
            report = dispatch(day=day, slot=slot, now=now, mode=mode, calendar=_calendar(), probe=_probe,
                              send=_smtp_send, message_for=_message_for, notify=_notify, after_minutes=after)
        else:
            signal = slot_status.load_settings()
            url = slot_status.raw_url(signal["branch"], signal["repository"])
            document = _fetch_document(url)
            report = {**cloud_decide(document, day=day, slot=slot, now=now, calendar=_calendar()), "slot": slot}
            if report["action"] == "send":
                if mode == "shadow":
                    report["action"] = "would_send"
                else:
                    try:
                        _smtp_send(_message_for(slot))
                    except Exception as exc:  # noqa: BLE001 - type only, never the message
                        return _emit({**report, "action": "send_failed", "detail": type(exc).__name__}, 2)
                    report["action"] = "sent"
                    report["published"] = _publish_requested(document, day, slot, now)
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        return _emit({"action": "error", "detail": type(exc).__name__}, 2)
    return _emit(report, 2 if report["action"] == "send_failed" else 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
