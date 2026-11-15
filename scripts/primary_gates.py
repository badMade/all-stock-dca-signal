"""One-command gates for a primary (Claude) executor slot: Gates 0, 1, 3, 4, the account pin and Gate 6's caps.

Why this exists (2026-11-13, 11:30 slot): the primary started at 11:39:25 ET and spent about five minutes on
gates before its first order at about 11:44:49, because the prompt had the model walk each gate as its own tool
turn. The delayed, script-driven backup then reached the same tickers first and CAT was bought twice. The
gates that need only a clock, two config files and already-fetched order rows are deterministic, so they run
here as ONE command that prints ONE JSON verdict.

This reuses ``dca_runner``'s own gate functions (``market_day_status``, ``gate_time_sanity``,
``gate_trading_window``, ``budget_snapshot``) and window bounds (``SLOTS``); it carries no copy of a window or
cap rule. ``gate4`` classifies the slot window's orders by ``placed_agent`` exactly as the live prompts' Gate 4
text does (the owner's own activity is ignored, ``agentic`` is the compatible executor, anything else is rogue).
Out of scope on purpose: per-order revalidation (kept as is, operator decision 2026-11-13) and anything that
contacts the broker. It never writes and never prints the account number.

Usage::

    python3 -m scripts.primary_gates clock --slot 1130 [--now <offset-aware ISO-8601>] [--latest-start HH:MM]
    python3 -m scripts.primary_gates budget --orders-pages <week baseline json> --trading-day YYYY-MM-DD \\
        --buying-power <usd> [--tier normal|elevated|high_volatility|stress] [--weekly-rollover-reserve <usd>]

    python3 -m scripts.primary_gates gate4 --slot 1130 --trading-day YYYY-MM-DD --orders-pages <saved day json>

``budget`` reads the account from ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (``--account`` is still accepted); ``gate4``
takes it from the environment only.

Exit codes: 0 for PROCEED, END_SILENTLY, OK, NO_HEADROOM, CLEAR and COMPATIBLE_CONTINUE; 1 for ABORT; 2 for a
usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import dca_config
import dca_runner
from dca_runner import BrokerOrder
from scripts.account_arg import add_account_argument, resolve_account

ET = ZoneInfo("America/New_York")
_HHMM_SHAPE = re.compile(r"[0-9]{2}:[0-9]{2}")
# The recognized compatible executor (Perplexity's standing backup task): docs/safeguards.md, and the default of
# dca_runner.count_compatible_executor_attempts. EXACT match only; unknown provenance is rogue, never compatible.
COMPATIBLE_EXECUTOR_AGENT = "agentic"


def _verdict(status: str, gate: Optional[int], reason: str, now_et: datetime) -> dict:
    return {"status": status, "gate": gate, "reason": reason, "now_et": now_et.isoformat()}


def evaluate_clock(slot_id: str, now: datetime, latest_start: Optional[time] = None) -> dict:
    """Run Gate 0 (market day), Gate 1 (time sanity) and Gate 3 (trading window), in that order.

    ``now`` must be offset-aware; it is converted to America/New_York here, so a UTC instant is judged by its
    ET hour and the host's wall clock is never consulted. A date the calendar does not cover is an ABORT, never
    a PROCEED.

    ``latest_start`` (ET wall-clock time) can only TIGHTEN Gate 1: a start at or after it is an ABORT even when
    ``dca_runner.SLOTS`` would accept it. It exists for the 3:30 prompt, whose text closes at 3:55 PM while
    ``SLOTS['1530']`` runs to 4:00 PM. It never widens the slot window and never outranks Gate 0.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evaluate_clock requires an offset-aware datetime")
    if slot_id not in dca_runner.SLOTS:
        raise ValueError(f"unknown slot {slot_id!r}")
    slot = dca_runner.SLOTS[slot_id]
    now_et = now.astimezone(ET)
    try:
        market = dca_runner.market_day_status(now_et.date())
    except dca_runner.CalendarCoverageError:
        return _verdict("ABORT", 0, "market calendar unavailable, cannot confirm trading day", now_et)
    if market.slot_should_end_silently(slot):
        return _verdict("END_SILENTLY", 0, "market closed or early-close afternoon", now_et)
    if not dca_runner.gate_time_sanity(now_et, slot):
        return _verdict("ABORT", 1, f"outside the {slot_id} acceptance window", now_et)
    if latest_start is not None and now_et.time().replace(tzinfo=None) >= latest_start:
        return _verdict("ABORT", 1, f"at or after the latest start {latest_start.strftime('%H:%M')} ET", now_et)
    if not dca_runner.gate_trading_window(now_et.date()):
        return _verdict("ABORT", 3, "outside the approved live-trading window", now_et)
    return _verdict("PROCEED", None, "gates 0, 1 and 3 passed", now_et)


