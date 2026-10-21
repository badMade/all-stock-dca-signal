"""Buying-agent helper for plan-first recommendation slots."""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from dca_market_history import orders_window_start
from dca_rec_models import SLOT_IDS, RecommendationSet
from dca_rec_store import RecommendationStore, load_settings

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def _now(value: str | None) -> datetime:
    moment = datetime.now(ET) if value is None else datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("--now must carry a UTC offset")
    return moment


def _emit(document: dict) -> None:
    print(json.dumps(document, separators=(",", ":"), allow_nan=False))


def _handed(store: RecommendationStore, slot: str, now: datetime) -> RecommendationSet:
    selection = store.select_for(slot, now, load_settings().max_age)
    if selection.status != "OK" or selection.recommendation is None:
        raise LookupError(selection.reason)
    # A cached set is only executable under the CURRENT config: the same ladder version, universe hash,
    # exclusions and per-order ceiling the Perplexity planner enforces (PR #1815 review).
    from scripts.backup_planner import current_config_reason

    reason = current_config_reason(selection.recommendation, trading_day=now.astimezone(ET).date(), slot=slot)
    if reason:
        raise LookupError(f"config_check_failed:{reason}")
    return selection.recommendation


def _freshness_reason(rec: RecommendationSet, now: datetime) -> str | None:
    """Why the exact set that ``begin`` handed may no longer be acted on at ``now``, or None.

    Checks only that set's own day and timestamp against the clock. It never selects a replacement mid-batch.
    ``begin`` accepted it as fresh, but fetching history can take minutes, so ``filter`` and every pre-order
    ``clock`` recheck it (PR #1815 review).
    """
    instant = now.astimezone(ET)
    as_of = rec.as_of.astimezone(ET)
    if rec.trading_day != instant.date() or as_of.date() != instant.date():
        return "handed_set_wrong_day"
    if as_of > instant:
        return "handed_set_from_the_future"
    if instant - as_of > load_settings().max_age:
        return "handed_set_stale"
    return None


def _pages(path_list: str, *, now: datetime) -> tuple[list[dict], list[dict]]:
    from scripts.assemble_order_pages import _order_rows, assemble

    page_paths = [Path(value) for value in sorted(glob.glob(path_list))] if any(ch in path_list for ch in "*?[") else [Path(path_list)]
    account = os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "").strip()
    envelope, _ = assemble([str(path) for path in page_paths], account_number=account,
                           trading_day=now.astimezone(ET).date().isoformat())
    rows: list[dict] = []
    for page in envelope["pages"]:
        if isinstance(page, dict):
            # The assembler's own parser: it accepts nested envelopes such as {"data":{"data":{"orders":[...]}}}
            # that a one-level read would see as empty (PR #1815 review).
            rows.extend(row for row in _order_rows(page) if isinstance(row, dict))
    return envelope["pages"], rows


def _created(row: dict) -> datetime | None:
    raw = row.get("created_at") or row.get("created_at_datetime")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _ticker(row: dict) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()


def _is_buy(row: dict) -> bool:
    return str(row.get("side", "")).lower() == "buy"


def _prior_ids(pages: list[dict]) -> list[str]:
    from scripts.assemble_order_pages import _order_rows

    result = []
    for page in pages:
        rows = _order_rows(page) if isinstance(page, dict) else []
        for row in rows:
            if isinstance(row, dict):
                oid = row.get("id") or row.get("order_id")
                if isinstance(oid, (str, int)) and not isinstance(oid, bool) and str(oid):
                    result.append(str(oid))
    return result


def _clock_reason(slot: str, now: datetime) -> str | None:
    """Why placing for ``slot`` at ``now`` is not allowed, or None when it is.

    A delayed task can still find a fresh recommendation after its own window has ended (a
    10:30 task waking at 11:31 sees an 11:25 file), or past an early close. So the real clock
    must be inside BOTH the slot's placement window and the actual regular session (PR #1667
    review). Market days and early closes come from the runner's own Gate 0 calendar.
    """
    import dca_runner
    from dca_order_plan import placement_window

    instant = now.astimezone(ET)
    day = instant.date()
    calendar = dca_runner.market_day_status(day)
    if calendar.is_closed:
        return "market_closed"
    close_at = datetime.combine(day, time(13 if calendar.is_early_close else 16, 0), ET)
    if not datetime.combine(day, time(9, 30), ET) <= instant < close_at:
        return "outside_regular_session"
    start, end = placement_window(slot, day)
    if not start <= instant < end:
        return "outside_slot_window"
    return None


