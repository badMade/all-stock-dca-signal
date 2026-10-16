"""Run or inspect one Robinhood data-courier cycle without placing orders."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, time
from pathlib import Path
from typing import Sequence

import dca_config
import dca_paths
import dca_runner
from dca_fileio import atomic_write_text
from dca_fill_compare import FillComparator
from dca_market_history import MarketHistoryStore
from dca_market_sources import ET, SourceUnavailable
from dca_rec_models import SLOT_IDS
from dca_rec_publish import build_document, publish_all, publishers_from_settings
from dca_rec_store import RecommendationStore, load_settings
from dca_recommender import RecommendationEngine, slots_for
from scripts.assemble_order_pages import _order_rows
from scripts.scheduler_lease import SchedulerLease, window_key


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(ET)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=ET) if parsed.tzinfo is None else parsed).astimezone(ET)


def _emit(value: object) -> None:
    print(json.dumps(value, separators=(",", ":"), allow_nan=False))


def _account_number() -> str:
    configured = dca_config.expand_config_env("${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", source="snapshot courier")
    return dca_config.resolve_broker_account(configured, source="snapshot courier")


def _universe(config_dir: Path) -> list[str]:
    tickers = dca_config.load_tickers(config_dir).get("tickers")
    if not tickers:
        raise ValueError("active ticker universe is unavailable")
    return list(tickers)


def _session_status(now: datetime) -> str | None:
    day = now.astimezone(ET).date()
    calendar = dca_runner.market_day_status(day)
    if calendar.is_closed:
        return "not_a_market_day"
    if not dca_runner.gate_trading_window(day):
        return "outside_approved_trading_window"
    open_at = datetime.combine(day, time(9, 30), ET)
    close_at = datetime.combine(day, time(13 if calendar.is_early_close else 16, 0), ET)
    instant = now.astimezone(ET)
    if not open_at <= instant < close_at:
        return "outside_regular_session"
    return None


RUN_MARKER = "fetch_plan.json"
# A courier run fetches for a minute or two; anything older than this is stale evidence, not a
# fresh snapshot, and must not be re-stamped as fresh by a late ingest.
MAX_RUN_AGE_SECONDS = 10 * 60
_CLOCK_SKEW_SECONDS = 60


def _planned_at(run_dir: Path, now: datetime) -> datetime | None:
    """The run's fetch-plan time when it is today's, not in the future, and not too old."""
    try:
        marker = json.loads((run_dir / RUN_MARKER).read_bytes(),
                            object_pairs_hook=dca_config._reject_duplicate_keys)
        planned = datetime.fromisoformat(marker["planned_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if planned.tzinfo is None or planned.utcoffset() is None:
        return None
    age = (now - planned).total_seconds()
    if planned.astimezone(ET).date() != now.astimezone(ET).date():
        return None
    if age < -_CLOCK_SKEW_SECONDS or age > MAX_RUN_AGE_SECONDS:
        return None
    return planned.astimezone(ET)


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fetch_plan(now: datetime, config_dir: Path) -> int:
    try:
        reason = _session_status(now)
        if reason:
            _emit({"status": "IDLE", "reason": reason})
            return 3
        account = _account_number()
        day = now.astimezone(ET).date()
        result = MarketHistoryStore(RecommendationStore().day_dir(day)).fetch_plan(
            trading_day=day, now=now, universe=_universe(config_dir), account_number=account,
        )
        # Bind the run to the moment its plan was issued: ingest stamps the recommendation with
        # this time and refuses a run that is too old or from the future (PR #1667 review).
        run_dir = Path(result["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(run_dir / RUN_MARKER, json.dumps({"planned_at": now.isoformat()}) + "\n")
        _emit({"status": "FETCH", **result})
        return 0
    except (ValueError, OSError, SourceUnavailable):
        _emit({"status": "ERROR", "reason": "configuration_unavailable"})
        return 2


def _inside_incoming(run_dir: Path, incoming: Path) -> bool:
    try:
        run_dir.resolve(strict=True).relative_to(incoming.resolve(strict=True))
        return run_dir.resolve(strict=True) != incoming.resolve(strict=True)
    except (OSError, ValueError):
        return False


def _ingest(run_dir: Path, now: datetime, config_dir: Path) -> int:
    day = now.astimezone(ET).date()
    store = RecommendationStore()
    incoming = store.day_dir(day) / "raw" / "incoming"
    if not _inside_incoming(run_dir, incoming):
        _emit({"status": "ERROR", "reason": "run_dir_outside_today_incoming"})
        return 2
    planned_at = _planned_at(run_dir, now)
    if planned_at is None:
        _emit({"status": "ERROR", "reason": "run_not_fresh_or_not_from_fetch_plan"})
        return 2
    # Everything downstream is "as of" the fetch, never the (later) ingest clock.
    now = planned_at
    lease_key = window_key(day.isoformat(), "plan_first", "ingest")
    lease = SchedulerLease(Path(dca_paths.cron_tracking_root()) / "scheduler_leases", lease_key)
    holder = f"snapshot-{os.getpid()}-{now.timestamp()}"
    acquired = lease.acquire(holder)
    if not getattr(acquired, "acquired", acquired):
        _emit({"status": "BUSY"})
        return 3
    try:
        account = _account_number()
        settings = load_settings(config_dir)
        # Validate and assemble in a temporary tree so malformed later pages cannot leave
        # an apparently complete day snapshot behind.
        target_raw = store.day_dir(day) / "raw"
        with tempfile.TemporaryDirectory(prefix="dca-snapshot-cycle-") as staged_root:
            history = MarketHistoryStore(Path(staged_root) / day.isoformat())
            history.raw_dir.mkdir(parents=True, exist_ok=True)
            for name in ("day_bars.json", "daily.json"):
                existing = target_raw / name
                if existing.is_file():
                    shutil.copyfile(existing, history.raw_dir / name)
            snapshot, broker = history.ingest_run(
                run_dir, trading_day=day, now=now, universe=_universe(config_dir), account_number=account,
            )
            # Keep raw broker rows from this run for the existing fill joiner.
            order_files = sorted(run_dir.glob("orders_*.json"))
            order_rows = [row for path in order_files for row in _order_rows(history._load_response(path))]
            engine = RecommendationEngine(config_dir=config_dir)
            results = {}
            computed = {}
            for slot in slots_for(now, settings.lookahead):
                result = engine.compute(snapshot, broker, now, slot=slot)
                if result.status == "OK" and result.recommendations and result.expected:
                    computed[slot] = (result.recommendations, result.expected)
                results[slot] = {
                    "status": result.status,
                    "reasons": list(result.reasons),
                    "orders": len(result.recommendations.items) if result.recommendations else 0,
                }
            for recommendation, expected in computed.values():
                store.write(recommendation, expected)
            target_raw.mkdir(parents=True, exist_ok=True)
            for name in ("day_bars.json", "daily.json"):
                staged = history.raw_dir / name
                if staged.is_file():
                    atomic_write_text(target_raw / name, staged.read_text(encoding="utf-8"))
        publishers = publishers_from_settings(settings.publish, repo_dir=_root())
        publish_sets = {slot: pair[0] for slot, pair in computed.items()}
        publish_results = publish_all(publishers, build_document(publish_sets, generated_at=now)) if computed else []
        comparator = FillComparator(store)
        comparator.write_report(day, comparator.compare(day, order_rows), generated_at=now)
        status = "OK" if all(row["status"] == "OK" for row in results.values()) else "PARTIAL"
        _emit({"status": status, "slots": results, "publish": [
            {"channel": item.channel, "ok": item.ok, "detail": item.detail} for item in publish_results
        ]})
        return 0
    except (SourceUnavailable, ValueError, OSError, KeyError, TypeError):
        _emit({"status": "ERROR", "reason": "courier_input_unavailable"})
        return 2
    finally:
        lease.release(holder)


def _status(now: datetime, config_dir: Path) -> int:
    settings = load_settings(config_dir)
    store = RecommendationStore()
    slots = {}
    for slot in SLOT_IDS:
        selected = store.select_for(slot, now, settings.max_age)
        files = sorted(store.day_dir(now.astimezone(ET).date()).glob(f"rec_{slot}_*.json"), reverse=True)
        newest = files[0] if files else None
        slots[slot] = {
            "status": "FRESH" if selected.status == "OK" else "STALE" if selected.reason == "stale" else "NONE",
            "reason": selected.reason,
            "file": str(newest) if newest else None,
            "age_seconds": selected.age_seconds,
        }
    _emit({"status": "OK", "slots": slots})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-plan")
    fetch.add_argument("--now")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--run-dir", required=True, type=Path)
    ingest.add_argument("--now")
    status = sub.add_parser("status")
    status.add_argument("--now")
    args = parser.parse_args(argv)
    try:
        now = _now(args.now)
        config_dir = dca_config.get_config_dir()
        if args.command == "fetch-plan":
            return _fetch_plan(now, config_dir)
        if args.command == "ingest":
            return _ingest(args.run_dir, now, config_dir)
        return _status(now, config_dir)
    except (ValueError, OSError, SourceUnavailable):
        _emit({"status": "ERROR", "reason": "configuration_unavailable"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
