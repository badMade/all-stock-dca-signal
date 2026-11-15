"""Goal progress snapshot capture: the PREVIOUS trading day's close (Phase 2, ADR tests 22-26).

Phase 2 of docs/adr-goals-and-loss-hedge-shadow.md. Pinned by tests/test_goal_snapshot_capture.py
and tests/test_goal_snapshot_schedule.py.

Read-only by construction. The scheduled agent session wires two plain callables to Robinhood
READ tools (positions -> ``get_equity_positions``, prior close -> ``get_equity_historicals``) and
passes them in; this module imports no broker, MCP or network client and names no order tool.
The tests check that statically, the same way the hedge and CSP shadow tests do.

Fail closed, per the ADR's trigger (a):

* A trading day whose read fails in ANY way (a raised error, a malformed position, a missing or
  non-finite close) writes NO row -- not even the positions that did read -- and returns one
  ``missed_snapshot`` alert. A partial snapshot would value the account low and read as a loss.
* Zero holdings on a trading day is a real state (a fully sold account), not a missed read, so it
  yields one explicit "no holdings" row rather than silence.
* Weekends and configured holidays (config/market_calendar.json through Gate 0) are exempt: no
  read, no row, no alert.

Rows are plain dicts shaped for ``LedgerStore.ingest_portfolio_snapshots`` (ISO day strings) and
carry no account field of any kind.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional

import dca_goal_progress
from dca_goal_progress import ALERT_MISSED_SNAPSHOT, Alert

PositionsFn = Callable[[date], Iterable[Mapping[str, Any]]]
PriorCloseFn = Callable[[str, date], float]
TradingDayFn = Callable[[date], bool]

# How far back previous_trading_day() looks before refusing. The longest real NYSE closure in the
# calendar is a holiday next to a weekend (4 days); 10 covers that with margin, and a longer gap
# means the calendar is wrong or exhausted, which must fail loudly rather than reach back months.
_MAX_LOOKBACK_DAYS = 10

# market_value is recomputed from quantity x prior_close and rounded to cents, so it always agrees
# with the ledger's own consistency check (LedgerStore._SNAPSHOT_VALUE_TOLERANCE = $0.01).
_CENTS = 2


@dataclass
class CaptureResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


def _is_trading_day(day: date, is_trading_day: Optional[TradingDayFn]) -> bool:
    # None means the real calendar; dca_goal_progress already routes that through Gate 0
    # (dca_runner.market_day_status) so both Phase 2 modules agree on what a trading day is.
    return dca_goal_progress._trading_day(day, is_trading_day)


def previous_trading_day(day: date, is_trading_day: Optional[TradingDayFn]) -> date:
    """The last trading day strictly before ``day``. ``None`` uses the real NYSE calendar."""
    for back in range(1, _MAX_LOOKBACK_DAYS + 1):
        candidate = day - timedelta(days=back)
        if _is_trading_day(candidate, is_trading_day):
            return candidate
    raise LookupError(f"no trading day in the {_MAX_LOOKBACK_DAYS} days before {day.isoformat()}")


def should_run(day: date, is_trading_day: Optional[TradingDayFn]) -> bool:
    """Whether the 07:31 task runs on ``day``: trading days only (test 26)."""
    return _is_trading_day(day, is_trading_day)


def scheduled_task_spec() -> dict[str, Any]:
    """The 07:31 ET task (tests 25-26). It records the PREVIOUS trading day's close, so a run at
    07:31 -- before the open -- reads a settled close rather than a live price. It runs on the
    operator's Mac ("Requires this computer") and must be long finished before the 3:40 PM chain."""
    return {
        "time": time(7, 31),
        "timezone": "America/New_York",
        "days": "trading",
        "records": "previous_trading_day_close",
        "requires_this_computer": True,
        "max_runtime_minutes": 30,
    }


def _missed(day: date, why: str) -> CaptureResult:
    # ``why`` names the failure class only, never the raw payload, so a mis-wired callable that
    # returns account data cannot echo it into the alert text.
    return CaptureResult(rows=[], alerts=[Alert(
        ALERT_MISSED_SNAPSHOT, day, f"snapshot for trading day {day.isoformat()} not captured: {why}")])


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def capture_previous_close(
    day: date,
    *,
    positions_fn: PositionsFn,
    prior_close_fn: PriorCloseFn,
    is_trading_day: Optional[TradingDayFn] = None,
) -> CaptureResult:
    """Snapshot holdings valued at ``day``'s close. ``day`` is the trading day being recorded
    (for the 07:31 task, ``previous_trading_day(today, ...)``). All-or-nothing: see module docs."""
    if not _is_trading_day(day, is_trading_day):
        return CaptureResult()
    try:
        positions = list(positions_fn(day))
    except Exception as exc:  # noqa: BLE001 -- any read failure is a missed snapshot, by ADR rule
        return _missed(day, f"positions read failed ({type(exc).__name__})")

    if not positions:
        return CaptureResult(rows=[{
            "snapshot_day": day.isoformat(), "symbol": None, "quantity": 0.0,
            "close_day": None, "prior_close": None, "market_value": 0.0,
        }])

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, position in enumerate(positions):
        if not isinstance(position, Mapping):
            return _missed(day, f"position {index} is not a mapping")
        symbol, quantity = position.get("symbol"), position.get("quantity")
        if not isinstance(symbol, str) or not symbol.strip() or not _finite_positive(quantity):
            return _missed(day, f"position {index} has no usable symbol or quantity")
        if symbol in seen:
            # Two rows for one symbol would double-count it; the read is not trustworthy.
            return _missed(day, f"position {index} repeats a symbol")
        seen.add(symbol)
        try:
            close = prior_close_fn(symbol, day)
        except Exception as exc:  # noqa: BLE001 -- same rule: no partial snapshot
            return _missed(day, f"close read failed for position {index} ({type(exc).__name__})")
        if not _finite_positive(close):
            return _missed(day, f"no usable close for position {index}")
        rows.append({
            "snapshot_day": day.isoformat(),
            "symbol": symbol,
            "quantity": float(quantity),
            "close_day": day.isoformat(),
            "prior_close": float(close),
            "market_value": round(float(quantity) * float(close), _CENTS),
        })
    return CaptureResult(rows=rows)
