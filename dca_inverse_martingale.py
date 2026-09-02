"""Opt-in deferred-accumulation ("inverse Martingale") sizing mode.

The normal graded ladder (``near_close_dca.graded_buy_multiplier``) buys
progressively *into* a decline: each newly crossed drop threshold places its
own order immediately, so a deep, sustained decline places more money at
worse and worse prices with no upper bound short of the daily/weekly budget
caps.

This module implements the opposite policy, entirely opt-in and off by
default (see ``config/inverse_martingale.json``): as price falls through a
ticker's configured tier thresholds, no order is placed. Instead the dollar
amount each tier *would* have bought is tracked ("accumulated"). Only once
price has bottomed and recovered by a configured offset does the accumulated
total fire as a real order — capped at one order per ticker per run (see
``near_close_dca.py``'s injection site for why), with any remainder carried
forward to a later run. If price never recovers to the offset, nothing is
ever bought for that descent, and the cycle is abandoned after
``max_cycle_days``/``max_fire_attempts``.

Deliberately mirrors :mod:`dca_cumulative`'s split between pure decision math
(:func:`evaluate_inverse_martingale_step`, no I/O) and a locked, atomically
written per-ticker ledger (:class:`InverseMartingaleLedger`). Unlike
``CumulativeCreditLedger``, which is scoped per trading day, this ledger is
scoped per ticker: a dip-to-recovery cycle must survive across day
boundaries, so trading-day isolation would silently reset an in-progress
cycle at midnight.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from dca_ticker_paths import configured_ticker_history_dir
from scripts.file_lock import cross_process_lock

STATUS_NONE = "none"
STATUS_DIPPING = "dipping"
STATUS_FIRING = "firing"


class CorruptedLedgerStateError(RuntimeError):
    """A ticker's persisted tracker.json exists but cannot be trusted.

    Raised by :meth:`InverseMartingaleLedger._read_locked` for a truncated/
    malformed file or an unsupported ledger version -- deliberately NOT
    folded into the same ``{}`` (no cycle yet) result a genuinely absent
    file returns. Treating "present but unreadable" the same as "never
    started" would let evaluation start a brand-new cycle from that {}, and
    its own compare-and-swap write would then overwrite the damaged file --
    permanently discarding whatever dollar balance it held, with no way to
    recover it (Codex review: "Refuse unreadable accumulator state instead
    of resetting it"). Callers must catch this per ticker and skip that
    ticker's evaluation for the run, leaving the file untouched for manual
    recovery, rather than silently proceeding.
    """

BASIS_PCT = "pct"
BASIS_STDDEV = "stddev"

_LEDGER_VERSION = 1

# Mirrors schemas/tickers.schema.json's own "active" item pattern -- the
# single source of truth for what a valid ticker symbol looks like in this
# repo (see ml-forecaster/train.py's identical _TICKER_SYMBOL_PATTERN for
# the same reuse). InverseMartingaleLedger._paths() below joins this
# UNVALIDATED against self.root to build a real filesystem path; a ticker
# containing path separators or a leading "/" (from a malformed
# inverse_martingale.json config key, or an alternate DCA_CONFIG_DIR's own
# unvalidated tickers.json -- load_tickers() casts to str but does not
# itself enforce this pattern) could otherwise escape stream_root(
# "inverse_martingale_dca") entirely and create/overwrite a file anywhere
# claim_fire()/record_step() is called with it (Codex review: "Constrain
# ledger paths to valid ticker symbols").
_TICKER_SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,6}$")

# Retention cap on InverseMartingaleLedger's persisted per-ticker audit
# trail (see _write_locked) -- see that method's own comment on why an
# unbounded list is unsafe for a ticker enrolled for a long time. At most 3
# scheduled evaluations/day (market-open, midday, final), 50 events covers
# roughly two to three trading weeks of history -- comfortably enough to
# debug a live cycle without growing the file forever.
_MAX_PERSISTED_EVENTS = 50

# Brokers reject sub-dollar notional orders. A schema-valid unit_usd below
# $1.00 combined with a small first tier can otherwise accumulate a
# sub-dollar balance that recovers and "fires" -- floored here, at the
# single source of truth for fire_amount_usd, not only at the candidate
# construction site in near_close_dca.py's injection loop. Flooring ONLY at
# the candidate level (the earlier fix) left the step's own fire_amount_usd
# -- what the ledger actually claims/settles against -- understated, so
# settlement would see more ACTUALLY placed than was ever claimed from the
# ledger and silently absorb the difference as a "resolved" cycle, having
# spent real money the accumulator never tracked (Codex review: "Keep
# sub-dollar fires in the accumulator").
_BROKER_MINIMUM_ORDER_USD = 1.0

#: Recorded as a live lease's ``settlement_account`` when the account identity
#: could not be computed at claim time -- the key was unwritable, or the adapter
#: refused the probe. Distinguishing that from "no key present at all" matters:
#: an absent key means a LEGACY lease predating the field (nothing to
#: contradict, so recovery may still release it), while this sentinel means a
#: live claim whose account is unknown, which must never be released on the
#: strength of some other account's empty history (Codex review: "Refuse live
#: claims without an account fingerprint"). No real fingerprint can collide with
#: it -- those are lowercase hex.
#:
#: Defined HERE rather than in near_close_dca (which writes it) so this module's
#: payload validator and that writer share one definition of the contract; a
#: validator that accepted a shape the writer never produces is how a damaged
#: tracker froze silently instead of being reported.
_LEASE_ACCOUNT_UNKNOWN = "unknown"

#: Characters of the HMAC-SHA256 hex digest kept as the account fingerprint.
#: near_close_dca._broker_account_fingerprint slices to exactly this, and
#: :func:`_is_valid_lease_account` accepts exactly this.
_LEASE_ACCOUNT_FINGERPRINT_LENGTH = 32

_LEASE_ACCOUNT_HEX_CHARS = frozenset("0123456789abcdef")


def _is_valid_lease_account(value: Any) -> bool:
    """True for the only two shapes a claim can ever write into
    ``settlement_account``: the ``"unknown"`` sentinel, or a lowercase hex HMAC
    fingerprint of exactly :data:`_LEASE_ACCOUNT_FINGERPRINT_LENGTH` characters.

    Anything else -- a truncated fingerprint, an uppercase copy, a hand-written
    note -- can never equal a freshly computed fingerprint, so recovery would
    withhold reconciliation forever and freeze the ticker's accumulator rather
    than guard it.
    """
    if not isinstance(value, str):
        return False
    if value == _LEASE_ACCOUNT_UNKNOWN:
        return True
    return (
        len(value) == _LEASE_ACCOUNT_FINGERPRINT_LENGTH
        and set(value) <= _LEASE_ACCOUNT_HEX_CHARS
    )


@dataclass(frozen=True)
class InverseMartingaleStep:
    """One evaluation's outcome: the decision made and why.

    ``fired`` is true exactly when this step produced a chunk to place this
    run (``fire_amount_usd`` > 0, normally <= the caller-supplied per-order
    cap). A cycle whose accumulated total exceeds that cap fires only the
    capped portion; the remainder stays tracked (``status_after ==
    "firing"``) for a later run to continue. ``fire_amount_usd`` is also
    floored to ``_BROKER_MINIMUM_ORDER_USD`` -- in the pathological case of a
    ``per_order_cap_usd`` configured below that floor, the floor wins, since
    a broker-rejected sub-dollar order is a harder constraint than a
    self-imposed spending cap -- but never above the balance the cycle has
    actually accumulated. When that balance is ITSELF below the minimum the
    step does not fire at all (``reason`` ends ``_below_broker_minimum``) and
    the balance is held for a later run, because placing more than was tracked
    would spend money the accumulator never deferred.
    """

    status_before: str
    status_after: str
    accumulated_usd_before: float
    accumulated_usd_after: float
    newly_crossed_tiers: tuple[float, ...]
    fired: bool
    fire_amount_usd: float
    reason: str
    #: Optional claim attribution for a crash-lease recovery step -- the
    #: original claim's trading day/stream/ref/order id, distinct from the
    #: fields above (which describe THIS reconciling evaluation, not the
    #: claim it is resolving). ``_write_locked`` folds this into the
    #: persisted event dict when present, so the attribution survives in the
    #: ledger's append-only audit trail even once a LATER transition
    #: replaces the tracker's top-level state via ``resolved_marker()``
    #: (which carries no such field and drops whatever the top level had) --
    #: see near_close_dca.py's recovery injection site, which is the only
    #: place this is populated (Codex review: "Persist recovery attribution
    #: in the audit event").
    recovery_metadata: Optional[dict[str, Any]] = None


def _sorted_tiers(tier_config: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    return tuple(sorted(tier_config))


def _drop_metric_to_price(
    reference_close: float, drop_metric: float, basis: str, stddev_reference: Optional[float]
) -> Optional[float]:
    """Invert a ``drop_metric`` (see ``evaluate_inverse_martingale_step``'s
    own ``drop_pct``/``drop_metric`` computation) back into the absolute
    price it corresponds to, given the exact reference frame -- the frozen
    ``reference_close``, and for ``"stddev"`` basis, the frozen
    ``stddev_reference`` -- it was computed against.

    Returns ``None`` (fail closed) for a stddev-basis cycle with no usable
    ``stddev_reference``, or a non-finite/nonpositive ``reference_close``.
    """
    if not math.isfinite(reference_close) or reference_close <= 0:
        return None
    if basis == BASIS_STDDEV:
        if not stddev_reference or not math.isfinite(stddev_reference) or stddev_reference <= 0:
            return None
        drop_pct = drop_metric * stddev_reference
    else:
        drop_pct = drop_metric / 100.0
    price = reference_close * (1.0 - drop_pct)
    return price if math.isfinite(price) and price > 0 else None


def _preserved_latch_kwargs(
    tracker_state: dict[str, Any],
    persisted_basis: Optional[str],
    status_before: str,
    *,
    carry_drop_metric: bool,
) -> dict[str, Any]:
    """The durable-latch fields a MID-CYCLE CONFIG RESET must carry into its
    ``resolved_marker``, rebuilt from persisted state alone.

    The two config-change resets (basis, and the tier table / ``unit_usd``
    fingerprint) run before this call's own ``drop_metric`` and
    ``reference_close`` exist, so unlike the abandonment path further down they
    have only what is already on disk to work from.

    Applies to a ``STATUS_FIRING`` cycle ONLY, and returns nothing otherwise.
    That is the one status whose reset can discard money already spent: a
    ``per_order_cap_usd``-capped continuation can have placed one or more real,
    settled chunks over earlier runs, and writing a latch-free marker let a
    later stream or trading day open a brand-new cycle while price was still
    inside the original descent and credit the edited tiers again, duplicating
    exposure those earlier fills already covered (Codex review: "Preserve the
    paid-fire latch across tier configuration resets"). A ``STATUS_DIPPING``
    cycle has banked tier credit but placed nothing, so it keeps resetting
    cleanly -- latching it would strand the ticker behind a reference price the
    operator's own edit was meant to move past, which the abandonment path can
    afford to do (its cutoffs mean the cycle is over regardless) and a
    deliberate config change cannot.

    ``fired_floor_price`` is an absolute price, so it survives a basis change
    intact as long as the metric is inverted through the basis it was RECORDED
    under. ``fired_floor_drop_metric`` does not -- it is denominated in the old
    basis's units, and re-persisting it across a basis change would hand the
    next evaluation a sigma-valued floor to compare percentage points against.
    That is the very misreading the basis reset exists to prevent, so
    ``carry_drop_metric`` is False there and the absolute price carries the
    floor alone.
    """
    if status_before != STATUS_FIRING:
        return {}
    reference_close = tracker_state.get("cycle_reference_close")
    if not isinstance(reference_close, (int, float)) or not math.isfinite(reference_close) or reference_close <= 0:
        return {}
    reference_close = float(reference_close)
    preserved: dict[str, Any] = {"rearm_reference_close": reference_close}
    lowest_drop_seen = tracker_state.get("lowest_drop_seen")
    if isinstance(lowest_drop_seen, (int, float)) and math.isfinite(lowest_drop_seen):
        floor_price = _drop_metric_to_price(
            reference_close,
            float(lowest_drop_seen),
            persisted_basis or BASIS_PCT,
            tracker_state.get("stddev_reference"),
        )
        if floor_price is not None:
            preserved["fired_floor_price"] = floor_price
        if carry_drop_metric:
            preserved["fired_floor_drop_metric"] = float(lowest_drop_seen)
    return preserved


def rearm_cursor_seed(reference_day_iso: Optional[str], trading_day: date) -> str:
    """One calendar day before ``reference_day_iso`` (or before ``trading_day``
    when absent), for seeding a freshly (re)armed durable latch's
    ``rearm_last_seen_day``.

    Every ``resolved_marker()`` call site below persists ``rearm_reference_close``
    alongside a cursor for the FIRST time this particular latch instance exists --
    no live corporate-action lookup has ever examined anything under this
    reference scale yet, regardless of how many days the cycle that just
    resolved was itself active. ``FinanceDataProvider.get_corporate_action_factor``'s
    ``since`` boundary is exclusive, so seeding the cursor directly at the
    reference day (typically ``cycle_start_trading_day``, the day
    ``rearm_reference_close``/``cycle_reference_close`` was actually captured
    on) would exclude a split or dividend effective ON that very day from the
    latch's first live search -- exactly the gap a same-day forward split
    could hide behind indefinitely (Codex review: "Include the cycle-start
    day in the live lookup", "Seed the latch cursor before a split-day cycle
    starts"). Pulling the seed back one extra day only WIDENS the very first
    search's window; it can never cause a split already correctly caught by
    the un-adjusted cursor to be missed. Once a live search actually examines
    a range, ``evaluate_inverse_martingale_step`` persists ITS OWN returned
    cursor directly (see the ``split_cursor_day`` assignment below), not
    through this helper -- only the untouched-since-arming seed needs the
    day pulled back.

    ``reference_day_iso`` comes from persisted ledger state, and ledger
    validation accepts any value ``date.fromisoformat`` parses -- including
    ``date.min`` (``"0001-01-01"``), a schema-valid but degenerate date a
    restored/corrupted tracker could carry. Subtracting a day from THAT would
    raise ``OverflowError`` and abort the whole scheduled run over one
    ticker's damaged state, rather than degrading gracefully like every other
    guard in this module (Codex review: "Guard the lower date boundary when
    seeding cursors"). Fall back to the reference day itself in that case --
    one day narrower than the normal seed, but still far short of excluding
    any action a real deployment could ever see, since no genuine cycle
    starts at the literal minimum representable date.
    """
    reference_day = date.fromisoformat(reference_day_iso) if reference_day_iso else trading_day
    try:
        return (reference_day - timedelta(days=1)).isoformat()
    except OverflowError:
        return reference_day.isoformat()


def resolved_marker(
    trading_day: date,
    stream: str,
    *,
    fired_floor_drop_metric: Optional[float] = None,
    fired_floor_price: Optional[float] = None,
    rearm_reference_close: Optional[float] = None,
    rearm_last_seen_day: Optional[str] = None,
) -> dict[str, Any]:
    """The state persisted when a cycle fully resolves (fires completely) or
    is abandoned (max_cycle_days/max_fire_attempts).

    Deliberately NOT an empty dict: ``last_evaluated_trading_day`` is a
    retry-idempotency marker, read by the ``status == "none"`` guard at the
    top of :func:`evaluate_inverse_martingale_step` (Codex review: "Require a
    fresh dip before rearming a fired cycle"). No OTHER per-cycle field
    (crossed_tier_thresholds, cycle_reference_close, stddev_reference,
    fire_attempts) is carried forward -- a genuinely new cycle starting on a
    later day must not inherit the DOLLAR/TIER state of the one that just
    resolved. ``rearm_reference_close`` (below) is the sole, deliberate
    exception: it exists precisely to durably block a later cycle from
    starting at all until price exits the paid-for dip, so unlike every
    other field here it MUST survive the resolve.

    ``last_evaluated_stream`` is also persisted, alongside the date, so that
    same-day guard can tell a genuine RETRY of the run that produced this
    marker apart from a later, independent scheduled slot the same day. The
    canonical path evaluates every enrolled ticker up to three times on one
    trading day (the 1030ET/1230ET/1530ET slots -- see run_dca.SLOT_MAP),
    each stamping its own ``stream`` ("morning"/"midday"/"afternoon") onto
    RunContext. A date-only marker cannot distinguish "1030ET retried" from
    "1230ET legitimately ran after 1030ET resolved/abandoned this cycle
    earlier today" -- both look identical -- so without the stream, the
    guard that exists to stop the FIRST case was also silently swallowing
    the second: a fresh dip a later slot would otherwise have started
    tracking from scratch was instead discarded with no credit ever given,
    since the next trading day starts from a new reference close (Codex
    review: "Distinguish later slots from retries before suppressing
    rearming").

    ``fired_floor_drop_metric``, passed by the two fire-resolution call
    sites AND the max_cycle_days/max_fire_attempts abandonment reset (never
    by a disabled-enrollment/basis-change/tier-config-change reset, none of
    which reflect a still-active, still-below-tier descent), records how
    deep the cycle had gotten at the moment it resolved -- the fired case's
    own ``lowest_drop_seen`` at the moment of the fire, or the abandoned
    case's own deepest point (including this very evaluation, which may
    itself be a new low). Letting a DIFFERENT stream past the same-day retry
    guard (see above) reopened a narrower but real hole in both cases: a
    price that merely stayed flat, or ticked only slightly, since that fire
    or abandonment is still BELOW the original day-old prior_close and would
    otherwise look like a brand-new dip to this fresh (``status == "none"``)
    evaluation -- re-crediting, and for the fired case potentially
    re-firing, for the SAME underlying descent (Codex review: "Require a
    fresh descent before rearming in a later slot" for the fire case;
    "Prevent abandoned cycles from immediately rearming" for the
    abandonment case, which additionally reset trading_days_active/
    fire_attempts back to zero, defeating the cutoffs those abandonment
    checks exist to enforce). The guard that reads this field requires
    today's drop to genuinely exceed it before arming a new cycle. Omitted
    (``None``) for every OTHER kind of reset, matching every other per-cycle
    field's own "nothing carries forward on a genuine reset" contract above.

    ``fired_floor_price``, passed alongside ``fired_floor_drop_metric`` by
    the same three call sites, is that identical floor expressed as an
    absolute PRICE instead of a normalized drop metric -- computed once, at
    write time, from the frozen ``cycle_reference_close``/``stddev_reference``
    still in scope at the moment of resolution (see
    ``_drop_metric_to_price``). ``fired_floor_drop_metric`` alone is not
    enough: it was computed against THIS cycle's frozen reference close, but
    the later same-day guard that reads it back (once the cycle has already
    resolved to ``"none"``) has no frozen reference of its own left to
    recompute a comparable metric from, and previously reused THAT call's
    own fresh ``previous_close`` instead -- a different reference than the
    one the floor was measured against. A price that recovered since the
    cycle started (previous_close has drifted upward) but stayed flat since
    the actual fire/abandonment moment could then read as a "deeper" drop
    than the stored floor purely from the reference-close mismatch,
    incorrectly re-arming and re-crediting a price level that had already
    been paid for (Codex review: "Compare the rearm floor in the same price
    frame"). A price boundary needs no reference frame at all -- the
    same-day guard can compare it directly against a fresh ``decision_price``
    with no unit conversion. Omitted when the frame can't be safely
    inverted (see ``_drop_metric_to_price``'s own ``None`` contract) --
    the same-day guard falls back to the older, frame-mismatched metric
    comparison in that case, matching a legacy marker written before this
    field existed.
    """
    marker: dict[str, Any] = {
        "status": STATUS_NONE,
        "last_evaluated_trading_day": trading_day.isoformat(),
        "last_evaluated_stream": stream,
    }
    if fired_floor_drop_metric is not None:
        marker["fired_floor_drop_metric"] = fired_floor_drop_metric
    if fired_floor_price is not None:
        marker["fired_floor_price"] = fired_floor_price
    # A fire is not eligible to rearm merely because a new run/day starts.
    # Retain the completed cycle's reference until price reaches it again;
    # only a later descent after that recovery is conceptually fresh.
    if rearm_reference_close is not None:
        marker["rearm_reference_close"] = rearm_reference_close
    # Seeded WITH the latch, from the close this resolving evaluation itself
    # observed. The split detector compares two CONSECUTIVE closes, so it
    # cannot run at all until some earlier STATUS_NONE evaluation has recorded
    # one. A fire in the final slot before an OVERNIGHT split left no such
    # record: the first post-split evaluation had nothing to compare against
    # and simply stored the already-adjusted close, so every ratio from then on
    # was ~1 and the discontinuity was never detectable. After a forward split
    # the stale pre-split latch then suppressed purchases indefinitely, and
    # after a reverse split the recovery check cleared it and re-credited the
    # same descent (Codex review: "Seed the split detector before the first
    # post-fire evaluation"). Only meaningful alongside an armed latch -- with
    # no rearm_reference_close there is nothing for a rebase to correct.
    if rearm_reference_close is not None and rearm_last_seen_day is not None:
        marker["rearm_last_seen_day"] = rearm_last_seen_day
    return marker


def rolling_stddev(daily_returns: list[float], window_days: int) -> Optional[float]:
    """Population stdev of the trailing ``window_days`` daily returns.

    Returns ``None`` (fail closed -- never a fabricated sigma) when fewer than
    ``window_days`` returns are available, or when ``window_days < 2``.
    """
    if window_days < 2 or len(daily_returns) < window_days:
        return None
    window = daily_returns[-window_days:]
    return statistics.pstdev(window)


#: If the archive's single most recent row is more than this many CALENDAR
#: days before `as_of`, the archive is treated as stopped-updating rather
#: than a legitimate trailing window -- see trailing_daily_returns()'s own
#: comment on why. 7 calendar days comfortably covers an ordinary weekend or
#: a single market holiday without over-tripping on routine non-trading days.
_STDDEV_HISTORY_MAX_STALENESS_DAYS = 7

# Corporate-action handling for the durable rearm latch. The marker is durable,
# so unlike an active cycle it can survive long enough for its absolute
# reference price to move onto a different corporate-action scale.
#
# Detection is NOT a price heuristic. Two earlier attempts were: first an
# enumerated ratio list, then a "within tolerance of a whole factor" rule. Both
# asked whether a close-to-close move LOOKED like a split, and neither could
# tell one from an ordinary gap -- an 80 -> 120 news gap reads as a reverse
# 3-for-2, a ~50% crash reads as a 2-for-1, and moves that large are entirely
# realistic for the volatile tickers this mode targets (Codex review: "Require
# corporate-action evidence before rebasing").
#
# The archive already carries the real thing: scripts/refresh_active_ticker_
# histories.py writes a "Stock Splits" column holding the data source's own
# reported ratio on each split's effective date. archived_split_factor() reads
# it, the caller passes the result in as `split_factor` exactly as it passes
# `stddev_reference`, and an unconfirmable archive rebases nothing.


def _parse_positive_finite(raw_value: Optional[str]) -> Optional[float]:
    """A finite, positive float parsed from a CSV cell, or ``None`` if unusable."""
    if raw_value in (None, ""):
        return None
    try:
        candidate = float(raw_value)
    except (ValueError, TypeError):
        return None
    return candidate if math.isfinite(candidate) and candidate > 0 else None


def _parse_corporate_action_cell(raw_value: Optional[str]) -> tuple[bool, Optional[float]]:
    """Classify one archive cell (``Stock Splits`` or ``Dividends``) as
    ordinary, a genuine positive value, or malformed.

    Returns ``(usable, value)``. ``usable`` is ``False`` for unparseable
    text, ``NaN``/``inf``, or a negative value -- exactly the cases
    :func:`_parse_positive_finite` collapses onto the SAME ``None`` a
    genuine ordinary-day ``"0.0"`` produces, making the two indistinguishable
    to a caller that only checks "is there a value". :func:`archived_split_factor`
    must reject a malformed cell outright rather than silently reading it as
    "no corporate action" and advancing its cursor past a row the archive
    never actually confirmed cleanly on (Codex review: "Reject malformed
    split cells before advancing the cursor"; "Reject malformed dividend
    cells before advancing the cursor"). ``value`` is the parsed positive
    number when one was recorded, ``None`` for the ordinary "0.0" case.
    """
    if raw_value in (None, ""):
        return False, None
    try:
        candidate = float(raw_value)
    except (ValueError, TypeError):
        return False, None
    if not math.isfinite(candidate) or candidate < 0:
        return False, None
    if candidate == 0.0:
        return True, None
    return True, candidate


def _interpolated_adjusted_closes(
    ordered_dates: list[date], adjusted_by_date: dict[date, Optional[float]], raw_by_date: dict[date, Optional[float]]
) -> dict[date, float]:
    """Adjusted close per date, gaps filled by linear interpolation WITHIN
    the adjusted series itself -- never by splicing in raw Close for a
    single date, and never by computing an adjustment ratio and carrying it
    across a gap either.

    A per-row fallback to raw Close (the original version of this function)
    or an inferred Adj-Close/Close ratio forward-filled across gaps (an
    intermediate version) both get this wrong exactly AT a split/dividend
    transition, where the two are on genuinely different scales and the
    ratio genuinely changes -- injecting a fake single-day return into the
    very sigma this whole module exists to compute honestly (Codex review:
    "Avoid mixing raw and adjusted prices in the sigma window"; mirrors
    ml-forecaster/features.py's identical, already-fixed problem for the
    same reason). Interpolating directly within the adjusted series is
    correct for an INTERIOR gap (a valid Adj Close exists on both sides).

    An EDGE gap (a trailing run of missing Adj Close with no LATER valid
    value, or a leading run with no EARLIER one) has no second anchor to
    interpolate against -- holding the nearest known ADJUSTED price flat
    for every affected date (an earlier version of this fix) turns every
    one of those dates' day-over-day returns into exactly 0%, artificially
    flattening the very sigma this function computes, and since the row
    still carries a normal raw Close, the staleness check above sees a
    "fresh" archive throughout, hiding the problem entirely (Codex review:
    "Reject trailing adjusted-price gaps instead of flattening them"). This
    scales the nearest anchor's own ratio (Adj Close / Close at the anchor
    date) by THIS date's own raw Close instead, so real day-to-day price
    movement still shows up in the computed returns -- the same "hold the
    ratio, not the price" principle applied to whichever side of the gap
    has no interpolation partner. Only a date with NO adjusted value
    anywhere in the series (nothing before or after) falls back to that
    date's own raw Close.
    """
    n = len(ordered_dates)
    adjusted = [adjusted_by_date[d] for d in ordered_dates]
    # Two linear passes (nearest known index at or before / at or after each
    # position) rather than an O(n^2) scan per gap -- this archive can be
    # the ticker's ENTIRE history, not just one window_days-sized slice.
    nearest_before: list[Optional[int]] = [None] * n
    last_known = None
    for i in range(n):
        if adjusted[i] is not None:
            last_known = i
        nearest_before[i] = last_known
    nearest_after: list[Optional[int]] = [None] * n
    next_known = None
    for i in range(n - 1, -1, -1):
        if adjusted[i] is not None:
            next_known = i
        nearest_after[i] = next_known

    def _scaled_by_anchor_ratio(anchor_idx: int, day: date) -> float:
        anchor_date = ordered_dates[anchor_idx]
        anchor_raw = raw_by_date.get(anchor_date)
        day_raw = raw_by_date.get(day)
        anchor_adj = adjusted[anchor_idx]
        if anchor_adj is not None and anchor_raw is not None and anchor_raw > 0 and day_raw is not None:
            return anchor_adj / anchor_raw * day_raw
        if anchor_adj is not None:
            return anchor_adj  # this date's own raw Close is unusable -- fall back to the flat anchor value
        return day_raw if day_raw is not None else 0.0

    result: dict[date, float] = {}
    for i, day in enumerate(ordered_dates):
        adj_val = adjusted[i]
        if adj_val is not None:
            result[day] = adj_val
            continue
        before_idx, after_idx = nearest_before[i], nearest_after[i]
        if before_idx is not None and after_idx is not None:
            adj_before = adjusted[before_idx]
            adj_after = adjusted[after_idx]
            if adj_before is not None and adj_after is not None:
                span = after_idx - before_idx
                weight = (i - before_idx) / span
                result[day] = adj_before + weight * (adj_after - adj_before)
        elif before_idx is not None:
            result[day] = _scaled_by_anchor_ratio(before_idx, day)  # trailing edge
        elif after_idx is not None:
            result[day] = _scaled_by_anchor_ratio(after_idx, day)  # leading edge
        else:
            raw_value = raw_by_date.get(day)
            if raw_value is not None:
                result[day] = raw_value  # no adjusted value ANYWHERE -- fall back to this date's raw Close
    return result


def trailing_daily_returns(
    ticker: str, *, as_of: date, window_days: int, history_dir: Optional[Path] = None
) -> list[float]:
    """Trailing close-to-close daily returns strictly before ``as_of``, oldest first.

    Reads ``<history_dir>/<TICKER>.csv`` (the shared ``ticker_data/`` archive;
    see ``dca_ticker_paths.configured_ticker_history_dir``), the same
    ``Date``/``Close``/``Adj Close`` columns
    ``scripts/refresh_active_ticker_histories.py`` writes. Rows on or after
    ``as_of`` are excluded -- this is evaluated at decision time for
    ``as_of``, so including same-day-or-later rows would be lookahead.
    Returns ``[]`` (fail closed -- see :func:`rolling_stddev`'s ``None``
    contract) when the archive file is absent, unreadable, has too few
    usable rows for even one return, or its single most recent row is more
    than ``_STDDEV_HISTORY_MAX_STALENESS_DAYS`` calendar days before
    ``as_of`` -- an archive whose refresh has silently stopped otherwise
    still has plenty of OLD rows to satisfy ``window_days + 1``, freezing an
    obsolete volatility estimate into a new cycle instead of refusing
    outright (Codex review: "Reject stale history before freezing sigma").

    Prefers the split/dividend-adjusted ``Adj Close`` per row, but does NOT
    fall back to raw ``Close`` row-by-row when a single row's ``Adj Close``
    is unusable -- see :func:`_interpolated_adjusted_closes`'s own docstring
    on why a per-row raw-Close splice (or an inferred, forward-filled
    adjustment ratio) both corrupt the series exactly at a split/dividend
    transition. Gaps are filled by interpolating within the adjusted series
    itself instead.
    """
    root = history_dir if history_dir is not None else configured_ticker_history_dir()
    symbol = ticker.strip().upper()
    # Mirrors InverseMartingaleLedger._paths()'s own identical guard -- see
    # that method's comment for why ".." must be excluded separately from
    # the pattern match (a real symbol can legitimately contain a single
    # "." per _TICKER_SYMBOL_PATTERN, e.g. "BRK.B", so the pattern alone
    # cannot reject a parent-directory traversal). Without this, a ticker
    # string containing path separators or ".." would be joined into `root`
    # unvalidated (Antigravity review: "Missing Symbol & Path Traversal
    # Validation in trailing_daily_returns()"). Returns [] rather than
    # raising, matching every other failure mode in this function (missing/
    # unreadable file, insufficient history) -- this is a pure, fail-closed
    # lookup, not state that needs CorruptedLedgerStateError's "skip only
    # this ticker" contract.
    if not _TICKER_SYMBOL_PATTERN.match(symbol) or ".." in symbol:
        return []
    path = Path(root) / f"{symbol}.csv"
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except (FileNotFoundError, OSError, UnicodeDecodeError, csv.Error):
        return []
    # Keyed by date, not a plain list: a duplicate Date in the archive (a
    # bad merge) would otherwise leave BOTH rows in the trailing window as
    # if they were consecutive trading sessions, manufacturing an
    # artificial same-day "return" that consumes a real window_days slot
    # and skews the snapshotted sigma a stddev-basis cycle crosses/recovers
    # against (Codex review: "Deduplicate dates before computing the
    # volatility window"). The last row read for a given date wins, the
    # same stable last-row-wins convention ml-forecaster/features.py's
    # load_history_csv() already uses for the identical archive-merge
    # scenario.
    adjusted_by_date: dict[date, Optional[float]] = {}
    raw_by_date: dict[date, Optional[float]] = {}
    latest_eligible_row_date: Optional[date] = None
    for row in rows:
        row_date_raw = row.get("Date")
        if not row_date_raw:
            continue
        try:
            row_date = date.fromisoformat(row_date_raw[:10])
        except ValueError:
            continue
        if row_date >= as_of:
            continue
        adjusted = _parse_positive_finite(row.get("Adj Close"))
        raw = _parse_positive_finite(row.get("Close"))
        adjusted_by_date[row_date] = adjusted
        raw_by_date[row_date] = raw
        # Freshness must be judged against a row that can actually
        # contribute a price -- a row whose Adj Close AND Close are BOTH
        # missing/invalid is excluded from ordered_dates below (see its own
        # filter) and never touches the computation either directly or as
        # an interpolation anchor, so advancing latest_eligible_row_date for
        # it would let the newest few rows go unusable while the freshness
        # check still passes on their date -- with sigma then computed
        # entirely from older, genuinely usable rows that could themselves
        # be well past the staleness cutoff (Codex review: "Base history
        # freshness on a usable price row").
        if adjusted is None and raw is None:
            continue
        if latest_eligible_row_date is None or row_date > latest_eligible_row_date:
            latest_eligible_row_date = row_date
    # Staleness is judged against the most recent row actually ELIGIBLE for
    # the return window above (strictly before as_of) AND usable (see the
    # check just above), not the archive's single most recent row overall --
    # a stray future-dated (or exactly as_of-dated), or entirely unusable,
    # row would otherwise make this look fresh while the actual computation
    # below still runs entirely on arbitrarily old pre-cutoff prices,
    # defeating the staleness check outright (Codex review: "Check freshness
    # against the latest eligible history row"; "Base history freshness on a
    # usable price row").
    if (
        latest_eligible_row_date is None
        or (as_of - latest_eligible_row_date).days > _STDDEV_HISTORY_MAX_STALENESS_DAYS
    ):
        return []
    ordered_dates = sorted(d for d, adj in adjusted_by_date.items() if adj is not None or raw_by_date.get(d) is not None)
    closes_by_date = _interpolated_adjusted_closes(ordered_dates, adjusted_by_date, raw_by_date)
    tail = [closes_by_date[d] for d in ordered_dates[-(window_days + 1):] if d in closes_by_date]
    returns = []
    for previous, current in zip(tail, tail[1:], strict=False):
        if previous > 0:
            ret = (current - previous) / previous
            if not math.isfinite(ret):
                # An extreme but individually-finite price PAIR (e.g. a
                # corrupted archive row of 1e-308 next to 1e308 -- both pass
                # _parse_positive_finite's own per-price finiteness check
                # above) can still overflow to +/-inf once divided into a
                # return here. rolling_stddev's statistics.pstdev() raises
                # outright on a non-finite input instead of returning its
                # own documented fail-closed None, and near_close_dca.py's
                # injection site does not wrap this call in a try/except --
                # an uncaught exception here would abort the ENTIRE
                # scheduled run over one ticker's corrupted history instead
                # of just skipping that ticker (Codex review: "Reject
                # non-finite returns before computing sigma"). Fail closed
                # for the WHOLE window rather than silently dropping just
                # this one return: a price pair extreme enough to overflow
                # a ratio is reason enough to distrust the archive's other
                # nearby returns too.
                return []
            returns.append(ret)
    return returns


def stddev_reference_for_ticker(
    ticker: str, *, as_of: date, window_days: int, history_dir: Optional[Path] = None
) -> Optional[float]:
    """Convenience wrapper: :func:`trailing_daily_returns` -> :func:`rolling_stddev`."""
    returns = trailing_daily_returns(ticker, as_of=as_of, window_days=window_days, history_dir=history_dir)
    return rolling_stddev(returns, window_days)


def archived_split_factor(
    ticker: str, *, since: date, as_of: date, history_dir: Optional[Path] = None
) -> Optional[tuple[float, date]]:
    """The cumulative split/dividend factor the ARCHIVE records for ``ticker``
    on rows after ``since`` and at or before ``as_of``, or ``None`` when the
    archive cannot answer.

    Reads the ``Stock Splits`` and ``Dividends`` columns
    ``scripts/refresh_active_ticker_histories.py`` writes -- the real
    corporate-action ratio (``numerator/denominator``) and per-share cash
    payout the data source reported on each event's effective/ex-dividend
    date, ``"0.0"`` on every ordinary day. That script rewrites the COMPLETE
    file on each refresh, so an event's own row is present and stable rather
    than depending on when the archive was last appended to.

    This exists because a price ratio is NOT evidence of a corporate action.
    The heuristic it replaces classified any close-to-close gap near a whole
    factor as a split, and gaps that large are entirely realistic for the
    volatile tickers this mode is aimed at: an 80 -> 120 news gap read as a
    reverse 3-for-2 and rebased a $100 latch to $150 instead of recognizing
    that price had recovered past it, while a ~50% crash read as a 2-for-1,
    rebased the reference down onto the crashed price and let the latch
    clear -- crediting tiers from an already-paid-for descent a second time
    (Codex review: "Require corporate-action evidence before rebasing").
    Real metadata has no such failure mode: an ordinary gap, however large,
    records ``0.0`` here.

    A cash dividend is folded into the SAME cumulative factor a split is,
    using the ratio ``(close + dividend) / close`` computed from that row's
    own raw ``Close`` -- the EX-DATE close, which already reflects the
    mechanical ex-dividend drop, so adding the dividend back reconstructs the
    pre-drop price scale. Dividing ``rearm_reference_close`` by this ratio
    (exactly like a split) scales it down by the same proportion the real
    price just dropped. Without this, a durable latch spanning a large
    special distribution stayed pinned to its pre-distribution reference even
    though the total-return price had genuinely recovered past it,
    indefinitely suppressing purchases on an enrolled ticker (Codex review:
    "Rebase the durable latch across cash distributions"). A dividend row
    with an unusable close makes the whole call return ``None`` without
    advancing the cursor, rather than silently folding in a no-op and
    reporting the row examined: the archive recorded a real distribution
    this call cannot value, so treating the row as "examined" would let a
    later run with a fixed/complete archive never re-look at it, exactly
    the "cannot tell" case the missing-column and stale-archive guards
    below already return ``None`` for (Codex review: "Preserve the cursor
    when a dividend cannot be valued").

    Both ``Stock Splits`` and ``Dividends`` must be present as ARCHIVE
    COLUMNS, not merely absent on individual rows: the writer script always
    emits both together, so a file missing either one is a legacy or
    partially generated archive this function cannot trust. Reading a
    missing column the same way as a genuine ``"0.0"`` (``DictReader``
    returns ``None`` for both) would falsely confirm "no corporate action"
    and advance the durable cursor past whatever rows the file actually
    lacks metadata for (Codex review: "Reject archives that omit split
    metadata").

    Returns ``None`` -- "cannot confirm", which callers treat as "do not
    rebase" -- for an absent, unreadable, symbol-invalid, or column-
    incomplete archive; for one whose most recent row is more than
    :data:`_STDDEV_HISTORY_MAX_STALENESS_DAYS` calendar days before ``as_of``
    (a refresh that has silently stopped cannot be distinguished from a
    market with no splits, and rebasing on a stale file's silence is exactly
    the false negative that strands a latch); for one whose newest row is
    OLDER than ``since`` (the archive has been rolled back to an
    earlier-but-still-fresh snapshot -- reporting that older date as the new
    cursor would let the caller persist a regression, and a split already
    folded into the reference on a LATER run would then be re-examined and
    folded in a second time once the complete archive returns (Codex review:
    "Never move the split cursor backward")); and for a cumulative factor
    that is not finite and positive (an extreme but individually-valid
    archive row, e.g. two split ratios whose product overflows, must not
    silently divide the reference by infinity or zero (Codex review: "Reject
    an unrepresentable cumulative split factor")). A readable, fresh archive
    with no event in the window returns a factor of ``1.0`` -- a confirmed
    "no corporate action", which is a different answer from ``None`` and
    lets the caller distinguish "nothing happened" from "cannot tell".

    Returns ``(factor, cursor)``. The cursor is the newest archive date this
    call actually EXAMINED -- ``as_of`` itself is INCLUDED (not excluded as
    lookahead): a split or dividend effective ON ``as_of`` is exactly the
    event the SAME evaluation's already-post-event ``decision_price``/
    ``previous_close`` need confirmed to correctly rebase, and the archive
    generally carries that day's own row by the time this mode evaluates it
    (Codex review: "Include effective-day splits before evaluating the
    latch"). Only a row strictly AFTER ``as_of`` is genuine lookahead. The
    caller must persist the returned cursor, not ``as_of`` verbatim, as the
    next query's ``since``: stamping a value newer than what was actually
    examined -- or falling back to today when this call returned ``None`` --
    both skip whatever lies in between forever, since every later run
    excludes anything at or before its own ``since`` (Codex review: "Keep the
    split cursor on the last examined archive day"; "Retain the split cursor
    when lookup fails").
    """
    root = history_dir if history_dir is not None else configured_ticker_history_dir()
    symbol = ticker.strip().upper()
    # Same traversal guard trailing_daily_returns applies -- see its comment on
    # why ".." must be excluded separately from the pattern match.
    if not _TICKER_SYMBOL_PATTERN.match(symbol) or ".." in symbol:
        return None
    path = Path(root) / f"{symbol}.csv"
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            # A missing column reads back as None for every row, identically
            # to a genuine "0.0"/absent cell -- indistinguishable from "no
            # corporate action" without checking the header itself first.
            if reader.fieldnames is None or not {"Stock Splits", "Dividends"} <= set(reader.fieldnames):
                return None
            rows = list(reader)
    except (FileNotFoundError, OSError, UnicodeDecodeError, csv.Error):
        return None
    factor = 1.0
    latest_row_date: Optional[date] = None
    saw_usable_row = False
    for row in rows:
        row_date_raw = row.get("Date")
        if not row_date_raw:
            # A blank Date is ordinary trailing-row noise UNLESS the row also
            # carries a genuine nonzero Stock Splits or Dividends value --
            # that combination means the archive recorded a real corporate
            # action with no date to place it on, which this call cannot
            # simply skip: skipping let a LATER, cleanly-dated row become
            # latest_row_date and get persisted as the cursor, after which a
            # corrected archive (the date filled back in) would exclude this
            # very row forever via `row_date <= since` (Codex review: "Reject
            # undated corporate-action rows before advancing"). Reuses the
            # same malformed-vs-ordinary classification the dated branches
            # below use, so "0.0"/blank still reads as inert. Both the
            # usability flag and the value must be checked: a malformed cell
            # (unparseable text, NaN, or negative) parses back to the SAME
            # None value as the ordinary "0.0" case, so checking only the
            # value would let a malformed-but-undated row slip through as if
            # it were inert (Codex review: "Reject malformed actions even
            # when the row is undated"). But a genuinely BLANK cell also
            # parses to that same (False, None) via
            # _parse_corporate_action_cell's own `raw_value in (None, "")`
            # branch, indistinguishable from a malformed nonblank one by the
            # usability flag alone -- an all-blank trailing CSV row (a
            # comma-only spreadsheet artifact, no action recorded at all) is
            # exactly the ordinary noise this whole branch exists to permit,
            # and treating its blank cells as malformed would reject every
            # split lookup over the archive until that row is removed (Codex
            # review: "Keep blank undated rows inert"). Only a NONBLANK cell
            # that still fails to parse counts as malformed.
            raw_undated_split = row.get("Stock Splits")
            raw_undated_dividend = row.get("Dividends")
            undated_split_ok, undated_split = _parse_corporate_action_cell(raw_undated_split)
            undated_dividend_ok, undated_dividend = _parse_corporate_action_cell(raw_undated_dividend)
            undated_split_malformed = raw_undated_split not in (None, "") and not undated_split_ok
            undated_dividend_malformed = raw_undated_dividend not in (None, "") and not undated_dividend_ok
            if (
                undated_split_malformed
                or undated_dividend_malformed
                or undated_split is not None
                or undated_dividend is not None
            ):
                return None
            continue
        try:
            row_date = date.fromisoformat(row_date_raw[:10])
        except ValueError:
            # A row with a Date present but unparseable -- unlike a genuinely
            # blank trailing row (the `not row_date_raw` case above, which is
            # never itself evidence of anything) -- means this archive is
            # damaged in a way that could be hiding a real split/dividend on
            # that row. Silently skipping it let a LATER, cleanly-dated row
            # become latest_row_date and get persisted as the cursor, after
            # which a corrected archive would exclude the malformed row's
            # actual date forever via `row_date <= since` -- the same
            # "cannot tell" case the missing-column and stale-archive guards
            # already return None for (Codex review: "Reject malformed
            # action-row dates before advancing the cursor").
            return None
        if row_date > as_of:
            continue
        saw_usable_row = True
        if latest_row_date is None or row_date > latest_row_date:
            latest_row_date = row_date
        if row_date <= since:
            continue
        split_ok, ratio = _parse_corporate_action_cell(row.get("Stock Splits"))
        if not split_ok:
            # Unparseable text, NaN, or a negative value reads back through
            # plain float-and-check as indistinguishable from the ordinary
            # "0.0" cell -- treating it that way would confirm "no split"
            # for a row the archive never actually reported cleanly on, and
            # a later correction is excluded forever by `row_date <= since`
            # once this row's date becomes the persisted cursor.
            return None
        # A genuine positive ratio multiplies in; "0.0"/no ratio recorded is
        # the ordinary-day value and leaves factor unchanged. A 1.0 recorded
        # on a row is a no-op split and multiplies harmlessly.
        if ratio is not None:
            factor *= ratio
        dividend_ok, dividend = _parse_corporate_action_cell(row.get("Dividends"))
        if not dividend_ok:
            # Same malformed-vs-ordinary ambiguity as the Stock Splits cell
            # above: unparseable text, NaN, or a negative payout must not be
            # read as "no distribution" and let the row's date become the
            # persisted cursor -- a later archive correction would then be
            # excluded forever by `row_date <= since`.
            return None
        if dividend is not None:
            # This row's own Close is the EX-DATE close -- already past the
            # mechanical ex-dividend drop, since the market prices it in the
            # same session. Reconstructing the pre-drop price as
            # ``close + dividend`` and dividing by the post-drop ``close``
            # gives the same "how much larger was the reference's price
            # scale" ratio a split's ``factor`` represents, so dividing
            # ``rearm_reference_close`` by it (exactly like a split) scales
            # the reference DOWN by the same proportion the real price just
            # dropped.
            close = _parse_positive_finite(row.get("Close"))
            if close is None:
                # Cannot value this distribution -- returning a factor that
                # silently omits it, while still reporting this row's date
                # as examined, would let the caller advance the durable
                # cursor past it forever, even once a later archive refresh
                # supplies a usable Close for the same row.
                return None
            factor *= (close + dividend) / close
    if not saw_usable_row or latest_row_date is None:
        return None
    if (as_of - latest_row_date).days > _STDDEV_HISTORY_MAX_STALENESS_DAYS:
        return None
    if latest_row_date < since:
        return None
    if not math.isfinite(factor) or factor <= 0:
        return None
    return factor, latest_row_date


def _capped_fire_amount(accumulated_before: float, per_order_cap_usd: Optional[float]) -> float:
    """The dollar amount to fire this evaluation, capped and broker-minimum
    floored, but never leaving an un-fireable sub-minimum remainder behind.

    Shared by both fire-resolution call sites (a fresh recovery firing from
    STATUS_NONE/STATUS_DIPPING, and a STATUS_FIRING continuation's retry) so
    the two cannot drift apart on this guarantee.

    Without the final adjustment below, a per_order_cap_usd tighter than the
    accumulated balance by less than the broker minimum -- e.g. a $10.50
    balance capped at $10.00 -- fires the capped $10.00 and leaves a $0.50
    remainder as a STATUS_FIRING continuation. That status BYPASSES tier
    accumulation entirely (only STATUS_DIPPING/STATUS_NONE evaluations credit
    new tier crossings), so the remainder can never grow past the broker
    minimum on its own; every later evaluation re-derives the identical
    sub-minimum fire_amount and defers again, until max_cycle_days/
    max_fire_attempts eventually abandons the cycle and discards it outright
    (Codex review: "Keep capped fire remainders placeable").

    That shrink must not itself drop below the broker minimum. A balance
    between one and two times ``_BROKER_MINIMUM_ORDER_USD`` with a cap at or
    below the minimum -- e.g. a $1.50 balance capped at $1.00 -- shrinks the
    capped $1.00 down to $0.50 to zero out the remainder, an amount now
    UNPLACEABLE itself; both evaluation branches then reject the sub-dollar
    fire, and the balance is stuck exactly like the un-shrunk case this
    function exists to prevent. When no partition of the balance leaves BOTH
    halves placeable while respecting the cap, the cap is the constraint that
    gives way: firing the full accumulated balance in one order is always
    placeable and always terminates the cycle, whereas honoring an
    unsatisfiable cap would strand money forever (Codex review: "Keep the
    adjusted fire amount above the broker minimum").
    """
    fire_amount = accumulated_before
    if per_order_cap_usd is not None:
        fire_amount = min(fire_amount, per_order_cap_usd)
    # Floor to the broker minimum, but NEVER above the balance actually
    # tracked. `max(_BROKER_MINIMUM_ORDER_USD, ...)` alone turned a
    # sub-dollar TRACKED balance into a larger real order -- a $0.30
    # accumulation (schema-valid: unit_usd=0.30 with a single unit) was
    # placed as $1 and the cycle then cleared, spending several times the
    # ladder amount the accumulator exists to defer, and magnifying further
    # the smaller the configured unit (Codex review: "Defer sub-dollar
    # balances instead of rounding them up"). Clamping to
    # `accumulated_before` keeps the pre-existing "floor beats a sub-dollar
    # per_order_cap_usd" behaviour -- that only ever raises the amount back
    # toward money the cycle really holds -- while making it impossible to
    # place more than was accumulated.
    fire_amount = min(max(_BROKER_MINIMUM_ORDER_USD, fire_amount), accumulated_before)
    # If firing fire_amount would leave a NONZERO remainder below the broker
    # minimum, shrink fire_amount just enough that the remainder is either
    # exactly $0 or the full broker minimum instead -- always safe against
    # per_order_cap_usd, since reducing an already-capped amount can only
    # move further under a MAXIMUM constraint. When per_order_cap_usd never
    # capped anything (fire_amount already equals accumulated_before), the
    # prospective remainder is 0 and this is a no-op.
    prospective_remainder = round(accumulated_before - fire_amount, 2)
    if 0 < prospective_remainder < _BROKER_MINIMUM_ORDER_USD:
        shrunk = round(fire_amount - (_BROKER_MINIMUM_ORDER_USD - prospective_remainder), 2)
        if shrunk >= _BROKER_MINIMUM_ORDER_USD:
            fire_amount = shrunk
        else:
            # No cap-respecting partition leaves both the fired amount and
            # the remainder placeable -- shrinking would just trade one
            # unplaceable amount for another. Fire the whole balance instead
            # of honoring the cap, so the cycle always terminates.
            fire_amount = accumulated_before
    return fire_amount



def _unchanged(
    tracker_state: dict[str, Any],
    status_before: str,
    accumulated_before: float,
    reason: str,
) -> tuple[dict[str, Any], InverseMartingaleStep]:
    """Helper to return an unchanged tracker state and an unfired InverseMartingaleStep."""
    return dict(tracker_state), InverseMartingaleStep(
        status_before=status_before,
        status_after=status_before,
        accumulated_usd_before=accumulated_before,
        accumulated_usd_after=accumulated_before,
        newly_crossed_tiers=(),
        fired=False,
        fire_amount_usd=0.0,
        reason=reason,
    )


def evaluate_inverse_martingale_step(
    tracker_state: dict[str, Any],
    *,
    decision_price: float,
    previous_close: float,
    trading_day: date,
    tier_config: tuple[tuple[float, float], ...],
    unit_usd: float,
    recovery_offset: float,
    max_cycle_days: int,
    max_fire_attempts: int,
    basis: str,
    stddev_reference: Optional[float] = None,
    per_order_cap_usd: Optional[float] = None,
    stream: str = "afternoon",
    split_factor: Optional[float] = None,
    split_cursor_day: Optional[date] = None,
) -> tuple[dict[str, Any], InverseMartingaleStep]:
    """Advance one ticker's accumulator by one evaluation, purely in memory.

    ``tracker_state`` is this ticker's persisted state (``{}`` if a cycle has
    never started). ``decision_price``/``previous_close`` are the same two
    prices ``evaluate_source_signal`` already computed for this ticker this
    run -- no additional data-provider call is made here. ``previous_close``
    seeds ``cycle_reference_close`` only when a cycle starts; every later
    step of that cycle uses the persisted snapshot, so a changing daily
    previous close cannot manufacture a recovery or move the cycle's tier
    thresholds. The one place a fresh ``previous_close`` is read on its own
    is the post-fire ``rearm_reference_close`` latch below, where detecting
    that price has left the old dip is precisely a question about TODAY's
    close rather than about the resolved cycle's frozen reference.

    ``stream`` identifies which of the (up to three) canonical scheduled
    slots this evaluation belongs to -- "morning", "midday", or "afternoon"
    (matching ``RunContext.stream``/``run_dca.SLOT_MAP``). It has no effect
    on the accumulation/recovery math itself; it exists solely so the
    same-day retry guard below can tell a genuine retry of THIS run apart
    from a later, independent slot evaluating the same ticker the same day
    -- see :func:`resolved_marker`'s docstring for the full rationale.

    ``basis`` selects how ``drop`` and ``recovery_offset`` are interpreted:
    ``"pct"`` treats them as thresholds in WHOLE PERCENTAGE POINTS against
    ``previous_close`` -- a tier threshold of ``1.0`` (matching
    ``config/inverse_martingale.json``'s own ``"drop_pct": 1.0`` convention)
    fires at a 1% drop, NOT a 0.01% drop. The internal drop fraction
    (``(reference_close - decision_price) / reference_close``, where 0.01
    means 1%) is multiplied by 100 below before ever being compared against a
    configured threshold -- a caller who instead configured a raw fraction
    (``0.01`` for 1%) would cross tier 1 at a 0.01% decline, roughly 100x
    earlier than intended, and since enrollment has already suppressed the
    normal ladder for that ticker, the accumulator would become the ticker's
    ONLY buying path and could accumulate/fire unintended orders off it
    (Codex review: "Document percentage thresholds in the evaluator's
    units"). ``"stddev"`` treats them as multiples of ``stddev_reference``
    (required, and must be a positive, finite number, for that basis -- see
    :func:`rolling_stddev`). A drop that cannot be computed (``stddev`` basis
    with no ``stddev_reference``) is fail-closed: the tracker is returned
    unchanged and no tier is crossed.

    Tier thresholds accumulate cumulatively within one evaluation: a gap-down
    that jumps straight past several thresholds credits all of them, not just
    the deepest one crossed.

    Recovery uses "at or above the offset" (not an exact band match), so a
    price that gaps straight past the trigger level between evaluations still
    fires rather than silently missing the cycle.

    A cycle active for more than ``max_cycle_days`` *trading* days (not
    calendar days -- see the ``trading_days_active`` tracking below) without
    firing, or a "firing" cycle stuck for ``max_fire_attempts`` without
    exhausting its balance, is abandoned back to ``"none"`` -- this is an
    explicit, documented cutoff, not infinite retry.
    """
    tiers = _sorted_tiers(tier_config)
    status_before = tracker_state.get("status", STATUS_NONE)
    accumulated_before = float(tracker_state.get("accumulated_usd", 0.0))
    crossed_before = set(tracker_state.get("crossed_tier_thresholds", []))
    fire_attempts = int(tracker_state.get("fire_attempts", 0))
    # Trading days, not calendar days: the canonical path can evaluate this
    # ticker up to three times on one calendar day (market-open, midday, and
    # final-afternoon streams all feed this same accumulator), and a cycle
    # can span weekends/holidays this function is never even called on --
    # deriving age from `(trading_day - cycle_start).days` conflated the
    # two, so a max_cycle_days=30 config value was actually enforced as ~30
    # CALENDAR days (~21 trading sessions), abandoning a cycle roughly 30%
    # earlier than the field name and its own docs promise (Codex review:
    # "Count cycle age in trading days"). Incremented only on this cycle's
    # FIRST evaluation for `trading_day` -- last_evaluated_trading_day
    # (already tracked for the same-day-retry guard below) is reused to
    # detect a same-day re-evaluation from a different stream, which must
    # not double-count.
    trading_days_active = int(tracker_state.get("trading_days_active", 0))
    if tracker_state.get("last_evaluated_trading_day") != trading_day.isoformat():
        trading_days_active += 1

    if tracker_state.get("settlement_pending"):
        # A prior evaluation THIS SAME RUN already claimed a fire -- partial
        # (accumulated balance > per_order_cap_usd, status stays "firing"
        # with the reduced remainder) OR full (the whole balance claimed at
        # once, status collapses to "none" -- see the ``remainder <= 0``
        # branches below) -- and is still waiting on its OWN deferred
        # settlement (after _process_buy_candidates_batch confirms the real
        # placement outcome). The "clean" claimed state either shape writes
        # is otherwise indistinguishable from any other available cycle to a
        # concurrent reader: a plain STATUS_FIRING remainder looks like a
        # normal, claimable continuation, and a plain STATUS_NONE marker
        # looks exactly like a genuinely resolved cycle ready to arm fresh
        # (doubly so now that a DIFFERENT stream is allowed past the
        # same-day retry guard below -- see "Distinguish later slots from
        # retries" -- since nothing but this flag marks a STATUS_NONE claim
        # as still in flight). A second overlapping process (or, for the
        # full-claim case, a genuinely later same-day slot) could otherwise
        # read that state, decide to fire/re-arm from it, and mutate the
        # ledger before THIS process's placement outcome is known; if this
        # process's own placement then fails or is only partially placed,
        # its rollback -- which restores the ORIGINAL pre-claim balance --
        # compare-and-swaps against the exact state it claimed, which no
        # longer matches once someone else moved it, silently discarding the
        # difference between the two snapshots (Codex review: "Reserve
        # partial fires until their placement settles"; "Keep full-fire
        # claims leased until settlement"). Refuse to advance at all while
        # pending, regardless of status_before or which stream is asking;
        # settlement clears this flag on the SAME trading state it read the
        # claim from.
        #
        # Checked FIRST, before EVERY other check in this function that can
        # reset, resolve, or otherwise advance a cycle -- not just the
        # abandonment cutoffs (see "Honor settlement leases before
        # abandoning attempts" below) and the basis-change/tier-config-change
        # resets, but the same-day retry guard just below too: a pending
        # claim can simultaneously look, to that guard, like an ordinary
        # already-resolved marker eligible for a fresh evaluation. A check
        # that ran later would let a second overlapping process (or this
        # same process's own next evaluation) read that exact state and
        # successfully claim_fire() over it -- resolving/re-arming a cycle
        # that is still one settlement away from actually being done, and
        # making the FIRST process's own deferred settlement CAS fail
        # against the now-moved state, silently dropping whatever it had
        # placed (Codex review: "Honor settlement leases before resetting
        # changed configurations"). Ordering this guard first, before ALL of
        # them, means a pending lease always wins: no check in this function
        # can advance, resolve, or reset a cycle out from under a claim that
        # is about to be resolved by this same run's own settlement step.
        return _unchanged(tracker_state, status_before, accumulated_before, "settlement_pending")

    if (
        status_before == STATUS_NONE
        and tracker_state.get("last_evaluated_trading_day") == trading_day.isoformat()
        and tracker_state.get("last_evaluated_stream") in (None, stream)
    ):
        # A retry of the SAME scheduled run for the SAME trading day must be
        # a pure no-op here. Without this guard, a retry landing after a
        # cycle resolved (fully fired, or was abandoned) EARLIER THE SAME
        # DAY would read the reset ("none") tracker and treat today's
        # still-depressed price as a brand-new dip, re-crediting tiers for a
        # descent that was already, legitimately, accounted for -- and, on a
        # later day, firing a SECOND time for what is really the same
        # underlying descent. Nothing else catches this: these synthetic
        # candidates are deliberately never ticker_guarded (see
        # near_close_dca.py's injection site), so the ordinary same-stream
        # broker duplicate check never runs for them either (Codex review:
        # "Require a fresh dip before rearming a fired cycle"). A genuinely
        # LATER day's fresh dip is unaffected: last_evaluated_trading_day
        # only ever equals trading_day on the day resolution/abandonment
        # itself happened.
        #
        # The stream check is what keeps this guard scoped to a genuine
        # RETRY rather than a legitimately later canonical slot: the
        # ``last_evaluated_stream in (None, stream)`` comparison suppresses
        # only when the marker is either a same-stream retry (equal) or was
        # written before this field existed (``None`` -- backward
        # compatible with an already-persisted marker, matching the same
        # "no persisted value is not a mismatch" convention the basis/
        # tier-config-fingerprint checks below use). A marker stamped by a
        # DIFFERENT stream earlier today (e.g. "morning" resolved/abandoned
        # this cycle, and "midday" is now evaluating) no longer matches, so
        # this later slot proceeds to a genuine fresh evaluation instead of
        # being silently discarded with no credit ever given (Codex review:
        # "Distinguish later slots from retries before suppressing
        # rearming"). The settlement_pending guard above runs first and
        # takes priority: a claimed-but-unsettled full fire's marker
        # ALSO satisfies this guard's own conditions (status none, today's
        # date), and must be refused regardless of stream -- only a marker
        # with no pending claim at all reaches this guard.
        return _unchanged(tracker_state, status_before, accumulated_before, "already_evaluated_today")

    if (
        status_before == STATUS_FIRING
        and tracker_state.get("last_evaluated_trading_day") == trading_day.isoformat()
        and tracker_state.get("last_evaluated_stream") in (None, stream)
        and tracker_state.get("last_evaluated_is_real", True)
    ):
        # Mirrors the STATUS_NONE guard just above, for the identical
        # reason, but for an in-progress (not yet fully placed) fire: a
        # retry of the SAME scheduled stream on the SAME trading day must
        # also be a pure no-op here. Without this, a retry that lands after
        # the settlement_pending lease from an EARLIER call this same
        # stream made has already been cleared (this run's own deferred
        # settlement step clears it once placement is known) reaches the
        # STATUS_FIRING continuation branch below, which unconditionally
        # increments fire_attempts -- so enough same-stream retries (e.g.
        # an external scheduler retrying a crashed run) burn through
        # max_fire_attempts purely from repeated evaluations of the SAME
        # logical scheduled slot, never from genuinely distinct scheduled
        # slots getting a chance to place the remaining balance, and the
        # abandonment cutoff below then discards the still-unplaced balance
        # (Codex review: "Preserve partial balances across same-stream
        # retries"). last_evaluated_stream is stamped at every point a
        # DIPPING or FIRING cycle advances (see those blocks' own comments)
        # specifically so this comparison is never stale. A genuinely
        # DIFFERENT stream later the same day is unaffected -- see
        # "Distinguish later slots from retries" on the STATUS_NONE guard
        # above for the identical reasoning -- and may legitimately
        # continue placing the remaining balance.
        #
        # ``last_evaluated_is_real`` (default True -- absent on every
        # legacy/real-evaluation-only marker) is the third condition:
        # near_close_dca.py's age-only bookkeeping for a session with no
        # usable decision data (`_inverse_martingale_advance_age_for_
        # missing_data`) must keep `trading_days_active` ticking through a
        # session where this ticker was never actually evaluated by ANY
        # stream, which means it also has to stamp `last_evaluated_trading_
        # day` to today -- otherwise a LATER same-day real evaluation would
        # increment `trading_days_active` a second time for the same
        # calendar day. But that age-only stamp necessarily carries forward
        # whatever STALE `last_evaluated_stream` a real evaluation on some
        # EARLIER day happened to leave behind (e.g. "afternoon"): if that
        # earlier stream's name coincides with whichever stream genuinely
        # evaluates this ticker FIRST later the same day, this guard would
        # misread a session that was never actually evaluated by that
        # stream as a same-stream retry, and silently swallow the real
        # continuation instead of placing it -- indefinitely, if only that
        # one stream ever has usable data (Codex review: "Separate age
        # updates from the retry stream identity"). The age-only helper
        # marks its own stamp with `last_evaluated_is_real=False` for
        # exactly this reason; every genuine evaluation (this branch and
        # the tier-crossing branch below) restamps it back to True the
        # moment it runs for real.
        return _unchanged(tracker_state, status_before, accumulated_before, "already_evaluated_today")

    persisted_basis = tracker_state.get("basis")
    # A tracker with NO persisted "basis" at all (created before this field
    # existed) is ordinarily not treated as a mismatch below -- backward
    # compatible, does not retroactively reset every pre-existing cycle. But
    # that leniency is only safe when the CURRENT configured basis is "pct":
    # every tracker ever created before the "basis" field existed was
    # necessarily a pct-basis cycle (stddev wasn't an option yet), so a
    # missing basis + a pct config is a genuine no-op, not corruption. A
    # missing basis + a STDDEV config combined with a likewise-missing
    # stddev_reference is different: nothing this function reads could ever
    # have populated stddev_reference for a cycle that also never recorded
    # its own basis, so this shape is indistinguishable from "the operator
    # switched this ticker's config to stddev while a legacy pct cycle was
    # active" -- and, left untreated as a mismatch, drop_metric's own
    # stddev-basis branch below reads `tracker_state.get("stddev_reference")`
    # (never the caller's freshly supplied one, for an active cycle), finds
    # None, and returns "stddev_unavailable" EVERY future call, forever,
    # before ever reaching the age/attempt abandonment cutoffs further down
    # -- permanently bricking the ticker while graded_buy_multiplier keeps
    # suppressing its normal ladder too (Codex review: "Handle missing basis
    # in the frozen-sigma check"). Treat it exactly like any other
    # basis-changed mismatch: reset, don't get stuck.
    basis_mismatch = (persisted_basis is not None and persisted_basis != basis) or (
        persisted_basis is None and basis == BASIS_STDDEV and tracker_state.get("stddev_reference") is None
    )
    if status_before != STATUS_NONE and basis_mismatch:
        # The ticker's configured basis changed while a cycle was active (an
        # operator edited config/inverse_martingale.json mid-cycle). The
        # persisted per-cycle fields (lowest_drop_seen, crossed thresholds,
        # stddev_reference) were computed under the OLD basis and are
        # meaningless -- or actively misleading -- under the new one: a
        # sigma-valued low reinterpreted as percentage points can fire
        # incorrectly, and a percentage-basis cycle switched to "stddev"
        # has no stddev_reference at all, which (without this check) hits
        # the stddev_unavailable guard below on every future call, forever,
        # BEFORE ever reaching the age-abandonment check further down.
        # Reset the cycle outright rather than attempt to interpret or
        # migrate mismatched units (Codex review: "Reset active cycles when
        # the configured basis changes").
        return resolved_marker(
            trading_day,
            stream,
            # The floor travels as an absolute PRICE only -- the persisted
            # metric is denominated in the basis being abandoned. See
            # _preserved_latch_kwargs.
            **_preserved_latch_kwargs(tracker_state, persisted_basis, status_before, carry_drop_metric=False),
            # Same cursor-vs-reference-scale mismatch as the fire-resolution
            # sites (Codex review: "Retain the reference-scale cursor when a
            # continuation resolves") -- harmless when _preserved_latch_kwargs
            # returned no rearm_reference_close (resolved_marker only stores
            # this alongside one), but correct whenever it did: that latch is
            # cycle_reference_close, frozen on cycle_start_trading_day, not today.
            rearm_last_seen_day=rearm_cursor_seed(tracker_state.get("cycle_start_trading_day"), trading_day),
        ), InverseMartingaleStep(
            status_before=status_before,
            status_after=STATUS_NONE,
            accumulated_usd_before=accumulated_before,
            accumulated_usd_after=0.0,
            newly_crossed_tiers=(),
            fired=False,
            fire_amount_usd=0.0,
            reason="cycle_reset_basis_changed",
        )

    # Tier identity is tracked only by numeric threshold
    # (crossed_tier_thresholds), not by any config-independent index. If an
    # operator edits a tier's threshold while a cycle is active -- e.g.
    # 1.0 -> 1.1 -- the persisted crossed_before set (still {1.0}) no
    # longer contains the new 1.1, so the loop below would treat 1.1 as a
    # brand-new tier and credit its allocation a second time for the same
    # conceptual dip, on top of whatever else is newly crossed. Persist a
    # fingerprint of the whole tier table at cycle-start (mirroring the
    # basis check just above) and reset -- rather than attempt to remap old
    # thresholds to new ones -- on any later mismatch (Codex review: "Reset
    # or freeze cycles when tier thresholds change"). A tracker with no
    # persisted fingerprint at all (created before this field existed) is
    # not a mismatch -- backward compatible.
    #
    # unit_usd is part of this fingerprint too, not just the tier table --
    # it's the dollar-per-unit multiplier the loop below applies to every
    # newly-crossed tier's `units` count (accumulated += units * unit_usd).
    # A mid-cycle unit_usd edit changes only the SCALE, not the tier
    # table's threshold/units identity, so it previously slipped past this
    # check entirely: tiers crossed before the edit had already banked
    # dollars at the OLD unit_usd, and tiers crossed after would bank
    # dollars at the NEW one, into the very same accumulated_usd total --
    # a mixed-scale balance with no way to recover which portion used which
    # rate (Codex review: "Freeze the dollar unit for active cycles").
    current_tier_fingerprint = [unit_usd, [[threshold, units] for threshold, units in tiers]]
    persisted_tier_fingerprint = tracker_state.get("tier_config_fingerprint")
    if (
        status_before != STATUS_NONE
        and persisted_tier_fingerprint is not None
        and persisted_tier_fingerprint != current_tier_fingerprint
    ):
        return resolved_marker(
            trading_day,
            stream,
            # The basis is UNCHANGED here -- only the tier table/unit_usd moved
            # -- so the persisted metric is still in the right units and travels
            # alongside the absolute price.
            **_preserved_latch_kwargs(tracker_state, persisted_basis, status_before, carry_drop_metric=True),
            # See the identical comment at the basis-changed reset above.
            rearm_last_seen_day=rearm_cursor_seed(tracker_state.get("cycle_start_trading_day"), trading_day),
        ), InverseMartingaleStep(
            status_before=status_before,
            status_after=STATUS_NONE,
            accumulated_usd_before=accumulated_before,
            accumulated_usd_after=0.0,
            newly_crossed_tiers=(),
            fired=False,
            fire_amount_usd=0.0,
            reason="cycle_reset_tier_config_changed",
        )

    # near_close_dca.py's own caller (_inverse_martingale_reconciled_price)
    # already validates decision_price/previous_close for finiteness before
    # ever reaching this function -- but this pure function is also called
    # and unit-tested directly, with no such external guarantee, so it must
    # not silently trust an unvalidated caller. An infinite decision_price
    # in particular is dangerous, not merely wrong: drop_pct below goes to
    # -inf (a "price" infinitely above the reference), which makes ANY
    # active cycle's recovery check ((lowest_drop_seen - drop_metric) >=
    # recovery_offset) trivially true regardless of the real accumulated
    # balance's true price history -- firing a real synthetic order off a
    # non-finite input (Antigravity review: "Missing Validation for
    # Non-Finite / Non-Positive decision_price"; mirrors the identical,
    # already-fixed risk at the reconciliation layer -- see that function's
    # own "Reject non-finite reconciled prices" comment).
    if not math.isfinite(decision_price) or decision_price <= 0:
        return _unchanged(tracker_state, status_before, accumulated_before, "invalid_decision_price")
    if not math.isfinite(previous_close) or previous_close <= 0:
        return _unchanged(tracker_state, status_before, accumulated_before, "invalid_previous_close")

    # A multi-day cycle's drop must be measured against the price frozen at
    # cycle-start (cycle_reference_close), never against each call's own,
    # naturally-changing "previous_close" (which is just yesterday's real
    # close) -- otherwise an unchanged price across two days can read as a
    # 0% drop on day 2 even though the cycle's real drop from its start is
    # still deep, corrupting both the accumulated-tier math and the
    # recovery-offset comparison (Codex review: "Measure a cycle against its
    # snapshotted reference close"). Only a brand-new cycle (status "none")
    # has no snapshot yet, so it uses the caller's freshly supplied value.
    reference_close = (
        previous_close if status_before == STATUS_NONE else float(tracker_state.get("cycle_reference_close", previous_close))
    )
    if not math.isfinite(reference_close) or reference_close <= 0:
        return _unchanged(tracker_state, status_before, accumulated_before, "invalid_reference_close")
    drop_pct = (reference_close - decision_price) / reference_close
    if basis == BASIS_STDDEV:
        # Once a cycle has started, the sigma reference is frozen at whatever
        # was snapshotted into the tracker at cycle-start (see below) -- a
        # freshly recomputed sigma passed in on a later call must NEVER move
        # the goalposts mid-cycle. Only a brand-new cycle (status "none")
        # uses the caller's freshly supplied value.
        effective_stddev_reference = (
            tracker_state.get("stddev_reference") if status_before != STATUS_NONE else stddev_reference
        )
        # NaN compares false against BOTH `not effective_stddev_reference`
        # (bool(nan) is True, so `not nan` is False) and `<= 0` (every NaN
        # comparison is False) -- an explicit math.isfinite() check is
        # required or a NaN reference silently sails through into
        # `drop_pct / effective_stddev_reference` below, polluting
        # drop_metric (and therefore every downstream tier/recovery
        # comparison) with NaN instead of refusing outright (Antigravity
        # review: "Missing math.isfinite() Check on effective_stddev_
        # reference"). _validate_tracker_payload already rejects a
        # non-finite persisted stddev_reference before it ever reaches this
        # function via the ledger, and rolling_stddev's caller-supplied
        # value can no longer be non-finite either (see
        # trailing_daily_returns' own non-finite-return rejection) -- this
        # check is pure defense in depth for evaluate_inverse_martingale_
        # step's own direct/test callers, matching every other numeric
        # input boundary in this function (decision_price, previous_close,
        # reference_close) already getting an explicit isfinite() check.
        if (
            not effective_stddev_reference
            or not math.isfinite(effective_stddev_reference)
            or effective_stddev_reference <= 0
        ):
            return _unchanged(tracker_state, status_before, accumulated_before, "stddev_unavailable")
        drop_metric = drop_pct / effective_stddev_reference
    else:
        drop_metric = drop_pct * 100.0  # tiers are expressed in whole percent

    # decision_price/previous_close/reference_close are each individually
    # required finite (and positive) above, but that does not bound their
    # DIFFERENCE or QUOTIENT: a tiny reference_close combined with an
    # extreme-but-finite decision_price -- both individually legal inputs
    # -- can still overflow `(reference_close - decision_price) /
    # reference_close` past float's max representable magnitude, producing
    # a non-finite drop_pct/drop_metric despite every input passing its own
    # check. Left unchecked, an active dipping cycle would then read
    # `lowest_drop_seen - (-inf)` as infinite in the recovery comparison
    # below, unconditionally satisfying ANY configured recovery_offset and
    # firing a real synthetic order from numerically invalid market data,
    # not a genuine price recovery (Codex review: "Reject non-finite
    # derived drop metrics"). Fail closed exactly like the reference_close/
    # stddev_reference checks just above -- no reset, no tier credit, just
    # skip this evaluation.
    if not math.isfinite(drop_metric):
        return _unchanged(tracker_state, status_before, accumulated_before, "invalid_drop_metric")

    new_state = dict(tracker_state)

    # Cycle-abandonment cutoffs. The settlement_pending lease (checked way
    # above, immediately after the already_evaluated_today guard) already
    # refused to reach this point at all while pending -- see that check's
    # own comment for why it must run before EVERY reset/abandonment path
    # in this function, not just this one.
    cycle_start = tracker_state.get("cycle_start_trading_day")
    if status_before != STATUS_NONE and cycle_start:
        exhausted_attempts = status_before == STATUS_FIRING and fire_attempts >= max_fire_attempts
        if trading_days_active > max_cycle_days or exhausted_attempts:
            # An abandoned cycle never spent any real money, but leaving
            # fired_floor_drop_metric unset (the original version of this
            # reset) let a LATER canonical stream that same trading day --
            # not blocked by the same-stream-only already_evaluated_today
            # guard above -- see an unchanged, still-below-tier price as a
            # brand-new dip and immediately re-arm a fresh cycle for the
            # SAME underlying descent, resetting trading_days_active/
            # fire_attempts back to zero and giving the identical stuck
            # decline another full max_cycle_days/max_fire_attempts budget
            # -- defeating the very cutoffs this block exists to enforce
            # (Codex review: "Prevent abandoned cycles from immediately
            # rearming"). Reusing the SAME same-day floor mechanism the two
            # fire-resolution sites already use closes this the identical
            # way: a later same-day stream only arms a new cycle once
            # today's drop genuinely exceeds this abandoned cycle's own
            # deepest point, not merely restates it. max() with this call's
            # own freshly computed drop_metric (not just the stale persisted
            # lowest_drop_seen) covers the case where THIS very evaluation
            # -- the one that triggers abandonment -- is itself a new low.
            abandonment_floor = max(float(tracker_state.get("lowest_drop_seen", drop_metric)), drop_metric)
            # Also carry the durable rearm_reference_close latch, exactly
            # like the two real fire-resolution sites below. "An abandoned
            # cycle never spent any real money" (the comment above) is true
            # for a STATUS_DIPPING abandonment that never fired at all, but
            # NOT for a STATUS_FIRING one: a per_order_cap_usd-capped
            # continuation can have already placed one or more real,
            # SETTLED partial chunks over prior runs before this evaluation
            # finally exhausts max_fire_attempts/max_cycle_days on the
            # remainder. Abandoning without the durable latch let a
            # still-depressed price on the very next evaluation start a
            # brand-new cycle and re-credit tiers that had already
            # contributed to that earlier, real, already-placed spend
            # (Codex review, third round: "Preserve the latch when
            # abandoning a partially paid fire"). Setting it unconditionally
            # here (covering the never-fired STATUS_DIPPING case too) is a
            # harmless, strictly conservative over-application -- it only
            # means a cycle that truly spent nothing also waits for price to
            # reach the original reference before rearming, matching this
            # module's fail-closed posture everywhere else.
            return resolved_marker(
                trading_day,
                stream,
                fired_floor_drop_metric=abandonment_floor,
                fired_floor_price=_drop_metric_to_price(
                    reference_close, abandonment_floor, basis, tracker_state.get("stddev_reference")
                ),
                rearm_reference_close=reference_close,
                # The cursor must match the price SCALE the latch is
                # actually armed at, not today. reference_close here is
                # cycle_reference_close -- frozen on cycle_start_trading_day,
                # potentially days or weeks before this abandonment fires --
                # so stamping today as "last examined" would let the very
                # next split lookup start its `since` from today and exclude
                # every archive row between cycle start and today via
                # `row_date <= since`, permanently missing a corporate
                # action that occurred during the abandoned cycle (Codex
                # review: "Retain the reference-scale cursor when a
                # continuation resolves"). Falling back to today only if a
                # legacy/corrupted-then-repaired tracker somehow lacks
                # cycle_start_trading_day here, matching this function's
                # existing fail-closed posture elsewhere.
                rearm_last_seen_day=rearm_cursor_seed(tracker_state.get("cycle_start_trading_day"), trading_day),
            ), InverseMartingaleStep(
                status_before=status_before,
                status_after=STATUS_NONE,
                accumulated_usd_before=accumulated_before,
                accumulated_usd_after=0.0,
                newly_crossed_tiers=(),
                fired=False,
                fire_amount_usd=0.0,
                reason=(
                    "cycle_abandoned_max_age" if trading_days_active > max_cycle_days
                    else "cycle_abandoned_max_attempts"
                ),
            )

    if status_before == STATUS_FIRING:
        # A prior run already triggered recovery but couldn't place the full
        # amount (blocked by budget/order-count caps, or a concurrent normal
        # BUY for this ticker -- see near_close_dca.py's injection site). The
        # fire amount is frozen at the moment recovery was first detected:
        # further price movement during an in-progress multi-run fire must
        # not grow or shrink the amount already decided.
        # Capped, broker-minimum-floored, and adjusted so a per_order_cap_usd
        # tighter than the balance never strands a sub-minimum remainder --
        # see _capped_fire_amount's own docstring.
        fire_amount = _capped_fire_amount(accumulated_before, per_order_cap_usd)
        if fire_amount < _BROKER_MINIMUM_ORDER_USD:
            # The whole remaining balance is below what the broker will accept,
            # so there is no placeable chunk this run. Hold it as an unchanged
            # continuation rather than rounding up: no order is attempted, so
            # fire_attempts is deliberately NOT incremented either. The cycle
            # still ages normally and max_cycle_days remains its backstop if the
            # balance never grows back above the minimum.
            new_state["last_evaluated_trading_day"] = trading_day.isoformat()
            new_state["last_evaluated_stream"] = stream
            new_state["last_evaluated_is_real"] = True
            new_state["trading_days_active"] = trading_days_active
            return new_state, InverseMartingaleStep(
                status_before=status_before,
                status_after=STATUS_FIRING,
                accumulated_usd_before=accumulated_before,
                accumulated_usd_after=accumulated_before,
                newly_crossed_tiers=(),
                fired=False,
                fire_amount_usd=0.0,
                reason="continuation_below_broker_minimum",
            )
        remainder = max(0.0, round(accumulated_before - fire_amount, 2))
        new_state["accumulated_usd"] = remainder
        new_state["fire_attempts"] = fire_attempts + 1
        new_state["last_evaluated_trading_day"] = trading_day.isoformat()
        # Stamped here too (overwritten below by resolved_marker() itself
        # when remainder <= 0, which already sets it correctly) -- see the
        # new STATUS_FIRING same-stream retry guard above this branch for
        # why this must always reflect the stream that most recently
        # touched this continuation, not a stale value inherited from a
        # PRIOR, already-resolved cycle.
        new_state["last_evaluated_stream"] = stream
        # A REAL evaluation, unlike near_close_dca.py's age-only bookkeeping
        # for a missing-data session -- see the same-stream guard's own
        # comment on `last_evaluated_is_real` for why this must be restamped
        # True every time a genuine evaluation runs, not just left at
        # whatever an earlier age-only advance the same day set it to.
        new_state["last_evaluated_is_real"] = True
        new_state["trading_days_active"] = trading_days_active
        if remainder <= 0:
            # Leased, not immediately final: see the settlement_pending
            # guard above for why an unleased STATUS_NONE marker written
            # here (before broker placement is even known) is unsafe (Codex
            # review: "Keep full-fire claims leased until settlement").
            # near_close_dca.py's deferred settlement step strips this flag
            # once the real placement outcome confirms the claim, or rebuilds
            # a rollback continuation from the ORIGINAL pre-claim state (not
            # this marker) if placement failed. fired_floor_drop_metric
            # records how deep THIS fire's balance was accumulated against
            # (tracker_state's own persisted low, unchanged by this branch)
            # -- see the same-day re-arm guard below for why a later same-
            # day evaluation from a DIFFERENT stream must not treat a price
            # still within that already-paid-for range as a fresh dip.
            new_state = resolved_marker(
                trading_day,
                stream,
                fired_floor_drop_metric=tracker_state.get("lowest_drop_seen"),
                fired_floor_price=_drop_metric_to_price(
                    reference_close, tracker_state.get("lowest_drop_seen"), basis, tracker_state.get("stddev_reference")
                )
                if tracker_state.get("lowest_drop_seen") is not None
                else None,
                rearm_reference_close=tracker_state.get("cycle_reference_close"),
                # cycle_reference_close is frozen from cycle_start_trading_day
                # -- potentially several runs/days before this multi-run
                # continuation finally resolves -- so stamping TODAY as
                # "last examined" understates how far back the archive
                # still needs re-checking from. The next split lookup's
                # `since` would start at today and exclude every row back
                # to cycle start via `row_date <= since`, permanently
                # missing a corporate action that occurred anywhere during
                # this continuation's lifetime (Codex review: "Retain the
                # reference-scale cursor when a continuation resolves").
                rearm_last_seen_day=rearm_cursor_seed(tracker_state.get("cycle_start_trading_day"), trading_day),
            )
            new_state["settlement_pending"] = True
            status_after = STATUS_NONE
        else:
            status_after = STATUS_FIRING
            # Marks this partial-fire continuation as claimed-but-not-yet-
            # settled -- see the settlement_pending guard above for why a
            # plain, freely-claimable STATUS_FIRING remainder is unsafe.
            new_state["settlement_pending"] = True
        return new_state, InverseMartingaleStep(
            status_before=status_before,
            status_after=status_after,
            accumulated_usd_before=accumulated_before,
            accumulated_usd_after=remainder,
            newly_crossed_tiers=(),
            fired=True,
            fire_amount_usd=fire_amount,
            reason="fire_continuation",
        )

    if status_before == STATUS_NONE:
        rearm_reference_close = tracker_state.get("rearm_reference_close")
        if rearm_reference_close is not None:
            # Rebase the latch across a CONFIRMED corporate action. ``split_factor``
            # comes from :func:`archived_split_factor` -- the archive's own
            # ``Stock Splits`` column, the real ratio the data source reported on
            # the split's effective date -- computed by the caller exactly as
            # ``stddev_reference`` is, so this function stays pure.
            #
            # It replaces a price-ratio heuristic that could not tell a corporate
            # action from an ordinary gap. Any close-to-close move near a whole
            # factor was classified as a split, and moves that large are entirely
            # realistic for the volatile tickers this mode targets: an 80 -> 120
            # news gap read as a reverse 3-for-2 and rebased a $100 latch to $150
            # rather than recognizing price had recovered past it, while a ~50%
            # crash read as a 2-for-1, rebased the reference onto the crashed
            # price and let the latch clear -- re-crediting an already-paid-for
            # descent (Codex review: "Require corporate-action evidence before
            # rebasing"). Metadata has no such failure mode: an ordinary gap,
            # however violent, records no split.
            #
            # Both directions are handled by one division. A k-for-1 FORWARD
            # split records k and divides the reference down onto the smaller
            # post-split prices; a 1-for-k REVERSE split records 1/k and divides
            # it up. The reverse case specifically must be caught HERE, before
            # the recovery check below -- both closes jump above a pre-split
            # reference, which the latch would otherwise read as the dip having
            # recovered, clear itself, and let the same descent be bought twice.
            #
            # ``None`` means the archive could not confirm anything (absent,
            # unreadable, or stale) and is deliberately NOT treated as "no
            # split": the reference is left exactly as it is. That is the
            # fail-closed direction -- an unrebased latch suppresses buying,
            # which costs opportunity, while a wrongly rebased one can authorize
            # a duplicate purchase. A confirmed 1.0 (a fresh archive with no
            # split in the window) divides by one and changes nothing.
            if split_factor is not None and split_factor > 0 and split_factor != 1.0:
                rearm_reference_close = rearm_reference_close / split_factor
                tracker_state = dict(tracker_state)
                tracker_state["rearm_reference_close"] = rearm_reference_close
                new_state = dict(tracker_state)
            # Recorded on EVERY evaluation while the latch is armed (not
            # just when a split is detected) so the NEXT evaluation always
            # has yesterday's real close to compare against -- two
            # evaluations on the SAME trading day (a later scheduled
            # stream) compare an unchanged previous_close to itself
            # (ratio == 1.0), which never matches a split factor, so no
            # special same-day case is needed here.
            tracker_state = dict(tracker_state)
            # The cursor is the newest archive day actually EXAMINED, not
            # today. archived_split_factor excludes rows after as_of as
            # lookahead, so stamping today skipped a split's own effective-day
            # row forever: excluded as lookahead on the day itself, then
            # excluded as `row_date <= since` on every later run (Codex review:
            # "Keep the split cursor on the last examined archive day").
            #
            # Left UNCHANGED -- not stamped to today -- when the archive could
            # not be consulted at all (temporarily missing, unreadable, or
            # stale). A prior version fell back to today here, which advanced
            # the cursor past every row between the OLD cursor and today even
            # though none of them were actually examined: once the archive
            # recovered, archived_split_factor's `row_date <= since` exclusion
            # then skipped that entire unexamined window forever, permanently
            # missing any split inside it -- stranding a forward-split latch
            # at its pre-split reference, or letting a reverse split clear it
            # and re-credit an already-paid descent (Codex review: "Retain the
            # split cursor when lookup fails"). Leaving the field exactly as
            # tracker_state already had it (absent, or the last CONFIRMED
            # examined day) means the next evaluation retries from that same
            # unexamined window instead of silently skipping it.
            if split_cursor_day is not None:
                tracker_state["rearm_last_seen_day"] = split_cursor_day.isoformat()
            new_state = dict(tracker_state)
        # previous_close is a PER-DAY value -- every evaluation of this
        # ticker on the same trading day (morning/midday/afternoon streams)
        # observes the exact same closing print, since it hasn't changed
        # since yesterday. An EQUAL previous_close only carries genuinely NEW
        # "price has recovered" information the first time a DIFFERENT
        # trading day evaluates this latch -- on a repeat same-day
        # observation it isn't "today's close recovering" at all, it's the
        # SAME data point being read again (routine on the very day of the
        # fire, since that fire's own reference usually WAS that day's
        # previous close). A STRICTLY GREATER previous_close, by contrast, is
        # unambiguous exit evidence regardless of which day observed it --
        # even a repeated same-day value already above the reference means
        # this ticker had already recovered before this latch was even worth
        # consulting (see test_same_day_rearm_check_uses_the_frozen_
        # reference_price_not_a_fresh_one, where the fire's OWN previous_close
        # already exceeded the reference).
        previous_close_is_fresh = tracker_state.get("last_evaluated_trading_day") != trading_day.isoformat()
        latch_exited = rearm_reference_close is not None and (
            decision_price >= rearm_reference_close
            or previous_close > rearm_reference_close
            or (previous_close_is_fresh and previous_close >= rearm_reference_close)
        )
        if rearm_reference_close is not None and not latch_exited:
            # A completed fire remains latched while price is still in the
            # descent that produced it.  In particular, do not discard this
            # protection at the next trading-day boundary: scheduled-run
            # retries deliberately do not discover their own prior order, so
            # a date-scoped marker could credit the same tiers again merely
            # because the unchanged dip was evaluated on a new day.
            #
            # The durable latch is AUTHORITATIVE over the separate same-day
            # floor guard below -- it is never deferred to based on a
            # metric-based shortcut. The floor's own drop_metric is computed
            # from THIS call's fresh previous_close, which can differ
            # substantially from the frozen reference the durable latch
            # itself is measured against (e.g. a cycle referenced to $100
            # that bottomed at $95 firing on a later day with
            # previous_close=96 still below the $100 reference): a shallow
            # metric relative to that fresh previous_close could satisfy the
            # floor's own "<=" check while the ORIGINAL dip -- measured
            # against the $100 reference the latch actually protects -- has
            # never been exited at all, incorrectly re-arming and
            # re-crediting tiers (Codex review, third round: "Keep the
            # durable latch authoritative over the same-day floor"). The
            # floor guard below only ever matters once the durable latch has
            # itself cleared (latch_exited above, or no latch exists at
            # all) -- as a strictly WEAKER, narrower protection than the
            # latch's own "anything below the reference" block, it has
            # nothing left to add while the latch is still armed.
            return _unchanged(tracker_state, status_before, accumulated_before, "awaiting_fresh_dip")
        if latch_exited:
            # Price has exited the paid-for dip.  Clear the DURABLE latch
            # before evaluating a subsequent descent.  A LATER day's close
            # counts as that exit too: today's below-reference print is then
            # precisely the fresh descent for which the latch was waiting.
            # An EQUAL previous_close/decision_price counts as exit too --
            # "at or above the reference", mirroring recovery's own "at or
            # above the offset" semantics elsewhere in this function -- not
            # merely ">" (Codex review, second round: "Treat an equal prior
            # close as latch recovery") -- see latch_exited's own comment
            # above for why an EQUAL previous_close specifically only
            # participates when previous_close_is_fresh.
            #
            # fired_floor_drop_metric/fired_floor_price (and the
            # last_evaluated_trading_day/last_evaluated_stream that gate
            # them) are a SEPARATE, day-scoped protection against a later
            # same-day slot re-crediting a descent a fire already covered
            # TODAY (see the same-day floor check just below) -- unlike the
            # durable latch above, they remain meaningful only through the
            # END of the trading day the fire happened on, and stop applying
            # naturally once the day rolls over (their own guard already
            # requires last_evaluated_trading_day == trading_day). Preserve
            # them here ONLY when still within that same day; wiping the
            # whole tracker_state unconditionally discarded that protection
            # on the very day of the fire whenever price also happened to
            # close back above the ORIGINAL pre-descent reference the same
            # day, letting a later same-day stream re-credit tiers within
            # the range that fire already paid for. Once the day has moved
            # on, those fields are already inert -- clear everything, same
            # as before.
            if tracker_state.get("last_evaluated_trading_day") == trading_day.isoformat():
                tracker_state = dict(tracker_state)
                tracker_state.pop("rearm_reference_close", None)
                tracker_state.pop("rearm_last_seen_day", None)
                new_state = dict(tracker_state)
            else:
                tracker_state = {}
                new_state = {}
        same_day_fire_floor = tracker_state.get("fired_floor_drop_metric")
        fired_floor_price = tracker_state.get("fired_floor_price")
        # Two ways to decide "hasn't broken below the floor yet":
        #
        # 1. PRICE comparison (preferred): fired_floor_price was computed
        #    once, at the moment the floor was set, from the frozen
        #    reference frame (cycle_reference_close/stddev_reference) that
        #    call actually used -- comparing it against a fresh
        #    decision_price needs no unit conversion and is immune to any
        #    reference drift.
        # 2. METRIC comparison (legacy fallback, only when no price is
        #    available -- e.g. a marker written before this field existed):
        #    same_day_fire_floor was ALSO computed against that same frozen
        #    reference frame, but drop_metric here is computed against
        #    THIS call's own fresh previous_close -- a DIFFERENT reference
        #    once enough time has passed for previous_close to drift from
        #    the original cycle_reference_close. A price that recovered
        #    since the cycle started, but stayed flat since the actual
        #    fire/abandonment moment, could then read as a "deeper" drop
        #    than the stored floor purely from that mismatch, incorrectly
        #    re-arming and re-crediting an already-paid-for price level
        #    (Codex review: "Compare the rearm floor in the same price
        #    frame").
        if fired_floor_price is not None and math.isfinite(fired_floor_price) and fired_floor_price > 0:
            floor_not_yet_broken = decision_price >= fired_floor_price
        elif same_day_fire_floor is not None:
            floor_not_yet_broken = drop_metric <= same_day_fire_floor
        else:
            floor_not_yet_broken = False  # no floor recorded at all -- this guard doesn't apply
        if floor_not_yet_broken and tracker_state.get("last_evaluated_trading_day") == trading_day.isoformat():
            # A cycle resolved via a REAL FIRE earlier today (from a
            # DIFFERENT stream -- a same-stream retry never reaches this
            # point at all, already refused by the guard above) already
            # spent real money covering every drop down to this floor.
            # Today's fixed prior_close-relative drop_metric alone cannot
            # tell "a genuine new descent" apart from "still within the
            # range that fire already bought" -- a price that merely
            # stayed flat, or ticked only slightly, since that fire would
            # otherwise look like a brand-new dip to this fresh evaluation
            # and re-credit (and potentially re-fire) for the SAME
            # underlying descent (Codex review: "Require a fresh descent
            # before rearming in a later slot"). Refuse to arm -- exactly
            # like "nothing crossed this call" below -- until price
            # genuinely falls BELOW the floor the last fire already
            # covered. Naturally stops applying once the trading day rolls
            # over: last_evaluated_trading_day no longer matches, and
            # tomorrow's evaluation starts from a fresh prior_close anyway.
            return _unchanged(tracker_state, status_before, accumulated_before, "same_day_fire_floor_not_broken")

    # status_before in {"none", "dipping"}: look for newly crossed tiers.
    newly_crossed: list[float] = []
    accumulated = accumulated_before
    for threshold, units in tiers:
        if drop_metric >= threshold and threshold not in crossed_before:
            newly_crossed.append(threshold)
            tier_credit = round(units * unit_usd, 2)
            if tier_credit <= 0:
                # A valid but tiny unit_usd/units combination (e.g.
                # unit_usd=0.001, units=1) rounds to exactly $0.00 at cent
                # precision -- crediting nothing for a tier that just
                # legitimately crossed, while still marking it as crossed
                # (so it can never be credited again either), and persisting
                # an active "dipping"/"firing" cycle with a $0.00 balance
                # that _validate_tracker_payload's positive-balance check
                # now correctly refuses to read back -- permanently
                # bricking the ticker's ledger (Codex review: "Preserve
                # sub-cent tier credits before persisting"). Floor every
                # newly-crossed tier's own credit to one cent -- small
                # enough to be a pure rounding artifact of an
                # unrealistically tiny unit_usd, but never zero -- so a
                # crossed tier always contributes a genuinely positive,
                # persistable balance.
                tier_credit = 0.01
            accumulated = round(accumulated + tier_credit, 2)
            if not math.isfinite(accumulated):
                # A schema-valid but extreme unit_usd/units combination
                # (e.g. unit_usd=1e200 with a units=1e200 tier) can produce
                # a `units * unit_usd` product that overflows to `inf`
                # (`round(inf, 2)` returns `inf` unchanged, and `inf <= 0`
                # is False, so the sub-cent floor above never catches it).
                # Persisting an infinite accumulated_usd would pass this
                # call cleanly, but the NEXT ledger read hits _validate_
                # tracker_payload's own finiteness check and raises
                # CorruptedLedgerStateError, permanently bricking the
                # ticker (skip-only-this-ticker, but with no in-repo path
                # back to a working balance) instead of refusing up front
                # here where the real cause is still visible (Codex
                # review: "Reject overflowing tier credits before
                # persistence"). Fail closed: return the tracker
                # completely UNCHANGED, crediting nothing for this
                # evaluation, rather than persisting a partially-credited,
                # non-finite balance.
                return _unchanged(tracker_state, status_before, accumulated_before, "invalid_tier_credit")

    if status_before == STATUS_NONE and not newly_crossed:
        # No cycle running and nothing crossed this call: nothing to do.
        return _unchanged(tracker_state, status_before, accumulated_before, "no_action")

    # We are dipping -- either just starting a fresh cycle this call, or
    # continuing one. The cycle low (lowest_drop_seen) must be updated on
    # EVERY dipping evaluation, not only when a brand-new tier crosses:
    # once every configured tier is already crossed, or between two tier
    # thresholds, price can still fall further without crossing anything
    # new, and a later recovery must be measured from that true bottom, not
    # a stale earlier low (Codex review: "Update the cycle low even when no
    # tier is crossed").
    new_state["status"] = STATUS_DIPPING
    new_state["accumulated_usd"] = accumulated
    if newly_crossed:
        new_state["crossed_tier_thresholds"] = sorted(crossed_before | set(newly_crossed))
    # Whether THIS evaluation is starting a brand-new cycle, decided before the
    # setdefault below actually stamps the field. resolved_marker() never
    # carries cycle_start_trading_day (nor trading_days_active), so its absence
    # is exactly "no cycle is currently running".
    starting_a_new_cycle = "cycle_start_trading_day" not in new_state
    new_state.setdefault("cycle_start_trading_day", trading_day.isoformat())
    new_state.setdefault("cycle_reference_close", reference_close)
    new_state.setdefault("basis", basis)
    new_state.setdefault("tier_config_fingerprint", current_tier_fingerprint)
    if basis == BASIS_STDDEV and "stddev_reference" not in new_state:
        new_state["stddev_reference"] = stddev_reference
    low = float(new_state.get("lowest_drop_seen", drop_metric))
    new_state["lowest_drop_seen"] = max(low, drop_metric)
    new_state["last_evaluated_trading_day"] = trading_day.isoformat()
    # Stamped alongside last_evaluated_trading_day, not left to whatever
    # stale value tracker_state's own dict copy carried forward from a
    # PRIOR, already-resolved cycle (resolved_marker() is the only other
    # place this field is set) -- see the new STATUS_FIRING same-stream
    # retry guard below for why an accurate, always-current value here
    # matters, not just for STATUS_NONE.
    new_state["last_evaluated_stream"] = stream
    # See the STATUS_FIRING same-stream guard's own comment on
    # `last_evaluated_is_real` -- restamped True on every real evaluation,
    # including this dipping/tier-crossing one, so a same-day age-only
    # advance's own False stamp never lingers past the next genuine
    # evaluation of any kind.
    new_state["last_evaluated_is_real"] = True
    # A new cycle is on its FIRST trading day by definition, whatever the
    # marker it replaced said. `trading_days_active` was computed near function
    # entry from the state as found, and its increment is suppressed when
    # last_evaluated_trading_day already equals today -- which is exactly the
    # case when a completed cycle exits its latch and re-arms in a LATER STREAM
    # on the same trading day, since that path deliberately preserves the
    # resolved marker's own last_evaluated_trading_day. The freshly armed cycle
    # then started at 0 instead of 1 and was allowed to stay active for one
    # extra session past max_cycle_days, able to fire after the configured
    # cutoff (Codex review: "Reset the age counter when rearming on the same
    # day"). Initialize it independently instead of inheriting the old cycle's
    # bookkeeping; a CONTINUING cycle keeps the computed value unchanged.
    new_state["trading_days_active"] = 1 if starting_a_new_cycle else trading_days_active

    if newly_crossed:
        # A newly-crossed tier is never itself a recovery trigger (drop_metric
        # just increased to reach it), so no recovery check is needed here.
        return new_state, InverseMartingaleStep(
            status_before=status_before,
            status_after=STATUS_DIPPING,
            accumulated_usd_before=accumulated_before,
            accumulated_usd_after=accumulated,
            newly_crossed_tiers=tuple(newly_crossed),
            fired=False,
            fire_amount_usd=0.0,
            reason="tier_crossed",
        )

    lowest_drop_seen = new_state["lowest_drop_seen"]
    recovered = (lowest_drop_seen - drop_metric) >= recovery_offset
    if recovered and accumulated_before > 0:
        # Capped, broker-minimum-floored, and adjusted so a per_order_cap_usd
        # tighter than the balance never strands a sub-minimum remainder --
        # see _capped_fire_amount's own docstring.
        fire_amount = _capped_fire_amount(accumulated_before, per_order_cap_usd)
        if fire_amount < _BROKER_MINIMUM_ORDER_USD:
            # Recovery triggered, but the tracked balance is below the broker
            # minimum. Defer rather than over-buy: the cycle stays DIPPING with
            # its balance intact, so a deeper later dip can still grow it past
            # the minimum, and max_cycle_days remains the backstop if it never
            # does. Nothing is bought, which is the accumulator's own contract
            # for a descent it cannot act on.
            new_state["last_evaluated_trading_day"] = trading_day.isoformat()
            new_state["last_evaluated_stream"] = stream
            new_state["last_evaluated_is_real"] = True
            return new_state, InverseMartingaleStep(
                status_before=status_before,
                status_after=STATUS_DIPPING,
                accumulated_usd_before=accumulated_before,
                accumulated_usd_after=accumulated_before,
                newly_crossed_tiers=(),
                fired=False,
                fire_amount_usd=0.0,
                reason="recovery_below_broker_minimum",
            )
        remainder = max(0.0, round(accumulated_before - fire_amount, 2))
        if remainder <= 0:
            # Leased until settlement -- see the settlement_pending guard's
            # comment and the identical branch in the STATUS_FIRING
            # continuation case above (Codex review: "Keep full-fire claims
            # leased until settlement"). fired_floor_drop_metric uses the
            # local `lowest_drop_seen` computed just above (this cycle's
            # true bottom), not tracker_state's own stale copy -- see the
            # same-day re-arm guard below for why this matters.
            new_state = resolved_marker(
                trading_day,
                stream,
                fired_floor_drop_metric=lowest_drop_seen,
                fired_floor_price=_drop_metric_to_price(
                    reference_close, lowest_drop_seen, basis, tracker_state.get("stddev_reference")
                ),
                rearm_reference_close=reference_close,
                # cycle_start_trading_day -- for a continuing cycle, the
                # ORIGINAL older start day whose price scale produced the
                # persisted cycle_reference_close this call is using; for a
                # cycle that just started THIS call (reference_close copied
                # from previous_close, which belongs to the PRECEDING
                # session, not trading_day itself), trading_day is that same
                # reference day. Either way rearm_cursor_seed() pulls the
                # cursor back one further calendar day so a split or
                # distribution effective ON that reference day still
                # satisfies the live lookup's exclusive `since` boundary on
                # the very first search under this fresh latch (Codex
                # review: "Retain the reference-scale cursor when a
                # continuation resolves"; "Seed the latch cursor before a
                # split-day cycle starts"; "Include the cycle-start day in
                # the live lookup"). new_state's own cycle_start_trading_day
                # is always populated by now (the setdefault() near function
                # entry stamps it to trading_day for a cycle starting THIS
                # call), so no separate starting_a_new_cycle branch is
                # needed here.
                rearm_last_seen_day=rearm_cursor_seed(new_state.get("cycle_start_trading_day"), trading_day),
            )
            new_state["settlement_pending"] = True
            status_after = STATUS_NONE
        else:
            new_state["status"] = STATUS_FIRING
            new_state["accumulated_usd"] = remainder
            new_state["fire_attempts"] = 1
            new_state["settlement_pending"] = True
            status_after = STATUS_FIRING
        return new_state, InverseMartingaleStep(
            status_before=status_before,
            status_after=status_after,
            accumulated_usd_before=accumulated_before,
            accumulated_usd_after=remainder,
            newly_crossed_tiers=(),
            fired=True,
            fire_amount_usd=fire_amount,
            reason="recovery_triggered",
        )
    return new_state, InverseMartingaleStep(
        status_before=status_before,
        status_after=STATUS_DIPPING,
        accumulated_usd_before=accumulated_before,
        accumulated_usd_after=accumulated_before,
        newly_crossed_tiers=(),
        fired=False,
        fire_amount_usd=0.0,
        reason="awaiting_recovery",
    )


_VALID_TRACKER_STATUSES = (STATUS_NONE, STATUS_DIPPING, STATUS_FIRING)

#: The three canonical scheduled slots that ever call
#: evaluate_inverse_martingale_step() (see resolved_marker's own docstring
#: and near_close_dca.py's run_dca.SLOT_MAP) -- the only values
#: last_evaluated_stream should ever legitimately hold.
_VALID_STREAMS = ("morning", "midday", "afternoon")


def _expected_lease_ref_id(stream: Optional[str], claim_day_iso: str, ticker: str) -> str:
    """Rebuild the deterministic ``settlement_ref_id`` a fire claim must have
    written for ``(stream, claim_day_iso, ticker)``.

    Deliberately duplicates ``near_close_dca._graded_dca_ref_id_prefix``'s
    format rather than importing it: near_close_dca.py imports FROM this
    module (claim_fire, resolved_marker, evaluate_inverse_martingale_step),
    so importing back would be circular. Kept honest against drift by
    test_lease_ref_id_matches_the_claim_sites_own_prefix_builder, which
    asserts byte-for-byte agreement with the real builder across every
    stream -- a validator that silently diverged from the format it is
    checking would reject every future legitimate lease, not just damaged
    ones (Codex review: "Validate the settlement ref against its lease
    fields").

    ``stream=None`` (a legacy lease predating ``settlement_stream``) maps to
    the historical single-stream ``"afternoon"`` convention -- the only
    format that era's claim sites ever wrote.
    """
    parts = ["graded-dca"]
    effective_stream = stream if stream is not None else "afternoon"
    if effective_stream != "afternoon":
        parts.append(effective_stream)
    parts.append("imart")
    return f"{'-'.join(parts)}-{claim_day_iso}-{ticker}"


def _validate_tracker_payload(data: dict[str, Any], state_path: Path) -> None:
    """Raise :class:`CorruptedLedgerStateError` for a field type that would otherwise
    crash :func:`evaluate_inverse_martingale_step` (e.g. ``float("corrupt")``) with an
    uncaught ``ValueError``/``TypeError`` well past this module's own boundary, OR for a
    syntactically valid but semantically impossible value that would silently produce a
    real, unearned purchase instead of crashing (e.g. a nonpositive balance in an active
    cycle -- see the ``accumulated_usd`` check below).

    ``_read_locked`` validates only the JSON envelope/version/ticker; a syntactically
    valid tracker with e.g. ``"accumulated_usd": "corrupt"`` passed that unchanged and
    then blew up deep inside the pure decision function, outside the narrow
    ``try/except CorruptedLedgerStateError`` around ``read()`` at the near_close_dca.py
    injection site -- aborting the ENTIRE scheduled run over one ticker's damaged state
    instead of the documented "skip only this ticker" contract (Codex review: "Validate
    ledger payload fields before returning"). Beyond crash prevention, fields are also
    checked here when an out-of-range-but-well-typed value would sail through every
    downstream check and still cause real harm -- not only a crash or a mispriced order,
    but WRONG IDEMPOTENCY behavior too: ``last_evaluated_trading_day``/
    ``last_evaluated_stream`` are compared (via ``==``/``in``), never parsed, so no type
    of value there can crash -- but a garbage value that never legitimately matches
    "today"/a real stream defeats the same-day and same-stream retry guards those
    comparisons implement (Codex review: "Validate persisted retry identity fields"),
    which is exactly the kind of real harm this function otherwise exists to catch.
    """

    def _corrupted(field: str, value: Any) -> CorruptedLedgerStateError:
        return CorruptedLedgerStateError(f"{state_path} has invalid {field!r}: {value!r}")

    status = data.get("status")
    if status is not None and status not in _VALID_TRACKER_STATUSES:
        raise _corrupted("status", status)

    _validate_settlement_fields(data, state_path, _corrupted)
    _validate_boolean_and_basis_fields(data, _corrupted)
    _validate_numeric_fields(data, status, _corrupted)
    _validate_cycle_abandonment_fields(data, status, _corrupted)
    _validate_tier_crossing_fields(data, status, _corrupted)
    _validate_fingerprint_and_identity_fields(data, status, _corrupted)


def _validate_settlement_fields(
    data: dict[str, Any], state_path: Path, _corrupted: Callable[[str, Any], CorruptedLedgerStateError]
) -> None:
    settlement_pending = data.get("settlement_pending")
    if settlement_pending is not None and not isinstance(settlement_pending, bool):
        raise _corrupted("settlement_pending", settlement_pending)
    # The danger a bare settlement_pending flag poses is that every downstream
    # call site reads it with a plain truthy check (this module's own claim
    # guard, and near_close_dca.py's age-only/disabled-enrollment cleanup
    # passes) and returns early before any age or abandonment logic runs. A
    # restored or hand-edited tracker carrying the flag with nothing real to
    # settle against would therefore freeze that ticker's balance and age
    # forever, immune to max_cycle_days, a disabled enrollment, or removal
    # from the tradable universe (Codex review: "Reject leases on dipping
    # trackers").
    #
    # That check was originally expressed as "a STATUS_DIPPING tracker can
    # never carry this lease", on the premise that the only writers were
    # evaluate_inverse_martingale_step's own resolved-marker and partial-
    # continuation shapes. near_close_dca.py's fire claim now writes a
    # RECOVERABLE lease instead -- deliberately the untouched PRE-fire state
    # (routinely "dipping") plus the settlement_* metadata that lets a later
    # invocation reconcile the claim against broker order history -- so
    # status alone no longer separates a real lease from a bogus one. What
    # does separate them is that metadata, so the rule below is stated
    # directly in those terms: a lease is legitimate exactly when it carries
    # everything needed to resolve it, whatever its status.
    if settlement_pending and not isinstance(data.get("settlement_target_state"), dict):
        raise _corrupted("settlement_pending", settlement_pending)

    # The crash-lease fields near_close_dca.py's claim site writes alongside
    # settlement_pending. Its recovery pass reads them as FINANCIAL inputs:
    # settlement_claimed_usd is converted with a bare float() outside any
    # exception handler, settlement_target_state is applied verbatim as the
    # ticker's next persisted state, and settlement_trading_day decides whether
    # an unmatched lease may be released at all. Unvalidated, a syntactically
    # valid but damaged/restored tracker such as `"settlement_claimed_usd":
    # "corrupt"` aborted the WHOLE scheduled run instead of raising here and
    # skipping only that ticker, and malformed target/day metadata could
    # release or write an outright incorrect balance (Codex review: "Validate
    # the newly persisted settlement fields on read"). Scoped to a lease that
    # is actually pending -- a resolved tracker carries none of these keys.
    if settlement_pending:
        ref_id = data.get("settlement_ref_id")
        if not isinstance(ref_id, str) or not ref_id.strip():
            raise _corrupted("settlement_ref_id", ref_id)
        claim_day = data.get("settlement_trading_day")
        if not isinstance(claim_day, str):
            raise _corrupted("settlement_trading_day", claim_day)
        try:
            date.fromisoformat(claim_day)
        except ValueError:
            raise _corrupted("settlement_trading_day", claim_day) from None
        claimed_usd = data.get("settlement_claimed_usd")
        if (
            claimed_usd is None
            or isinstance(claimed_usd, bool)
            or not isinstance(claimed_usd, (int, float))
        ):
            raise _corrupted("settlement_claimed_usd", claimed_usd)
        # Same OverflowError guard the generic numeric loop below uses: an
        # oversized JSON integer raises out of math.isfinite() rather than
        # returning False, and an uncaught OverflowError escapes the
        # per-ticker CorruptedLedgerStateError boundary entirely.
        try:
            claimed_finite = math.isfinite(claimed_usd)
        except OverflowError:
            raise _corrupted("settlement_claimed_usd", claimed_usd) from None
        if not claimed_finite or claimed_usd < 0:
            raise _corrupted("settlement_claimed_usd", claimed_usd)
        target_state = data.get("settlement_target_state")
        if not isinstance(target_state, dict):
            raise _corrupted("settlement_target_state", target_state)
        # The target is a FUTURE persisted state -- applied wholesale when
        # reconciliation proves the claimed order landed in full -- so it has
        # to satisfy the same schema as any tracker read off disk. Validated
        # recursively rather than trusted, since applying a corrupt target
        # simply moves the corruption to the next read. Validated in exactly
        # the shape it is APPLIED in: the target carries its own
        # settlement_pending flag (evaluate_inverse_martingale_step sets it on
        # both the resolved-marker and partial-continuation shapes) and every
        # apply site strips that flag, so the recursion must strip it too --
        # otherwise the target would be held to the lease schema above, whose
        # ref/day/claim fields belong to the ENCLOSING tracker, not to it.
        _validate_tracker_payload(
            {k: v for k, v in target_state.items() if k != "settlement_pending" and not k.startswith("settlement_")},
            state_path,
        )
        # settlement_live is read via bool(...) with a fail-closed default of
        # True; a non-bool truthy value (the string "false", say) would be
        # silently misread as a live lease, exactly like settlement_pending.
        settlement_live = data.get("settlement_live")
        if settlement_live is not None and not isinstance(settlement_live, bool):
            raise _corrupted("settlement_live", settlement_live)
        # settlement_account is a non-secret account fingerprint, compared with
        # `!=` against the recovery run's own broker identity to refuse an
        # absence-based release from the wrong account. A value that can never
        # equal a real fingerprint silently freezes the lease forever instead of
        # guarding it -- the same "a garbage value defeats the comparison it
        # participates in" failure the retry-identity fields above are validated
        # against.
        #
        # Checked STRUCTURALLY, not merely for non-emptiness. Claim creation can
        # only ever write the "unknown" sentinel or a
        # _LEASE_ACCOUNT_FINGERPRINT_LENGTH-character lowercase hex HMAC, so any
        # other nonempty string ("garbage", a truncated copy, a hand-edited
        # note) is damage. Accepting it let recovery compare a value that cannot
        # match, withhold reconciliation on every run, and freeze that ticker's
        # accumulator permanently and silently (Codex review: "Validate
        # settlement account fingerprints structurally"). Raising instead
        # surfaces the damaged tracker through the same CorruptedLedgerStateError
        # boundary as every other field here, which the injection site catches
        # per ticker -- one damaged ticker is skipped and logged rather than
        # quietly stuck.
        settlement_account = data.get("settlement_account")
        if settlement_account is not None and not _is_valid_lease_account(settlement_account):
            raise _corrupted("settlement_account", settlement_account)
        # settlement_stream records which roster stream made the claim, so a
        # later stream recovering the lease can tell whether those dollars
        # belong to the afternoon-only kill-switch figure. A non-string value
        # can never equal a real stream name, so it would silently make every
        # cross-stream check fail closed forever rather than answer the
        # question -- the same failure mode settlement_account is validated
        # against just above.
        # Checked against _VALID_STREAMS, the same set last_evaluated_stream is
        # validated against below -- not merely for non-emptiness. near_close_dca
        # decides whether a recovered claim belongs to the afternoon kill-switch
        # figure by asking whether its stream is anything OTHER than morning or
        # midday, so an unrecognized value like "garbage" reads as afternoon and
        # injects capital the kill switch is defined to exclude, inflating the
        # trailing total and spuriously capping later candidates (Codex review:
        # "Reject unknown settlement stream values"). A legacy lease predating
        # the field carries None and stays exempt -- that is genuinely unknown
        # provenance, which the caller already treats as ineligible.
        settlement_stream = data.get("settlement_stream")
        if settlement_stream is not None and settlement_stream not in _VALID_STREAMS:
            raise _corrupted("settlement_stream", settlement_stream)
        # A nonblank ref_id alone is not enough: claim creation can only ever
        # produce the ONE deterministic ref derived from settlement_stream,
        # settlement_trading_day and this tracker's own ticker (see
        # near_close_dca's claim site and _graded_dca_ref_id_prefix). A
        # restored or damaged tracker carrying some OTHER order's ref for the
        # same broker account would otherwise pass this check, and recovery
        # would then treat that unrelated order's terminal fill as proof
        # THIS claim landed -- clearing the lease and booking notional that
        # was never this ticker's (Codex review: "Validate the settlement
        # ref against its lease fields"). Checked last, once claim_day and
        # settlement_stream are both known and already individually valid.
        expected_ref_id = _expected_lease_ref_id(settlement_stream, claim_day, state_path.parent.name)
        if ref_id != expected_ref_id:
            raise _corrupted("settlement_ref_id", ref_id)

    # last_evaluated_is_real is compared via `.get(..., True)` (see the
    # STATUS_FIRING same-stream guard) -- a non-bool value would silently
    # sail through that truthy-ish comparison instead of being caught here,
    # same as every other boolean-typed field in this function.

def _validate_boolean_and_basis_fields(
    data: dict[str, Any], _corrupted: Callable[[str, Any], CorruptedLedgerStateError]
) -> None:
    last_evaluated_is_real = data.get("last_evaluated_is_real")
    if last_evaluated_is_real is not None and not isinstance(last_evaluated_is_real, bool):
        raise _corrupted("last_evaluated_is_real", last_evaluated_is_real)

    # A persisted "basis" outside {"pct", "stddev"} was previously accepted
    # unvalidated and reached evaluate_inverse_martingale_step's own
    # basis-mismatch check (`persisted_basis != basis`), which treats ANY
    # mismatch -- including one caused by corruption, not a genuine operator
    # config edit -- as "the configured basis changed mid-cycle" and resets
    # the cycle via resolved_marker(), permanently discarding a real,
    # positive accumulated_usd balance instead of raising here and letting
    # the caller skip only this one damaged ticker (Codex review: "Validate
    # persisted basis before resetting the cycle"). A tracker with no
    # persisted "basis" at all (created before this field existed) is not
    # corruption -- see that check's own comment on why None is treated as
    # "no mismatch", not rejected here either.
    basis = data.get("basis")
    if basis is not None and basis not in (BASIS_PCT, BASIS_STDDEV):
        raise _corrupted("basis", basis)


def _validate_numeric_fields(
    data: dict[str, Any], status: Optional[str], _corrupted: Callable[[str, Any], CorruptedLedgerStateError]
) -> None:
    for field in (
        "accumulated_usd", "cycle_reference_close", "stddev_reference", "lowest_drop_seen",
        # fired_floor_drop_metric (see resolved_marker's own docstring):
        # compared directly against a freshly computed drop_metric at the
        # top of the same-day re-arm guard below. A non-numeric value there
        # raises an uncaught TypeError, aborting the entire scheduled run
        # over one ticker's damaged state; a NaN value compares false
        # against every "<="/">=" check (same as every other field here),
        # which would silently make the floor look "not yet broken" -- NOT
        # a fail-closed outcome for THIS specific field, since it lets an
        # already-paid-for tier rearm and re-credit instead of refusing
        # (Codex review: "Validate the persisted fire-floor metric before
        # comparing it").
        "fired_floor_drop_metric", "rearm_reference_close",
    ):
        value = data.get(field)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _corrupted(field, value)
            # json.loads() parses an arbitrarily large JSON integer into an
            # unbounded Python int with no size limit of its own -- it
            # passes the isinstance(value, (int, float)) check just above,
            # but math.isfinite() itself raises OverflowError trying to
            # convert an out-of-float-range int, rather than returning False
            # the way it does for a genuinely non-finite float. Uncaught,
            # that OverflowError escapes this function entirely instead of
            # becoming the documented CorruptedLedgerStateError, so the
            # per-ticker recovery boundary in near_close_dca.py's injection
            # site never catches it and the WHOLE scheduled run aborts over
            # one ticker's damaged tracker (Codex review: "Treat oversized
            # ledger integers as corruption").
            try:
                finite = math.isfinite(value)
            except OverflowError:
                raise _corrupted(field, value) from None
            if not finite:
                raise _corrupted(field, value)

    # cycle_reference_close specifically must also be POSITIVE, not merely
    # finite -- every legitimately created cycle sets it from a validated
    # decision_price/previous_close (both already required positive-finite
    # elsewhere), so a persisted value of exactly 0 or negative can only be
    # corruption. Left at "merely finite" (the generic loop above), a
    # restored tracker with e.g. `cycle_reference_close: 0` would pass this
    # validation, and every SUBSEQUENT evaluation would then hit
    # evaluate_inverse_martingale_step's own `if not math.isfinite(
    # reference_close) or reference_close <= 0` guard and return
    # "invalid_reference_close" -- which returns BEFORE the age/attempt
    # abandonment cutoffs further down in that function ever run, freezing
    # the balance indefinitely instead of eventually abandoning it, while
    # enrollment continues suppressing the ticker's normal ladder the whole
    # time (Codex review: "Reject nonpositive cycle reference closes at
    # ledger load").
    cycle_reference_close = data.get("cycle_reference_close")
    if cycle_reference_close is not None and cycle_reference_close <= 0:
        raise _corrupted("cycle_reference_close", cycle_reference_close)

    # rearm_reference_close must also be POSITIVE, not merely finite -- like
    # cycle_reference_close above, every legitimately persisted value is
    # copied straight from a validated decision_price/previous_close (a
    # completed fire's own frozen reference), so a persisted 0 or negative
    # value can only be corruption. Left at "merely finite", a restored or
    # manually damaged marker with such a value would make
    # evaluate_inverse_martingale_step's `decision_price >=
    # rearm_reference_close` exit check trivially true for every positive
    # price, immediately clearing the durable rearm latch and letting an
    # already-paid-for descent re-credit tiers instead of failing closed
    # (Codex review: "Reject nonpositive rearm references").
    rearm_reference_close = data.get("rearm_reference_close")
    if rearm_reference_close is not None and rearm_reference_close <= 0:
        raise _corrupted("rearm_reference_close", rearm_reference_close)

    # rearm_last_seen_day is the ISO trading day the latch was last evaluated,
    # and it bounds the archive window the caller queries for a confirmed split
    # (see archived_split_factor). An unparseable value there would make the
    # caller compute a nonsense window -- either missing a real split or
    # re-applying one already applied -- so it is validated as a real date, the
    # same way settlement_trading_day and last_evaluated_trading_day are.
    rearm_last_seen_day = data.get("rearm_last_seen_day")
    if rearm_last_seen_day is not None:
        if not isinstance(rearm_last_seen_day, str):
            raise _corrupted("rearm_last_seen_day", rearm_last_seen_day)
        try:
            date.fromisoformat(rearm_last_seen_day)
        except ValueError:
            raise _corrupted("rearm_last_seen_day", rearm_last_seen_day) from None

    # fired_floor_drop_metric must also be POSITIVE, not merely finite --
    # every legitimately produced value comes from a crossed tier's own
    # drop_metric (abandonment_floor, or lowest_drop_seen at fire time),
    # which can only reach the tracker once drop_metric has already met or
    # exceeded some positive configured threshold. A restored resolved
    # marker with a nonpositive value here, and no fired_floor_price (the
    # explicitly supported legacy shape -- see resolved_marker()'s own
    # docstring on when it's omitted), would otherwise let the same-day
    # re-arm guard's metric-based fallback compare a fresh POSITIVE
    # drop_metric against this NEGATIVE floor, always conclude the floor
    # was broken, and immediately re-credit tiers from a descent that
    # already fired or was abandoned (Codex review: "Reject nonpositive
    # persisted fire-floor metrics").
    fired_floor_drop_metric = data.get("fired_floor_drop_metric")
    if fired_floor_drop_metric is not None and fired_floor_drop_metric <= 0:
        raise _corrupted("fired_floor_drop_metric", fired_floor_drop_metric)

    # lowest_drop_seen on an ACTIVE (dipping/firing) tracker must also be
    # POSITIVE, not merely finite -- it is only ever set once at least one
    # positive configured tier threshold has been crossed (see the
    # newly_crossed loop's own `low = float(new_state.get("lowest_drop_seen",
    # drop_metric)); new_state["lowest_drop_seen"] = max(low, drop_metric)`),
    # so a persisted value of exactly 0 or negative can only be corruption.
    # Left unchecked, a restored STATUS_FIRING tracker with a nonpositive
    # value here would settle normally, and its own continuation branch
    # copies this value straight into fired_floor_drop_metric via
    # `resolved_marker(..., fired_floor_drop_metric=tracker_state.get(
    # "lowest_drop_seen"))` -- the check just above already rejects a
    # nonpositive fired_floor_drop_metric, so the NEXT ledger read of that
    # freshly written marker raises CorruptedLedgerStateError, permanently
    # skipping the enrolled ticker while its normal ladder stays suppressed
    # (Codex review: "Reject nonpositive active-cycle lows").
    lowest_drop_seen = data.get("lowest_drop_seen")
    if status in (STATUS_DIPPING, STATUS_FIRING) and lowest_drop_seen is not None and lowest_drop_seen <= 0:
        raise _corrupted("lowest_drop_seen", lowest_drop_seen)

    # fired_floor_price (see resolved_marker's own docstring: "Compare the
    # rearm floor in the same price frame") is compared directly against a
    # fresh decision_price by the same same-day re-arm guard -- a
    # non-numeric value raises the same uncaught-TypeError risk
    # fired_floor_drop_metric's own check above exists to prevent, and a
    # nonpositive/non-finite value must be rejected outright rather than
    # silently participating in a `decision_price >= fired_floor_price`
    # comparison (a NaN there is also always False, again silently making
    # the floor look "not yet broken" -- the SAME fail-open failure mode
    # fired_floor_drop_metric's own validation exists to close).
    fired_floor_price = data.get("fired_floor_price")
    if fired_floor_price is not None:
        fired_floor_price_invalid = isinstance(fired_floor_price, bool) or not isinstance(
            fired_floor_price, (int, float)
        )
        if not fired_floor_price_invalid:
            # An arbitrarily large JSON integer parses into an unbounded
            # Python int, which passes the isinstance() check just above but
            # makes math.isfinite() RAISE OverflowError rather than return
            # False -- the same overflow crossed_tier_thresholds' own
            # _is_finite_number guards against. Uncaught here, that escaped
            # this function as a bare OverflowError instead of becoming
            # CorruptedLedgerStateError, so the per-ticker recovery boundary
            # at near_close_dca.py's injection site never caught it and one
            # damaged tracker aborted the whole scheduled run (Codex review:
            # "Convert oversized fire-floor prices into ledger corruption").
            try:
                fired_floor_price_invalid = not math.isfinite(fired_floor_price) or fired_floor_price <= 0
            except OverflowError:
                fired_floor_price_invalid = True
        if fired_floor_price_invalid:
            raise _corrupted("fired_floor_price", fired_floor_price)

    # Every legitimate fired_floor_drop_metric/fired_floor_price is written
    # by resolved_marker() together with last_evaluated_trading_day, in the
    # SAME call -- resolved_marker()'s own signature stamps
    # last_evaluated_trading_day unconditionally (see its docstring) while
    # the floor fields are optional kwargs, but every real caller
    # (evaluate_inverse_martingale_step's own fire/abandonment branches)
    # passes both or neither. A restored or truncated marker that keeps a
    # valid floor but LOSES last_evaluated_trading_day would otherwise pass
    # validation -- and the same-day re-arm guard above
    # (`tracker_state.get("last_evaluated_trading_day") ==
    # trading_day.isoformat()`) can then never match today's date, silently
    # disabling the floor protection it exists to provide: a later stream
    # evaluating the SAME calendar day would treat an unchanged price still
    # within the already-covered range as a genuinely fresh dip, re-crediting
    # tiers a real fire already paid for (Codex review: "Require a date for
    # persisted fire floors").
    if (fired_floor_drop_metric is not None or fired_floor_price is not None) and data.get(
        "last_evaluated_trading_day"
    ) is None:
        raise _corrupted("last_evaluated_trading_day", None)

    # An active cycle ("dipping" or "firing") must carry a genuinely
    # positive balance. evaluate_inverse_martingale_step's STATUS_FIRING
    # branch floors ANY accumulated_usd -- including zero or negative -- up
    # to the broker minimum and fires it unconditionally (the fire amount is
    # frozen at cycle-trigger time, so it deliberately never re-checks the
    # balance is still positive on a later continuation run); a corrupted or
    # manually edited tracker left in an active status with a nonpositive
    # balance would otherwise produce a real, entirely unearned purchase on
    # its very next evaluation instead of being refused here (Codex review:
    # "Reject nonpositive balances in active tracker states"). A resolved
    # ("none") tracker never carries accumulated_usd at all -- resolved_
    # marker() omits the key -- so this check is scoped to the two active
    # statuses only.
    if status in (STATUS_DIPPING, STATUS_FIRING):
        accumulated = data.get("accumulated_usd")
        if accumulated is None or accumulated <= 0:
            raise _corrupted("accumulated_usd", accumulated)
    else:
        # A resolved ("none") or brand-new (no status key at all) tracker
        # must never carry a nonzero balance either -- resolved_marker()
        # omits accumulated_usd entirely, and a genuinely fresh tracker (the
        # very first tier crossing) starts from {} with nothing persisted
        # yet. Left unchecked, a restored or manually damaged tracker that
        # keeps a STALE positive accumulated_usd despite a non-active status
        # passes this validation silently and gets picked up as the
        # STARTING balance the very next time a tier crosses --
        # evaluate_inverse_martingale_step's own `accumulated_before =
        # float(tracker_state.get("accumulated_usd", 0.0))` reads it
        # unconditionally, regardless of status -- inflating every
        # subsequent capped fire by the leftover stale amount instead of
        # starting a genuinely fresh cycle (Codex review: "Reject balances
        # on resolved tracker states"). Omitted or exactly 0.0 is fine
        # (matches every legitimate resolved/fresh tracker); only a
        # genuinely nonzero leftover value is corruption.
        accumulated = data.get("accumulated_usd")
        if accumulated is not None and accumulated != 0:
            raise _corrupted("accumulated_usd", accumulated)

        # A resolved/fresh tracker must never carry cycle-only metadata
        # either -- resolved_marker() omits every one of these fields (it
        # persists only status/last_evaluated_trading_day/last_evaluated_
        # stream/fired_floor_drop_metric/fired_floor_price -- see its own
        # docstring), and a genuinely fresh {} tracker has none of them by
        # definition. A restored or manually edited tracker that keeps a
        # STALE cycle_reference_close (or the other fields below) despite a
        # non-active status is dangerous specifically because
        # evaluate_inverse_martingale_step's own new-cycle bootstrap uses
        # `new_state.setdefault(...)` for every one of them (`new_state =
        # dict(tracker_state)` at the top of that function) -- setdefault()
        # only fills in a MISSING key, so a stale leftover value silently
        # survives into the brand-new cycle instead of being overwritten by
        # the freshly computed reference_close/basis/fingerprint the new
        # cycle is supposed to start from, corrupting every drop_pct/tier-
        # crossing/recovery computation for that cycle's entire lifetime
        # (Codex review: "Reject cycle snapshots on resolved tracker
        # states").
        for field in (
            "cycle_reference_close",
            "lowest_drop_seen",
            "crossed_tier_thresholds",
            "cycle_start_trading_day",
            "stddev_reference",
            "tier_config_fingerprint",
            "basis",
        ):
            if data.get(field) is not None:
                raise _corrupted(field, data.get(field))

def _validate_cycle_abandonment_fields(
    data: dict[str, Any], status: Optional[str], _corrupted: Callable[[str, Any], CorruptedLedgerStateError]
) -> None:
    for field in ("fire_attempts", "trading_days_active"):
        value = data.get(field)
        # Rejects a negative value too, not just the wrong type -- both
        # counters are read straight back with `int(tracker_state.get(
        # field, 0))` and incremented normally each evaluation, then
        # compared against the configured POSITIVE limits
        # (max_fire_attempts/max_cycle_days). A sufficiently negative
        # starting value therefore delays abandonment for however many
        # extra sessions/attempts it takes to climb back past zero and then
        # reach the real limit -- dozens or hundreds, for a large enough
        # negative value -- letting a restored or manually edited tracker's
        # stale balance keep firing well past the cutoffs those limits
        # exist to enforce (Codex review: "Reject negative persisted cycle
        # counters").
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise _corrupted(field, value)
        # A resolved/fresh tracker must never carry a nonzero age counter
        # either -- resolved_marker() omits both fields entirely, exactly
        # like accumulated_usd/the cycle-only metadata above. A restored or
        # manually edited tracker that keeps a STALE trading_days_active
        # despite a non-active status is dangerous the moment the next tier
        # crossing starts a brand-new cycle: evaluate_inverse_martingale_
        # step's own `trading_days_active = int(tracker_state.get(
        # "trading_days_active", 0))` reads it unconditionally and then
        # compares it against max_cycle_days on the very next evaluation --
        # a stale value already near (or past) the limit abandons the
        # newly-accumulated balance almost immediately, defeating the
        # cutoff's entire "give a fresh cycle its full budget" purpose
        # (Codex review: "Reject cycle-age counters on resolved trackers").
        if status not in (STATUS_DIPPING, STATUS_FIRING) and value is not None and value != 0:
            raise _corrupted(field, value)

    # Every legitimate write of trading_days_active happens unconditionally
    # for BOTH active statuses (evaluate_inverse_martingale_step's own
    # `new_state["trading_days_active"] = trading_days_active` runs before
    # the dipping/firing branch split, and the STATUS_FIRING continuation
    # branch sets it too), and fire_attempts is always set the moment a
    # cycle first enters STATUS_FIRING (`new_state["fire_attempts"] = 1`)
    # and every continuation after. A restored active tracker that OMITS
    # trading_days_active is not "handled" by the fallback default --
    # evaluate_inverse_martingale_step's own `int(tracker_state.get(
    # "trading_days_active", 0))` silently restarts the counter at zero,
    # even if cycle_start_trading_day is months old, granting the stale
    # balance another FULL max_cycle_days budget instead of abandoning it
    # on schedule. A STATUS_FIRING tracker missing fire_attempts similarly
    # regains the full max_fire_attempts retry budget it may have already
    # exhausted (Codex review: "Require age counters on active trackers").
    if status in (STATUS_DIPPING, STATUS_FIRING) and data.get("trading_days_active") is None:
        raise _corrupted("trading_days_active", None)
    if status == STATUS_FIRING and data.get("fire_attempts") is None:
        raise _corrupted("fire_attempts", None)


def _validate_tier_crossing_fields(
    data: dict[str, Any], status: Optional[str], _corrupted: Callable[[str, Any], CorruptedLedgerStateError]
) -> None:
    def _is_finite_number(value: Any) -> bool:
        """True only for a real, finite int/float.

        math.isfinite() alongside the type check: NaN/inf ARE real int/float
        instances in Python, so the type check alone lets them through --
        inconsistent with every other numeric field validated just above, which
        all reject non-finite values explicitly (Antigravity review: "Non-Finite
        Floats in _validate_tracker_payload() Tier Thresholds").

        The OverflowError guard is the same one the generic numeric loop uses:
        json.loads() parses an arbitrarily large JSON integer into an unbounded
        Python int, which passes isinstance() but makes math.isfinite() RAISE
        rather than return False. Uncaught, that escaped this function instead
        of becoming CorruptedLedgerStateError, so the per-ticker recovery
        boundary at near_close_dca.py's injection site never caught it and one
        damaged tracker aborted the WHOLE scheduled run (Codex review: "Convert
        oversized crossed tiers into ledger corruption").
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    crossed = data.get("crossed_tier_thresholds")
    if crossed is not None and (
        not isinstance(crossed, list) or any(not _is_finite_number(v) for v in crossed)
    ):
        raise _corrupted("crossed_tier_thresholds", crossed)

    # An active tracker's cycle-identity fields are ALWAYS written together
    # by every legitimate code path the moment a cycle starts (a fresh cycle
    # cannot even begin without crossing at least one tier, and
    # crossed_tier_thresholds/cycle_reference_close/lowest_drop_seen are all
    # set in that same evaluation -- see the tier-crossing branch above) and
    # only ever carried forward afterward, never dropped -- including across
    # the "dipping" -> "firing" transition. A restored, manually edited, or
    # otherwise damaged-but-valid tracker that keeps a positive balance but
    # LOSES one of these is therefore never a legitimate state, and is
    # dangerous specifically because each field's absence is individually
    # "handled" elsewhere by silently reconstructing from CURRENT prices (or
    # simply omitting a same-day protection) instead of crashing:
    #   - crossed_tier_thresholds missing -> crossed_before defaults to an
    #     empty set, so evaluate_inverse_martingale_step's tier-crossing loop
    #     treats every currently-crossed tier as NEWLY crossed and credits
    #     them again on top of the existing balance (a persisted $7 at tiers
    #     1/2/4 becomes $14 on the very next evaluation that still shows a
    #     deep enough dip). Only actually READ by the tier-crossing loop,
    #     which a "firing" tracker's own evaluation branch returns before
    #     ever reaching -- so this specific field is required for "dipping"
    #     only, not "firing".
    #   - cycle_reference_close missing -> silently falls back to TODAY's
    #     previous_close instead of the cycle's real frozen starting price,
    #     corrupting the drop_pct this whole evaluation is computed from --
    #     `reference_close = ... float(tracker_state.get("cycle_reference_
    #     close", previous_close))` above falls back for ANY non-"none"
    #     status_before, "firing" included, not just "dipping".
    #   - lowest_drop_seen missing -> silently falls back to TODAY's
    #     drop_metric instead of the cycle's true historical low, which can
    #     make the recovery-offset comparison fire incorrectly in either
    #     direction for a "dipping" cycle -- and for a "firing" cycle
    #     specifically, this is also the value the STATUS_FIRING
    #     continuation branch's own `resolved_marker(..., fired_floor_drop_
    #     metric=tracker_state.get("lowest_drop_seen"))` call reads when the
    #     fire fully resolves: a missing value there means the resolved
    #     marker carries NO fired_floor_drop_metric at all, silently
    #     reopening the exact same "a different stream later the same day
    #     re-credits the same tiers at an unchanged price" hole
    #     fired_floor_drop_metric exists to close (Codex review: "Require
    #     the fire-floor metadata on firing trackers"), on top of the
    #     "dipping" recovery-offset danger above.
    # (Codex review: "Require complete snapshots for active trackers").
    if status in (STATUS_DIPPING, STATUS_FIRING):
        for required_field in ("cycle_reference_close", "lowest_drop_seen"):
            if data.get(required_field) is None:
                raise _corrupted(required_field, data.get(required_field))
    if status == STATUS_DIPPING and (not isinstance(crossed, list) or not crossed):
        raise _corrupted("crossed_tier_thresholds", crossed)

    # An active "stddev"-basis cycle must carry a positive, frozen
    # stddev_reference -- evaluate_inverse_martingale_step deliberately
    # ignores the caller's freshly computed sigma for an active cycle
    # (`tracker_state.get("stddev_reference") if status_before !=
    # STATUS_NONE else stddev_reference` above), reading only the
    # PERSISTED value, so a restored or damaged tracker missing it is not
    # "handled" by silently reconstructing anything -- it hits the
    # `stddev_unavailable` refusal every single evaluation, BEFORE this
    # function ever reaches the age/attempt abandonment cutoffs further
    # below (which run after this basis block). The cycle can therefore
    # never age out, never resolve, and never fire, while
    # graded_buy_multiplier() keeps suppressing this ticker's normal ladder
    # for as long as config.inverse_martingale stays enrolled -- effectively
    # bricking the ticker's DCA entirely instead of failing closed at load
    # time the way every other missing-required-field case here does
    # (Codex review: "Require the frozen sigma for active stddev cycles").
    # The generic finite-number loop above already validates
    # stddev_reference IS finite whenever it's present, but a value of
    # exactly None (missing) or a nonpositive one both pass that check
    # unchanged -- covered here instead.
    if status in (STATUS_DIPPING, STATUS_FIRING) and data.get("basis") == BASIS_STDDEV:
        stddev_reference = data.get("stddev_reference")
        if stddev_reference is None or stddev_reference <= 0:
            raise _corrupted("stddev_reference", stddev_reference)

    # cycle_start_trading_day is written together with the three fields just
    # above the moment a cycle first enters "dipping" (same setdefault()
    # block in evaluate_inverse_martingale_step) and carried forward
    # unchanged into "firing" -- a legitimate tracker in EITHER active status
    # always has it. evaluate_inverse_martingale_step's own max_cycle_days/
    # max_fire_attempts abandonment check is gated on this field being
    # truthy (`if status_before != STATUS_NONE and cycle_start:`) before it
    # even looks at trading_days_active/fire_attempts -- so a restored or
    # manually damaged tracker that keeps trading_days_active, fire_attempts,
    # and a positive accumulated_usd but LOSES cycle_start_trading_day would
    # otherwise bypass both cutoffs indefinitely (never abandoned back to
    # "none") and still emit a real, unbounded-age purchase whenever recovery
    # happens to occur (Codex review: "Require cycle-start metadata for
    # active trackers"). Checked for both statuses, not just "dipping" --
    # unlike crossed_tier_thresholds/cycle_reference_close/lowest_drop_seen
    # above (which the "firing" evaluation branch never reads), a missing
    # cycle_start_trading_day is dangerous in BOTH active states equally,
    # since the abandonment gate itself applies to both.
    if status in (STATUS_DIPPING, STATUS_FIRING) and data.get("cycle_start_trading_day") is None:
        raise _corrupted("cycle_start_trading_day", None)
    # Presence alone (checked just above) isn't the whole contract: unlike
    # last_evaluated_trading_day, cycle_start_trading_day is only ever
    # compared for TRUTHINESS (`if status_before != STATUS_NONE and
    # cycle_start:`), never parsed or equality-compared -- so a malformed
    # but non-empty string wouldn't crash or silently misbehave the way the
    # other identity fields could. Validated anyway, for the same
    # defense-in-depth reason every other field in this function is: a
    # value that LOOKS like a trading day but isn't one is exactly the kind
    # of quiet corruption this function exists to catch before it reaches
    # the pure decision function (Antigravity review: "Validate
    # cycle_start_trading_day ISO Date Format").
    cycle_start_trading_day = data.get("cycle_start_trading_day")
    if cycle_start_trading_day is not None:
        if not isinstance(cycle_start_trading_day, str):
            raise _corrupted("cycle_start_trading_day", cycle_start_trading_day)
        try:
            date.fromisoformat(cycle_start_trading_day)
        except ValueError:
            raise _corrupted("cycle_start_trading_day", cycle_start_trading_day) from None

    # tier_config_fingerprint is read only for an INEQUALITY comparison
    # against the currently-configured tier table
    # (`persisted_tier_fingerprint != current_tier_fingerprint` in
    # evaluate_inverse_martingale_step) -- so, unlike a field that would
    # crash on a bad type, a CORRUPTED fingerprint (wrong shape, truncated,
    # non-numeric) compares unequal to any real fingerprint and is
    # misread as "the operator edited the tier config" -- silently
    # resolving the cycle via resolved_marker() and permanently discarding
    # a real, positive accumulated_usd balance, exactly the failure mode
    # this function otherwise exists to prevent (Antigravity review:
    # "Unvalidated tier_config_fingerprint ... Causes Silent State &
    # Balance Reset"). A tracker with no persisted fingerprint at all
    # (created before this field existed) is not corruption -- see
    # evaluate_inverse_martingale_step's own "backward compatible" comment
    # on the identical convention for basis/cycle_start_trading_day.
    # Every number a real fingerprint can ever contain is strictly positive --
    # unit_usd (a dollar amount), each tier's threshold (a drop_pct/sigma
    # cutoff), and each tier's units multiplier -- since config-loaded values
    # are validated by inverse_martingale.schema.json before ever reaching
    # this fingerprint (see current_tier_fingerprint's own construction in
    # evaluate_inverse_martingale_step). A merely-finite check alone lets a
    # value like 0 or -2.0 through: it compares unequal to any real
    # fingerprint just as reliably as a wrong-shaped one, so it triggers the
    # exact same silent cycle-reset/balance-discard failure mode the shape
    # check above already exists to catch, just via a different kind of
    # malformed payload (Codex review: "Reject nonpositive values in
    # persisted tier fingerprints").

def _validate_fingerprint_and_identity_fields(
    data: dict[str, Any], status: Optional[str], _corrupted: Callable[[str, Any], CorruptedLedgerStateError]
) -> None:
    tier_config_fingerprint = data.get("tier_config_fingerprint")
    if tier_config_fingerprint is not None:
        def _positive_finite(value: Any) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0

        valid_fingerprint_shape = (
            isinstance(tier_config_fingerprint, list)
            and len(tier_config_fingerprint) == 2
            and _positive_finite(tier_config_fingerprint[0])
            and isinstance(tier_config_fingerprint[1], list)
            # Every REAL configured tier table has at least one tier --
            # inverse_martingale.schema.json requires a nonempty "tiers"
            # array, and current_tier_fingerprint's own construction in
            # evaluate_inverse_martingale_step always mirrors that
            # nonempty table. Without this length check, a restored/
            # corrupted fingerprint like [1.0, []] passes every check above
            # (`all(...)` over an empty iterable is vacuously True), and is
            # then misread as "the operator legitimately configured zero
            # tiers" -- silently resolving the cycle via resolved_marker()
            # and discarding a real, positive accumulated_usd balance
            # instead of taking the corrupted-ledger path this function
            # otherwise exists to enforce (Codex review: "Require a
            # nonempty persisted tier table").
            and len(tier_config_fingerprint[1]) > 0
            and all(
                isinstance(pair, list) and len(pair) == 2 and all(_positive_finite(value) for value in pair)
                for pair in tier_config_fingerprint[1]
            )
        )
        if not valid_fingerprint_shape:
            raise _corrupted("tier_config_fingerprint", tier_config_fingerprint)

        # A crossed threshold that doesn't correspond to any REAL configured
        # tier is worse than merely finite-but-wrong: evaluate_inverse_
        # martingale_step's tier-crossing loop iterates the CURRENT config's
        # own tiers and checks `threshold not in crossed_before` -- so a
        # crossed_tier_thresholds value like 3.0 alongside a real 1/2/4-tier
        # fingerprint doesn't shadow anything real. Every one of 1.0, 2.0,
        # 4.0 still reads as "not in crossed_before" the next time the drop
        # reaches them, and gets credited a SECOND time on top of the
        # existing balance instead of being recognized as already paid for.
        # A duplicate value within crossed_tier_thresholds itself is the
        # same problem in miniature (a set can never legitimately contain a
        # value twice). Only checked once tier_config_fingerprint's own
        # shape is confirmed valid above -- a legitimately fingerprint-less
        # tracker (created before that field existed) has no table to
        # validate crossed values against and is not penalized for it
        # (Codex review: "Validate crossed tiers against the persisted tier
        # table").
        crossed = data.get("crossed_tier_thresholds")
        if crossed is not None:
            valid_thresholds = {pair[0] for pair in tier_config_fingerprint[1]}
            if len(crossed) != len(set(crossed)) or any(value not in valid_thresholds for value in crossed):
                raise _corrupted("crossed_tier_thresholds", crossed)

    # Both fields below are compared (== / in), never parsed, so no value
    # here can CRASH -- but a garbage value that never legitimately equals
    # "today"'s ISO date or a real stream silently defeats the guards those
    # comparisons implement instead of merely failing to match anything
    # useful: a garbage last_evaluated_trading_day never equals a future
    # trading_day.isoformat() either, so the same-day no-op guards in
    # evaluate_inverse_martingale_step never fire and trading_days_active
    # advances on EVERY evaluation that trading day (not just the first),
    # abandoning a real balance well before max_cycle_days actually elapses;
    # a garbage last_evaluated_stream never matches any real stream (or
    # None), so a genuine SAME-STREAM retry of an already-continued
    # STATUS_FIRING fire is no longer recognized as one, re-incrementing
    # fire_attempts (and, once settlement_pending is no longer set, able to
    # place another real order for what is really the same logical slot)
    # (Codex review: "Validate persisted retry identity fields").
    last_evaluated_trading_day = data.get("last_evaluated_trading_day")
    if last_evaluated_trading_day is not None:
        if not isinstance(last_evaluated_trading_day, str):
            raise _corrupted("last_evaluated_trading_day", last_evaluated_trading_day)
        try:
            date.fromisoformat(last_evaluated_trading_day)
        except ValueError:
            raise _corrupted("last_evaluated_trading_day", last_evaluated_trading_day) from None

    last_evaluated_stream = data.get("last_evaluated_stream")
    if last_evaluated_stream is not None and last_evaluated_stream not in _VALID_STREAMS:
        raise _corrupted("last_evaluated_stream", last_evaluated_stream)



def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that raises on a duplicate key in one JSON object.

    ``json.loads`` silently keeps the LAST occurrence of a duplicate object
    key by default -- a restored or externally edited tracker.json with two
    ``"accumulated_usd"`` entries (or a duplicate anywhere else in the
    document) would otherwise be read back with whichever value happened to
    come last silently winning, and the ledger would trust that arbitrary
    survivor as real state -- inflating a real inverse-Martingale fire
    rather than routing the ambiguous document through
    :class:`CorruptedLedgerStateError`, the same corruption dca_config.py's
    own ``_reject_duplicate_keys`` and ml-forecaster/publish.py's identical
    hook already guard their own JSON reads with (Codex review: "Reject
    duplicate keys in tracker JSON"). Duplicated rather than imported --
    this module keeps its own JSON parsing rather than depending on
    dca_config.py's, and a reverse import from dca_config back into this
    module would be circular (dca_config.py imports StrategyConfig from
    near_close_dca.py, which itself imports this module).
    """
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON document")
        seen.add(key)
        result[key] = value
    return result


class InverseMartingaleLedger:
    """Durable per-ticker cycle state under ``stream_root('inverse_martingale_dca')``.

    One JSON file per ticker (not per trading day -- a cycle spans day
    boundaries). No file exists for a ticker until its first tier crossing,
    matching :class:`dca_cumulative.CumulativeCreditLedger`'s sparse-directory
    behavior. Reads/writes are serialized with the same
    ``cross_process_lock`` + ``atomic_write_text`` convention used there.
    """

    def __init__(self, root: Optional[str | Path] = None) -> None:
        self.root = Path(root) if root is not None else Path(stream_root("inverse_martingale_dca"))

    def _paths(self, ticker: str) -> tuple[Path, Path]:
        symbol = ticker.strip().upper()
        # ".." alone (or "A.." etc.) matches _TICKER_SYMBOL_PATTERN's own
        # character class -- "." is a legitimate part of a real symbol
        # (e.g. "BRK.B") -- so the pattern check below cannot by itself
        # reject a parent-directory traversal. No real ticker ever needs
        # two consecutive dots, so excluding that substring outright closes
        # the gap without narrowing what the pattern otherwise allows.
        if not _TICKER_SYMBOL_PATTERN.match(symbol) or ".." in symbol:
            # A ticker containing path separators, a leading "/", "..", or
            # any character outside a real symbol's alphabet would
            # otherwise be joined into self.root unvalidated -- see
            # _TICKER_SYMBOL_PATTERN's own module-level comment for the
            # full path-traversal rationale. Raised as
            # CorruptedLedgerStateError (not a bare ValueError) so it is
            # caught by the SAME try/except near_close_dca.py's injection
            # site already wraps around every read()/claim_fire() call --
            # skip only this one malformed ticker, not abort the entire
            # scheduled run.
            raise CorruptedLedgerStateError(f"{ticker!r} is not a valid ticker symbol")
        ticker_dir = self.root / symbol
        return ticker_dir / "tracker.json", ticker_dir / "tracker.lock"

    def _read_locked(self, state_path: Path, *, expected_symbol: str) -> dict[str, Any]:
        try:
            raw = state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            # A FileNotFoundError means "no cycle yet" ({} is correct), but
            # any OTHER read failure -- invalid UTF-8 bytes (disk corruption,
            # a bad write), a permissions error, an I/O error -- means the
            # file EXISTS but cannot be trusted, exactly the
            # CorruptedLedgerStateError contract (see its own docstring).
            # Previously only FileNotFoundError was caught here, so an
            # encoding-damaged tracker.json crashed with an uncaught
            # UnicodeDecodeError before ever reaching the JSON-parsing
            # try/except below that already converts THOSE failures
            # correctly -- near_close_dca.py's injection site catches only
            # CorruptedLedgerStateError around read(), so this aborted the
            # entire scheduled run over one ticker's damaged file instead of
            # the documented "skip only this ticker" behavior (Codex
            # review: "Convert invalid UTF-8 tracker data into ledger
            # corruption").
            raise CorruptedLedgerStateError(f"{state_path} could not be read: {exc}") from exc
        try:
            data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except ValueError as exc:
            # json.JSONDecodeError (a genuine syntax error) is itself a
            # ValueError subclass, and _reject_duplicate_keys() above raises
            # a plain ValueError for a duplicate key -- both are equally
            # "this document cannot be trusted" and belong in the identical
            # CorruptedLedgerStateError fail-closed path.
            raise CorruptedLedgerStateError(f"{state_path} contains invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CorruptedLedgerStateError(f"{state_path} does not contain a JSON object")
        if data.get("version") != _LEDGER_VERSION:
            raise CorruptedLedgerStateError(
                f"{state_path} has unsupported ledger version {data.get('version')!r} "
                f"(expected {_LEDGER_VERSION})"
            )
        # _write_locked always stamps the symbol it wrote for INTO the
        # payload's own "ticker" field -- if this file was copied or
        # restored into the wrong ticker's directory (an operator mistake,
        # a bad backup/restore script), that field will disagree with
        # `expected_symbol` (derived from the DIRECTORY this file was read
        # from). Silently trusting the directory alone would let the
        # caller evaluate a cycle against the WRONG ticker's prices and
        # fire the source ticker's real accumulated balance as an order for
        # a completely different security (Codex review: "Validate the
        # tracker ticker before using its balance"). A legacy file with no
        # "ticker" field at all (written before this field existed) is not
        # a mismatch -- backward compatible.
        stored_ticker = data.get("ticker")
        if stored_ticker is not None and stored_ticker != expected_symbol:
            raise CorruptedLedgerStateError(
                f"{state_path} is under {expected_symbol}'s directory but its own payload "
                f"records ticker {stored_ticker!r} -- refusing to treat a possibly misplaced "
                f"file as {expected_symbol}'s accumulator state"
            )
        _validate_tracker_payload(data, state_path)
        return {k: v for k, v in data.items() if k not in ("version", "ticker", "events")}

    def _write_locked(
        self, symbol: str, state_path: Path, new_state: dict[str, Any], step: InverseMartingaleStep
    ) -> None:
        if not new_state:
            state_path.unlink(missing_ok=True)
            return
        existing_events: list[dict[str, Any]] = []
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("events"), list):
                # _validate_tracker_payload() (see _read_locked) never
                # validates the shape of individual audit events -- they are
                # best-effort, not trusted state a caller makes financial
                # decisions from -- so a restored or hand-edited tracker can
                # reach here with a syntactically valid but non-object entry
                # (e.g. a bare number) still in its events list. The
                # recovery-eviction-exemption logic just below tests
                # `"recovery_metadata" not in candidate_event` against every
                # entry, which raises TypeError for a non-dict value; letting
                # that exception escape this best-effort write would abort
                # the whole write (and, via record_step's caller, potentially
                # the scheduled run) over a single malformed audit entry the
                # ledger was never contractually obligated to preserve
                # (Codex review: "Validate event entries before filtering
                # them"). Drop non-dict entries here, before they ever reach
                # that logic, the same way this whole except-block already
                # discards an unreadable prior file outright.
                existing_events = [item for item in existing["events"] if isinstance(item, dict)]
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Unlike _read_locked (which must fail closed with
            # CorruptedLedgerStateError, since it hands back trusted state a
            # caller will make real financial decisions from), this is only
            # best-effort preservation of the prior audit trail -- an
            # unreadable existing file (invalid UTF-8, a permissions/IO
            # error) is not a reason to abort the write this method exists
            # to perform. Previously only (FileNotFoundError,
            # JSONDecodeError) were caught here, leaving an inconsistency
            # with _read_locked's own broader handling (Antigravity review:
            # "Exception Handling Consistency in InverseMartingaleLedger.
            # _write_locked()"). Proceed with an empty event history, same
            # as a genuinely absent prior file.
            pass
        payload = dict(new_state)
        payload["version"] = _LEDGER_VERSION
        payload["ticker"] = symbol
        # Bounded, not unlimited: an enrolled ticker's tracker persists
        # across day boundaries by design (unlike CumulativeCreditLedger's
        # per-day ledger, which naturally resets), and EVERY evaluation --
        # including a plain no-op no-fire one (already_evaluated_today,
        # awaiting_recovery, data_unavailable_age_advanced, etc.) -- reaches
        # this write. Without a cap, a long-running enrolled ticker's
        # tracker.json grows without bound, and every read-modify-write
        # gets progressively slower re-serializing an ever-larger event
        # list (Codex review: "Bound the persisted accumulator event
        # history"). The most RECENT events are the ones actually useful
        # for debugging a live cycle; older ones are dropped once the cap
        # is exceeded.
        event: dict[str, Any] = {
            "status_before": step.status_before,
            "status_after": step.status_after,
            "accumulated_usd_before": step.accumulated_usd_before,
            "accumulated_usd_after": step.accumulated_usd_after,
            "newly_crossed_tiers": list(step.newly_crossed_tiers),
            "fired": step.fired,
            "fire_amount_usd": step.fire_amount_usd,
            "reason": step.reason,
        }
        if step.recovery_metadata is not None:
            # A crash-lease recovery's claim attribution -- see
            # InverseMartingaleStep.recovery_metadata's own docstring. Kept
            # under its own key rather than merged flat into the event, so it
            # can never collide with a field above and is unambiguous to a
            # reader of the raw event log.
            event["recovery_metadata"] = step.recovery_metadata
        combined_events = existing_events + [event]
        # A prior-day or cross-stream recovery's event is the ONLY durable
        # claim-aware audit record left once a later resolved_marker()
        # transition overwrites the top-level last_recovery field (see
        # near_close_dca.py's recovered_state["last_recovery"] assignment).
        # Slicing this list down to the most recent _MAX_PERSISTED_EVENTS
        # entries -- as ordinary events are, further above -- would evict
        # that record after as few as ~17 trading days at three scheduled
        # streams/day, silently losing the claim day, stream, ref, and order
        # ID the exact same way the pre-audit-event bug did (Codex review:
        # "Keep recovery attribution past the event cap"). Prefer dropping
        # the oldest ORDINARY events first, in place, so relative
        # chronological order is preserved and a recovery event only gives
        # up its slot once no ordinary event is left to evict instead.
        if len(combined_events) > _MAX_PERSISTED_EVENTS:
            ordinary_to_drop = len(combined_events) - _MAX_PERSISTED_EVENTS
            trimmed_events: list[dict[str, Any]] = []
            for candidate_event in combined_events:
                if ordinary_to_drop > 0 and "recovery_metadata" not in candidate_event:
                    ordinary_to_drop -= 1
                    continue
                trimmed_events.append(candidate_event)
            # A ticker with more crash-lease recoveries than
            # _MAX_PERSISTED_EVENTS itself (repeated recovery failures) has
            # no ordinary event left to evict -- every remaining candidate
            # carries recovery_metadata, so the loop above alone would let
            # combined_events grow without bound, defeating the whole point
            # of the cap and making every read-modify-write progressively
            # more expensive (Codex review: "Keep recovery retention
            # bounded"). Fall back to the same most-recent-N slice ordinary
            # events already get, keeping the newest recovery attribution
            # (still the most useful for live debugging) over the oldest.
            if len(trimmed_events) > _MAX_PERSISTED_EVENTS:
                trimmed_events = trimmed_events[-_MAX_PERSISTED_EVENTS:]
            combined_events = trimmed_events
        payload["events"] = combined_events
        state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def read(self, ticker: str) -> dict[str, Any]:
        symbol = ticker.strip().upper()
        state_path, _ = self._paths(ticker)
        return self._read_locked(state_path, expected_symbol=symbol)

    def record_step(
        self,
        ticker: str,
        new_state: dict[str, Any],
        step: InverseMartingaleStep,
    ) -> None:
        """Persist ``new_state`` and append an audit event for ``step``.

        An empty ``new_state`` (cycle finished or abandoned) removes the
        tracker file entirely rather than writing an empty shell, keeping the
        ledger directory sparse.
        """
        symbol = ticker.strip().upper()
        state_path, lock_path = self._paths(ticker)
        with cross_process_lock(lock_path):
            self._write_locked(symbol, state_path, new_state, step)

    def compare_and_swap_transition(
        self,
        ticker: str,
        *,
        expected_prior_state: dict[str, Any],
        new_state: dict[str, Any],
        step: InverseMartingaleStep,
    ) -> bool:
        """Atomically persist a transition when the prior snapshot still matches.

        This primitive applies to every accumulator transition, not only fire
        claims.  Callers that evaluated a stale snapshot must leave the newer
        on-disk state untouched.
        """
        symbol = ticker.strip().upper()
        state_path, lock_path = self._paths(ticker)
        with cross_process_lock(lock_path):
            current = self._read_locked(state_path, expected_symbol=symbol)
            if current != expected_prior_state:
                return False
            self._write_locked(symbol, state_path, new_state, step)
            return True

    def claim_fire(
        self,
        ticker: str,
        *,
        expected_prior_state: dict[str, Any],
        new_state: dict[str, Any],
        step: InverseMartingaleStep,
    ) -> bool:
        """Atomically claim a fire when the prior snapshot still matches.

        Two overlapping scheduled-stream processes (e.g. a slow/retried
        earlier slot still running when a later slot starts) that both read
        the SAME pre-fire state and independently decide to fire would,
        without this, both proceed to synthesize a fire candidate and reach
        broker placement for the same descent. Wrapping the read-compare-
        write in one lock acquisition means only the first writer's claim
        can succeed; a second process's claim call re-reads the (now
        different, already-claimed) state under the same lock and returns
        ``False`` -- the caller must not proceed to placement this run
        (Codex review: "Reserve each fire atomically before broker
        placement").
        """
        return self.compare_and_swap_transition(
            ticker,
            expected_prior_state=expected_prior_state,
            new_state=new_state,
            step=step,
        )

    def events(self, ticker: str) -> list[dict[str, Any]]:
        state_path, _ = self._paths(ticker)
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        events = data.get("events")
        return list(events) if isinstance(events, list) else []


__all__ = [
    "BASIS_PCT",
    "BASIS_STDDEV",
    "STATUS_DIPPING",
    "STATUS_FIRING",
    "STATUS_NONE",
    "CorruptedLedgerStateError",
    "InverseMartingaleLedger",
    "InverseMartingaleStep",
    "evaluate_inverse_martingale_step",
    "resolved_marker",
    "rolling_stddev",
    "stddev_reference_for_ticker",
    "trailing_daily_returns",
]
