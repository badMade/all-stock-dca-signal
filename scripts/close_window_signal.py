#!/usr/bin/env python3
"""Read-only close-window ``what_would_have_fired`` summary for the active watchlist.

JUL 30 2026 CLOSE-WINDOW SIGNAL COMPARISON PATCH. During the close-of-day
validator pass (3:35 post-trade, 3:40 watchdog) the operator wants to see what
the day's signal actually looked like, independent of whether anything executed:
which watchlist tickers would have fired in background scheduled mode, and which
would have fired in foreground primary mode. On a foreground primary day the
background stream produces no artifact at all, so without this summary there is
nothing to compare the foreground execution against.

ANALYTICS ONLY. Nothing here places, reviews, cancels, sells, or modifies an
order, and nothing here decides a budget. It maps a price pair onto the graded
sizing ladder and labels the outcome.

No network either: this module is pure. The caller fetches quotes under the
repo's existing market-data provider policy (Finance connector primary, Alpaca
and Robinhood validation/context, yfinance fallback only when needed) and hands
each ticker's ``decision_price``, ``prior_close`` and ``data_source`` in. Keeping
the fetch outside means the summary is deterministic, testable without a
connector, and cannot become a second, drifting copy of the provider policy.

The reported multiplier is capped at 3x. The live ladder
(``config/sizing_ladder.json``) runs to 5x, but this summary reports 0x through
3x buckets by request; anything the ladder grades above 3x is reported as 3.0
with ``ladder_multiplier_label='3x_plus'`` so the cap is visible rather than
silently lossy. The cap is a REPORTING cap only. It never touches execution
sizing, which continues to use the full ladder via
``near_close_dca.graded_buy_multiplier``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Reporting cap requested for the close-window summary. Not an execution cap.
REPORTED_MULTIPLIER_CAP = 3.0
LABEL_CAPPED = "3x_plus"

# Per-ticker mode verdicts.
STATUS_WOULD_HAVE_FIRED = "would_have_fired"
STATUS_NO_SIGNAL = "no_signal"
STATUS_UNKNOWN_MISSING_DATA = "unknown_missing_data"
# Signal present, but the mode in question was not the one operating today.
STATUS_SIGNAL_MODE_NOT_ACTIVE = "would_have_fired_mode_not_active"

# reason_if_not_fired codes.
REASON_MISSING_DECISION_PRICE = "missing_or_invalid_decision_price"
REASON_MISSING_PRIOR_CLOSE = "missing_or_invalid_prior_close"
REASON_NOT_BELOW_PRIOR_CLOSE = "decision_price_not_below_prior_close"
REASON_DROP_BELOW_FIRST_TIER = "drop_below_first_ladder_tier"
REASON_TREND_NOT_DOWN = "sixty_minute_trend_not_down"


def _positive_float(value: Any) -> Optional[float]:
    """Return a finite positive float, or None for anything unusable.

    A zero or negative price is rejected rather than carried: it produces a
    nonsense drop percentage, and a summary row that looks like a 100% drop is
    worse than one that says the data was missing.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def reported_multiplier(raw_multiplier: float) -> Tuple[float, Optional[str]]:
    """Cap a ladder multiplier for reporting and label it when the cap bit.

    Returns ``(capped_multiplier, label)`` where ``label`` is
    :data:`LABEL_CAPPED` only when the source ladder graded the drop above the
    cap, and None otherwise.
    """
    if raw_multiplier > REPORTED_MULTIPLIER_CAP:
        return (REPORTED_MULTIPLIER_CAP, LABEL_CAPPED)
    return (float(raw_multiplier), None)


def ladder_multiplier_for_drop(drop_fraction: float, tiers: Sequence[Tuple[float, float]]) -> float:
    """Uncapped ladder multiplier for a drop fraction (0.02 == 2.0% down).

    Mirrors ``near_close_dca.graded_buy_multiplier``'s tier selection (largest
    threshold less than or equal to the drop wins, below the first tier is 0x),
    without its risk-guard and execution concerns: this is a summary of the
    ladder shape, not a sizing decision.
    """
    multiplier = 0.0
    for threshold, tier_multiplier in sorted(tiers):
        if drop_fraction >= threshold:
            multiplier = float(tier_multiplier)
    return multiplier


