#!/usr/bin/env python3
"""The public "slot finished" signal a Perplexity backup reads before doing anything else.

2026-10-30: the seven Perplexity backup fires cost about 2,500 credits a trading day, nearly all of it on fires that
end as ``stood_down_local_active``: each one still listed devices and ran a Mac sub-agent just to learn that Claude's
slot had already succeeded (docs/cost_tracking.md, "Perplexity backup target band"; target under 200). The primary
now publishes one small document per trading day and the backup reads it once:

    {"schema": "slot_status/1", "trading_day": "2026-10-29", "market": "open" | "closed", "updated_at": "...",
     "slots": {"0935": {"status": "in_progress" | "succeeded" | "failed", "stamped_at": "..."}, ...}}

The writer is the Gate 9 finalizer every live prompt calls on every outcome (``scripts/finalize_run_evidence.py``,
2026-11-03: ``succeeded`` for a run that finished as designed, ``failed`` for an error or abort), the primary's
``local_heartbeat start`` / ``finish`` when a caller uses them, and ``market-day`` below, once each morning. On
2026-11-02 the signal never updated because no live prompt calls start/finish; the finalizer is what they all call. The file is public (the ``slot-status`` branch of a public repo), so it carries only the day, the market
state and each slot's status and time: never a ticker, amount, account, order or reference.

``is_done`` is the reader's contract, the same rule the short Perplexity prompt applies by hand: stand down only on
a slot marked ``succeeded`` today, or a closed market. Anything else, ``in_progress`` included (a run still going 20
minutes after its slot may have crashed), falls through to the existing full check. So a wrong, stale or missing
signal can only cost credits, never skip a safety check or add a buy. Files here are inputs only, never purchase
evidence (rule 7 of the backup prompts).

Publishing is best-effort: ``update`` never raises, so a push failure never fails or delays the primary run.

    python3 -m scripts.slot_status market-day [--trading-day D] [--now T]   # each morning: open or closed
    python3 -m scripts.slot_status show                                     # the local document
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import dca_config
import dca_paths
from dca_fileio import atomic_write_text
from dca_rec_publish import GitHubBranchPublisher
from scripts.file_lock import cross_process_lock

ET = ZoneInfo("America/New_York")
SCHEMA = "slot_status/1"
FILENAME = "slot_status.json"
# The seven canonical slots, in order (dca_order_plan.LATEST_PLACEMENT_ET's keys). Spelled out rather than imported
# so this module stays light enough for local_heartbeat to import on every start and finish.
SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")
# backup_requested (2026-10-31, scripts/backup_dispatch.py): a backup was triggered for the slot. It never stands a
# backup down (is_done needs succeeded), and it tells the GitHub watcher the Mac already handled the slot.
STATUSES = ("in_progress", "succeeded", "failed", "backup_requested")
MARKETS = ("open", "closed")
# The signal's repository comes from config/slot_status.json "repository": a separate PUBLIC data-only repository,
# because badMade/all-stock-dca is private and an unauthenticated raw read of it returns 404 (found 2026-10-31).
_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
# The branch publisher force-pushes a one-file tree: sharing the recommendations branch would erase
# recommendations.json. main and claude/** are refused by the publisher itself (they trigger CI).
_RESERVED_BRANCHES = {"recommendations"}
# Set (to anything non-empty) to keep every start/finish local: conftest.py sets it for the whole suite, so a test
# that runs local_heartbeat never pushes to the real remote. Same convention as DCA_DISABLE_DOTENV.
DISABLE_ENV = "DCA_DISABLE_SLOT_STATUS_PUBLISH"


def state_dir() -> Path:
    return Path(dca_paths.cron_tracking_root()) / "slot_status"


def state_path() -> Path:
    return state_dir() / FILENAME


def raw_url(branch: str, repository: str) -> str:
    """Where a cloud reader fetches the published document (the public signal repository, never the trading one)."""
    if not _REPOSITORY.fullmatch(repository or "") or ".." in repository:
        raise ValueError("repository must look like owner/name")
    return f"https://raw.githubusercontent.com/{repository}/{branch}/{FILENAME}"


def _stamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the clock value has no timezone; refusing to guess one")
    return now.astimezone(ET).isoformat(timespec="seconds")


def _check_day(day: str) -> str:
    date.fromisoformat(day)  # ValueError on a malformed day
    return day


def _empty(day: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "trading_day": day, "market": "open", "updated_at": None, "slots": {}}


def _load(day: str) -> dict[str, Any]:
    """Today's local document, or a fresh one. A corrupt or foreign file is replaced, never trusted."""
    try:
        document = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty(day)
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("trading_day") != day
            or not isinstance(document.get("slots"), dict) or document.get("market") not in MARKETS):
        return _empty(day)
    return document