def _cap_breach(rows: list[dict], orders: list[dict], budget: dict, day) -> str | None:
    """Which cap the fresh history plus ``orders`` would breach, or None.

    Uses the runner's own snapshot (``dca_runner.budget_snapshot``) over the repo's own row
    parser, so day/week spend is counted exactly as Gate 6 counts it.
    """
    from dca_runner import MAX_ORDERS_PER_DAY, budget_snapshot
    from scripts.backup_planner import _to_broker_order

    seen: set[str] = set()
    history = []
    for row in rows:
        order_id = str(row.get("id") or "")
        if order_id and order_id in seen:
            continue  # the same order on two pages must not count twice
        seen.add(order_id)
        history.append(_to_broker_order(row))
    snapshot = budget_snapshot(history, day)
    total = round(sum(order["usd"] for order in orders), 2)
    if snapshot.day_notional_usd + total > float(budget["daily_cap_usd"]) + 1e-9:
        return "daily"
    if snapshot.week_notional_usd + total > float(budget["weekly_cap_usd"]) + 1e-9:
        return "weekly"
    if snapshot.day_order_count + len(orders) > MAX_ORDERS_PER_DAY:
        return "order_count"
    return None


def _filter(slot: str, pages_arg: str, now: datetime, root: str | None) -> int:
    from dca_order_plan import placement_window
    from scripts.assemble_order_pages import AssemblyRefused

    reason = _clock_reason(slot, now)
    if reason:
        _emit({"status": "NONE", "slot": slot, "reason": reason})
        return 3
    store = RecommendationStore(root)
    # The set `begin` HANDED, not a fresh selection: a newer file landing between begin and
    # filter must not change what this run places.
    rec = store.handed(now.astimezone(ET).date(), slot)
    if rec is None:
        _emit({"status": "NONE", "slot": slot, "reason": "nothing_handed_run_begin_first"})
        return 3
    stale = _freshness_reason(rec, now)
    if stale:
        _emit({"status": "NONE", "slot": slot, "reason": stale})
        return 3
    try:
        pages, rows = _pages(pages_arg, now=now)
    except (AssemblyRefused, OSError, ValueError):
        _emit({"status": "ERROR", "slot": slot, "reason": "incomplete_order_history"})
        return 2

    start, end = placement_window(slot, rec.trading_day)
    for row in rows:
        created = _created(row)
        if created is None or not start <= created.astimezone(ET) <= end:
            continue
        if row.get("placed_agent") in {"user", "recurring", "drip"}:
            continue
        # The 10:30 live prompt Gate 4 explicitly treats unknown provenance as rogue.
        _emit({"status": "NONE", "slot": slot, "reason": "another_executor_in_window"})
        return 3

    since = rec.as_of
    bought = {_ticker(row) for row in rows if _is_buy(row) and _created(row) and _created(row).astimezone(ET).date() == rec.trading_day
              and _created(row) > since}
    orders = [item.to_json() for item in rec.items if item.ticker not in bought]
    # Re-apply the aggregate caps against this FRESH history (PR #1667 review). The program sized
    # the set within the caps as of its last cycle, but a buy elsewhere since then can push the
    # day or week over. All-or-nothing like the Perplexity backup's rule 4: never trim the batch.
    try:
        breach = _cap_breach(rows, orders, store.expected_for(rec).budget, rec.trading_day)
    except (OSError, ValueError, TypeError, KeyError):
        _emit({"status": "ERROR", "slot": slot, "reason": "budget_unverifiable"})
        return 2
    if breach:
        _emit({"status": "NONE", "slot": slot, "reason": f"caps_exceeded_since_compute:{breach}"})
        return 3
    save_to = store.placements_dir(rec.trading_day, slot)
    save_to.mkdir(parents=True, exist_ok=True)
    prior_file = save_to / "prior_ids.json"
    prior_file.write_text(json.dumps(_prior_ids(pages), separators=(",", ":")) + "\n", encoding="utf-8")
    _emit({"status": "OK", "slot": slot, "orders": orders, "prior_ids_file": str(prior_file), "save_to": str(save_to)})
    return 0


def _strict_json(path: Path):
    import dca_config

    return json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)


def _validated_placement(response_path: Path, check_path: Path) -> tuple[str | None, str]:
    """``(order_id, state)`` only for a response that ``validate_native_placement check`` passed.

    The agent saves that check's JSON verdict beside the response (``<TICKER>.check.json``).
    Gate 9 evidence counts an order only when the verdict is ``ok`` AND names the same order id
    the (strictly parsed) response carries. A stale, duplicate-keyed, wrong-symbol or
    wrong-amount response that failed the check is never reported as submitted (PR #1667 review).
    """
    try:
        verdict = _strict_json(check_path)
        response = _strict_json(response_path)
    except (OSError, ValueError, UnicodeError):
        return None, ""
    data = response.get("data") if isinstance(response, dict) else None
    order = data.get("order") if isinstance(data, dict) else None
    order_id = str(order.get("id") or "").strip() if isinstance(order, dict) else ""
    if not (isinstance(verdict, dict) and verdict.get("ok") is True and order_id
            and str(verdict.get("order_id") or "").strip() == order_id):
        return None, ""
    return order_id, str(order.get("state") or "").lower()


