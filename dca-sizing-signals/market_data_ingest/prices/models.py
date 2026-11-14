"""Immutable value types passed between providers, the store, and callers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One RAW (unadjusted) daily bar as a vendor delivered it."""

    source: str
    symbol: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trades: int | None = None


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """A split, reverse split, stock dividend, or cash dividend, stored as delivered.

    Split-type actions carry ``split_from -> split_to`` shares (NVDA's 10-for-1 is 1 -> 10, a
    1-for-20 reverse split is 20 -> 1, a 5% stock dividend is 100 -> 105 or 1 -> 1.05). Cash
    dividends carry ``cash_amount`` per pre-ex-date share.
    """

    source: str
    action_type: str
    symbol: str
    ex_date: date
    split_from: float | None = None
    split_to: float | None = None
    cash_amount: float | None = None
    currency: str | None = None
    declaration_date: date | None = None
    record_date: date | None = None
    pay_date: date | None = None
    historical_adjustment_factor: float | None = None
    vendor_id: str | None = None


# The version identity of one corporate action within its source: the vendor's own id when it
# sends one, else symbol|action_type|ex_date. The read macro (``actions_known_at``) partitions by
# ACTION_VERSION_SQL and the write-side dedupe compares action_version_key against it, so the two
# must stay the same expression (tests/prices/test_store_point_in_time.py checks they agree).
# A vendor id is prefixed with its kind (PR #2282 review): Massive serves splits and dividends
# from two endpoints with two id spaces, so a split and a dividend that happen to share an id
# must not become "two versions of one action".
ACTION_VERSION_SQL = (
    "CASE WHEN vendor_id IS NULL THEN symbol || '|' || action_type || '|' || CAST(ex_date AS VARCHAR) "
    "ELSE (CASE WHEN action_type = 'cash_dividend' THEN 'dividend' ELSE 'split' END) || ':' || vendor_id END"
)


def action_version_key(action: CorporateAction) -> str:
    """The Python twin of ACTION_VERSION_SQL (DuckDB casts a DATE to its ISO form)."""
    if action.vendor_id is not None:
        kind = "dividend" if action.action_type == "cash_dividend" else "split"
        return f"{kind}:{action.vendor_id}"
    return f"{action.symbol}|{action.action_type}|{action.ex_date.isoformat()}"


@dataclass(frozen=True, slots=True)
class Quote:
    """A last trade. ``ts`` is when it traded; ``as_of`` is when we fetched it."""

    symbol: str
    price: float
    ts: datetime
    feed: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class PriceOK:
    """A usable, already-recorded intraday price."""

    symbol: str
    price: float
    ts: datetime
    feed: str
    delayed: bool


@dataclass(frozen=True, slots=True)
class NoSuggestion:
    """The fail-closed answer: no price, so no suggestion. Deliberately has no price field."""

    symbol: str
    reason: str
    error_class: str


@dataclass(frozen=True, slots=True)
class AdjustedBar:
    """A bar as seen from ``as_of_date``: raw values plus view-computed adjustments.

    ``close_split_adjusted`` applies splits and stock dividends; ``close_adjusted`` also
    applies cash dividends (total-return basis) and is ``None`` when a dividend's prior close
    is unknown, rather than silently skipping that factor.
    """

    symbol: str
    session: date
    source: str
    close_raw: float
    close_split_adjusted: float
    close_adjusted: float | None
    volume_raw: float
    volume_split_adjusted: float


@dataclass(frozen=True, slots=True)
class SplicedBar:
    symbol: str
    session: date
    source: str
    close: float


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    symbol: str
    status: str
    sessions: int
    window_start: date | None
    window_end: date | None
    worst_close_diff_pct: float | None
    volume_flag: bool
    adj_close_worst_diff_pct: float | None
    splice_date: date | None
    splice_factor: float | None

    @property
    def close_status(self) -> str:
        return self.status


@dataclass(frozen=True, slots=True)
class JobResult:
    run_id: str
    status: str
    exit_code: int
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Per-file outcome of a legacy directory import. Failures are listed, never skipped."""

    imported: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed
