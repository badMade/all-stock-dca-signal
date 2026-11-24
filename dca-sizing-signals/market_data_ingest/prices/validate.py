"""Batch validation run before anything is staged or written.

Strict by operator decision (PLAN Q4): one bad row rejects the whole batch, and the error
names every offending row so the cause is visible in the run record.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .errors import ValidationError
from .models import CorporateAction, DailyBar, action_version_key

BAR_SOURCES = frozenset({"massive", "alpaca"})
ACTION_TYPES = frozenset({"forward_split", "reverse_split", "stock_dividend", "cash_dividend"})
SPLIT_TYPES = frozenset({"forward_split", "reverse_split", "stock_dividend"})
MAX_LISTED = 20
# A raw close-to-close move larger than this, unexplained by a known split-type action, is an
# anomaly (backtest protocol item 6). 50% catches every split of 2-for-1 or more and a
# misplaced decimal, while a real one-day move that large is rare enough that holding the batch
# for a person to look at costs little.
MAX_ABS_MOVE = 0.5


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
    # A datetime is a date subclass but would hash and key differently from the stored DATE.
    if not isinstance(action.ex_date, date) or isinstance(action.ex_date, datetime):
        out.append(f"{where}: ex_date is not a date")
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
    """Refuse the whole batch if any action is malformed."""
    problems = [p for a in actions for p in action_problems(a)]
    if problems:
        _raise("corporate actions", problems)


def split_ambiguous_actions(
    actions: Sequence[CorporateAction],
) -> tuple[list[CorporateAction], list[str]]:
    """The batch without ambiguous identities, and a description of each one held back.

    Call after ``validate_actions``. An exact repeat of an action (overlapping vendor pages) is
    kept once: before the versioned dedupe the insert's SELECT DISTINCT absorbed it, and it must
    not fail a run (PR #2282 review). Two DIFFERENT actions with one (source, action_version_key)
    are ambiguous -- two versions of one action at one as_of, or two id-less same-day actions of
    one type -- so the read macro would keep one by hash order and the dedupe would re-insert the
    other every run. Those identities are held back whole; everything else is returned in order.
    """
    by_key: dict[tuple[str, str], list[CorporateAction]] = {}
    for a in actions:
        group = by_key.setdefault((a.source, action_version_key(a)), [])
        if a not in group:
            group.append(a)
    kept = [group[0] for group in by_key.values() if len(group) == 1]
    held = [
        f"{group[0].symbol} {group[0].action_type} {group[0].ex_date}: {len(group)} different versions in one batch"
        for group in by_key.values()
        if len(group) > 1
    ]
    return kept, held


@dataclass(frozen=True, slots=True)
class BarAnomaly:
    """One raw close that moved more than the bound from the close before it, after known splits."""

    symbol: str
    session: date
    prior_close: float
    close: float
    move: float

    @property
    def reason(self) -> str:
        return (
            f"close {self.close} vs prior close {self.prior_close}: move {self.move:+.2%} beyond the bound "
            "with no known split-type action to explain it"
        )


def bar_anomalies(
    bars: Iterable[DailyBar],
    actions: Iterable[CorporateAction],
    *,
    previous_close: Mapping[str, float | tuple[date, float]] | None = None,
    max_abs_move: float = MAX_ABS_MOVE,
) -> list[BarAnomaly]:
    """Every bar whose close moved beyond ``max_abs_move`` from the session before it (backtest item 6).

    Call after ``validate_bars`` (prices are positive and finite). Bars are compared per symbol in
    session order; the first bar of a symbol is compared with ``previous_close[symbol]`` when given
    (the stored close preceding the batch), so a jump across two batches is caught. Pass it as
    ``(session, close)`` so splits between the two sessions are applied exactly; a bare close
    (session unknown) applies only a split whose ex-date is the first bar's own session, never an
    older one that the stored close may already sit after. A split-type
    action with ``prior_session < ex_date <= session`` scales the prior close by ``split_from /
    split_to`` before the move is measured, the same factor ``adjusted_bars`` applies. Cash
    dividends are ignored: none is large enough to matter against a 50% bound. Nothing is
    interpolated or dropped here; the caller decides what a non-empty result does.
    """
    splits: dict[str, list[CorporateAction]] = {}
    for a in actions:
        if a.action_type in SPLIT_TYPES:
            splits.setdefault(a.symbol, []).append(a)
    by_symbol: dict[str, list[DailyBar]] = {}
    for b in bars:
        by_symbol.setdefault(b.symbol, []).append(b)
    prev = dict(previous_close or {})
    out: list[BarAnomaly] = []
    for symbol in sorted(by_symbol):
        ordered = sorted(by_symbol[symbol], key=lambda b: b.session)
        prior: tuple[date, float] | None = None
        if symbol in prev:
            given = prev[symbol]
            # A bare close: treat it as the session just before the first bar, so only a split
            # going ex on the first bar's own session can apply.
            prior = given if isinstance(given, tuple) else (date.fromordinal(ordered[0].session.toordinal() - 1), given)
        for bar in ordered:
            if prior is not None:
                prior_session, prior_close = prior
                factor = 1.0
                for a in splits.get(symbol, ()):
                    if prior_session < a.ex_date <= bar.session:
                        factor *= a.split_from / a.split_to
                move = bar.close / (prior_close * factor) - 1
                if abs(move) > max_abs_move:
                    out.append(BarAnomaly(symbol, bar.session, prior_close, bar.close, move))
            prior = (bar.session, bar.close)
    return out
