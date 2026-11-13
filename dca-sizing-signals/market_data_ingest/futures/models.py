"""Futures records and batch validation (docs/adr-overnight-futures-tracker.md, D5).

Strict, like market_data_ingest/prices/validate.py: one bad row rejects the whole batch, and
the error names every offending row by position and reason only (never vendor text).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from market_data_ingest.prices.errors import ValidationError
from market_data_ingest.prices.validate import MAX_LISTED

#: Roots the tracker uses (D4): the three E-minis and their micros. Anything else is a typo
#: or a vendor symbology change, and either should stop the batch rather than land quietly.
ROOTS = frozenset({"ES", "NQ", "YM", "MES", "MNQ", "MYM"})


@dataclass(frozen=True, slots=True)
class FuturesContract:
    contract_id: str
    root: str
    underlying_index: str
    expiry_utc: datetime
    multiplier: float
    source: str


@dataclass(frozen=True, slots=True)
class FuturesBar:
    """One 1-minute bar. ``bar_start_utc`` is the minute it opens; it closes one minute later."""

    contract_id: str
    bar_start_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    source: str


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _raise(kind: str, problems: Sequence[str]) -> None:
    shown = "; ".join(problems[:MAX_LISTED])
    more = f" (+{len(problems) - MAX_LISTED} more)" if len(problems) > MAX_LISTED else ""
    raise ValidationError(f"{len(problems)} invalid {kind}: {shown}{more}")


def validate_contracts(contracts: Sequence[FuturesContract]) -> None:
    problems: list[str] = []
    seen: dict[tuple[str, str], FuturesContract] = {}
    for i, c in enumerate(contracts):
        # Two different versions of one contract in ONE batch share as_of and ingested_at, so a
        # later read could only pick between them by hash order (PR #2264 review: multiplier 5
        # and 500 in one batch both landed and contract() returned 5). Refuse the batch instead.
        key = (c.source, c.contract_id)
        if key in seen and seen[key] != c:
            problems.append(f"#{i} conflicts with an earlier version of the same contract")
        seen.setdefault(key, c)
        if not (_text(c.contract_id) and _text(c.underlying_index) and _text(c.source)):
            problems.append(f"#{i} missing id, index or source")
        if c.root not in ROOTS:
            problems.append(f"#{i} unknown root")
        if not _aware(c.expiry_utc):
            problems.append(f"#{i} expiry is not timezone-aware")
        if not (_finite(c.multiplier) and c.multiplier > 0):
            problems.append(f"#{i} multiplier must be positive")
    if problems:
        _raise("contracts", problems)


def _bar_problems(i: int, b: FuturesBar, known_contracts: frozenset[tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    if not _text(b.source):
        problems.append(f"#{i} missing source")
    # Keyed by (source, contract_id) (PR #2265 review): a bar from one vendor against a contract
    # only another vendor stored would have no contract (or multiplier) under its own source.
    if (b.source, b.contract_id) not in known_contracts:
        problems.append(f"#{i} unknown contract for its source")
    if not _aware(b.bar_start_utc):
        problems.append(f"#{i} bar start is not timezone-aware")
    elif b.bar_start_utc.second or b.bar_start_utc.microsecond:
        problems.append(f"#{i} bar start is not on a minute boundary")
    prices = (b.open, b.high, b.low, b.close)
    if not all(_finite(p) and p > 0 for p in prices):
        problems.append(f"#{i} prices must be finite and positive")
    elif b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
        problems.append(f"#{i} high/low do not bracket open/close")
    if not (isinstance(b.volume, int) and not isinstance(b.volume, bool) and b.volume >= 0):
        problems.append(f"#{i} volume must be a non-negative integer")
    return problems


def validate_bars(bars: Sequence[FuturesBar], known_contracts: Iterable[tuple[str, str]]) -> None:
    """Refuse the batch unless every bar is valid and belongs to a known (source, contract_id)."""
    known = frozenset(known_contracts)
    problems: list[str] = []
    seen: dict[tuple[str, str, datetime], FuturesBar] = {}
    for i, b in enumerate(bars):
        problems.extend(_bar_problems(i, b, known))
        if _aware(b.bar_start_utc):
            # Keyed per source: two vendors' bars for one minute are separate series, not a
            # conflict (reads keep them apart too; see FuturesStore._LATEST_BARS).
            key = (b.source, b.contract_id, b.bar_start_utc)
            # Two different bars for one minute in ONE batch is ambiguous, not a correction:
            # a correction arrives in a later batch with a later as_of.
            if key in seen and seen[key] != b:
                problems.append(f"#{i} conflicts with an earlier bar for the same minute")
            seen.setdefault(key, b)
    if problems:
        _raise("futures bars", problems)