def _write(day: str, change) -> dict[str, Any]:
    """Load, apply ``change``, write atomically, under one lock so two slots never lose each other's update."""
    state_dir().mkdir(parents=True, exist_ok=True)
    with cross_process_lock(state_dir() / "slot_status.lock"):
        document = _load(day)
        change(document)
        atomic_write_text(state_path(), json.dumps(document, sort_keys=True, indent=2) + "\n")
    return document


def record(day: str, slot: str, status: str, *, now: datetime) -> dict[str, Any]:
    """Set one slot's status for ``day`` and return the whole day document. A new day starts empty."""
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    stamp = _stamp(now)
    _check_day(day)

    def change(document: dict[str, Any]) -> None:
        document["slots"][slot] = {"status": status, "stamped_at": stamp}
        document["updated_at"] = stamp

    return _write(day, change)


def mark_market(day: str, market: str, *, now: datetime) -> dict[str, Any]:
    """Set whether the market is open on ``day`` (published once each morning)."""
    if market not in MARKETS:
        raise ValueError(f"unknown market state {market!r}")
    stamp = _stamp(now)
    _check_day(day)

    def change(document: dict[str, Any]) -> None:
        document["market"] = market
        document["updated_at"] = stamp

    return _write(day, change)


def market_for_day(day: str, calendar: Mapping[str, Any]) -> str:
    """``closed`` on a weekend or a full holiday, else ``open``.

    An early-close day is ``open``: the backup's gates still judge each window, so the few fires after 13:00 on
    those days simply take the full path. A year the calendar does not cover fails closed (ValueError), the same
    rule as scripts/backup_gates.py.
    """
    parsed = date.fromisoformat(day)
    if parsed.year not in set(calendar.get("years_covered", [])):
        raise ValueError(f"market calendar does not cover {parsed.year}; extend config/market_calendar.json")
    if parsed.weekday() >= 5 or day in set(calendar.get("holidays_closed", [])):
        return "closed"
    return "open"


