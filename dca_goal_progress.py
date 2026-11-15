"""Goal progress: TWR and MWR from fills, and the alerts for missed snapshots and bad goal config.

Phase 2 of docs/adr-goals-and-loss-hedge-shadow.md. Pure functions only: no network, no broker,
no order tool (tests/test_goal_progress_no_orders.py). Robinhood positions are read by the caller
and passed in, so this module never touches the account itself.

Conventions are pinned by tests/test_goal_progress_golden.py:

* A buy is an external contribution equal to its dollar amount on its trading day.
* Holdings are valued at that day's close.
* r_t = V_t / (V_(t-1) + C_t) - 1 ; TWR = prod(1 + r_t) - 1.
* MWR is XIRR (actual/365) over contributions and the terminal value.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import dca_goals

# Trigger (a): one missed trading day alerts; weekends and configured holidays are exempt.
ALERT_MISSED_SNAPSHOT = "missed_snapshot"
# Trigger (b): goals exist but load_goals would go neutral because the data is bad.
ALERT_CONFIG_INVALID = "goal_config_invalid"
ALERT_CONFIG_AMBIGUOUS = "goal_config_ambiguous"

_DAYS_PER_YEAR = Decimal(365)
_PRECISION = 50


class MissingCloseError(LookupError):
    """A valuation day has no close for a held ticker. Never carried forward silently."""


@dataclass(frozen=True)
class Alert:
    kind: str
    day: Optional[date]
    detail: str


@dataclass
class DayResult:
    progress_rows: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)


# ---- time-weighted and money-weighted returns ----------------------------------------------

def _normal_closes(closes: Mapping[str, Mapping[Any, Any]]) -> dict[str, dict[date, Decimal]]:
    """Accept ISO date strings or dates as keys, and strings or Decimals as prices."""
    return {t: {_as_date(d): Decimal(str(p)) for d, p in by_day.items()} for t, by_day in closes.items()}


def _holdings_value(quantities: Mapping[str, Decimal], closes: Mapping[str, Mapping[date, Decimal]],
                    day: date) -> Decimal:
    total = Decimal(0)
    for ticker, qty in quantities.items():
        if qty == 0:
            continue
        by_day = closes.get(ticker, {})
        if day not in by_day:
            raise MissingCloseError(f"no close for {ticker} on {day.isoformat()}")
        total += qty * by_day[day]
    return total


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def subperiod_returns(fills: Iterable[Mapping[str, Any]], closes: Mapping[str, Mapping[date, Decimal]],
                      valuation_days: Sequence[date]) -> list[Decimal]:
    """r_t = V_t / (V_(t-1) + C_t) - 1 for each valuation day, in order."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        closes = _normal_closes(closes)
        by_day: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
        for fill in fills:
            by_day[_as_date(fill["date"])].append(fill)
        quantities: dict[str, Decimal] = defaultdict(Decimal)
        previous = Decimal(0)
        returns: list[Decimal] = []
        for day in valuation_days:
            contribution = Decimal(0)
            for fill in by_day.get(day, []):
                quantities[fill["ticker"]] += Decimal(str(fill["quantity"]))
                contribution += Decimal(str(fill["amount_usd"]))
            value = _holdings_value(quantities, closes, day)
            base = previous + contribution
            if base == 0:
                raise ValueError(f"no capital at risk on {day.isoformat()}; sub-period return undefined")
            returns.append(value / base - 1)
            previous = value
        return returns