@dataclass
class CloseWindowRow:
    """One watchlist ticker's close-window what-would-have-fired verdict."""

    ticker: str
    decision_price: Optional[float]
    prior_close: Optional[float]
    drop_pct: Optional[float]
    ladder_multiplier_capped_at_3x: float
    background_mode_status: str
    foreground_mode_status: str
    reason_if_not_fired: Optional[str]
    data_source: Optional[str]
    ladder_multiplier_label: Optional[str] = None
    lookback_price: Optional[float] = None


def build_close_window_row(
    ticker: str,
    quote: Mapping[str, Any],
    *,
    tiers: Sequence[Tuple[float, float]],
    background_mode_active: bool,
    foreground_primary_mode_active: bool,
) -> CloseWindowRow:
    """Grade one ticker. See :func:`build_close_window_summary`.

    ``quote`` may optionally carry ``lookback_price`` (the 60-minute intraday
    lookback bar close). When present, a signal is graded ``would_have_fired``
    only when ``decision_price < lookback_price`` too, mirroring the live
    signal path's trend gate (``near_close_dca.graded_buy_multiplier``'s
    ``intraday_trend_down`` check) so the summary does not overstate what
    would actually have fired. ``lookback_price`` is optional and omitted by
    every existing caller today; when absent the trend gate is not applied,
    preserving prior behavior exactly (Codex review: "Include the trend gate
    in fired verdicts").
    """
    decision_price = _positive_float(quote.get("decision_price"))
    prior_close = _positive_float(quote.get("prior_close"))
    lookback_price = _positive_float(quote.get("lookback_price"))
    data_source = quote.get("data_source")
    data_source = data_source if isinstance(data_source, str) and data_source else None

    if decision_price is None or prior_close is None:
        reason = REASON_MISSING_DECISION_PRICE if decision_price is None else REASON_MISSING_PRIOR_CLOSE
        return CloseWindowRow(
            ticker=ticker,
            decision_price=decision_price,
            prior_close=prior_close,
            drop_pct=None,
            ladder_multiplier_capped_at_3x=0.0,
            background_mode_status=STATUS_UNKNOWN_MISSING_DATA,
            foreground_mode_status=STATUS_UNKNOWN_MISSING_DATA,
            reason_if_not_fired=reason,
            data_source=data_source,
            lookback_price=lookback_price,
        )

    drop_fraction = (prior_close - decision_price) / prior_close
    drop_pct = round(drop_fraction * 100.0, 4)
    raw_multiplier = ladder_multiplier_for_drop(drop_fraction, tiers)
    capped, label = reported_multiplier(raw_multiplier)

    reason: Optional[str] = None
    if decision_price >= prior_close:
        reason = REASON_NOT_BELOW_PRIOR_CLOSE
    elif raw_multiplier <= 0:
        reason = REASON_DROP_BELOW_FIRST_TIER
    elif lookback_price is not None and decision_price >= lookback_price:
        reason = REASON_TREND_NOT_DOWN

    if reason is not None:
        background_status = STATUS_NO_SIGNAL
        foreground_status = STATUS_NO_SIGNAL
    else:
        background_status = (
            STATUS_WOULD_HAVE_FIRED if background_mode_active else STATUS_SIGNAL_MODE_NOT_ACTIVE
        )
        foreground_status = (
            STATUS_WOULD_HAVE_FIRED if foreground_primary_mode_active else STATUS_SIGNAL_MODE_NOT_ACTIVE
        )

    return CloseWindowRow(
        ticker=ticker,
        decision_price=decision_price,
        prior_close=prior_close,
        drop_pct=drop_pct,
        ladder_multiplier_capped_at_3x=capped,
        background_mode_status=background_status,
        foreground_mode_status=foreground_status,
        reason_if_not_fired=reason,
        data_source=data_source,
        ladder_multiplier_label=label,
        lookback_price=lookback_price,
    )