def is_done(document: Any, *, trading_day: str, slot: str) -> bool:
    """True only when the backup for ``slot`` on ``trading_day`` may stand down without any other call."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return False
    if document.get("trading_day") != trading_day or document.get("market") not in MARKETS:
        return False
    if document["market"] == "closed":
        return True
    slots = document.get("slots")
    entry = slots.get(slot) if isinstance(slots, dict) else None
    if not isinstance(entry, dict) or not entry.get("stamped_at"):
        return False
    return entry.get("status") == "succeeded"


def _publish(document: Mapping[str, Any], publisher: Any) -> tuple[bool, str]:
    try:
        result = publisher.publish(document)
    except Exception as exc:  # noqa: BLE001 - a publisher bug must never break the primary's start or finish
        return False, type(exc).__name__  # the type only: a message can carry a remote URL with a token
    return bool(result.ok), str(result.detail)


def update(day: str, slot: str, status: str, *, now: datetime | None = None, publisher: Any = "configured") -> dict:
    """Record then publish; never raises. ``publisher`` defaults to the configured one; None only records."""
    if publisher == "configured":
        publisher = _configured_publisher()
    try:
        document = record(day, slot, status, now=now or datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - best-effort by design (see module docstring)
        return {"recorded": False, "published": False, "detail": type(exc).__name__}
    if publisher is None:
        return {"recorded": True, "published": False, "detail": "publishing disabled"}
    ok, detail = _publish(document, publisher)
    return {"recorded": True, "published": ok, "detail": detail}


# publish (2026-11-04): the live runs record through finalize_run_evidence inside a Cowork bridge VM that mounts the
# checkout but has no GitHub credentials, so their own pushes fail (2026-11-03: seven slots recorded, none reached the
# public branch). The Mac's launchd agent (deploy/launchd/com.all-stock-dca.slot-status-publish.plist.template) runs
# ``publish`` with the Mac's credentials. It pushes the document exactly as recorded and remembers the hash of the
# last one it pushed, so its fallback timer does not add a commit every few minutes.
_PUBLISHED_HASH = "last_published.sha256"


def _published_hash_path() -> Path:
    return state_dir() / _PUBLISHED_HASH


def _local_document() -> tuple[dict[str, Any], str] | None:
    """The recorded document and its hash, or None when it is missing or not a valid slot-status document."""
    try:
        text = state_path().read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, ValueError):
        return None
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA
            or not isinstance(document.get("slots"), dict) or document.get("market") not in MARKETS):
        return None
    try:
        _check_day(str(document.get("trading_day")))
    except ValueError:
        return None
    return document, hashlib.sha256(text.encode("utf-8")).hexdigest()


def publish_local(publisher: Any) -> tuple[int, dict[str, Any]]:
    """Push the recorded document unchanged; (exit code, report). Never raises, never rewrites the document."""
    loaded = _local_document()
    if loaded is None:
        return 2, {"ok": False, "error": "no_local_document"}
    document, digest = loaded
    report = {"ok": True, "trading_day": document["trading_day"]}
    try:
        already = _published_hash_path().read_text(encoding="utf-8").strip() == digest
    except OSError:
        already = False
    if already:
        return 0, {**report, "published": False, "detail": "unchanged"}
    if publisher is None:
        return 1, {**report, "published": False, "detail": "publishing disabled"}
    ok, detail = _publish(document, publisher)
    if not ok:
        return 1, {**report, "published": False, "detail": detail}  # not remembered: the next run retries
    try:
        atomic_write_text(_published_hash_path(), digest + "\n")
    except OSError:
        pass  # only costs a repeat push on the next run
    return 0, {**report, "published": True, "detail": detail}


def load_settings(config_dir: Path | None = None) -> dict[str, Any]:
    path = (config_dir or dca_config.get_config_dir()) / "slot_status.json"
    return json.loads(path.read_text(encoding="utf-8"))


def publisher_from_settings(settings: Mapping[str, Any], *, repo_dir: str | Path) -> GitHubBranchPublisher | None:
    if not settings.get("enabled"):
        return None
    branch = settings.get("branch", "slot-status")
    if branch in _RESERVED_BRANCHES:
        raise ValueError(f"branch {branch!r} is reserved for another published file")
    return GitHubBranchPublisher(repo_dir, branch=branch, remote=settings.get("remote", "origin"), filename=FILENAME)


def _configured_publisher() -> GitHubBranchPublisher | None:
    """The publisher config/slot_status.json names, or None (disabled, unreadable or invalid settings)."""
    if os.environ.get(DISABLE_ENV):
        return None
    try:
        return publisher_from_settings(load_settings(), repo_dir=Path(__file__).resolve().parents[1])
    except (OSError, ValueError):
        return None


def _calendar() -> dict[str, Any]:
    return json.loads((dca_config.get_config_dir() / "market_calendar.json").read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    market = sub.add_parser("market-day", help="Publish whether the market is open today (run once each morning).")
    market.add_argument("--trading-day", help="YYYY-MM-DD; default today in ET")
    market.add_argument("--now", help="offset-aware ISO time; for tests")
    sub.add_parser("show", help="Print the local document.")
    sub.add_parser("publish", help="Push the local document unchanged (the Mac's launchd agent runs this).")
    args = parser.parse_args(argv)

    if args.command == "publish":
        code, report = publish_local(_configured_publisher())
        print(json.dumps(report, sort_keys=True))
        return code

    if args.command == "show":
        try:
            print(state_path().read_text(encoding="utf-8").strip())
        except OSError:
            print(json.dumps({"ok": False, "error": "no_local_document"}))
            return 2
        return 0

    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
        day = args.trading_day or _stamp(now)[:10]
        state = market_for_day(day, _calendar())
        document = mark_market(day, state, now=now)
    except (OSError, ValueError, TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 2
    publisher = _configured_publisher()
    published, detail = _publish(document, publisher) if publisher is not None else (False, "publishing disabled")
    print(json.dumps({"ok": True, "trading_day": day, "market": state, "published": published, "detail": detail},
                     sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