def time_weighted_return(fills: Iterable[Mapping[str, Any]], closes: Mapping[str, Mapping[date, Decimal]],
                         valuation_days: Sequence[date]) -> Decimal:
    """Cumulative TWR over the window: prod(1 + r_t) - 1."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        growth = Decimal(1)
        for r in subperiod_returns(fills, closes, valuation_days):
            growth *= 1 + r
        return growth - 1


def xirr(flows: Sequence[tuple[date, Decimal]]) -> float:
    """Annualized money-weighted return (actual/365). Bisection on a bracketing interval.

    Contributions are negative, the terminal value positive. Raises ValueError when the flows
    have no sign change, since no rate then solves the equation.
    """
    if not flows:
        raise ValueError("xirr needs at least one cash flow")
    origin = min(d for d, _ in flows)
    with localcontext() as ctx:
        ctx.prec = _PRECISION

        def npv(rate: Decimal) -> Decimal:
            return sum(cf / (1 + rate) ** (Decimal((d - origin).days) / _DAYS_PER_YEAR) for d, cf in flows)

        low, high = Decimal("-0.999999"), Decimal(1)
        while npv(high) > 0 and high < Decimal("1E12"):
            high *= 2
        if npv(low) * npv(high) > 0:
            raise ValueError("xirr: cash flows have no sign change; no rate solves them")
        for _ in range(400):
            mid = (low + high) / 2
            if npv(mid) > 0:
                low = mid
            else:
                high = mid
        return float((low + high) / 2)


# ---- alerts ---------------------------------------------------------------------------------

def _trading_day(day: date, is_trading_day: Optional[Callable[[date], bool]]) -> bool:
    if is_trading_day is not None:
        return bool(is_trading_day(day))
    # Real calendar: config/market_calendar.json through Gate 0. Imported lazily; it is heavy.
    from dca_runner import market_day_status

    return not market_day_status(day).is_closed


def goal_config_alert(config_dir: Path | str, now: datetime) -> Optional[Alert]:
    """Trigger (b). None when the goal config is fine, intentionally paused, empty, or missing.

    Every entry is validated whatever its effective date, so a broken goal alerts before it
    takes effect. A goal that is not yet effective is valid and does not alert.
    """
    path = Path(config_dir) / dca_goals.GOALS_FILENAME
    if not path.exists():
        return None  # no goals file: no goals, nothing to report
    try:
        doc = dca_goals.parse_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return Alert(ALERT_CONFIG_INVALID, None, f"goals.json unreadable or invalid: {exc}")
    if set(doc) - dca_goals._DOCUMENT_KEYS:
        return Alert(ALERT_CONFIG_INVALID, None, f"unknown top-level fields {sorted(set(doc) - dca_goals._DOCUMENT_KEYS)}")
    if doc.get("status") != "active":
        return None  # an intentional pause, not bad data
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return Alert(ALERT_CONFIG_INVALID, None, "version must be an integer")
    goals = doc.get("goals")
    if not isinstance(goals, list):
        return Alert(ALERT_CONFIG_INVALID, None, "goals must be a list")
    if not goals:
        return None

    in_effect: Counter = Counter()
    for entry in goals:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        try:
            if kind == "aspirational":
                goal = dca_goals._growth(entry)
                effective = goal.effective_from
            elif kind == "loss_hedge":
                _, effective = dca_goals._hedge(entry)
            else:
                return Alert(ALERT_CONFIG_INVALID, None, f"unknown goal kind {kind!r}")
        except (ValueError, KeyError, TypeError) as exc:
            return Alert(ALERT_CONFIG_INVALID, None, f"goal {entry.get('goal_id')!r} invalid: {exc}")
        if effective <= now:
            in_effect[kind] += 1
    for kind, count in in_effect.items():
        if count > 1:
            return Alert(ALERT_CONFIG_AMBIGUOUS, None, f"{count} in-effect {kind} goals; need exactly one")
    return None


def close_day(day: date, *, snapshot: Optional[Sequence[Mapping[str, Any]]], config_dir: Path | str,
              now: datetime, is_trading_day: Optional[Callable[[date], bool]] = None) -> DayResult:
    """Decide one day's goal-progress output. Fail closed: no row whenever a condition is unmet.

    * Bad goal config: alert at once, no row (trigger b).
    * No snapshot on a trading day: no row, ``missed_snapshot`` alert (trigger a).
    * No snapshot on a weekend or holiday: nothing written, no alert.
    * Snapshot present: one progress row for the day.
    """
    config_alert = goal_config_alert(config_dir, now)
    if config_alert is not None:
        return DayResult(progress_rows=[], alerts=[Alert(config_alert.kind, day, config_alert.detail)])
    if snapshot is None:
        if _trading_day(day, is_trading_day):
            return DayResult(progress_rows=[], alerts=[Alert(
                ALERT_MISSED_SNAPSHOT, day, f"no account snapshot on trading day {day.isoformat()}")])
        return DayResult()
    return DayResult(progress_rows=[{"day": day, "positions": [dict(row) for row in snapshot]}])


# ---- Friday "Goal progress" section and INFO status (ADR tests 27-28) ------------------------

# The Friday Notion refresh runs at 5:00 PM ET (operator instruction, 2026-11-14); the Goal
# progress section rides that run's payload on Fridays only.
FRIDAY_REFRESH_TIME = time(17, 30)  # task 041d9d90 in config/scheduled_tasks.json (Fridays 5:30 PM ET)
GOAL_PROGRESS_SECTION = "Goal progress"
_FRIDAY = 4  # date.weekday()
# Under a year of window an annualized figure extrapolates noise (a 9-day 8% reads as several
# hundred percent a year), so the section shows the period return only and never the word.
_ANNUALIZE_MIN_DAYS = 365


def in_friday_refresh(day: date) -> bool:
    return day.weekday() == _FRIDAY


def friday_refresh_sections(day: date) -> list[str]:
    """Sections this module contributes to the refresh run on ``day``: Goal progress on Fridays."""
    return [GOAL_PROGRESS_SECTION] if in_friday_refresh(day) else []


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.2f}%"


def goal_progress_section(rows: Iterable[Mapping[str, Any]], *, today: date) -> dict[str, Any]:
    """The Friday section text. One line per goal; an annualized figure only from a full year on."""
    lines = []
    for row in rows:
        window = int(row["window_days"])
        line = f"{row['goal_id']}: period return {_pct(row['period_return'])} over {window} days"
        annualized = row.get("annualized_return")
        if window >= _ANNUALIZE_MIN_DAYS and annualized is not None:
            line += f", annualized {_pct(annualized)}"
        lines.append(line)
    if not lines:
        lines.append("no goal progress recorded yet")
    return {"title": GOAL_PROGRESS_SECTION, "day": today.isoformat(), "text": "\n".join(lines)}


def goal_status_info(config_dir: Path | str, now: datetime) -> Optional[dict[str, Any]]:
    """INFO (never an alert) for a goal set that is intentionally off: a paused status or an empty
    goal list. None when goals are active, and also when the config is bad -- that case is
    goal_config_alert's to raise, and reporting it twice would hide which channel owns it."""
    path = Path(config_dir) / dca_goals.GOALS_FILENAME
    if not path.exists():
        return {"kind": "goal_list_empty", "level": "info", "reason": "no goals file; no goals configured"}
    try:
        doc = dca_goals.parse_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    status = doc.get("status")
    if status != "active":
        return {"kind": "goal_inactive", "level": "info", "reason": f"goals.json status is {status!r}"}
    if isinstance(doc.get("goals"), list) and not doc["goals"]:
        return {"kind": "goal_list_empty", "level": "info", "reason": "no goals configured"}
    return None
