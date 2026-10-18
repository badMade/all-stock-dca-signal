"""Reconcile Massive against yahoo_legacy, and guard any splice between the two.

Comparison basis: Yahoo ``Close`` is split-adjusted as of its download, which we take to be the
file's last session. Massive bars are therefore read through ``adjusted_bars`` as of that same
date, so both sides sit on one split basis. Yahoo volume is split-adjusted the same way.

Tiers per session, on ``|massive - yahoo| / yahoo``: <= 0.1% pass, <= 1% flag, else block.
A symbol takes its worst session (fail closed: one bad print is worth looking at). Volume off
by more than 10% sets ``volume_flag`` and never blocks on its own. Adj Close is reported for
information only, because Yahoo's dividend adjustment is not an exact contract.
"""

from __future__ import annotations

from datetime import date, datetime

from .errors import SpliceNotValidatedError
from .models import ReconciliationResult, SplicedBar
from .store import PriceStore

PASS_MAX = 0.001
FLAG_MAX = 0.01
VOLUME_TOLERANCE = 0.10
MIN_SESSIONS = 60
MAX_SESSIONS = 250
_TIER_RANK = {"pass": 0, "flag": 1, "block": 2}


def classify_close_diff(diff: float) -> str:
    if diff <= PASS_MAX:
        return "pass"
    if diff <= FLAG_MAX:
        return "flag"
    return "block"


def _rel(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b)


def reconcile_symbol(
    store: PriceStore,
    symbol: str,
    *,
    run_id: str,
    now: datetime,
    max_sessions: int = MAX_SESSIONS,
    min_sessions: int = MIN_SESSIONS,
) -> ReconciliationResult:
    yahoo = {r["session"]: r for r in store.yahoo_legacy_bars(symbol)}
    result: ReconciliationResult
    if not yahoo:
        result = ReconciliationResult(symbol, "insufficient", 0, None, None, None, False, None, None, None)
        store.insert_reconciliation(result, run_id=run_id, as_of=now)
        return result
    reference_date = max(yahoo)
    massive = {b.session: b for b in store.adjusted_bars(symbol, as_of_date=reference_date) if b.source == "massive"}
    window = sorted(set(yahoo) & set(massive))[-max_sessions:]
    if len(window) < min_sessions:
        result = ReconciliationResult(
            symbol,
            "insufficient",
            len(window),
            window[0] if window else None,
            window[-1] if window else None,
            None,
            False,
            None,
            None,
            None,
        )
        store.insert_reconciliation(result, run_id=run_id, as_of=now)
        return result

    worst_tier, worst_close, worst_adj, volume_flag = "pass", 0.0, None, False
    for session in window:
        m, y = massive[session], yahoo[session]
        diff = _rel(m.close_split_adjusted, y["close"])
        tier = "block" if diff is None else classify_close_diff(diff)
        if _TIER_RANK[tier] > _TIER_RANK[worst_tier]:
            worst_tier = tier
        worst_close = max(worst_close, diff if diff is not None else float("inf"))
        adj = _rel(m.close_adjusted, y["adj_close"])
        if adj is not None:
            worst_adj = adj if worst_adj is None else max(worst_adj, adj)
        vol = _rel(m.volume_split_adjusted, y["volume"])
        if (vol is None and m.volume_split_adjusted != y["volume"]) or (vol is not None and vol > VOLUME_TOLERANCE):
            volume_flag = True

    end = window[-1]
    result = ReconciliationResult(
        symbol=symbol,
        status=worst_tier,
        sessions=len(window),
        window_start=window[0],
        window_end=end,
        worst_close_diff_pct=worst_close * 100,
        volume_flag=volume_flag,
        adj_close_worst_diff_pct=None if worst_adj is None else worst_adj * 100,
        splice_date=end,
        splice_factor=yahoo[end]["close"] / massive[end].close_split_adjusted,
    )
    store.insert_reconciliation(result, run_id=run_id, as_of=now)
    return result


def spliced_adjusted_series(store: PriceStore, symbol: str, splice_date: date, *, as_of_date: date) -> list[SplicedBar]:
    """yahoo_legacy before ``splice_date``, vendor bars from it, on one split basis.

    Refused unless the latest reconciliation for ``symbol`` passed, ended exactly at
    ``splice_date``, and recorded a splice factor. Yahoo closes are scaled by that factor and
    by any splits between the Yahoo file's last session and ``as_of_date``.
    """
    results = store.reconciliation_results(symbol)
    latest = results[-1] if results else None
    if (
        latest is None
        or latest["status"] != "pass"
        or latest["window_end"] != splice_date
        or not latest["splice_factor"]
    ):
        state = "no reconciliation" if latest is None else f"latest is {latest['status']} ending {latest['window_end']}"
        raise SpliceNotValidatedError(f"{symbol}: no passing reconciliation ending {splice_date} ({state})")
    yahoo = store.yahoo_legacy_bars(symbol)
    reference_date = max(r["session"] for r in yahoo)
    later_splits = store.split_price_factor(symbol, after=reference_date, through=as_of_date)
    scale = later_splits / latest["splice_factor"]
    series = [
        SplicedBar(symbol, r["session"], "yahoo_legacy", r["close"] * scale)
        for r in yahoo
        if r["session"] < splice_date
    ]
    series += [
        SplicedBar(symbol, b.session, b.source, b.close_split_adjusted)
        for b in store.adjusted_bars(symbol, as_of_date=as_of_date)
        if b.session >= splice_date
    ]
    return series
