"""Batch validation run before anything is staged or written.

Strict by operator decision (PLAN Q4): one bad row rejects the whole batch, and the error
names every offending row so the cause is visible in the run record.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from .errors import ValidationError
from .models import CorporateAction, DailyBar, action_version_key

BAR_SOURCES = frozenset({"massive", "alpaca"})
ACTION_TYPES = frozenset({"forward_split", "reverse_split", "stock_dividend", "cash_dividend"})
SPLIT_TYPES = frozenset({"forward_split", "reverse_split", "stock_dividend"})
MAX_LISTED = 20


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _raise(kind: str, problems: Sequence[str]) -> None:
    shown = "; ".join(problems[:MAX_LISTED])
    more = f" (+{len(problems) - MAX_LISTED} more)" if len(problems) > MAX_LISTED else ""
    raise ValidationError(f"{len(problems)} invalid {kind}: {shown}{more}")


def bar_problems(bar: DailyBar) -> list[str]:
    where = f"{bar.symbol} {bar.session}"
    out: list[str] = []
    if bar.source not in BAR_SOURCES:
        out.append(f"{where}: unknown source {bar.source!r}")
    if not isinstance(bar.symbol, str) or not bar.symbol.strip():
        out.append(f"{where}: empty symbol")
    prices = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
    bad = [name for name, value in prices.items() if not _finite(value) or value <= 0]
    if bad:
        out.append(f"{where}: non-positive or non-finite {', '.join(bad)}")
        return out  # the ordering checks below are meaningless without valid prices
    if not _finite(bar.volume) or bar.volume < 0:
        out.append(f"{where}: negative or non-finite volume")
    if bar.low > bar.high:
        out.append(f"{where}: low above high")
    if bar.high < max(bar.open, bar.close):
        out.append(f"{where}: high below open/close")
    if bar.low > min(bar.open, bar.close):
        out.append(f"{where}: low above open/close")
    if bar.vwap is not None and (not _finite(bar.vwap) or bar.vwap <= 0):
        out.append(f"{where}: invalid vwap")
    if bar.trades is not None and (not isinstance(bar.trades, int) or bar.trades < 0):
        out.append(f"{where}: invalid trade count")
    return out


def validate_bars(bars: Iterable[DailyBar]) -> None:
    problems: list[str] = []
    seen: set[tuple[str, str, object]] = set()
    for bar in bars:
        problems.extend(bar_problems(bar))
        key = (bar.source, bar.symbol, bar.session)
        if key in seen:
            problems.append(f"{bar.symbol} {bar.session}: appears twice in one batch")
        seen.add(key)
    if problems:
        _raise("bars", problems)


def action_problems(action: CorporateAction) -> list[str]:
    where = f"{action.symbol} {action.action_type} {action.ex_date}"
    out: list[str] = []
    if action.action_type not in ACTION_TYPES:
        out.append(f"{where}: unknown action type")
    if not isinstance(action.symbol, str) or not action.symbol.strip():
        out.append(f"{where}: empty symbol")
    if action.action_type in SPLIT_TYPES:
        ratio = (action.split_from, action.split_to)
        if not all(_finite(v) and v is not None and v > 0 for v in ratio):
            out.append(f"{where}: split ratio must be two positive numbers")
    if action.action_type == "cash_dividend":
        amount = action.cash_amount
        if amount is None or not _finite(amount) or amount <= 0:
            out.append(f"{where}: cash amount must be positive")
    return out


def validate_actions(actions: Iterable[CorporateAction]) -> None:
    """Refuse the batch if any action is malformed or two share one version identity.

    Two actions with the same (source, action_version_key) in one batch are two versions of one
    action delivered at the same as_of: the read macro would keep one by hash order and the
    versioned dedupe would re-insert the other on every run (PR #2265 follow-up). A vendor that
    sends no id and two distinct same-day actions of one type lands here too, loudly, instead of
    one of them vanishing from the adjusted series.
    """
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for a in actions:
        problems.extend(action_problems(a))
        key = (a.source, action_version_key(a))
        if key in seen:
            problems.append(f"{a.symbol} {a.action_type} {a.ex_date}: two versions of one action in one batch")
        seen.add(key)
    if problems:
        _raise("corporate actions", problems)