def build_close_window_summary(
    quotes: Mapping[str, Mapping[str, Any]],
    *,
    tiers: Sequence[Tuple[float, float]],
    trading_day: str,
    watchlist: Optional[Iterable[str]] = None,
    background_mode_active: bool = True,
    foreground_primary_mode_active: bool = False,
) -> Dict[str, Any]:
    """Build the read-only ``what_would_have_fired`` summary for the watchlist.

    ``quotes`` maps ticker to ``{'decision_price': float, 'prior_close': float,
    'data_source': str, 'lookback_price': float (optional)}``, where
    ``decision_price`` is the current market-close decision price and
    ``prior_close`` is the prior COMPLETED regular-session close. ``tiers`` is
    the graded ladder as ``(drop_fraction, multiplier)`` pairs, i.e. exactly
    ``StrategyConfig.sizing_tiers`` (see ``dca_config.load_sizing_tiers``).
    ``lookback_price`` is the optional 60-minute intraday lookback bar close;
    when a caller supplies it, a ticker fires only when ``decision_price`` is
    also below it, mirroring the live signal path's trend gate
    (``near_close_dca.graded_buy_multiplier``). Omitting it (every existing
    caller today) does not apply the trend gate, so the summary can overstate
    would-have-fired signals relative to the live path in that case -- see
    ``REASON_TREND_NOT_DOWN``.

    ``watchlist`` pins the tickers and their order; when omitted the keys of
    ``quotes`` are used, sorted. A watchlist ticker with no quote is still
    reported, as an ``unknown_missing_data`` row: a ticker silently dropped
    because its provider failed is indistinguishable from one that had no
    signal, and that is the ambiguity this summary exists to remove.

    ``background_mode_active`` is False on a day where the background scheduled
    stream did not produce valid evidence, so a fired signal is reported as
    ``would_have_fired_mode_not_active`` for that mode. Comparing the two mode
    columns is the point of the summary on a foreground primary day.

    Analytics only. Returns a plain dict; writes nothing and trades nothing.
    """
    tickers: List[str] = list(watchlist) if watchlist is not None else sorted(quotes)
    rows = [
        build_close_window_row(
            ticker,
            quotes.get(ticker) or {},
            tiers=tiers,
            background_mode_active=background_mode_active,
            foreground_primary_mode_active=foreground_primary_mode_active,
        )
        for ticker in tickers
    ]

    fired = [row for row in rows if row.reason_if_not_fired is None]
    return {
        "summary_type": "what_would_have_fired",
        "trading_day": trading_day,
        "analytics_only": True,
        "places_reviews_cancels_sells_or_modifies_orders": False,
        "foreground_primary_mode_active": foreground_primary_mode_active,
        "background_mode_active": background_mode_active,
        "reported_multiplier_cap": REPORTED_MULTIPLIER_CAP,
        "reported_multiplier_cap_note": (
            "Reporting cap only. Execution sizing continues to use the full "
            "config/sizing_ladder.json ladder; a drop the ladder grades above "
            f"{REPORTED_MULTIPLIER_CAP:g}x is reported as {REPORTED_MULTIPLIER_CAP:g} "
            f"with ladder_multiplier_label='{LABEL_CAPPED}'."
        ),
        "watchlist_count": len(rows),
        "would_have_fired_count": len(fired),
        "would_have_fired_tickers": [row.ticker for row in fired],
        "rows": [asdict(row) for row in rows],
    }


SUMMARY_CSV_FIELDS = (
    "ticker",
    "decision_price",
    "prior_close",
    "lookback_price",
    "drop_pct",
    "ladder_multiplier_capped_at_3x",
    "ladder_multiplier_label",
    "background_mode_status",
    "foreground_mode_status",
    "reason_if_not_fired",
    "data_source",
)


__all__ = [
    "REPORTED_MULTIPLIER_CAP",
    "LABEL_CAPPED",
    "STATUS_WOULD_HAVE_FIRED",
    "STATUS_NO_SIGNAL",
    "STATUS_UNKNOWN_MISSING_DATA",
    "STATUS_SIGNAL_MODE_NOT_ACTIVE",
    "REASON_MISSING_DECISION_PRICE",
    "REASON_MISSING_PRIOR_CLOSE",
    "REASON_NOT_BELOW_PRIOR_CLOSE",
    "REASON_DROP_BELOW_FIRST_TIER",
    "REASON_TREND_NOT_DOWN",
    "SUMMARY_CSV_FIELDS",
    "CloseWindowRow",
    "reported_multiplier",
    "ladder_multiplier_for_drop",
    "build_close_window_row",
    "build_close_window_summary",
]
