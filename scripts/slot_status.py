#!/usr/bin/env python3
"""The public "slot finished" signal a Perplexity backup reads before doing anything else.

2026-10-30: the seven Perplexity backup fires cost about 2,500 credits a trading day, nearly all of it on fires that
end as ``stood_down_local_active``: each one still listed devices and ran a Mac sub-agent just to learn that Claude's
slot had already succeeded (docs/cost_tracking.md, "Perplexity backup target band"; target under 200). The primary
now publishes one small document per trading day and the backup reads it once:

    {"schema": "slot_status/1", "trading_day": "2026-10-29", "market": "open" | "closed", "updated_at": "...",
     "slots": {"0935": {"status": "in_progress" | "succeeded" | "failed", "stamped_at": "..."}, ...}}

The writer is the primary's own ``local_heartbeat start`` / ``finish`` (and ``market-day`` below, once each
morning). The file is public (the ``slot-status`` branch of a public repo), so it carries only the day, the market
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
import json
import os
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
STATUSES = ("in_progress", "succeeded", "failed")
MARKETS = ("open", "closed")
# The public repository whose data branch the Perplexity backups read (same slug as backup_procedure's).
GITHUB_REPOSITORY = "badMade/all-stock-dca"
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


def raw_url(branch: str) -> str:
    """Where a cloud reader fetches the published document."""
    return f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/{branch}/{FILENAME}"


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
    args = parser.parse_args(argv)

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