def _finish(slot: str, started_text: str, now: datetime, root: str | None, evidence_root: str | None = None) -> int:
    finalize = importlib.import_module("scripts.finalize_run_evidence")

    # --root is the RECOMMENDATION store; the finalizer's --root is the parent of every stream's artifacts. They are
    # different trees, so the evidence root is never inferred from the recommendation root, and a custom
    # recommendation root (replay) never silently writes evidence to the live default (PR #1815 review).
    if root is not None and evidence_root is None:
        _emit({"status": "ERROR", "slot": slot, "reason": "evidence_root_required_with_custom_root"})
        return 2

    store = RecommendationStore(root)
    started = _now(started_text)
    day = started.astimezone(ET).date()
    placement_dir = store.placements_dir(day, slot)
    handed_path = store.handed_path(day, slot)
    handed = RecommendationSet.from_json(json.loads(handed_path.read_text(encoding="utf-8")))
    orders = []
    refused = False
    for item in handed.items:
        path = placement_dir / f"{item.ticker}.json"
        if not path.exists():
            refused = True
            continue
        order_id, status = _validated_placement(path, placement_dir / f"{item.ticker}.check.json")
        if order_id:
            # The saved response keeps the broker's raw state; Gate 9 only accepts its own canonical
            # statuses, and every accepted-but-unfilled state is an in-flight "submitted" order.
            canonical = status if status in ("filled", "unconfirmed") else "submitted"
            orders.append({"ticker": item.ticker, "order_id": order_id, "submitted_amount_usd": item.usd,
                           "status": canonical})
        else:
            refused = True
    args = ["--slot", slot, "--started-at", started.isoformat(), "--outcome", "ORDERS_PLACED" if orders else "NO_ORDERS_SUBMITTED",
            "--order-submit-status", "submitted" if orders else ("not_submitted_error" if refused else "no_orders_submitted")]
    if orders:
        expected = store.expected_for(handed)
        args += ["--orders-json", json.dumps(orders, separators=(",", ":"))]
        args += ["--budget-json", json.dumps(expected.budget, separators=(",", ":"))]
    if evidence_root is not None:
        args += ["--root", evidence_root]
    verdict = finalize.main(args)
    _emit({"status": "OK" if verdict == 0 else "ERROR", "slot": slot, "verdict": verdict})
    return 0 if verdict == 0 else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("begin", "filter", "clock"):
        item = sub.add_parser(command)
        item.add_argument("--slot", required=True, choices=SLOT_IDS)
        item.add_argument("--now")
        item.add_argument("--root")
        if command == "filter":
            item.add_argument("--pages", required=True)
    finish = sub.add_parser("finish")
    finish.add_argument("--slot", required=True, choices=SLOT_IDS)
    finish.add_argument("--started-at", required=True)
    finish.add_argument("--now")
    finish.add_argument("--root", help="recommendation store root")
    finish.add_argument("--evidence-root", help="finalizer cron-tracking root; required with a custom --root")
    sub.choices["begin"].add_argument("--replace-unplaced", action="store_true",
                                      help="operator recovery: replace the handed set while no placement exists")
    args = parser.parse_args(argv)
    try:
        now = _now(args.now)
        if args.command == "clock":
            # Run immediately before EACH order: a batch must stop the moment the window ends.
            reason = _clock_reason(args.slot, now)
            if reason is None:
                handed = RecommendationStore(args.root).handed(now.astimezone(ET).date(), args.slot)
                if handed is not None:
                    reason = _freshness_reason(handed, now)
            _emit({"status": "OK" if reason is None else "NONE", "slot": args.slot, "reason": reason})
            return 0 if reason is None else 3
        if args.command == "begin":
            reason = _clock_reason(args.slot, now)
            if reason:
                _emit({"status": "NONE", "slot": args.slot, "reason": reason})
                return 3
            store = RecommendationStore(args.root)
            try:
                rec = _handed(store, args.slot, now)
            except LookupError as exc:
                _emit({"status": "NONE", "slot": args.slot, "reason": str(exc)})
                return 3
            try:
                store.record_handed(rec, replace_unplaced=args.replace_unplaced)
            except ValueError as exc:
                # A different set after placements already exist: never re-buy on a retry.
                _emit({"status": "NONE", "slot": args.slot, "reason": str(exc)})
                return 3
            # The ISO week (weekly cap) or seven days back (kill switch), whichever is earlier,
            # so `filter` can re-check the caps against complete fresh history.
            since = orders_window_start(rec.trading_day).astimezone(UTC)
            _emit({"status": "OK", "slot": args.slot, "as_of": rec.as_of.isoformat(), "orders_since": since.isoformat(),
                   "save_to": str(store.placements_dir(rec.trading_day, rec.slot)), "orders": [item.to_json() for item in rec.items]})
            return 0
        if args.command == "filter":
            return _filter(args.slot, args.pages, now, args.root)
        return _finish(args.slot, args.started_at, now, args.root, args.evidence_root)
    except Exception as exc:
        _emit({"status": "ERROR", "reason": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