def account_pin(raw: Optional[str]) -> dict:
    """Validate the account number as resolved from the environment; fail closed on anything unusable.

    The shape is the repo's canonical one, ``dca_config.ACCOUNT_NUMBER_SHAPE`` (6 to 12 digits), not "any run of
    digits": the unexpanded ``${...}`` placeholder, the ``read_only`` literal, an empty value and anything else that is
    not an account number are refused. Surrounding whitespace is stripped. The number is returned only on success
    and the CLI never prints it.
    """
    value = (raw or "").strip()
    if not value:
        return {"ok": False, "reason": "account number is unset or empty"}
    if not dca_config.ACCOUNT_NUMBER_SHAPE.fullmatch(value):
        return {"ok": False, "reason": "account number is not 6 to 12 digits (placeholder or malformed)"}
    return {"ok": True, "account_number": value}


def _usable_buying_power(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def evaluate_budget(
    orders: Sequence[BrokerOrder],
    day: date,
    *,
    buying_power_usd: Any,
    daily_cap_usd: float,
    weekly_cap_usd: float,
    max_orders_per_day: int,
) -> dict:
    """Gate 6 cap arithmetic from broker rows: remaining dollars and orders, and the spendable ceiling.

    Counts every order regardless of ``placed_agent`` (the caps are shared across all slots and manual buying)
    and counts a cancelled or rejected buy toward the order count, as ``dca_runner.budget_snapshot`` does.
    Remaining values are clamped at zero. Unconfirmed buying power is an ABORT, never assumed sufficient.
    """
    if not _usable_buying_power(buying_power_usd):
        return {"status": "ABORT", "reason": "buying power could not be confirmed"}
    try:
        snapshot = dca_runner.budget_snapshot(orders, day)
    except dca_runner.BudgetDataError as exc:
        return {"status": "ABORT", "reason": f"budget data unusable: {type(exc).__name__}"}
    day_remaining = round(max(0.0, daily_cap_usd - snapshot.day_notional_usd), 2)
    week_remaining = round(max(0.0, weekly_cap_usd - snapshot.week_notional_usd), 2)
    orders_remaining = max(0, max_orders_per_day - snapshot.day_order_count)
    spendable = round(min(day_remaining, week_remaining, float(buying_power_usd)), 2)
    status = "NO_HEADROOM" if spendable <= 0 or orders_remaining == 0 else "OK"
    return {
        "status": status,
        "day_remaining_usd": day_remaining,
        "week_remaining_usd": week_remaining,
        "orders_remaining": orders_remaining,
        "spendable_usd": spendable,
    }


def resolve_tier_caps(tier: str, weekly_rollover_reserve_usd: Any = None) -> tuple[float, float]:
    """The (daily, weekly) dollar caps for a budget tier the PROMPT has already selected.

    Tier SELECTION (by today's count of 3x-or-greater signals) stays in the 3:30 prompt; this only turns a chosen
    tier into caps. The stress tier's weekly ceiling is ``min(nominal, NORMAL weekly + available rollover reserve)``,
    computed by ``dca_runner._effective_weekly_cap`` itself so there is no second copy of the rule. A tier that does
    not use a reserve ignores one that is passed, as the runner does. Fail closed: an unknown tier name (names are
    exact, lower-case), a stress tier with no reserve, or a reserve that is not a finite non-negative number raises
    ``ValueError``.
    """
    by_name = {candidate.name: candidate for candidate in dca_runner.BUDGET_TIERS}
    if tier not in by_name:
        raise ValueError(f"unknown budget tier {tier!r}; expected one of {', '.join(by_name)}")
    chosen = by_name[tier]
    if not chosen.uses_rollover_reserve:
        return float(chosen.daily_cap_usd), float(chosen.weekly_cap_usd)
    if weekly_rollover_reserve_usd is None:
        raise ValueError(f"the {tier} tier requires a weekly rollover reserve (--weekly-rollover-reserve)")
    if not _usable_buying_power(weekly_rollover_reserve_usd):  # same finite, non-negative, non-bool number check
        raise ValueError("the weekly rollover reserve must be a finite, non-negative number")
    return float(chosen.daily_cap_usd), float(dca_runner._effective_weekly_cap(chosen, weekly_rollover_reserve_usd))


class _EnvelopeRefused(ValueError):
    """A saved ``orders_pages`` envelope that must not be trusted. The message is a fixed string, safe to print:
    it never carries a row, an order id or an account number."""


def _load_orders(path: Path, account: str, *, expected_day: Optional[str] = None) -> list[BrokerOrder]:
    """Rows of a saved ``orders_pages`` envelope as ``BrokerOrder``.

    Refuses an envelope for another account and, when ``expected_day`` is given, one saved for another trading day
    (a stale file left over from an earlier run would otherwise be read as today's history).
    """
    from scripts.assemble_order_pages import _order_rows
    from scripts.backup_planner import _to_broker_order

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        raise _EnvelopeRefused("orders_pages is not an envelope with a pages list")
    if str(payload.get("account_number") or "").strip() != account.strip():
        raise _EnvelopeRefused("orders_pages account does not match the environment account")
    if expected_day is not None and str(payload.get("trading_day") or "").strip() != expected_day:
        raise _EnvelopeRefused("orders_pages was saved for a different trading day")
    return [_to_broker_order(row) for page in payload["pages"] for row in _order_rows(page)]


def classify_window_orders(orders: Sequence[BrokerOrder], slot_id: str, day: date) -> dict:
    """Gate 4's classification of the slot window's orders, as the live prompts' Gate 4 text states it.

    An order is IN THE WINDOW when its ``created_at`` (submission, not fill time) falls inside the slot's
    ``dca_runner.SLOTS`` interlock window on ``day``, both ends inclusive. Each is classified by ``placed_agent``
    alone, EXACT match, so an order's side and state do not matter (a cancelled or rejected attempt, or a sell, is
    still that executor's activity):

    * ``user``, ``recurring``, ``drip``: the owner's own activity, counted and never blocking;
    * ``agentic``: the compatible executor, whose tickers are reported as ``already_covered_tickers``;
    * anything else, including a missing value, a different case or stray whitespace: rogue.

    Any rogue order is an ABORT, even beside a compatible one. No rogue and at least one compatible order is
    COMPATIBLE_CONTINUE; otherwise CLEAR. Note ``dca_runner.gate_sole_executor`` deliberately ignores ``placed_agent``
    and treats even a manual order as a second writer; this follows the prompt (and ``backup_planner`` and
    ``plan_first_slot``), because the prompt is what runs. The output carries counts only, never an order id.
    """
    if slot_id not in dca_runner.SLOTS:
        raise ValueError(f"unknown slot {slot_id!r}")
    from scripts.backup_planner import _USER_AGENTS  # the owner's agents; one definition, shared with the backup

    start, end = dca_runner.SLOTS[slot_id].window_bounds_utc(day)
    in_window = own = compatible = rogue = 0
    covered: set[str] = set()
    for order in orders:
        created = dca_runner._require_aware(order.created_at).astimezone(timezone.utc)
        if not start <= created <= end:
            continue
        in_window += 1
        agent = order.placed_agent
        if agent in _USER_AGENTS:
            own += 1
        elif agent == COMPATIBLE_EXECUTOR_AGENT:
            compatible += 1
            covered.add(order.symbol.strip().upper())
        else:
            rogue += 1
    document = {
        "status": "ABORT" if rogue else ("COMPATIBLE_CONTINUE" if compatible else "CLEAR"),
        "orders_in_window": in_window,
        "own_activity": own,
        "compatible": compatible,
        "rogue": rogue,
        "already_covered_tickers": sorted(covered),
    }
    if rogue:
        document["reason"] = "executor-placed order(s) of unknown provenance in the slot window; a second executor may be live"
    return document


def _emit(document: dict) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _exit_code(document: dict) -> int:
    return 1 if document.get("status") == "ABORT" else 0


def _hhmm(text: str) -> time:
    """argparse type: a strict 24-hour HH:MM; anything else is a usage error (exit 2).

    strptime alone accepts single-digit fields, so ``15:5`` would parse as 15:05 -- before the 3:30 window,
    aborting every start -- instead of failing as a typo (PR #2290 review). The shape is checked first.
    """
    if not _HHMM_SHAPE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not HH:MM (24-hour)")
    try:
        return datetime.strptime(text, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not HH:MM (24-hour)") from exc


def _cmd_clock(args: argparse.Namespace) -> int:
    if args.slot not in dca_runner.SLOTS:
        print(f"unknown slot {args.slot!r}", file=sys.stderr)
        return 2
    if args.now is None:
        now = datetime.now(ET)
    else:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            print("--now is not an ISO-8601 timestamp", file=sys.stderr)
            return 2
        if now.tzinfo is None or now.utcoffset() is None:
            print("--now must carry a UTC offset", file=sys.stderr)
            return 2
    try:
        document = evaluate_clock(args.slot, now, latest_start=args.latest_start)
    except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only, never connector text
        document = {"status": "ABORT", "gate": None, "reason": f"unexpected {type(exc).__name__}"}
    pin = account_pin(os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"))
    # Report only whether the pin is usable; the number itself is never printed (AGENTS.md secrets policy).
    document["account_ok"] = pin["ok"]
    if not pin["ok"]:
        document["account_reason"] = pin["reason"]
    _emit(document)
    return _exit_code(document)


def _cmd_budget(args: argparse.Namespace) -> int:
    # The account comes from the environment, like `clock`; AGENTS.md keeps the number off command lines and out of
    # logs. `--account` stays accepted so an existing caller keeps working. Neither path ever prints the number.
    raw_account = args.account if args.account is not None else os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER")
    pin = account_pin(raw_account)
    if not pin["ok"]:
        document = {"status": "ABORT", "reason": pin["reason"]}
        _emit(document)
        return _exit_code(document)
    try:
        day = date.fromisoformat(args.trading_day)
        daily_cap, weekly_cap = resolve_tier_caps(args.tier, args.weekly_rollover_reserve)
        orders = _load_orders(args.orders_pages, pin["account_number"])
        document = evaluate_budget(
            orders,
            day,
            buying_power_usd=args.buying_power,
            daily_cap_usd=daily_cap,
            weekly_cap_usd=weekly_cap,
            max_orders_per_day=dca_runner.MAX_ORDERS_PER_DAY,
        )
        document["tier"] = args.tier
    except ValueError as exc:
        # A tier problem names itself (no connector text, no account); anything else reports its type only.
        reason = str(exc) if "tier" in str(exc) or "rollover" in str(exc) else f"orders_pages unusable: {type(exc).__name__}"
        document = {"status": "ABORT", "reason": reason}
    except Exception as exc:  # noqa: BLE001 - fail closed; type name only
        document = {"status": "ABORT", "reason": f"orders_pages unusable: {type(exc).__name__}"}
    _emit(document)
    return _exit_code(document)


def _cmd_gate4(args: argparse.Namespace) -> int:
    # The account comes from the environment only, read the way `clock` reads it, so an unusable value is an ABORT
    # verdict (exit 1) naming the account, never the number.
    pin = account_pin(os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"))
    if not pin["ok"]:
        document = {"status": "ABORT", "reason": pin["reason"]}
        _emit(document)
        return _exit_code(document)
    try:
        orders = _load_orders(args.orders_pages, pin["account_number"], expected_day=args.trading_day.isoformat())
        document = classify_window_orders(orders, args.slot, args.trading_day)
    except _EnvelopeRefused as exc:
        document = {"status": "ABORT", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - fail closed; type name only, never a row or an order id
        document = {"status": "ABORT", "reason": f"orders_pages unusable: {type(exc).__name__}"}
    _emit(document)
    return _exit_code(document)


def _iso_date(text: str) -> date:
    """argparse type: a calendar date as YYYY-MM-DD; anything else is a usage error (exit 2)."""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not YYYY-MM-DD") from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    clock = sub.add_parser("clock", help="Gates 0, 1 and 3 plus the account-pin check.")
    clock.add_argument("--slot", required=True)
    clock.add_argument("--now", default=None, help="Offset-aware ISO-8601 instant; defaults to the real clock.")
    clock.add_argument(
        "--latest-start",
        type=_hhmm,
        default=None,
        help="HH:MM ET; a start at or after it aborts at Gate 1. Can only tighten the slot window.",
    )
    budget = sub.add_parser("budget", help="Gate 6 cap arithmetic from a saved week-baseline orders_pages file.")
    budget.add_argument("--orders-pages", type=Path, required=True)
    add_account_argument(budget, "Account the week envelope was fetched for; must match it.")
    budget.add_argument("--trading-day", required=True)
    budget.add_argument("--buying-power", type=float, required=True)
    budget.add_argument(
        "--tier",
        choices=[candidate.name for candidate in dca_runner.BUDGET_TIERS],
        default="normal",
        help="The budget tier the prompt selected (default normal, the tier every slot but 3:30 uses).",
    )
    budget.add_argument(
        "--weekly-rollover-reserve",
        type=float,
        default=None,
        help="Available prior-week rollover reserve in USD; required with --tier stress, ignored otherwise.",
    )
    gate4 = sub.add_parser("gate4", help="Gate 4: classify the slot window's orders from a saved day orders_pages file.")
    gate4.add_argument("--slot", required=True, choices=sorted(dca_runner.SLOTS))
    gate4.add_argument("--trading-day", required=True, type=_iso_date, help="Today's trading day, YYYY-MM-DD.")
    gate4.add_argument("--orders-pages", type=Path, required=True, help="The saved day envelope; must be for this day.")
    args = parser.parse_args(argv)
    if args.command == "budget":
        resolve_account(budget, args)
    if args.command == "gate4":
        return _cmd_gate4(args)
    return _cmd_clock(args) if args.command == "clock" else _cmd_budget(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
