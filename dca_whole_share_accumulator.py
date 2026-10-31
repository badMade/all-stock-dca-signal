"""Whole-share accumulation for tickers not tradable for fractional buys.

BYDDY (see ``config/tickers.json``'s ``exclusion_reasons``) is not tradable
for fractional/notional buys on Robinhood, so a normal graded-ladder
dollar-amount buy always blocks at ``near_close_dca``'s
``is_tradable_for_fractional_buy`` gate. Rather than sitting in
``EXCLUDED_TICKERS`` and never buying, BYDDY lives in its own
``near_close_dca.WHOLE_SHARE_TICKERS`` category: this module tracks what the
graded ladder *would have bought* each day for a ticker in that category (a
"virtual" contribution, computed the exact same way as any active ticker's
signal -- see ``near_close_dca._run_once_impl``'s whole-share evaluation
block, which is this module's real-money caller) in a durable, cross-day
ledger, and fires a single whole-share purchase once the accumulated total
reaches the price of one share. A big enough single contribution (or price
drop) can cross more than one share's price at once; the ledger fires that
many whole shares and carries any leftover fraction of a share's price
forward.

Wired into the canonical live path (``run_dca.run_slot()``/``run_pipeline()``
via ``near_close_dca._run_once_impl``) -- see ``docs/whole_share_accumulation.md``
for the full wiring writeup, scope, and its one remaining unverified
assumption (that the Robinhood MCP place/review tools accept a ``quantity``
argument).

This module deliberately contains only deterministic math plus a durable
ledger, mirroring ``dca_cumulative.py``'s split of concerns: callers remain
responsible for their own price, spread, budget, and broker checks before
acting on a decision returned by this module.

Does NOT import from ``near_close_dca`` at module level -- ``near_close_dca``
imports :class:`WholeShareAccumulationLedger` from here, and a module-level
import in the other direction would be circular, exactly why
``dca_inverse_martingale.py`` (the closest precedent) does the same.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, TypeVar
from uuid import uuid4

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from scripts.file_lock import cross_process_lock

if TYPE_CHECKING:
    # Type-only: near_close_dca imports this module at load time, so a runtime
    # import here would be circular (see the module docstring). PR #1312 once
    # dropped this guard for ``config: Any`` to silence an unused-import lint
    # that ruff never actually raises for an annotation-only import; the
    # annotation is what lets a static checker catch a wrong object passed
    # as ``config`` (2026-09-07 diagnostic X:OWN-04).
    from near_close_dca import StrategyConfig

_log = logging.getLogger(__name__)


T = TypeVar("T")

CENT = Decimal("0.01")


class WholeShareClaimPersistenceError(RuntimeError):
    """Raised by :meth:`WholeShareAccumulationLedger.execute_claimed` when
    ``operation`` (the irreversible broker call) SUCCEEDED but persisting the
    resulting ``placed`` marker to the ledger's own state file afterward
    failed.

    Carries the already-accepted ``result`` (e.g. the broker's real order
    id) so a caller can distinguish this from an outright submission failure
    -- letting ``operation()`` itself raise means nothing was ever accepted,
    and every existing failure branch is correct to restore the deducted
    credit via ``preserve_pending()``. THIS exception means the OPPOSITE: a
    real, live order now exists at the broker, and blindly falling into that
    same restore-and-retry branch would let a later fire spend the exact
    same deducted dollars a second time while the first order is still
    working (Codex review discussion_r3804235587, "Preserve broker
    acceptance when persisting `placed` fails"). A caller catching this
    should treat it like any other successful placement -- keep the order
    id, do NOT call ``preserve_pending()`` -- while noting (e.g. in the run
    artifact/logs) that this ledger's own ``placed``/ownership bookkeeping
    for the claim could not be durably recorded and may need manual
    reconciliation if a retry is ALSO attempted before the next successful
    ledger write for this ticker.
    """

    def __init__(self, message: str, result: Any) -> None:
        super().__init__(message)
        self.result = result


def _money(value: Any, *, field: str) -> Decimal:
    """Return a non-negative cent amount, rejecting invalid inputs."""
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{field} must be finite")
    if numeric < 0:
        raise ValueError(f"{field} must not be negative")
    return Decimal(str(numeric)).quantize(CENT, rounding=ROUND_HALF_UP)


def compute_virtual_contribution_usd(
    ticker: str,
    decision_price: float,
    previous_close: float,
    previous_low: Optional[float],
    config: StrategyConfig,
) -> tuple[float, str]:
    """Return the dollar amount today's graded ladder would have bought.

    Convenience wrapper for a caller that wants this module's contribution
    math WITHOUT going through ``near_close_dca._evaluate_all_signals``'s
    full source-reconciliation pipeline (e.g. a one-off manual check, or an
    external script). ``near_close_dca._run_once_impl``'s real wired path
    does NOT call this -- it already has a fully-reconciled
    ``ReconciledDecision.suggested_amount_usd`` computed by the exact same
    underlying functions this wraps, so recomputing here would be redundant.

    Imports ``near_close_dca`` lazily (see the module docstring) since this
    is the one function that genuinely needs its ladder functions at
    runtime; every other name in this module has no such dependency.

    Returns ``(0.0, BASE_NOT_BELOW_CLOSE)`` on any non-buy day (not below the
    prior close, or a drop too shallow to clear the first ladder tier); this
    never invents a positive contribution the ladder itself would not have
    bought.
    """
    from near_close_dca import (
        BASE_NOT_BELOW_CLOSE,
        graded_buy_multiplier,
        select_first_allocation_base_usd,
    )

    if previous_close <= 0:
        return 0.0, BASE_NOT_BELOW_CLOSE
    drop_vs_previous_close = (previous_close - decision_price) / previous_close
    multiplier = graded_buy_multiplier(drop_vs_previous_close, config, ticker)
    base_amount_usd, base_selection_reason = select_first_allocation_base_usd(
        decision_price, previous_close, previous_low, config, ticker
    )
    if multiplier <= 0:
        return 0.0, BASE_NOT_BELOW_CLOSE
    contribution = base_amount_usd * multiplier
    if contribution > 0:
        contribution = max(1.0, contribution)
    return contribution, base_selection_reason


@dataclass(frozen=True)
class WholeShareAccumulationDecision:
    """The result of applying one contribution to a ticker's accumulator.

    ``shares_to_buy`` is the number of whole shares this contribution's
    combined total newly qualifies for -- ``0`` when the balance is still
    short of one share's price, and also ``0`` when returned from an
    already-resolved cached retry (see
    :meth:`WholeShareAccumulationLedger.record_contribution`'s idempotency
    contract) even though the ORIGINAL call fired. ``order_notional_usd`` is
    ``shares_to_buy * current_share_price_usd``, rounded to the cent; it is
    the dollar amount a caller would pass to a quantity-based whole-share
    order (see ``dca_live_adapters.RobinhoodBroker.place_whole_share_buy``).
    ``pending_after_usd`` is always less than ``current_share_price_usd``
    when ``shares_to_buy > 0`` (any full share's worth is deducted), and
    equal to the combined total otherwise.
    """

    pending_before_usd: float
    contribution_usd: float
    current_share_price_usd: float
    shares_to_buy: int
    order_notional_usd: float
    pending_after_usd: float
    claim_token: Optional[str] = None


def apply_contribution(
    pending_usd: float,
    contribution_usd: float,
    current_share_price_usd: float,
) -> WholeShareAccumulationDecision:
    """Pure decision function: combine a contribution with the pending
    balance and decide how many whole shares (if any) the total now covers.

    ``current_share_price_usd`` must be strictly positive AFTER rounding to
    the cent -- a price below half a cent quantizes to ``$0.00`` and would
    otherwise divide by zero below (Codex review: "Reject prices that
    quantize to zero"). A zero, negative, non-finite, or sub-cent price can
    never safely size a whole-share order, and treating it as "buy 0 shares,
    keep everything pending" (rather than raising) would let a bad price
    silently stall accumulation forever without surfacing the problem to a
    caller.
    """
    if not math.isfinite(current_share_price_usd) or current_share_price_usd <= 0:
        raise ValueError("current_share_price_usd must be positive and finite")
    pending = _money(pending_usd, field="pending_usd")
    contribution = _money(contribution_usd, field="contribution_usd")
    price = _money(current_share_price_usd, field="current_share_price_usd")
    if price <= 0:
        raise ValueError("current_share_price_usd must be at least $0.01 after rounding to the cent")
    total = (pending + contribution).quantize(CENT, rounding=ROUND_HALF_UP)

    shares_to_buy = int(total // price)
    order_notional = (Decimal(shares_to_buy) * price).quantize(CENT, rounding=ROUND_HALF_UP)
    pending_after = (total - order_notional).quantize(CENT, rounding=ROUND_HALF_UP)

    return WholeShareAccumulationDecision(
        pending_before_usd=float(pending),
        contribution_usd=float(contribution),
        current_share_price_usd=float(price),
        shares_to_buy=shares_to_buy,
        order_notional_usd=float(order_notional),
        pending_after_usd=float(pending_after),
    )


def _finite_non_negative(raw: object) -> Optional[float]:
    """``raw`` as a finite, non-negative float, or None if it is not one.

    A ledger is arbitrary JSON that ``_read_state()`` admits on shape alone, so
    a corrupt or hand-edited entry can carry ``"NaN"`` or ``"Infinity"`` --
    both of which ``float()`` accepts. Neither survives a comparison the way a
    number does: ``min(pending_usd, nan)`` returns ``pending_usd``, because
    every comparison against NaN is False and ``min`` keeps its first argument.
    So an entitlement recorded as NaN did not merely fail to cap a redispatch,
    it handed it the WHOLE shared pool -- the exact ownership breach the cap
    exists to prevent (Codex review: "Reject non-finite restored credits before
    redispatch"). None means "no usable figure", and every caller treats that
    as no entitlement rather than as an unbounded one.
    """
    # bool is a subclass of int, so float(False)/float(True) succeed as
    # 0.0/1.0 with no TypeError/ValueError to catch -- a persisted dollar
    # field corrupted to a JSON boolean (e.g. withdrawn_usd_total: false)
    # was therefore read as a "valid" zero instead of triggering the
    # entitlement invalidation this function's callers rely on. These
    # fields are numeric, never truth-valued (Codex review: "Reject
    # booleans in persisted dollar fields").
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    except OverflowError:
        # A JSON integer too large to represent as a float (e.g.
        # ``claim_credit_remainder_usd: 10**400``) raises OverflowError, not
        # TypeError/ValueError -- and every caller of this function relies on
        # it to fail closed on exactly this kind of corrupted numeric field.
        # Left uncaught, a single such value anywhere in a ledger's
        # ``recorded_idempotency_keys`` map aborted even read-only callers
        # (``pending_usd()``, ``events()``) that merely iterate every cached
        # entry to validate it, taking the WHOLE ticker down over one
        # unrelated corrupted field instead of writing off just that one
        # entitlement (Codex review: "Handle integer overflow while scanning
        # cached claims").
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


# Sentinel distinguishing "this cache entry predates restored_credit_usd" from
# "this entry genuinely restored $0.00" -- see _redispatchable_credit_usd().
_RESTORED_CREDIT_FIELD = "restored_credit_usd"

#: The value of _WITHDRAWN_TOTAL_FIELD when this claim's restored credit was
#: recorded. Same role for ``restored_credit_usd`` that
#: _CLAIM_REMAINDER_AS_OF_FIELD plays for ``claim_credit_remainder_usd``: the
#: two fields name slices of ONE fungible pool at different points in a claim's
#: life, so both have to age against the same counter. Kept as its own field
#: rather than sharing the remainder's snapshot because the paths that rewrite
#: the remainder (a fresh fire, a redispatch, a reprice) advance that snapshot
#: without touching the restored credit, and reading a stale credit against a
#: fresh snapshot would age it by nothing. Issue #868 collapses both pairs into
#: a single aged entitlement; this keeps the two correct until it does.
_RESTORED_CREDIT_AS_OF_FIELD = "restored_credit_as_of_withdrawn_usd"

#: How much of this claim's _RESTORED_CREDIT_AS_OF_FIELD snapshot instant is
#: already claimed by an EARLIER claim tied at that exact same withdrawal
#: counter value -- see _allocate_credit_tie_offset() (Codex review: "Avoid
#: charging one withdrawal to every restored claim"). Absent (a pre-fix
#: entry, or one with nothing else tied at its snapshot) reads as $0 -- no
#: tie protection, i.e. exactly the old, un-staggered aging behavior.
_RESTORED_CREDIT_TIE_OFFSET_FIELD = "restored_credit_tie_offset_usd"


def _initialize_restored_credit_aging(
    cached: dict, state: dict, migration_stagger: Optional[Dict[float, float]] = None
) -> bool:
    """Migrate restored credit written before its withdrawal snapshot existed.

    Takes ``state`` (not a bare ``withdrawn_total`` float) purely to read the
    current withdrawal counter for the backfilled snapshot. Deliberately does
    NOT route that snapshot through :func:`_allocate_credit_tie_offset`: this
    runs on EVERY :meth:`WholeShareAccumulationLedger.record_contribution`
    call, over an entry whose _AS_OF is missing but whose tie-batch fields on
    ``state`` may well be PRESENT and already advanced by this exact entry's
    own earlier, unrelated writes (e.g. a test -- or an upgrade/rollback --
    that clears only the newest _AS_OF field, leaving the tie-batch anchor
    and width it was originally recorded under untouched). Trusting that
    stale batch state here re-derives an inflated offset from THIS claim's
    own prior history rather than a genuine tie against a DIFFERENT claim,
    reopening the self-collision aging bug (caught by
    ``test_a_legacy_restored_credit_is_snapshotted_before_future_withdrawals``).

    ``migration_stagger``, when given, is a dict private to ONE caller's loop
    over every cached entry (never persisted, never read from ``state``)
    mapping an as-of snapshot value to how much this SAME migration pass has
    already assigned other entries landing on that identical snapshot. Two
    pre-upgrade credits sharing a snapshot both reading offset $0 let a
    single later withdrawal age BOTH of them to zero, stranding whichever
    one's money didn't actually get spent (Codex review: "Stagger tie
    offsets when loading legacy credits"). This is a genuinely different,
    narrower fix than routing through the live batch tracker: it only ties
    a migrated entry against OTHER entries migrated in this exact call, so
    it cannot reopen the self-collision hazard above -- the live
    ``state[_CREDIT_TIE_BATCH_...]`` fields are never read or written here.
    Omitted (the default), this falls back to the old flat-$0 behavior, for
    any caller that cannot offer a scoped stagger dict.
    """
    changed = False
    restored = cached.get(_RESTORED_CREDIT_FIELD)
    order_notional = _finite_non_negative(cached.get("order_notional_usd"))
    if restored is None and order_notional is not None and order_notional > 0:
        cached[_RESTORED_CREDIT_FIELD] = order_notional
        restored = order_notional
        changed = True
    if restored is not None and _RESTORED_CREDIT_AS_OF_FIELD not in cached:
        as_of_now = state.get(_WITHDRAWN_TOTAL_FIELD, 0.0)
        cached[_RESTORED_CREDIT_AS_OF_FIELD] = as_of_now
        if migration_stagger is not None:
            offset = migration_stagger.get(as_of_now, 0.0)
            cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = offset
            migration_stagger[as_of_now] = offset + (_finite_non_negative(restored) or 0.0)
        else:
            cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = 0.0
        changed = True
    return changed


def _redispatchable_credit_usd(
    cached: dict, pending_usd: float, withdrawn_total: float = 0.0
) -> float:
    """How much of the shared pending pool a zero-contribution redispatch of
    ``cached`` may spend.

    A retired claim restored its OWN notional back into ``state["pending_usd"]``
    via :meth:`WholeShareAccumulationLedger.preserve_pending`, but that pool is
    SHARED across every contribution this ticker has accumulated. Sizing a
    redispatch off the whole balance would let one preserved fire spend
    unrelated later contributions, so the redispatch is capped at the credit
    this specific claim actually put back (Codex review: "Limit whole-share
    redispatch sizing to the claim's restored credit").

    Entries written BEFORE ``restored_credit_usd`` existed carry no such field.
    Defaulting those to ``0.0`` would cap every subsequent zero-contribution
    retry at nothing and permanently strand an actionable preserved fire across
    an upgrade or rollback cycle, even though the ledger still holds the
    restored balance. A MISSING field with a positive ``order_notional_usd``
    therefore falls back to that -- the amount that fire deducted, and
    precisely what every preserve_pending() caller hands back as
    ``deducted_usd`` -- so a pre-upgrade entry redispatches exactly the credit
    its restoration returned (Codex review discussion_r3849268546, "Preserve
    redispatch for pre-upgrade ledger entries"). Falling back to the whole
    pending balance instead would keep the cap's own P1 hazard alive for the
    length of the upgrade window: a legacy entry could still spend an
    unrelated key's contribution, which is the very thing this helper exists
    to prevent. A field that is PRESENT and ``0.0`` is honored as a genuine
    "restored nothing" cap.

    A MISSING field with ``order_notional_usd`` also ``0`` is a DIFFERENT
    shape from a pre-upgrade preserved fire: it never had any order attempted
    at all -- a genuinely fresh contribution that never reached the share
    price is created already resolved (nothing to place or settle), with
    ``order_notional_usd`` zero by construction, not because an order was
    preserved. Falling back to ``order_notional_usd`` there capped it at
    nothing even though its real accumulated contribution sits, aged exactly
    like every other remainder read, in ``claim_credit_remainder_usd`` -- a
    same-slot zero-contribution retry (the eligibility splice recognizing a
    clean re-evaluation) could see the balance as actionable while both this
    cap and the live/preview reclaim paths reported zero shares, stranding it
    (Codex review: "Preserve zero-share contributions for same-slot
    redispatch").

    Both the primary ``restored_credit_usd`` figure and the legacy
    ``order_notional_usd`` fallback are read through :func:`_finite_non_negative`
    rather than a bare ``float()`` cast. A hand-edited or pre-upgrade cache
    entry can carry ``"NaN"``/``"Infinity"`` for either field, and neither
    survives a comparison the way a real number does -- an unvalidated
    fallback would hand a zero-contribution redispatch the ENTIRE finite
    ``pending_usd`` pool instead of just this claim's own money, the same
    ownership breach the whole cap exists to prevent (Codex review: "Reject
    non-finite legacy fallback notionals"). An unusable figure on either path
    fails CLOSED to the next fallback rather than granting an unbounded cap.
    """
    if _RESTORED_CREDIT_FIELD in cached:
        surviving = _finite_non_negative(cached.get(_RESTORED_CREDIT_FIELD))
        if surviving is None:
            # Fails CLOSED, and deliberately does not take the legacy
            # fallback below. That fallback exists for an entry written
            # before this field existed -- one where the field is ABSENT --
            # and it hands back order_notional_usd with no withdrawal aging
            # applied. A field that is PRESENT -- even as JSON null, which
            # _finite_non_negative() also rejects -- and unreadable is a
            # different thing: the entry is new enough to have had a
            # snapshot, so granting it the un-aged legacy figure lets a claim
            # whose restored money another claim already withdrew spend an
            # unrelated later contribution up to that old notional (Codex
            # review: "Write off malformed restored credit instead of
            # falling back"; "Treat null restored credits as invalid").
            return 0.0
        # Aged by what the pool was DRAWN by since this credit was recorded, for
        # exactly the reason _claim_credit_remainder_usd() ages its own figure: the
        # balance clamp below cannot tell "never spent" from "spent and refilled".
        # Claim A restores $10, claim B withdraws that $10, claim C then contributes
        # a fresh $10 -- a pool-only clamp reads $10 present and hands A a $10 cap
        # that lets it submit an order funded entirely by C's contribution (Codex
        # review discussion_r3867623114, "Age restored credit before redispatching
        # it"). The withdrawal counter records B's draw permanently, so A's cap
        # stays written off no matter what lands afterwards.
        #
        # An entry carrying a restored credit but no snapshot was written before the
        # counter existed: it is charged no withdrawal and falls back to the balance
        # clamp alone, exactly as it behaved when it was written.
        #
        # ABSENT is not the same thing as PRESENT-but-null, the same
        # distinction this module draws everywhere else. `as_of is not
        # None` treated an explicit JSON null the SAME as a truly missing
        # key -- both skipped aging entirely -- but a PRESENT null means
        # the entry is new enough to have had a snapshot recorded and it
        # was lost/corrupted, not that none was ever taken. Trusting it as
        # "no snapshot" let a claim whose restored money another claim
        # already withdrew spend an unrelated later contribution up to the
        # un-aged notional (Codex review: "Reject null restored-credit
        # snapshots"). Only true key absence gets the legacy exemption now.
        if _RESTORED_CREDIT_AS_OF_FIELD in cached:
            as_of = cached.get(_RESTORED_CREDIT_AS_OF_FIELD)
            as_of_usd = _finite_non_negative(as_of)
            withdrawn_usd = _finite_non_negative(withdrawn_total)
            if (
                as_of_usd is None
                or withdrawn_usd is None
                or as_of_usd > withdrawn_usd
            ):
                # An unreadable snapshot cannot prove the entitlement survived any
                # intervening draw, so it is written off rather than trusted.
                return 0.0
            # Offset by however much of this exact snapshot instant an
            # EARLIER-recorded, still-tied claim already claimed -- see
            # _allocate_credit_tie_offset() (Codex review: "Avoid charging
            # one withdrawal to every restored claim"). Absent/unreadable
            # reads as $0 (no protection -- the old, un-staggered behavior),
            # never as an unbounded offset.
            tie_offset = _finite_non_negative(cached.get(_RESTORED_CREDIT_TIE_OFFSET_FIELD)) or 0.0
            drawn_since_snapshot = max(0.0, withdrawn_usd - as_of_usd)
            surviving -= max(0.0, drawn_since_snapshot - tie_offset)
        return max(0.0, min(pending_usd, surviving))
    # restored_credit_usd is missing or None: a pre-upgrade entry. Validate the
    # legacy order_notional_usd fallback the same way -- a bare float() cast
    # would let a "NaN"/"Infinity" notional through and hand the redispatch
    # the whole pending pool (see the docstring above).
    order_notional = _finite_non_negative(cached.get("order_notional_usd"))
    if order_notional is not None and order_notional > 0:
        return min(pending_usd, order_notional)
    return min(pending_usd, _claim_credit_remainder_usd(cached, pending_usd, withdrawn_total))


#: How much of a claim's own entitlement is still sitting in the shared pending
#: pool rather than committed to its currently-outstanding order. Written by
#: every path that sizes a fire for a claim; folded back into
#: ``restored_credit_usd`` when that fire is preserved.
_CLAIM_REMAINDER_FIELD = "claim_credit_remainder_usd"


#: Running total of every dollar this ledger has ever RESERVED for an order.
#: Monotonic: reservations released back to the pool do not decrement it. Paired
#: with a per-claim snapshot so a fold can tell how much the pool was drawn
#: while a remainder sat in it -- see _claim_credit_remainder_usd().
_WITHDRAWN_TOTAL_FIELD = "withdrawn_usd_total"

#: The value of _WITHDRAWN_TOTAL_FIELD when this claim's remainder was recorded.
_CLAIM_REMAINDER_AS_OF_FIELD = "claim_credit_remainder_as_of_withdrawn_usd"

#: Parallel to _RESTORED_CREDIT_TIE_OFFSET_FIELD, for claim_credit_remainder_usd
#: -- see _allocate_credit_tie_offset() (Codex review: "Avoid charging one
#: withdrawal to every restored claim").
_CLAIM_REMAINDER_TIE_OFFSET_FIELD = "claim_credit_remainder_tie_offset_usd"


def _claim_credit_remainder_usd(cached: dict, pending_usd: float, withdrawn_total: float) -> float:
    """The un-spent part of this claim's entitlement STILL in the pending pool.

    A fire never spends the claim's whole entitlement unless the price divides
    it exactly: a $20 credit redispatched at $8 a share buys two shares for $16
    and leaves $4 of the CLAIM'S OWN money in the pool. Preserving the failed
    $16 order restores the pool to $20, so recording ``deducted_usd`` alone as
    the new cap would strand that $4 -- a later retry at $18 would be capped at
    $16 and report zero shares even though the claim's own credit covers it
    (Codex review: "Preserve the claim-owned remainder across failed
    redispatches").

    The recorded figure is a SNAPSHOT from when the fire was sized, and the pool
    it names is fungible, so two things have to reduce it:

    * **What the pool was DRAWN by since.** Every dollar reserved for an order
      between the snapshot and now could have come out of this remainder --
      money is not labelled once it is in the pool. ``withdrawn_total`` is
      monotonic, so ``withdrawn_total - claim's snapshot`` is that draw, and it
      is subtracted in full. This is deliberately pessimistic: a fire funded
      entirely by its own fresh contribution still writes the remainder down,
      because nothing in a shared pool can prove otherwise. Under-crediting
      only costs the claim a later retry; over-crediting spends money that was
      never its.
    * **What the pool currently HOLDS.** A belt-and-braces upper bound for any
      path that moves the balance without reserving anything.

    The withdrawal test is the one that matters, because the balance test alone
    cannot see replenishment: claim A leaves $4, claim B spends it to nothing,
    an unrelated claim C then contributes a fresh $4, and a pool-only clamp
    reads $4 present and hands A a $20 cap that lets it redispatch C's money
    (Codex review: "Track remainder ownership across pool replenishment"). The
    withdrawal counter records B's draw permanently, so A's remainder stays
    written off no matter what lands afterwards.

    Absent on an entry written before this field existed, and on any entry whose
    fire spent its entitlement exactly; ``0.0`` is the correct, conservative
    reading of both -- it can only under-credit the claim, never let it reach
    money that was never its. An entry carrying a remainder but no snapshot
    (written before the counter existed) is charged no withdrawal and falls back
    to the balance clamp alone, exactly as it behaved when it was written.
    """
    remainder = cached.get(_CLAIM_REMAINDER_FIELD)
    if remainder is None:
        return 0.0
    surviving = _finite_non_negative(remainder)
    if surviving is None:
        # Same reasoning as _redispatchable_credit_usd(): an unreadable
        # remainder is no remainder, never an unbounded one.
        return 0.0
    if _CLAIM_REMAINDER_AS_OF_FIELD in cached:
        as_of = cached.get(_CLAIM_REMAINDER_AS_OF_FIELD)
        as_of_usd = _finite_non_negative(as_of)
        withdrawn_usd = _finite_non_negative(withdrawn_total)
        if (
            as_of_usd is None
            or withdrawn_usd is None
            or as_of_usd > withdrawn_usd
        ):
            # An unreadable snapshot cannot prove the entitlement survived any
            # intervening draw, so it is written off rather than trusted.
            return 0.0
        # Same fix as _redispatchable_credit_usd(): offset by however much of
        # this exact snapshot instant an EARLIER-recorded, still-tied claim
        # already claimed -- see _allocate_credit_tie_offset() (Codex review:
        # "Avoid charging one withdrawal to every restored claim").
        tie_offset = _finite_non_negative(cached.get(_CLAIM_REMAINDER_TIE_OFFSET_FIELD)) or 0.0
        drawn_since_snapshot = max(0.0, withdrawn_usd - as_of_usd)
        surviving -= max(0.0, drawn_since_snapshot - tie_offset)
    return max(0.0, min(surviving, float(pending_usd)))


def _record_withdrawal(state: dict, reserved_usd: float) -> None:
    """Add ``reserved_usd`` to the monotonic pool-withdrawal counter.

    Called wherever an order reserves money out of ``pending_usd``. A reclaim
    that RESIZES an existing reservation passes only the increase; releasing
    money back (a preserve, or a downward reprice) records nothing, because the
    counter measures how much the pool has been drawn, not what is outstanding.
    """
    if reserved_usd <= 0:
        return
    state[_WITHDRAWN_TOTAL_FIELD] = round(float(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0)) + reserved_usd, 2)


#: The withdrawal-counter value at which the CURRENT batch of tied credit
#: snapshots is anchored. A "batch" is every credit snapshot recorded back to
#: back at the exact same ``_WITHDRAWN_TOTAL_FIELD`` value, with no
#: intervening real withdrawal to tell them apart -- see
#: :func:`_allocate_credit_tie_offset`.
_CREDIT_TIE_BATCH_ANCHOR_FIELD = "credit_tie_batch_anchor_withdrawn_usd"

#: How much surviving credit the current tie batch has already claimed --
#: i.e. how many dollars of the batch's shared withdrawal-counter instant are
#: already spoken for by EARLIER claims in this same batch.
_CREDIT_TIE_BATCH_WIDTH_FIELD = "credit_tie_batch_width_usd"

#: Durable audit trail: for every (claim, credit field, snapshot instant)
#: currently carrying a LIVE tie offset, the exact value that offset is
#: supposed to hold. Keyed by ``f"{dedup_key}:{field}:{_snapshot_key(as_of)}"``.
#: Unlike any bound derived from OTHER claims' current state -- three
#: straight rounds of review on this PR each broke a different such
#: derivation (grouping by matching snapshot broke when a sibling claim
#: moved on; summing other claims' current amounts broke when a sibling's
#: balance later shrank; even excluding only this claim's own amount from a
#: shared high-water mark still let a corrupted offset expand to swallow
#: whatever a DIFFERENT claim registered afterward at the same anchor) --
#: this sidesteps reconstruction entirely: the true, exact offset for a
#: given (claim, field, snapshot) is recorded the moment it is legitimately
#: computed. Validating a live offset is then a straight equality check
#: against its own audit entry, not a derived bound at all, so nothing about
#: ANY other claim's current or historical state can loosen or tighten it.
#: See the validation pass at the end of :meth:`WholeShareAccumulationLedger._read_state`.
#:
#: NOT write-once for a given key, though: a live offset can legitimately
#: change AFTER its first audit entry two ways, and both must move the
#: audit alongside it or the next load sees a stale mismatch and wrongly
#: writes off real money -- :func:`_retire_credit_tie_interval` shifting a
#: still-open sibling's offset down to close a gap (Codex review: "Keep
#: audits synchronized when shifting tie offsets"), and a claim's own
#: interval being reset to $0/relabeled onto a DIFFERENT field once it is
#: consumed (Codex review: "Retire the remainder audit when relabeling
#: credit") -- see :func:`_forget_tie_offset_audit`.
_CREDIT_TIE_OFFSET_AUDIT_FIELD = "credit_tie_offset_audit"


def _tie_offset_audit_key(dedup_key: str, field: str, as_of: float) -> str:
    return f"{dedup_key}:{field}:{_snapshot_key(as_of)}"


def _forget_tie_offset_audit(state: dict, dedup_key: str, field: str, as_of: float) -> None:
    """Remove the audit entry for a (claim, field, snapshot) whose live
    interval is being reset to $0/consumed, rather than leaving it behind to
    mismatch the reset value on the next load.

    A claim resetting its OWN remainder/restored-credit field to $0 after
    folding it elsewhere (settled, preserved, or relabeled onto the other
    field) is not corruption -- it is the SAME (claim, field, snapshot)
    registration legitimately ending. Left un-cleared, its old nonzero
    audit entry would mismatch the freshly-zeroed live value the very next
    time ``_read_state()`` runs, logging a corruption warning and
    "writing off" an entitlement that was already correctly zero -- harmless
    to the balance (there was nothing left to lose) but a permanent,
    self-inflicted false-positive on every subsequent load (Codex review:
    "Retire the remainder audit when relabeling credit"). A missing key is
    a no-op: nothing to forget for an entry that predates the audit trail
    entirely, or was already cleared.
    """
    audit = state.get(_CREDIT_TIE_OFFSET_AUDIT_FIELD)
    if isinstance(audit, dict):
        audit.pop(_tie_offset_audit_key(dedup_key, field, as_of), None)


def _snapshot_key(value: float) -> str:
    """Stable string key for a withdrawal-counter snapshot value.

    Rounds to the cent first (every snapshot value in this module already is
    one) so the same logical instant always maps to the same JSON dict key,
    regardless of minor float-repr differences between where it was written
    and where it is looked up.
    """
    return f"{round(value, 2):.2f}"


def _allocate_credit_tie_offset(
    state: dict, surviving_amount: float, *, dedup_key: str, field: str
) -> float:
    """Return how much of THIS claim's ``_AS_OF`` snapshot is already spoken
    for by other claims tied at the exact same withdrawal-counter instant --
    the value a caller stores into that claim's own
    ``_RESTORED_CREDIT_TIE_OFFSET_FIELD``/``_CLAIM_REMAINDER_TIE_OFFSET_FIELD``
    alongside an ``_AS_OF_FIELD`` still set to the plain, unmodified
    ``state[_WITHDRAWN_TOTAL_FIELD]``.

    ``dedup_key`` (this claim's own ``recorded_idempotency_keys`` key) and
    ``field`` (``_RESTORED_CREDIT_FIELD`` or ``_CLAIM_REMAINDER_FIELD``)
    identify exactly which claim/field this call's returned offset belongs
    to, so it can be recorded into the durable
    ``_CREDIT_TIE_OFFSET_AUDIT_FIELD`` audit trail alongside the live batch
    bookkeeping below -- see that field's own docstring for why this
    replaced every earlier attempt at a derived bound.

    Before this helper existed, every claim's ``_AS_OF`` snapshot was just
    the raw, POOL-WIDE withdrawal counter, with nothing recorded alongside
    it. Two claims restoring/leaving credit at the exact same withdrawal
    snapshot (a completely ordinary case: two contributions sized in the
    same run, or two fires preserved back to back before anything else
    touches the pool) then shared the identical ``as_of`` value with no way
    to tell them apart, and ``_redispatchable_credit_usd()``/
    ``_claim_credit_remainder_usd()`` aged EACH of them by the FULL
    withdrawal delta since that shared snapshot -- not a proportional or
    FIFO share of it, the WHOLE thing, independently, for every claim that
    happened to tie. Claim A and claim B each leave $10 in a $20 pool at the
    same snapshot; a single later $10 reservation aged BOTH down to $0, even
    though only $10 actually left the pool and $10 legitimately remains --
    one of the two claims stranded at $0 for no reason tied to its own money
    ever having been spent (Codex review: "Avoid charging one withdrawal to
    every restored claim").

    The fix treats every batch of claims tied at the same counter value as a
    FIFO queue: the first claim recorded in a batch has a tie offset of $0
    (nothing precedes it), and each later claim in the SAME batch is offset
    by the cumulative surviving amount every EARLIER claim in that batch
    already claimed. Aging then subtracts ``max(0, (withdrawn_now - as_of) -
    tie_offset)`` instead of the raw, un-offset delta: the first $10 of any
    later withdrawal is absorbed entirely by the earliest-recorded claim in
    the tied batch, the next $10 by the next, and so on, so a single $10
    withdrawal drains exactly A's $10 (the earlier claim) and leaves B's $10
    (the later one, offset by A's $10) completely untouched, instead of
    charging the same $10 to both.

    Deliberately does NOT advance ``as_of`` itself into the future the way
    an earlier version of this fix did (each claim's own disjoint slice of
    the withdrawal axis, stored directly as its ``as_of``) -- that collided
    with an independent, pre-existing fail-closed guard elsewhere in this
    module that treats ``as_of > withdrawn_now`` as proof a snapshot was
    corrupted or hand-tampered (a value that could never arise legitimately
    under the OLD, un-staggered semantics, where every ``as_of`` was always
    read straight off the CURRENT counter). Encoding the same FIFO ordering
    as a same-instant OFFSET instead keeps ``as_of`` exactly what it always
    was -- the plain counter value at snapshot time, never ahead of it --
    while still letting aging tell tied claims apart.

    A batch resets (offset restarts at $0) the moment the withdrawal counter
    moves: ``_CREDIT_TIE_BATCH_ANCHOR_FIELD`` records which counter value the
    CURRENT batch's claims are tied at, and a call whose current counter
    doesn't match it starts a brand new batch rather than extending a stale
    one. This is what keeps a claim's OWN successive re-snapshots of its OWN
    entitlement (e.g. a redispatch-fire's leftover remainder, written
    immediately after that SAME fire's own real withdrawal already moved the
    counter) from inheriting tie-batch space an EARLIER, unrelated write
    reserved: that earlier write's batch is anchored at the OLD counter
    value, this call's counter has since moved, so it starts a fresh batch
    at offset $0 -- exactly the un-aged-by-anything-but-its-own-history
    behavior a claim's own transition should have.

    Anchor/width values that fail :func:`_finite_non_negative` (corrupt or
    hand-edited state) are treated as "no batch yet" and reset to a fresh
    one anchored at the current counter, for the same reason a corrupt
    ``_AS_OF``/credit figure elsewhere in this module fails to $0 rather than
    trusting it: an unreadable batch cannot prove which claims it already
    covers, so starting over (offset $0, the maximally UN-protective choice
    for whichever claim gets it) can only cost a later retry, never grant
    unbounded credit.

    A zero (or negative, already clamped to zero) ``surviving_amount`` still
    returns a valid offset, but never grows the batch's width -- there is
    nothing on the axis for a later, real claim to need protecting from.
    """
    withdrawn_now = _finite_non_negative(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0)) or 0.0
    anchor = _finite_non_negative(state.get(_CREDIT_TIE_BATCH_ANCHOR_FIELD))
    width = _finite_non_negative(state.get(_CREDIT_TIE_BATCH_WIDTH_FIELD)) or 0.0
    if anchor is None or anchor != withdrawn_now:
        anchor = withdrawn_now
        width = 0.0
    offset = width
    added = max(0.0, surviving_amount)
    new_width = round(width + added, 2)
    state[_CREDIT_TIE_BATCH_ANCHOR_FIELD] = anchor
    state[_CREDIT_TIE_BATCH_WIDTH_FIELD] = new_width
    # Durable, write-once record of exactly this offset for exactly this
    # (claim, field, snapshot) -- see _CREDIT_TIE_OFFSET_AUDIT_FIELD's own
    # docstring for why this replaced every earlier attempt at deriving a
    # bound from other claims' state. Recorded unconditionally, including
    # when `offset` is $0: an audit entry's mere PRESENCE is what lets
    # _read_state() distinguish "this exact value was legitimately computed"
    # from "no record exists, cannot be proven either way".
    audit = state.setdefault(_CREDIT_TIE_OFFSET_AUDIT_FIELD, {})
    if not isinstance(audit, dict):
        audit = {}
        state[_CREDIT_TIE_OFFSET_AUDIT_FIELD] = audit
    audit[_tie_offset_audit_key(dedup_key, field, anchor)] = offset
    return offset


def _retire_credit_tie_interval(
    state: dict,
    recorded_keys: dict,
    as_of_value: float,
    removed_offset: float,
    removed_width: float,
) -> None:
    """Undo a single claim's own PRIOR tie-batch registration so its credit
    can be re-allocated fresh, without corrupting every OTHER claim already
    registered in the same batch.

    A caller re-registering a claim's credit (a reclaim, a reprice, a
    preserve) that was already counted in the CURRENT batch must first
    "take back" that old registration -- otherwise re-allocating the new
    total on top of it double-counts the unchanged portion (Codex review:
    "Avoid registering the reclaimed/preserved remainder twice"). Simply
    decrementing ``state[_CREDIT_TIE_BATCH_WIDTH_FIELD]`` by the old width
    is only safe when the interval being removed was the TAIL of the batch
    -- nothing else registered after it. When some OTHER claim tied at the
    same instant registered its own interval AFTER this one, blindly
    shrinking the width instead compacts the freed space from the WRONG
    position: the next fresh allocation lands exactly where that other
    claim's still-live interval already sits, silently overlapping it
    (Codex review: "Keep later tie offsets valid when moving a
    remainder"). For example, A registers $4 at offset 0 (width 4), B then
    registers $4 at offset 4 (width 8); naively subtracting A's $4 drops
    the width back to 4, and A's fresh re-allocation lands at offset 4 --
    exactly B's own offset.

    The correct fix shifts every OTHER claim's tie offset that sits AFTER
    the removed interval down by the removed width, closing the gap left
    behind instead of reusing its coordinates -- an ordinary array-delete,
    not a blind size decrement. Only entries tied to the SAME batch instant
    (``as_of_value``, matched against each candidate offset field's own
    ``_AS_OF`` sibling) are eligible: an entry from a different, already-
    reset batch has nothing on this axis to shift. Both offset fields a
    claim can carry (``_CLAIM_REMAINDER_TIE_OFFSET_FIELD`` for a still-open
    remainder, ``_RESTORED_CREDIT_TIE_OFFSET_FIELD`` for a resolved claim's
    restored credit) are checked, since either can belong to a claim that
    registered after the one being retired. A zero or negative width is a
    no-op: there is nothing registered to take back.
    """
    if removed_width <= 0:
        return
    width = _finite_non_negative(state.get(_CREDIT_TIE_BATCH_WIDTH_FIELD)) or 0.0
    state[_CREDIT_TIE_BATCH_WIDTH_FIELD] = max(0.0, round(width - removed_width, 2))
    for other_dedup_key, other in recorded_keys.items():
        if not isinstance(other, dict):
            continue
        for offset_field, as_of_field, field in (
            (_CLAIM_REMAINDER_TIE_OFFSET_FIELD, _CLAIM_REMAINDER_AS_OF_FIELD, _CLAIM_REMAINDER_FIELD),
            (_RESTORED_CREDIT_TIE_OFFSET_FIELD, _RESTORED_CREDIT_AS_OF_FIELD, _RESTORED_CREDIT_FIELD),
        ):
            other_as_of = _finite_non_negative(other.get(as_of_field))
            if other_as_of is None or other_as_of != as_of_value:
                continue
            other_offset = _finite_non_negative(other.get(offset_field))
            if other_offset is None or other_offset <= removed_offset:
                continue
            shifted_offset = max(0.0, round(other_offset - removed_width, 2))
            other[offset_field] = shifted_offset
            # This is a legitimate change to an EXISTING (claim, field,
            # as_of) registration's own offset -- not a new one -- so its
            # _CREDIT_TIE_OFFSET_AUDIT_FIELD entry must move with it. Left
            # stale, the next _read_state() load would see this claim's
            # live offset no longer match what was audited for it and write
            # off a perfectly legitimate credit (Codex review: "Keep audits
            # synchronized when shifting tie offsets").
            audit = state.setdefault(_CREDIT_TIE_OFFSET_AUDIT_FIELD, {})
            if not isinstance(audit, dict):
                audit = {}
                state[_CREDIT_TIE_OFFSET_AUDIT_FIELD] = audit
            audit_key = _tie_offset_audit_key(other_dedup_key, field, other_as_of)
            if audit_key in audit:
                audit[audit_key] = shifted_offset


#: The pool amount a claim's fire may be SIZED from, as distinct from the
#: shared-pool figures its decision REPORTS. Written by every path that sizes a
#: fire for a claim; read by the reclaim/reprice paths.
_CLAIM_SIZING_BASIS_FIELD = "claim_sizing_basis_usd"


def _claim_sizing_basis_usd(cached: dict) -> float:
    """The amount a reclaim of ``cached`` may re-size this claim's fire from.

    ``pending_before_usd`` cannot serve as this basis. A capped redispatch
    deliberately records the WHOLE shared pool in that field -- the decision's
    balance fields describe the pool the fire draws from, so preview and live
    call agree with the ledger -- while sizing the order itself off the claim's
    own restored credit. Repricing an unresolved redispatch from
    ``pending_before_usd`` therefore re-opens the cap the redispatch just
    applied: a claim with $20 restored in a $25 pool fires one $20 share and
    caches $25, and an immediate retry at $12 reprices from $25 and authorizes
    two shares for $24, spending $4 that was never this claim's (Codex review:
    "Preserve the restored-credit cap through claim reclaims").

    Absent on an entry written before this field existed, where
    ``pending_before_usd`` was both the reported balance and the sizing basis;
    falling back to it there reproduces exactly the previous behavior.
    """
    basis = cached.get(_CLAIM_SIZING_BASIS_FIELD)
    if basis is None:
        return float(cached["pending_before_usd"])
    return float(basis)


# Same canonical ticker-symbol shape schemas/tickers.schema.json enforces for
# "active"/"whole_share" entries (letters and dots only, 1-6 characters).
# StrategyConfig.whole_share_tickers isn't itself schema-validated at every
# entry point, though -- config loaded via an alternate $DCA_CONFIG_DIR or an
# explicit whole_share_tickers= keyword override reaches this ledger's
# every-method ticker parameter WITHOUT ever passing through that schema
# check. Every method here uses the ticker verbatim as a filesystem path
# component (self.root / symbol / "ledger.json"); a malformed value like
# "../../outside" would resolve OUTSIDE this ledger's own artifact tree,
# letting durable whole-share state be written at an unintended location
# (Codex review: "Validate whole-share symbols before using them as paths").
#
# Requires at least one LETTER via the lookahead -- the bare "[A-Z.]{1,6}"
# character class alone also accepts a symbol made ENTIRELY of dots (e.g.
# ".." or "."), which every method here still resolves as a filesystem path
# component: self.root / ".." / "ledger.json" writes the ledger and its
# lock in the PARENT directory instead of under this ledger's own
# whole-share stream tree, risking collisions with unrelated artifacts
# (Codex review: "Require a letter in whole-share symbols").
_SYMBOL_PATTERN = re.compile(r"^(?=.*[A-Z])[A-Z.]{1,6}$")

#: Historically doubled as the reclaim window for an UNRESOLVED fire's own
#: ``claimed_at`` reservation inside this module's own ``record_contribution``
#: (via a `_claim_is_stale()` helper this module used to define). PR #515
#: ("Fix whole-share retry ownership and fill settlement") replaced THAT use
#: entirely: ownership of a fire's claim is now enforced by
#: :meth:`WholeShareAccumulationLedger.execute_claimed` holding the
#: per-ticker cross-process lock across BOTH the final ownership check and
#: the irreversible broker call itself, so a live owner cannot be reclaimed
#: out from under it, and a genuinely crashed owner's OS-level lock releases
#: immediately -- ``record_contribution`` reclaims an unresolved,
#: not-yet-``placed`` fire immediately, with no wall-clock wait.
#:
#: This constant survives for a SEPARATE, still-live purpose:
#: ``near_close_dca._whole_share_claim_is_stale``/
#: ``_reconcile_unresolved_whole_share_claim`` use it to decide how long to
#: wait before trusting a CLEAN "no live order found" broker lookup as real
#: evidence a placement was never accepted or was genuinely rejected, rather
#: than a possibly-still-in-flight submission whose order simply is not
#: indexed at the broker yet -- a question about broker-side eventual
#: consistency, unrelated to this ledger's own in-process claim ownership.
UNRESOLVED_CLAIM_STALE_SECONDS = 300


class WholeShareAccumulationLedger:
    """Durably retain a ticker's cross-day pending accumulation balance.

    Unlike :class:`dca_cumulative.CumulativeCreditLedger` (one file PER
    TRADING DAY, reset to zero every day), this ledger persists a SINGLE
    balance per ticker across day boundaries -- a whole share's price is
    usually many days' worth of sub-dollar ladder contributions, so the
    balance must survive every day it has not yet crossed the threshold.
    State lives at ``stream_root("whole_share_accumulation")/<TICKER>/ledger.json``.

    Every mutating method takes the SAME ``idempotency_key`` for one fire
    cycle: :meth:`record_contribution` stores the fire decision under it;
    :meth:`settle` and :meth:`preserve_pending` look it up to know how much
    THIS fire deducted and mark it resolved, so a retried
    ``record_contribution`` call for the same key never re-suggests a fire
    that was already settled or restored (Codex review: "Reconcile cached
    fire decisions before returning them").
    """

    def __init__(self, root: Optional[str | Path] = None) -> None:
        self.root = Path(root) if root is not None else Path(stream_root("whole_share_accumulation"))

    def discover_symbols(self) -> list[str]:
        """Every ticker symbol with an existing ledger directory under
        this root, regardless of whether it is currently enrolled in ANY
        config category (active whole-share, excluded, or removed
        entirely).

        Used by near_close_dca's cross-day recovery pre-pass to find an
        unresolved claim for a ticker an operator has since removed from
        ``config.whole_share_tickers`` -- e.g. delisted, or moved to
        ``excluded_tickers``. Without this, a genuinely unresolved claim
        for a ticker no longer enrolled can never be reconciled again by
        any later run (the pre-pass only iterates the CURRENT config's
        own ticker set), permanently stranding its accumulated balance
        and any real fill/shortfall the ledger never gets to book (Codex
        review: "Reconcile claims for tickers removed from
        configuration"). Only inspects directory NAMES (cheap, no
        per-symbol lock/read) -- callers still reconcile each one through
        the normal unresolved-claims machinery, which is itself a no-op
        for a symbol with nothing left to reconcile.
        """
        if not self.root.exists():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and _SYMBOL_PATTERN.match(entry.name)
        )

    def _redispatch_resolved_or_placed_claim(
        self,
        state: Dict[str, Any],
        day_dir: Path,
        state_path: Path,
        dedup_key: str,
        cached: Dict[str, Any],
        trading_day: str,
        contribution_usd: float,
        current_share_price_usd: float,
        stream: str,
        recorded_at: Optional[str],
    ) -> WholeShareAccumulationDecision:
        # A cached entry currently marked "placed" (broker
        # accepted the order via execute_claimed(), settle() has
        # not yet run) is NOT eligible for the redispatch fix
        # below even if it happens to also be "resolved" from a
        # PRIOR fire under this same slot -- reclaiming or
        # redispatching a live, in-flight broker submission here
        # would let a retry act on a SECOND fire while the first
        # is still being settled, double-spending the same
        # dedup_key (Codex review discussion_r3804235552, "Reject
        # already-placed claims"; mirrored here on the
        # record_contribution side of that same fix -- the
        # execute_claimed()-side half rejects an in-flight
        # placement, this half stops a retry from ever reaching
        # it in the first place).
        #
        # "placing" (set by execute_claimed() durably BEFORE it
        # calls the broker, cleared once it writes "placed" or
        # once the claim is later resolved via settle()/
        # preserve_pending()) means "a submission for this claim
        # was attempted, but this ledger does not yet know
        # whether it succeeded" -- e.g. the owning process was
        # SIGKILLed between the broker accepting the order and
        # this ledger recording that fact. Treating a
        # "placing"-only entry as freely reclaimable here would
        # let this retry pass execute_claimed()'s own checks (the
        # OS lock the crashed owner held has already released)
        # and submit the SAME fire a second time while the first
        # order may still be genuinely live and simply not yet
        # visible in eventually-consistent broker history (Codex
        # review discussion_r3818217230, "Persist placement
        # intent before invoking the broker"). Unlike an
        # ordinary unresolved-and-reclaimable claim, a
        # "placing"-only entry is NOT resolved here -- it stays
        # exactly as-is (falls through to the flat "nothing to
        # do" return below, mirroring the resolved/placed case)
        # until a caller reconciles it against actual broker
        # order history and calls settle()/preserve_pending()
        # (both of which clear "placing" as part of resolving
        # the claim). It remains discoverable for that
        # reconciliation the whole time: unresolved_claim()/
        # unresolved_claims_any_day() key off "resolved" alone,
        # not "placed"/"placing", so a "placing" claim is never
        # invisible to the existing reconciliation machinery.
        if (
            cached.get("resolved")
            and not cached.get("placed")
            and not cached.get("placing")
            and contribution_usd <= 0
        ):
            # A ZERO-contribution retry against an already-resolved
            # slot -- e.g. the preserved-fire redispatch loop in
            # _run_once_impl, re-checking a ticker whose earlier fire
            # was blocked by a transient gate (ceiling/cap/spread/
            # review) and restored via preserve_pending -- must not
            # be flattened to "nothing to do" forever just because
            # THIS exact (trading_day, idempotency_key) slot already
            # resolved once. preserve_pending's restoration already
            # put that fire's full notional back into
            # state["pending_usd"]; if that restored balance ALONE
            # (no new money added -- contribution_usd is 0) now
            # covers a share at the CURRENT price, this is a
            # genuinely NEW, actionable fire the caller should act
            # on, not a duplicate of the one already resolved. The
            # old unconditional zero-shares return stranded that
            # restored balance until some LATER day's signal
            # happened to be a fresh BUY again, with no bound on how
            # long that might take (Codex review: "Allow same-day
            # redispatch of preserved fires"). A NONZERO
            # contribution_usd here (a genuine fresh BUY signal, not
            # a redispatch) is intentionally left out of scope --
            # re-opening a resolved slot for new money risks
            # double-counting the original contribution and is a
            # separate concern from this narrow redispatch case.
            # Also gated (above) on NOT "placed" -- a "placed"
            # entry is a live/just-settled broker submission, not
            # a preserve_pending() restoration, so there is no
            # freed balance here that redispatching should act on
            # (Codex review discussion_r3804235552).
            restored_credit = _redispatchable_credit_usd(
                cached, state["pending_usd"], float(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0))
            )
            redispatch = apply_contribution(restored_credit, 0.0, current_share_price_usd)
            if redispatch.shares_to_buy > 0:
                # Off the CAPPED decision, before the replace()
                # below rewrites the balances to the full pool's:
                # what this claim did not spend of its OWN credit,
                # not what unrelated contributions left behind.
                claim_remainder = redispatch.pending_after_usd
                total_pending = state["pending_usd"]
                redispatch = replace(
                    redispatch,
                    pending_before_usd=total_pending,
                    pending_after_usd=round(total_pending - redispatch.order_notional_usd, 2),
                )
                # A redispatched fire is just as actionable as a
                # fresh or reclaimed one, so it needs the SAME
                # fencing: without a fresh token here, every
                # caller wired for claim_token ownership checks
                # (still_claimed/preserve_pending/settle) would
                # see claim_token=None for this decision and fall
                # back to timestamp-only matching, losing the
                # same-timestamp reclaim protection this PR adds
                # for every other positive decision (Codex
                # review: "Issue an ownership token for
                # redispatched fires").
                redispatch = replace(redispatch, claim_token=uuid4().hex)
                cached["resolved"] = False
                cached["claimed_at"] = recorded_at
                cached["claim_token"] = redispatch.claim_token
                # A fresh claim_token means a genuinely NEW fire
                # attempt -- settle()'s shares_credited marker
                # (see its own docstring) is scoped to the
                # PHYSICAL order a specific claim_token
                # represents, not to this reusable dedup_key
                # entry across its whole lifetime. Left set from
                # the PRIOR (already-settled) fire this slot
                # resolved, a genuinely new physical purchase
                # placed under this redispatch would silently
                # never get counted into shares_purchased_total
                # (Codex review: "Reset the credit marker when
                # redispatching a new fire").
                cached["shares_credited"] = False
                cached["placed"] = False
                cached["pending_before_usd"] = redispatch.pending_before_usd
                cached["contribution_usd"] = 0.0
                cached["current_share_price_usd"] = redispatch.current_share_price_usd
                cached["shares_to_buy"] = redispatch.shares_to_buy
                cached["order_notional_usd"] = redispatch.order_notional_usd
                cached["pending_after_usd"] = redispatch.pending_after_usd
                _record_withdrawal(state, redispatch.order_notional_usd)
                cached[_CLAIM_REMAINDER_FIELD] = claim_remainder
                # Snapshotted AFTER this fire's own withdrawal, so
                # a claim never writes off its own remainder. The
                # tie offset (see _allocate_credit_tie_offset())
                # naturally comes back $0 here in the ordinary
                # case -- this fire's own withdrawal just moved
                # the counter, starting a fresh batch -- but
                # routing it through the same helper as every
                # other write site (rather than a bare counter
                # read) still protects the rare case where
                # ANOTHER claim's snapshot already ties at this
                # exact post-withdrawal instant (Codex review:
                # "Avoid charging one withdrawal to every restored
                # claim").
                cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = _allocate_credit_tie_offset(
                    state, claim_remainder, dedup_key=dedup_key, field=_CLAIM_REMAINDER_FIELD
                )
                cached[_CLAIM_REMAINDER_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
                # The order was sized off restored_credit, but the
                # balance fields above now report the whole pool.
                # A reclaim of this still-unresolved fire must
                # re-size from the CAP, never from those reported
                # balances -- see _claim_sizing_basis_usd().
                cached[_CLAIM_SIZING_BASIS_FIELD] = restored_credit
                recorded_keys = state.setdefault("recorded_idempotency_keys", {})
                recorded_keys[dedup_key] = cached
                state["pending_usd"] = redispatch.pending_after_usd
                state["events"].append(
                    {
                        "trading_day": trading_day,
                        "stream": stream,
                        "recorded_at": recorded_at,
                        "pending_before_usd": redispatch.pending_before_usd,
                        "contribution_usd": 0.0,
                        "current_share_price_usd": redispatch.current_share_price_usd,
                        "shares_to_buy": redispatch.shares_to_buy,
                        "order_notional_usd": redispatch.order_notional_usd,
                        "pending_after_usd": redispatch.pending_after_usd,
                        "decision": "redispatch_fire",
                    }
                )
                day_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
                return redispatch
        # contribution_usd is 0.0, not the original cached
        # amount -- this call makes NO new contribution (the
        # ledger already reflects that original contribution;
        # pending_usd is untouched here). Reporting the ORIGINAL
        # positive amount double-counted it: a caller (see
        # _process_whole_share_candidate) persists this field as
        # "what THIS call contributed" for run-artifact
        # reporting, so a resolved-slot retry (e.g. after a
        # below-threshold accumulation, or a blocked fire that
        # was preserved) would otherwise describe a contribution
        # that never actually happened on this call (Codex
        # review: "Report zero contribution for resolved slot
        # retries").
        return WholeShareAccumulationDecision(
            pending_before_usd=cached["pending_before_usd"],
            contribution_usd=0.0,
            current_share_price_usd=cached["current_share_price_usd"],
            shares_to_buy=0,
            order_notional_usd=0.0,
            pending_after_usd=state["pending_usd"],
        )

    def _reprice_unresolved_claim(
        self,
        state: Dict[str, Any],
        state_path: Path,
        dedup_key: str,
        cached: Dict[str, Any],
        trading_day: str,
        current_share_price_usd: float,
        stream: str,
        recorded_at: Optional[str],
    ) -> WholeShareAccumulationDecision:
        # Reclaim immediately -- no wall-clock staleness wait. PR #515
        # replaced the former UNRESOLVED_CLAIM_STALE_SECONDS lease
        # (which used to gate this branch on elapsed time) with
        # ownership enforced by execute_claimed()'s cross-process
        # lock, which spans both the final ownership check and the
        # actual irreversible broker call: a caller that is genuinely
        # still live and about to place through execute_claimed()
        # either already holds that lock (this call blocks until it
        # releases) or, if this call reclaims first, will itself fail
        # execute_claimed()'s own ownership check once it gets there
        # and abstain -- so an immediate reclaim here can never let
        # two callers both submit the same fire. A crashed owner's
        # claim is reclaimed on the very next retry instead of making
        # every retry wait out a fixed lease. Re-price it against the
        # CURRENT quote (this call's own current_share_price_usd
        # argument), not
        # the original attempt's stale cached price: a fire claimed
        # at $10 and reclaimed after the price moved to $20 would
        # otherwise still hand back a decision sized (quantity AND
        # notional) for the stale $10 price -- and the caller passes
        # that STALE current_share_price_usd straight into
        # _whole_share_risk_gates for the max_per_order_usd ceiling
        # check, letting a one-share order that actually costs ~$20
        # pass a $10-per-order ceiling as if it only cost $10 (Codex
        # review: "Reprice reclaimed stale fires before placement").
        # The total dollars already committed (pending_before_usd +
        # contribution_usd from the ORIGINAL call) is unaffected by
        # price -- only how many shares that total buys, and how
        # much remains pending, changes with the fresh price.
        # Read BEFORE the reprice is sized, not after. The claim can
        # only command what it still OWNS: the money already reserved
        # in its outstanding order, plus whatever of its own remainder
        # survives in the pool. Sizing off the frozen
        # basis + contribution alone assumes the original remainder is
        # still exclusively its own, and the balance check further down
        # only asks whether the POOL can cover the increase -- which it
        # can, out of somebody else's money. A fires $16 from a $20
        # basis leaving $4, B consumes that $4, C later contributes $4,
        # and reclaiming A at a price that raises its order to $20
        # swallowed C's whole contribution (Codex review: "Cap
        # repricing by the claim's surviving remainder").
        #
        # Charged against the counter as it stands BEFORE this reclaim's
        # own withdrawal, so a claim is never made to write off its own
        # draw.
        surviving_remainder = _claim_credit_remainder_usd(
            cached, state["pending_usd"], float(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0))
        )
        had_prior_remainder = _CLAIM_REMAINDER_FIELD in cached
        reserved_usd = _finite_non_negative(cached.get("order_notional_usd")) or 0.0
        frozen_total = _claim_sizing_basis_usd(cached) + float(cached["contribution_usd"])
        if had_prior_remainder:
            # An entry with no remainder field predates it; there is
            # nothing to age, so it keeps the frozen total it has always
            # used.
            reprice_total = min(frozen_total, reserved_usd + surviving_remainder)
        else:
            reprice_total = frozen_total
        repriced = apply_contribution(
            reprice_total, 0.0, current_share_price_usd
        )
        # Reconcile the repriced remainder against the ledger's
        # CURRENT balance, not an absolute snapshot -- mirroring
        # preserve_pending's own established convention. A DIFFERENT
        # idempotency key can have recorded its own contribution
        # (advancing state["pending_usd"]) any time between this
        # fire's original claim and its reclaim here; overwriting
        # state["pending_usd"] with repriced.pending_after_usd
        # outright would silently discard that later activity (e.g.
        # a $15 fire at $10 leaves $5 pending; a later $4
        # contribution under another key makes it $9; reclaiming the
        # first fire at $20 must write $9 + ($15 repriced remainder -
        # $5 original remainder) = $19, not a bare $15) (Codex
        # review: "Preserve newer ledger activity while repricing
        # stale claims").
        # Expressed as the change in what this claim RESERVES from the
        # pool, not as the difference between two remainders. The two
        # are identical whenever the sizing basis and the reported
        # balance are the same figure, but a capped redispatch makes
        # them diverge -- its cached remainder is pool-level while the
        # repriced remainder is basis-level, and subtracting one from
        # the other would move the pool by an amount neither reserved.
        pending_delta = cached["order_notional_usd"] - repriced.order_notional_usd
        # A DOWNWARD reprice (the price dropped) can make
        # pending_delta more negative than the currently available,
        # unreserved balance: the reclaim's total is recomputed from
        # the ORIGINAL frozen pending_before_usd/contribution_usd,
        # which assumes the full original remainder is still
        # exclusively this claim's -- but if a DIFFERENT key already
        # spent part of that shared pool (its own fire, funded in
        # part by this claim's original remainder), only what is
        # CURRENTLY unspent is actually safe to draw further from.
        # Applying the delta anyway would drive state["pending_usd"]
        # negative and hand back an actionable decision that spends
        # more than what remains (Codex review: "Prevent downward
        # repricing from overdrawing pending credit"). Defer instead:
        # leave this claim exactly as-is (unresolved, untouched) and
        # return the same "nothing to do" decision the still-reserved
        # branch above returns -- a later retry, once the pool holds
        # enough balance again, can complete the reclaim safely.
        # An UPWARD reprice (pending_delta negative) draws further
        # from the pool, but the pool-level check above is silent
        # about WHOSE money that draw spends. If a DIFFERENT claim
        # already consumed part of what this claim's own remainder
        # left behind, the pool as a whole can still cover the draw
        # -- the pool-level check alone passes -- while none of what
        # remains is genuinely this claim's: A leaves a $5
        # remainder, B spends it, C then contributes $2 (pool now
        # $2), and an upward reprice drawing $2 more would pass the
        # pool-level check even though A's own surviving remainder
        # is $0 -- spending C's $2 instead (Codex review: "Cap
        # reprice draws at the surviving claim remainder"). The
        # remainder check is skipped for a release (pending_delta
        # >= 0): a release never spends anyone else's money, and
        # _claim_credit_remainder_usd() is not cheap to call for the
        # common no-op case.
        # The remainder cap above (reprice_total = min(frozen_total,
        # reserved_usd + surviving_remainder)) already bounds this
        # reprice by what the claim genuinely owns, so `repriced` --
        # sized off that capped total -- can never ask for anyone
        # else's money: a reprice that wanted to draw MORE than this
        # claim owns (A leaves a $5 remainder, B spends it, C
        # contributes $2, and reclaiming A upward would spend C's $2
        # off the UNCAPPED basis) instead produces a smaller,
        # genuinely-owned order once capped (Codex review: "Cap
        # repricing by the claim's surviving remainder"). When that
        # capped total is not even enough for one share
        # (repriced.shares_to_buy == 0), the whole reservation this
        # claim still owns is released back rather than left
        # deferred and stranded: an earlier version of this branch
        # deferred instead, leaving the reservation neither placed
        # nor released until the price happened to fall back to what
        # the (now permanently smaller, after B's withdrawal) capped
        # total could afford -- money the claim could otherwise have
        # redispatched immediately (Codex review: "Release the
        # surviving reservation after a zero-share reprice"). Only a
        # genuine POOL-level liquidity shortfall -- the pool itself
        # cannot currently cover even this already-capped reprice --
        # still defers; that is not an ownership question and cannot
        # be resolved by capping the claim's own share any further.
        overdraws_pool = state["pending_usd"] + pending_delta < 0
        if overdraws_pool:
            return WholeShareAccumulationDecision(
                pending_before_usd=cached["pending_before_usd"],
                contribution_usd=0.0,
                current_share_price_usd=cached["current_share_price_usd"],
                shares_to_buy=0,
                order_notional_usd=0.0,
                pending_after_usd=state["pending_usd"],
            )
        # Read above, before the reprice was sized -- see there.
        prior_remainder_aged = surviving_remainder
        cached["claimed_at"] = recorded_at
        cached["claim_token"] = uuid4().hex
        # Same reasoning as the redispatch branch above: a fresh
        # claim_token is a genuinely NEW attempt, so any
        # shares_credited marker left by a fenced-out caller's own
        # earlier genuine placement against this SAME dedup_key
        # (settle() still counts that caller's real shares even
        # when it cannot mark the entry "resolved" -- see settle()'s
        # docstring) must not suppress crediting THIS reclaim's own,
        # different physical purchase once it settles.
        cached["shares_credited"] = False
        cached["current_share_price_usd"] = repriced.current_share_price_usd
        cached["shares_to_buy"] = repriced.shares_to_buy
        cached["order_notional_usd"] = repriced.order_notional_usd
        cached["pending_after_usd"] = repriced.pending_after_usd
        # Only the INCREASE counts: a downward reprice hands money
        # back to the pool, it does not draw on it. Taken from
        # pending_delta, computed ABOVE against the pre-reprice
        # notional -- cached["order_notional_usd"] has already been
        # overwritten by this point, so subtracting it here would
        # always yield exactly zero and the counter would miss every
        # upward reprice (Codex review: "Record the pre-assignment
        # reprice withdrawal delta").
        _record_withdrawal(state, -pending_delta)
        # Carried forward from the AGED previous remainder, not rebuilt
        # from the frozen sizing basis alone. repriced.pending_after_usd
        # is recomputed from pending_before_usd/contribution_usd as they
        # were when this claim was first recorded, so it silently
        # resurrects entitlement that another claim has since withdrawn
        # -- and re-snapshotting at the current counter then makes that
        # intervening withdrawal look OLDER than the remainder, so the
        # preserve fold can hand this claim credit that replenished the
        # pool afterwards (Codex review discussion_r3867623115, "Retain
        # remainder aging when reclaiming a claim").
        #
        # ``prior_remainder_aged + pending_delta`` is the claim's own
        # entitlement moved by this reprice: a reprice UPWARD releases
        # its own reservation back (positive delta, entitlement grows by
        # exactly what it released), a reprice DOWNWARD draws more out
        # (negative delta). The basis-level figure stays an upper bound,
        # so the two agree exactly when nothing interfered and the aged
        # one wins when something did -- under-crediting costs the claim
        # a later retry, over-crediting spends money that was never its.
        #
        # An entry with no remainder field at all predates the field;
        # aging from a phantom 0.0 would write off credit it never had
        # recorded, so it keeps the basis-level figure it always used.
        if had_prior_remainder:
            reclaimed_remainder = round(
                max(
                    0.0,
                    min(repriced.pending_after_usd, prior_remainder_aged + pending_delta),
                ),
                2,
            )
        else:
            reclaimed_remainder = repriced.pending_after_usd
        # Captured BEFORE _CLAIM_REMAINDER_FIELD is overwritten below:
        # when this reclaim's own withdrawal delta is zero (a
        # same-price or release-only reprice -- _record_withdrawal()
        # above no-ops on that), the withdrawal counter has not
        # moved, so this is NOT a fresh batch: the claim's PRIOR
        # remainder registration is still live in the CURRENT batch.
        # Re-allocating the new reclaimed_remainder on top of it
        # unconditionally would count the unchanged (or barely
        # changed) portion of this claim's own money a second time,
        # inflating the shared batch and letting a DIFFERENT claim
        # reclaiming/redispatching at this same instant be charged
        # for a withdrawal this claim's own money already covers
        # (Codex review: "Do not re-register an unchanged remainder
        # on reclaim"). As with preserve_pending()'s identical fix,
        # simply reusing the old offset unchanged is not safe either
        # -- another claim can have registered into this same batch
        # instant since -- so the old registration is undone via
        # _retire_credit_tie_interval() first (which also shifts any
        # OTHER claim's already-registered offset that landed after
        # this one, rather than blindly shrinking the width -- see
        # that helper's docstring, Codex review: "Keep later tie
        # offsets valid when moving a remainder"), then
        # reclaimed_remainder is allocated fresh, landing this claim
        # at wherever the batch currently stands.
        recorded_keys = state.setdefault("recorded_idempotency_keys", {})
        prior_remainder_as_of = _finite_non_negative(cached.get(_CLAIM_REMAINDER_AS_OF_FIELD))
        if prior_remainder_as_of is not None and prior_remainder_as_of == float(state[_WITHDRAWN_TOTAL_FIELD]):
            prior_remainder_registered = _finite_non_negative(cached.get(_CLAIM_REMAINDER_FIELD)) or 0.0
            prior_remainder_offset = _finite_non_negative(cached.get(_CLAIM_REMAINDER_TIE_OFFSET_FIELD)) or 0.0
            _retire_credit_tie_interval(
                state, recorded_keys, prior_remainder_as_of, prior_remainder_offset, prior_remainder_registered
            )
        cached[_CLAIM_REMAINDER_FIELD] = reclaimed_remainder
        # Tie offset via _allocate_credit_tie_offset() (Codex review:
        # "Avoid charging one withdrawal to every restored claim") --
        # naturally $0 in the ordinary case, since this reprice's own
        # withdrawal delta (recorded just above) already moved the
        # counter and starts a fresh batch, but still protects the
        # narrower case of a DIFFERENT claim reclaiming at the exact
        # same counter value with no real draw of its own in between.
        #
        # Allocated exactly ONCE here, against reclaimed_remainder --
        # NOT allocated again below when this same figure is folded
        # into restored_credit_usd. Calling _allocate_credit_tie_offset
        # a second time for the SAME dollars would widen the shared
        # batch by reclaimed_remainder twice for money that only
        # entered the pool once, handing the restored-credit
        # registration an offset inflated by its own amount --
        # protecting it from aging it should not be exempt from, so a
        # later claim reclaiming/redispatching credit at this exact
        # counter value could still be charged for a withdrawal this
        # claim's own restored money already covered (Codex review:
        # "Avoid registering the reclaimed credit twice"). Reused
        # verbatim below instead: relabeling this claim's remainder as
        # restored credit does not change WHERE its interval sits on
        # the withdrawal axis, so the SAME offset value describes both.
        claim_remainder_tie_offset = _allocate_credit_tie_offset(
            state, reclaimed_remainder, dedup_key=dedup_key, field=_CLAIM_REMAINDER_FIELD
        )
        cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = claim_remainder_tie_offset
        cached[_CLAIM_REMAINDER_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
        # A reprice that lands on 0 shares (the price rose enough that
        # the original committed total no longer buys even one share)
        # has nothing left for any caller to place or settle -- the
        # whole committed amount was just folded back into
        # state["pending_usd"] above as ordinary accumulated balance.
        # Mark it resolved here, mirroring the ORIGINAL decision
        # path's own "resolved": shares_to_buy == 0 rule just below,
        # so a LATER stale-claim check on this same key returns the
        # already-resolved branch (a zero-shares decision reflecting
        # the CURRENT balance, no delta) instead of reclaiming again.
        # Left unresolved, a second reclaim would recompute
        # apply_contribution from the SAME frozen
        # pending_before_usd/contribution_usd, treating dollars this
        # reclaim already restored as still owed -- if a different key
        # had meanwhile spent that restored balance down, the second
        # reclaim's delta could drive state["pending_usd"] negative
        # (Codex review: "Prevent old claims from re-spending
        # consumed credit"). A positive shares_to_buy, in contrast,
        # is a genuine actionable fire that still needs a caller to
        # place and settle/preserve it, so it stays unresolved.
        cached["resolved"] = repriced.shares_to_buy == 0
        if cached["resolved"]:
            # The reprice bought nothing, so the claim's whole
            # entitlement went back into state["pending_usd"] above and
            # nothing of it is outstanding -- structurally the same
            # position preserve_pending() leaves a claim in, and it has
            # to record the same thing.
            #
            # Without this the entry resolves having never written
            # restored_credit_usd, so a later zero-contribution
            # redispatch falls through _redispatchable_credit_usd()'s
            # missing-field branch to order_notional_usd -- which this
            # very reprice just overwrote with 0. The cap reads $0 and
            # the released credit is stranded permanently, even though
            # the ledger still holds it (Codex review
            # discussion_r3867623112, "Preserve credit when a reclaim
            # reprices to zero shares").
            #
            # Recorded as the aged entitlement computed above, not the
            # raw basis-level total, so a claim cannot recover credit
            # another claim withdrew while this fire sat unresolved.
            # The remainder is then zeroed, exactly as both preserve
            # paths do: it has been folded into the restored credit and
            # would otherwise be counted a second time.
            cached[_RESTORED_CREDIT_FIELD] = reclaimed_remainder
            # Reuses claim_remainder_tie_offset rather than calling
            # _allocate_credit_tie_offset() again -- see that call
            # site's own comment above for why a second allocation of
            # the SAME reclaimed_remainder would double-widen the
            # shared batch for money that only entered the pool once.
            cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = claim_remainder_tie_offset
            cached[_RESTORED_CREDIT_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
            # The offset value above was only ever audited under
            # _CLAIM_REMAINDER_FIELD's own audit key (from the
            # _allocate_credit_tie_offset() call above) -- relabeling
            # it onto _RESTORED_CREDIT_FIELD here needs its OWN audit
            # entry too, or _read_state() finds no audit record for
            # (claim, restored_credit_usd, this snapshot) and trusts
            # ANY value stored there unconditionally, including a
            # later hand-edited one (Codex review: "Record an audit
            # for relabeled restored credits"). Written directly
            # (not through _allocate_credit_tie_offset(), which would
            # double-widen the batch per the comment above) since the
            # interval's position on the withdrawal axis is unchanged
            # -- only which field describes it is.
            _restored_audit = state.setdefault(_CREDIT_TIE_OFFSET_AUDIT_FIELD, {})
            if not isinstance(_restored_audit, dict):
                _restored_audit = {}
                state[_CREDIT_TIE_OFFSET_AUDIT_FIELD] = _restored_audit
            _restored_audit[
                _tie_offset_audit_key(dedup_key, _RESTORED_CREDIT_FIELD, state[_WITHDRAWN_TOTAL_FIELD])
            ] = claim_remainder_tie_offset
            # The remainder's interval was just relabeled onto
            # restored_credit_usd above (audited under ITS key) --
            # the remainder field is reset to $0 at this SAME
            # snapshot, so its own now-superseded audit entry (still
            # holding claim_remainder_tie_offset) must be forgotten,
            # or it mismatches this reset the instant this loads
            # again (Codex review: "Retire the remainder audit when
            # relabeling credit").
            _forget_tie_offset_audit(
                state, dedup_key, _CLAIM_REMAINDER_FIELD, state[_WITHDRAWN_TOTAL_FIELD]
            )
            cached[_CLAIM_REMAINDER_FIELD] = 0.0
            cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = 0.0
            cached[_CLAIM_REMAINDER_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
        recorded_keys[dedup_key] = cached
        state["pending_usd"] = round(state["pending_usd"] + pending_delta, 2)
        # A reclaim mutates the cached quantity/notional/resolution
        # state and the global pending balance exactly like a fresh
        # contribution does, but -- unlike the fresh-contribution
        # branch below, which always appends to state["events"] --
        # this branch used to persist silently. events() and the
        # on-disk audit trail then retained only the ORIGINAL stale
        # fire, with nothing explaining the later balance adjustment
        # or repriced decision, making operator reconciliation of
        # the recovery path incorrect (Codex review: "Record
        # stale-claim repricing in the ledger event history").
        state["events"].append(
            {
                "trading_day": trading_day,
                "stream": stream,
                "recorded_at": recorded_at,
                "pending_before_usd": cached["pending_before_usd"],
                "contribution_usd": cached["contribution_usd"],
                "current_share_price_usd": repriced.current_share_price_usd,
                "shares_to_buy": repriced.shares_to_buy,
                "order_notional_usd": repriced.order_notional_usd,
                "pending_after_usd": state["pending_usd"],
                "decision": "reclaim_reprice",
            }
        )
        atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return WholeShareAccumulationDecision(
            pending_before_usd=cached["pending_before_usd"],
            contribution_usd=cached["contribution_usd"],
            current_share_price_usd=repriced.current_share_price_usd,
            shares_to_buy=repriced.shares_to_buy,
            order_notional_usd=repriced.order_notional_usd,
            # The RECONCILED state["pending_usd"] just persisted above,
            # not the isolated repriced.pending_after_usd -- when a
            # DIFFERENT idempotency key changed the balance since this
            # claim was recorded, those two differ by exactly
            # pending_delta (Codex review: "Return the reconciled
            # pending balance after a stale reprice"). A caller (see
            # _process_whole_share_candidate) persists this field as
            # whole_share_pending_usd when the reprice yields 0
            # shares, so returning the pre-reconciliation value would
            # report a stale balance to run artifacts even though the
            # ledger itself now holds the correct, reconciled one.
            pending_after_usd=state["pending_usd"],
            claim_token=cached["claim_token"],
        )

    def _record_fresh_contribution(
        self,
        state: Dict[str, Any],
        day_dir: Path,
        state_path: Path,
        dedup_key: Optional[str],
        trading_day: str,
        contribution_usd: float,
        current_share_price_usd: float,
        stream: str,
        recorded_at: Optional[str],
    ) -> WholeShareAccumulationDecision:
        pending = state["pending_usd"]
        decision = apply_contribution(pending, contribution_usd, current_share_price_usd)
        state["pending_usd"] = decision.pending_after_usd
        _record_withdrawal(state, decision.order_notional_usd)
        if dedup_key is not None:
            claim_token = uuid4().hex if decision.shares_to_buy > 0 else None
            # This claim's OWN newly-owned credit. A claim that FIRED
            # (shares_to_buy > 0) drew on the whole pool "by right" --
            # the same convention _claim_sizing_basis_usd() documents --
            # and its own withdrawal just moved the counter, so it always
            # starts a brand new tie batch with no sibling to share it
            # with; decision.pending_after_usd (the whole resulting pool)
            # is genuinely its own remainder in that case, unchanged from
            # before this fix. A claim that did NOT fire (still pending,
            # shares_to_buy == 0), though, can be tied at the exact same
            # withdrawal-counter instant as ANOTHER un-fired claim, and
            # decision.pending_after_usd there mixes in every EARLIER
            # tied claim's own still-pending money too: two $10 sub-share
            # contributions tied at the same instant would otherwise
            # record batch widths of $10 then $20 for a pool that only
            # ever holds $20, and the SAME cumulative figure written into
            # the remainder field below would let the second claim
            # believe it still owns $20 of it. A withdrawal that later
            # drains the whole $20 pool, followed by an unrelated $10
            # replenishment, then reports the second claim as still
            # owning $10 of the REPLENISHING claim's money instead of the
            # $0 its own $10 share actually has left (Codex review:
            # "Track only newly owned credit in the tie batch"). Using
            # just this claim's own contribution there instead (its
            # order_notional_usd is always exactly $0 when nothing
            # fired) keeps the batch's total width equal to the pool's
            # real total and gives every tied claim only its own
            # non-overlapping slice of the withdrawal axis.
            own_added_usd = (
                decision.pending_after_usd
                if decision.shares_to_buy > 0
                else decision.contribution_usd
            )
            # Snapshotted AFTER this fire's own withdrawal above. The tie
            # offset (see _allocate_credit_tie_offset()) protects a
            # zero-order sub-share contribution here from a DIFFERENT
            # claim's own zero-order contribution landing at this exact
            # same instant (Codex review: "Avoid charging one withdrawal
            # to every restored claim").
            remainder_tie_offset = _allocate_credit_tie_offset(
                state, own_added_usd, dedup_key=dedup_key, field=_CLAIM_REMAINDER_FIELD
            )
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            recorded_keys[dedup_key] = {
                "pending_before_usd": decision.pending_before_usd,
                "contribution_usd": decision.contribution_usd,
                "current_share_price_usd": decision.current_share_price_usd,
                "shares_to_buy": decision.shares_to_buy,
                "order_notional_usd": decision.order_notional_usd,
                "pending_after_usd": decision.pending_after_usd,
                # This claim's own non-overlapping credit -- see
                # own_added_usd's own comment above for why this is not
                # decision.pending_after_usd -- folded back into
                # restored_credit_usd if the order is ever preserved, so
                # the claim keeps reach over its own change and nothing
                # more.
                _CLAIM_REMAINDER_FIELD: own_added_usd,
                _CLAIM_REMAINDER_AS_OF_FIELD: state[_WITHDRAWN_TOTAL_FIELD],
                _CLAIM_REMAINDER_TIE_OFFSET_FIELD: remainder_tie_offset,
                # A fresh fire draws on the whole pool by right, so its
                # reported balance and its sizing basis are the same
                # figure. Only a CAPPED redispatch makes the two diverge.
                _CLAIM_SIZING_BASIS_FIELD: decision.pending_before_usd,
                # A decision with 0 shares never needs settle()/
                # preserve_pending() to resolve it -- nothing was
                # deducted -- so it starts already resolved; only a real
                # fire (shares_to_buy > 0) needs a later settle/preserve
                # call to flip this.
                "resolved": decision.shares_to_buy == 0,
                # Only meaningful while unresolved --
                # when a real fire happens, this reserves the key against
                # a second overlapping caller until settle()/preserve_pending()
                # resolves it or a retry takes ownership.
                "claimed_at": recorded_at,
                "claim_token": claim_token,
            }
            if claim_token is not None:
                decision = replace(decision, claim_token=claim_token)
        state["events"].append(
            {
                "trading_day": trading_day,
                "stream": stream,
                "recorded_at": recorded_at,
                "pending_before_usd": decision.pending_before_usd,
                "contribution_usd": decision.contribution_usd,
                "current_share_price_usd": decision.current_share_price_usd,
                "shares_to_buy": decision.shares_to_buy,
                "order_notional_usd": decision.order_notional_usd,
                "pending_after_usd": decision.pending_after_usd,
                "decision": "fire" if decision.shares_to_buy > 0 else "pending",
            }
        )
        day_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return decision

    def record_contribution(
        self,
        ticker: str,
        trading_day: str,
        contribution_usd: float,
        current_share_price_usd: float,
        *,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> WholeShareAccumulationDecision:
        """Apply a contribution and retain a structured event for it.

        Optimistically deducts any newly-qualified whole share(s)' worth from
        the pending balance -- mirroring
        :meth:`dca_cumulative.CumulativeCreditLedger.record`, a caller that
        cannot act on a positive ``shares_to_buy`` this run (order blocked
        before placement) MUST call :meth:`preserve_pending` with the SAME
        ``idempotency_key`` to put the deducted amount back, and a caller
        that successfully places the order MUST call :meth:`settle` (also
        with the same key) to confirm it. Until one of those is called, the
        ledger already reflects the order as taken.

        ``idempotency_key``, when provided, makes a second call for the same
        ``(ticker, idempotency_key)`` pair a no-op -- exactly like
        :meth:`dca_cumulative.CumulativeCreditLedger.record`'s own dedup
        contract, a scheduler retry of the same slot must never apply the
        same day's contribution twice. If that cached fire has already been
        resolved (via :meth:`settle` or :meth:`preserve_pending`), this
        returns a zero-shares decision reflecting the CURRENT balance
        instead of re-suggesting the original (already-handled) fire.

        An unresolved cached fire is reclaimed immediately by a retry.
        Broker placement must go through :meth:`execute_claimed`, which
        prevents reclaim while the irreversible request is in flight.
        """
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            raise ValueError("ticker is required for a whole-share contribution")
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"{trading_day}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            aging_initialized = False
            migration_stagger: Dict[float, float] = {}
            for entry in recorded_keys.values():
                if isinstance(entry, dict):
                    aging_initialized |= _initialize_restored_credit_aging(entry, state, migration_stagger)
            if aging_initialized:
                atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
            if dedup_key is not None and dedup_key in recorded_keys:
                cached = recorded_keys[dedup_key]
                if cached.get("resolved") or cached.get("placed") or cached.get("placing"):
                    return self._redispatch_resolved_or_placed_claim(
                        state,
                        day_dir,
                        state_path,
                        dedup_key,
                        cached,
                        trading_day,
                        contribution_usd,
                        current_share_price_usd,
                        stream,
                        recorded_at,
                    )
                return self._reprice_unresolved_claim(
                    state,
                    state_path,
                    dedup_key,
                    cached,
                    trading_day,
                    current_share_price_usd,
                    stream,
                    recorded_at,
                )
            return self._record_fresh_contribution(
                state,
                day_dir,
                state_path,
                dedup_key,
                trading_day,
                contribution_usd,
                current_share_price_usd,
                stream,
                recorded_at,
            )

    def preserve_pending(
        self,
        ticker: str,
        trading_day: str,
        deducted_usd: float,
        *,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        claim_token: Optional[str] = None,
        on_release: Optional[Callable[[], None]] = None,
    ) -> float:
        """Restore a failed fire's deducted amount to the CURRENT balance.

        ``on_release``, when given, is invoked AT MOST ONCE per call, and
        only for the call that actually resolves a claimed cache entry --
        i.e. exactly the case where ``cached is not None`` below and this
        call's ownership check passes, so ``cached["resolved"]`` is about to
        flip to ``True``. It fires STRICTLY BEFORE this method's own
        ``atomic_write_text`` below, while the per-ticker ``cross_process_
        lock`` is still held, so the caller can make its own durable
        evidence write (e.g. a recovery/reversal artifact) land before this
        release becomes irreversible on disk -- mirroring ``settle()``'s own
        ``on_claim_won`` hook and the identical caller-side need documented
        on ``near_close_dca._reconcile_unresolved_whole_share_claim``
        (``before_release``/``before_settle``): without ordering the write
        before the commit, a caller that persists evidence only AFTER this
        call returns can crash in between, leaving no durable evidence AND
        no unresolved claim left to retry from (Codex review: "Keep
        zero-fill claims retryable until reversal persists"). An unclaimed
        call (no ``idempotency_key``/``recorded_at``, no cached entry, an
        already-resolved entry, or a caller that lost the claim per
        ``_claim_matches``) never invokes ``on_release`` -- there is no
        winning resolution here for it to guard. A raised exception from
        ``on_release`` is DELIBERATELY left uncaught: it propagates straight
        out of this method before ``atomic_write_text`` runs, so nothing
        (the balance restoration, the cached-entry resolution, the audit
        event) is committed, and the claim remains exactly as unresolved as
        it was before this call -- the caller's own best-effort ``except
        Exception`` boundaries (see the ``on_release`` call sites in
        ``near_close_dca.py``) are what turn that into a safe no-op retry
        candidate, not this method.

        ``deducted_usd`` is the amount THIS fire took out of the pool --
        normally the fire decision's own ``order_notional_usd`` -- ADDED to
        whatever the ledger's balance is right now, read fresh under this
        call's own lock. This is deliberately NOT "overwrite the balance
        with an absolute recomputed total": a concurrent stream may have
        recorded its own contribution between this fire's
        :meth:`record_contribution` call and this restoration, and
        overwriting would silently discard it (Codex review: "Preserve
        concurrent contributions instead of overwriting them" -- a fire
        that deducted $10 from an $8+$5=$13 total leaving $3 pending, with a
        concurrent +$4 landing before this restoration (balance now $7),
        must restore to $7 + $10 = $17, not overwrite to the original $13).

        ``idempotency_key``, when it matches the key :meth:`record_contribution`
        used for this fire, marks that cached entry resolved so a later
        retry of the same slot returns a zero-shares decision instead of
        re-suggesting the same fire.

        When ``idempotency_key``/``recorded_at`` are given and the cache
        entry is UNRESOLVED, this verifies the caller still holds the claim
        (``cached["claimed_at"] == recorded_at``) before crediting anything
        back -- a caller fenced out by a stale-claim reclaim (see
        :meth:`still_claimed`) must not be able to restore a deduction that
        now belongs to a different, still-viable fire attempt; doing so
        would double-credit the pool once that attempt settles or is itself
        preserved (Codex review: "Reject resolutions from callers that lost
        the claim"). An ALREADY-resolved entry (settled or preserved by
        someone else) is likewise a no-op here, for the same reason. Both
        cases return the CURRENT balance, unchanged, rather than raising --
        a caller that lost a race should fail safe/silent here, not crash.
        """
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            raise ValueError("ticker is required for a whole-share contribution")
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"{trading_day}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            cached = recorded_keys.get(dedup_key) if dedup_key is not None else None
            if cached is not None and (
                cached.get("resolved")
                or cached.get("placed")
                or not self._claim_matches(cached, claim_token=claim_token, recorded_at=recorded_at)
            ):
                # Already resolved by someone else, this caller no longer
                # holds the claim, or -- the case this ``placed`` check
                # closes -- a genuine order was accepted by
                # execute_claimed() for THIS SAME claim_token/claimed_at
                # after the caller's own broker-history evidence was
                # gathered but before this call acquired the lock. The
                # stale-claim recovery path
                # (near_close_dca._reconcile_unresolved_whole_share_claim)
                # decides "no live order found, safe to restore" from a
                # broker lookup that ran BEFORE this call, not under this
                # lock; execute_claimed() shares this exact per-ticker
                # lock and can complete a real placement -- including its
                # own durable ``placed`` write -- entirely in the gap
                # between that lookup and this call acquiring the lock.
                # Re-reading ``cached`` here (this method already reads
                # ``state`` fresh under the lock every call) catches that:
                # a caller whose claim_token/claimed_at still match can
                # nonetheless have had its claim resolved out from under
                # it by a placement that landed in that gap. Restoring
                # the deduction and clearing ``placed`` here (the prior
                # round's zero-fill fix) would erase the now-durable
                # marker for a real order and let a same-day retry
                # redispatch the same money a second time (Codex review
                # discussion_r3821705222, "Preserve a placement that
                # races stale-claim restoration"). Deliberately does NOT
                # also check ``placing``: an in-line caller reacting to
                # its OWN execute_claimed() call raising (e.g.
                # OrderConfirmationRequiredError, or any other placement
                # exception) reaches this method with ``placing`` still
                # ``True`` ON DISK -- execute_claimed() sets it durably
                # BEFORE calling the broker and only clears it after a
                # confirmed outcome, so a raised exception leaves it set
                # -- and that caller is the correct, sole owner clearing
                # a known-failed attempt, not a racing observer; gating on
                # ``placing`` here would wrongly refuse that legitimate
                # restoration. ``placed`` alone is sufficient: because
                # this call and execute_claimed() share the SAME
                # cross_process_lock spanning the whole broker call
                # (Codex review discussion_r3821705226 confirms this),
                # the two can never interleave mid-flight -- by the time
                # this call acquires the lock, any execute_claimed() that
                # started is either fully done (``placed`` durably True)
                # or crashed with ``placing`` still True and the lock
                # released, and that crash case is exactly what the
                # broker-history lookup and staleness wait upstream in
                # ``_reconcile_unresolved_whole_share_claim`` already
                # exist to resolve safely.
                return state["pending_usd"]
            restored_amount = float(_money(deducted_usd, field="deducted_usd"))
            pending_before = state["pending_usd"]
            withdrawn_total = float(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0))
            pending_after = round(pending_before + restored_amount, 2)
            state["pending_usd"] = pending_after
            if cached is not None and dedup_key is not None:
                cached["resolved"] = True
                # The claim's entitlement in the pool is what this restoration
                # puts back PLUS whatever of its own credit the failed order
                # never spent -- see _claim_credit_remainder_usd(). Recording
                # restored_amount alone strands that change (Codex review:
                # "Preserve the claim-owned remainder across failed
                # redispatches"). Zeroed after folding it in: the claim now has
                # nothing outstanding, and a stale remainder would otherwise be
                # counted twice by a later restoration.
                # Captured BEFORE the remainder fields below are zeroed: if
                # this claim's own remainder was snapshotted at the SAME
                # withdrawal-counter instant we're still at (no real
                # withdrawal happened in between -- preserve/release never
                # calls _record_withdrawal()), that remainder's own dollar
                # figure already widened the CURRENT tie batch once, when it
                # was recorded. Registering the WHOLE folded
                # (remainder + restored_amount) total again here would count
                # those same dollars a second time, inflating the batch and
                # handing this claim's own restored credit an offset padded
                # by its own money -- so a DIFFERENT claim reclaiming/
                # redispatching at this same instant could still be charged
                # for a withdrawal this claim's restored credit already
                # covers (Codex review: "Avoid registering the preserved
                # remainder twice"). The fix is NOT to simply reuse the
                # remainder's own old offset unchanged, though: OTHER claims
                # can have registered into this exact batch instant between
                # this claim's own remainder registration and now (e.g. a
                # batch of same-second fires each preserved back to back),
                # and reusing a stale offset would let this claim's now-
                # larger interval silently overlap theirs. Instead, undo
                # exactly the width this claim's own remainder previously
                # contributed, then re-allocate fresh for the full folded
                # total -- landing this claim at whatever position the batch
                # is CURRENTLY at (correctly ahead of every other claim
                # registered since), while still adding only the genuinely
                # new restored_amount dollars net (the same figure the old
                # remainder contribution is first removed and re-added).
                # "Removed" is not simply "width shrinks by this amount",
                # though: an OTHER claim that registered its OWN interval
                # after this one's old remainder would have its offset
                # compacted out from under it. _retire_credit_tie_interval()
                # shifts every later offset down by the removed width
                # instead, so it stays valid (Codex review: "Keep later tie
                # offsets valid when moving a remainder").
                prior_remainder_as_of = _finite_non_negative(cached.get(_CLAIM_REMAINDER_AS_OF_FIELD))
                if prior_remainder_as_of is not None and prior_remainder_as_of == withdrawn_total:
                    prior_remainder_registered = _finite_non_negative(cached.get(_CLAIM_REMAINDER_FIELD)) or 0.0
                    prior_remainder_offset = _finite_non_negative(cached.get(_CLAIM_REMAINDER_TIE_OFFSET_FIELD)) or 0.0
                    _retire_credit_tie_interval(
                        state, recorded_keys, prior_remainder_as_of, prior_remainder_offset, prior_remainder_registered
                    )
                cached["restored_credit_usd"] = round(
                    _claim_credit_remainder_usd(cached, pending_before, withdrawn_total) + restored_amount, 2
                )
                # Snapshotted with it, so a later redispatch ages this cap by
                # whatever the pool is drawn by in the meantime rather than
                # trusting a balance that unrelated contributions may have
                # refilled -- see _redispatchable_credit_usd(). Releases never
                # call _record_withdrawal(), so two DIFFERENT claims preserved
                # back to back share the exact same withdrawal-counter
                # snapshot -- the tie offset from _allocate_credit_tie_offset()
                # is what tells them apart, rather than charging a later
                # withdrawal to both independently (Codex review: "Avoid
                # charging one withdrawal to every restored claim").
                cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = _allocate_credit_tie_offset(
                    state, cached["restored_credit_usd"], dedup_key=dedup_key, field=_RESTORED_CREDIT_FIELD
                )
                cached[_RESTORED_CREDIT_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
                # Snapshot reset with it: the claim now has nothing
                # outstanding, so a later restoration cannot re-apply a
                # withdrawal delta measured against the old snapshot. The
                # remainder's own now-superseded audit entry (under its OLD
                # as_of -- prior_remainder_as_of, computed above) is
                # forgotten first: when that old as_of equals the CURRENT
                # counter (the _retire_credit_tie_interval() branch just
                # above), this reset would otherwise collide with it,
                # leaving a stale nonzero audit value that mismatches the
                # fresh $0 the very next load (Codex review: "Retire the
                # remainder audit when relabeling credit"). A no-op when
                # prior_remainder_as_of is None (no prior remainder field at
                # all) or the audit was never populated for it.
                if prior_remainder_as_of is not None:
                    _forget_tie_offset_audit(state, dedup_key, _CLAIM_REMAINDER_FIELD, prior_remainder_as_of)
                cached[_CLAIM_REMAINDER_FIELD] = 0.0
                cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = 0.0
                cached[_CLAIM_REMAINDER_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
                # Clear both placement markers when THIS call is the one
                # resolving the claim. "placing" is safe to clear
                # unconditionally here -- reaching this point at all means
                # this restoration is confirmed-terminal (either no
                # placement was ever attempted, or a broker lookup already
                # confirmed no live order remains), never a "we don't know
                # the outcome" crash-recovery guess, so there is nothing left
                # to protect by leaving it set. "placed" is the one that
                # matters for redispatch: a caller that reaches
                # preserve_pending()/preserve_pending_if_claimed() for a
                # confirmed terminal ZERO-fill (a clean rejection/
                # cancellation, or an order that filled for nothing) has no
                # live order left to guard against a duplicate submission of
                # -- leaving "placed" set here would keep suppressing the
                # zero-contribution redispatch path in record_contribution()
                # even though the restored balance may now be enough to buy
                # a share and there is no live order left to protect (Codex
                # review discussion_r3818217243, "Clear placed after
                # releasing a terminal zero-fill order"). A caller settling
                # a REAL fill instead goes through settle(), which does NOT
                # clear "placed" -- that marker is accurate terminal
                # placement history there, not a stale guard blocking
                # redispatch of dead money.
                cached["placed"] = False
                cached["placing"] = False
                # This call is the one resolving the claim -- fire the
                # caller's hook now, still inside the lock and strictly
                # before the atomic_write_text below, so its own durable
                # write (if any) is guaranteed to land before this release
                # becomes irreversible. Left uncaught on purpose: a raise
                # here aborts this method before anything is committed, so
                # the claim stays exactly as unresolved as it was.
                if on_release is not None:
                    on_release()
            state["events"].append(
                {
                    "trading_day": trading_day,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "pending_before_usd": pending_before,
                    "deducted_usd": restored_amount,
                    "pending_after_usd": pending_after,
                    "decision": "preserved_pending",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return pending_after

    def preserve_pending_if_claimed(
        self,
        ticker: str,
        trading_day: str,
        deducted_usd: float,
        *,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        claim_token: Optional[str] = None,
        expected_placed: bool,
        expected_placing: bool,
        on_release: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Atomically verify ownership and restore a CONFIRMED broker
        attempt's deduction in ONE locked operation, returning whether THIS
        call is the one that resolved the claim.

        ``on_release``, when given, is invoked EXACTLY ONCE, and only when
        this call is the one returning ``True`` -- i.e. this call is the one
        that actually won ownership and performed the release, the SAME
        condition that gates this method's own return value. It fires
        STRICTLY BEFORE the ``atomic_write_text`` below, while the
        per-ticker ``cross_process_lock`` is still held, so a caller that
        needs its own durable evidence (e.g. a recovery/reversal artifact
        recording a confirmed zero-fill order) to land before this release
        becomes irreversible can do so from inside the hook -- identical in
        spirit to ``settle()``'s ``on_claim_won`` and to plain
        ``preserve_pending()``'s own ``on_release`` above, just gated by
        this method's atomic ownership+placement-marker check instead of
        the simpler ``cached is not None`` one. See
        ``near_close_dca._reconcile_unresolved_whole_share_claim``'s
        ``before_release`` docstring for the full caller-side motivation
        (Codex review: "Keep zero-fill claims retryable until reversal
        persists"). Every early ``return False`` below (no cached entry, an
        already-resolved entry, a caller that lost the claim, or a
        placement-marker mismatch indicating a concurrent
        ``execute_claimed()`` raced this restoration) never invokes
        ``on_release`` -- none of those cases is a winning resolution.
        A raised exception from ``on_release`` is DELIBERATELY left
        uncaught: it propagates out of this method before
        ``atomic_write_text`` runs, so the balance restoration, the
        cached-entry resolution, and the audit event are all left
        uncommitted and the claim remains exactly as unresolved as before
        this call -- callers wrap this in their own best-effort ``except
        Exception`` (see the ``on_release`` call sites in
        ``near_close_dca.py``) to turn that into a safe, retryable outcome.

        A separate :meth:`still_claimed` check followed by a separate
        :meth:`preserve_pending` call is NOT atomic -- another attempt can
        reclaim the fire in the gap between the two independently locked
        operations, so ``still_claimed()`` can pass immediately before
        ``preserve_pending()`` silently declines to restore anything
        (Codex review: "Make zero-fill claim release atomic"). A caller
        that needs to know whether its restoration actually won ownership
        before booking a real, broker-CONFIRMED attempt (e.g. a confirmed
        zero-fill/cancelled order) into run-level counters like
        ``max_orders_per_day`` must use this instead of composing the two
        separately -- returning ``False`` unresolved rather than crediting
        or booking anything wrongly attributed to a claim this caller no
        longer owns.

        Otherwise identical to :meth:`preserve_pending`: the SAME
        ownership/staleness rules (:meth:`_claim_matches`), the SAME
        pending-balance restoration math, and the SAME cached-entry
        resolution -- just performed inside a single lock acquisition and
        reporting the resolution outcome instead of the resulting balance.

        ``expected_placed`` and ``expected_placing`` must be the placement
        marker values the caller observed on ITS OWN snapshot of this claim
        (e.g. from :meth:`unresolved_claim`) before it gathered the broker
        evidence that justifies this restoration -- typically BEFORE the
        network round-trip that produced that evidence, and therefore
        potentially stale by the time this call acquires the lock. This
        method's ownership check above intentionally does not reject an
        entry already marked ``placed`` (see the comment just below), since
        ``placed=True`` at snapshot time is the expected, legitimate case
        this method exists to release. But a placement that happens IN THE
        GAP between the caller's snapshot and this call acquiring the lock
        -- a genuinely NEW order, submitted by a concurrent
        ``execute_claimed()`` for this exact still-current claim_token,
        landing after the caller decided "safe to restore" but before that
        decision is applied here -- is a different, live order this call
        has no evidence about, and restoring/clearing over it would make
        that live order's credit spendable again (Codex review
        discussion_r3825818000, "Preserve orders placed during zero-fill
        restoration"). Re-reading ``placed``/``placing`` fresh under this
        lock and comparing against what the caller actually saw at snapshot
        time catches that race: if either is now set when the caller's
        snapshot showed neither, a placement raced this call and it must
        decline, exactly like any other "someone else's claim now" case.
        ``placing`` is checked here (unlike plain ``preserve_pending()``'s
        deliberate omission for discussion_r3821705222) because the two
        methods see a different caller shape: ``preserve_pending()``'s
        omission relies on the SAME caller that set ``placing`` being the
        one reacting to its own failed placement, so a ``placing``-true
        snapshot there is always this caller's own known-failed attempt.
        Here the caller is a DETACHED reconciliation pass, working from a
        broker lookup gathered before it ever saw ``placing`` at all, so a
        ``placing=True`` transition observed only under this lock is
        necessarily someone else's concurrent placement, not this caller's
        own -- and must be treated with at least as much caution as an
        outright ``placed`` transition.
        """
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            raise ValueError("ticker is required for a whole-share contribution")
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"{trading_day}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            cached = recorded_keys.get(dedup_key) if dedup_key is not None else None
            if (
                dedup_key is None
                or cached is None
                or cached.get("resolved")
                or not self._claim_matches(cached, claim_token=claim_token, recorded_at=recorded_at)
            ):
                # No cached entry at all, already resolved by someone else,
                # or this caller no longer holds the claim -- nothing to
                # restore/resolve on this caller's behalf.
                #
                # Deliberately does NOT also reject an entry already marked
                # ``placed`` the way plain ``preserve_pending()`` now does
                # (Codex review discussion_r3821705222, "Preserve a placement
                # that races stale-claim restoration") -- this method exists
                # SPECIFICALLY for the CONFIRMED-terminal-zero-fill case,
                # where ``placed=True`` is the expected, legitimate starting
                # state: a real order genuinely WAS submitted and durably
                # marked ``placed`` by ``execute_claimed()``, and the caller
                # reaching this method holds DIRECT broker evidence (an exact
                # ref_id match on a genuinely terminal, zero-fill order) that
                # THIS SPECIFIC order is done and bought nothing -- not a
                # staleness guess from an absence of evidence. Ownership is
                # still enforced by ``_claim_matches`` above: a caller whose
                # claim_token no longer matches (a newer reclaim already took
                # this dedup_key) is still refused here, exactly as it always
                # was. Plain ``preserve_pending()``'s new ``placed`` guard
                # protects a DIFFERENT call shape -- the stale-claim recovery
                # path's staleness-based release on a clean "no order found
                # anywhere" lookup, which carries no such direct per-order
                # evidence and can race a genuinely still-in-flight
                # ``execute_claimed()`` call for the SAME token.
                return False
            if (
                bool(cached.get("placed")) != expected_placed
                or bool(cached.get("placing")) != expected_placing
            ):
                # The freshly-read-under-lock placement state no longer
                # matches the caller's pre-lookup snapshot. A concurrent
                # execute_claimed() may have placed a genuinely new order
                # for this exact still-current claim in that gap. Declining here
                # (rather than restoring/clearing as below) leaves that live
                # order's ``placed``/``placing`` markers intact, exactly
                # like the ownership/staleness declines above (Codex review
                # discussion_r3825818000, "Preserve orders placed during
                # zero-fill restoration"). The comparison is symmetric
                # (``!=`` on ``placed``, plus a direct ``placing`` check) as
                # cheap defense in depth, but in practice only the "expected
                # False, now True" direction is reachable: nothing else in
                # this class clears ``placed``/``placing`` back to False
                # except this same restoration path itself, which is
                # exclusive per claim_token via the ``resolved`` check
                # above.
                return False
            restored_amount = float(_money(deducted_usd, field="deducted_usd"))
            pending_before = state["pending_usd"]
            withdrawn_total = float(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0))
            pending_after = round(pending_before + restored_amount, 2)
            state["pending_usd"] = pending_after
            cached["resolved"] = True
            # See preserve_pending()'s identical fold for the reasoning, and
            # its "Avoid registering the preserved remainder twice" fix for
            # why the old registration is undone and re-allocated fresh
            # rather than either re-derived from the folded total (double-
            # counts) or reused unchanged (can collide with another claim
            # registered into this same batch instant in between).
            # _retire_credit_tie_interval() shifts any OTHER claim's offset
            # that landed after this one's old registration, rather than
            # blindly shrinking the width -- see that helper's docstring
            # (Codex review: "Keep later tie offsets valid when moving a
            # remainder").
            prior_remainder_as_of = _finite_non_negative(cached.get(_CLAIM_REMAINDER_AS_OF_FIELD))
            if prior_remainder_as_of is not None and prior_remainder_as_of == withdrawn_total:
                prior_remainder_registered = _finite_non_negative(cached.get(_CLAIM_REMAINDER_FIELD)) or 0.0
                prior_remainder_offset = _finite_non_negative(cached.get(_CLAIM_REMAINDER_TIE_OFFSET_FIELD)) or 0.0
                _retire_credit_tie_interval(
                    state, recorded_keys, prior_remainder_as_of, prior_remainder_offset, prior_remainder_registered
                )
            cached["restored_credit_usd"] = round(
                _claim_credit_remainder_usd(cached, pending_before, withdrawn_total) + restored_amount, 2
            )
            # See preserve_pending()'s identical fold: tie offset via
            # _allocate_credit_tie_offset() (Codex review: "Avoid charging one
            # withdrawal to every restored claim").
            cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = _allocate_credit_tie_offset(
                state, cached["restored_credit_usd"], dedup_key=dedup_key, field=_RESTORED_CREDIT_FIELD
            )
            cached[_RESTORED_CREDIT_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
            # See preserve_pending()'s identical fix (Codex review: "Retire
            # the remainder audit when relabeling credit") -- forget the
            # remainder's own now-superseded audit entry before resetting
            # the live field to $0, so a retired-at-this-same-instant
            # registration doesn't collide with a stale nonzero audit value.
            if prior_remainder_as_of is not None:
                _forget_tie_offset_audit(state, dedup_key, _CLAIM_REMAINDER_FIELD, prior_remainder_as_of)
            cached[_CLAIM_REMAINDER_FIELD] = 0.0
            cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = 0.0
            cached[_CLAIM_REMAINDER_AS_OF_FIELD] = state[_WITHDRAWN_TOTAL_FIELD]
            # See preserve_pending()'s identical fix for the full reasoning
            # (Codex review discussion_r3818217243, "Clear placed after
            # releasing a terminal zero-fill order") -- this method exists
            # SPECIFICALLY for the confirmed-terminal-zero-fill reconciliation
            # case, so clearing both markers here is the primary case that
            # fix targets, not just a mirrored precaution.
            cached["placed"] = False
            cached["placing"] = False
            # This call is the one winning ownership and performing the
            # release -- fire the caller's hook now, still inside the lock
            # and strictly before the atomic_write_text below, so its own
            # durable write (if any) is guaranteed to land before this
            # release becomes irreversible. Left uncaught on purpose: a
            # raise here aborts this method before anything is committed,
            # so the claim stays exactly as unresolved as it was, and this
            # method never reaches its own ``return True`` below.
            if on_release is not None:
                on_release()
            state["events"].append(
                {
                    "trading_day": trading_day,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "pending_before_usd": pending_before,
                    "deducted_usd": restored_amount,
                    "pending_after_usd": pending_after,
                    "decision": "preserved_pending",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return True

    def unresolved_claim(
        self,
        ticker: str,
        trading_day: str,
        idempotency_key: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Return a COPY of the raw cached entry for this slot if one exists
        and is UNRESOLVED (recorded but not yet settled or preserved), else
        ``None``. Purely read-only -- makes no ledger writes.

        Lets a caller that reaches a broad, ref-id-agnostic broker check
        (e.g. ``same_day_strategy_order_exists``, which only proves SOME
        same-day order exists for the ticker, not which one) recognize when
        an EARLIER attempt this same day left this exact idempotency slot
        reserved but unresolved -- e.g. a placement that raised and whose
        ref-id reconciliation lookup ALSO raised (Codex review: "Keep
        ambiguous claims reserved when reconciliation fails") leaves the
        claim deliberately untouched rather than guessing. Without a way to
        notice that later, a same-day retry that reaches the broad duplicate
        check above would return early forever, and the claim's reservation
        (including any per-order-ceiling-clamped shortfall still deducted
        from ``pending_usd``) would never be reconciled even once the broker
        confirms the order landed (Codex review: "Reconcile reserved claims
        before the duplicate early return").
        """
        if not idempotency_key:
            return None
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return None
        state_path = self.root / symbol / "ledger.json"
        dedup_key = f"{trading_day}:{idempotency_key}"
        with cross_process_lock(self.root / symbol / "ledger.lock"):
            state = self._read_state(state_path, symbol)
            cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
            if cached is None or cached.get("resolved"):
                return None
            return dict(cached)

    def unresolved_claims_any_day(
        self,
        ticker: str,
        idempotency_key: Optional[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return ``[(trading_day, cached_entry), ...]`` for EVERY unresolved
        claim recorded under this ``idempotency_key``, regardless of which
        trading day each was recorded under, sorted oldest trading day
        first. Empty list if none exist. Purely read-only -- makes no
        ledger writes.

        :meth:`unresolved_claim` only looks at ONE specific trading day --
        exactly the day a same-day retry passes. An ambiguous placement
        whose ref-id reconciliation lookup ALSO failed (see
        :meth:`unresolved_claim`'s own docstring) can remain unresolved
        past midnight; the NEXT trading day's run passes a brand-new
        ``trading_day_iso`` that can never retrieve that prior day's entry,
        and the broad same-day duplicate check a caller might otherwise
        rely on only looks at orders from TODAY onward -- it would never
        even notice a stale claim whose real order landed on an EARLIER
        day. A rejected placement can then leave its full reserved credit
        deducted forever, and an accepted, ceiling-clamped placement can
        strand its unsubmitted shortfall and omit the purchase from the
        audit trail (Codex review: "Reconcile unresolved claims across
        trading days"). Scanning every ``recorded_idempotency_keys`` entry
        for this key lets a caller discover and reconcile such a claim
        against its ORIGINAL trading day/ref_id, whenever it happens to run.

        Returns EVERY match, not just the first: a single ticker can
        accumulate more than one unresolved claim over time (e.g. an older
        placement genuinely rejected, so it never gets an exact ref-id
        match, alongside a LATER ambiguous placement that was actually
        accepted). Returning only the first would let the older,
        irreconcilable entry starve every later one from ever being
        checked -- the accepted claim's purchase would stay absent from
        the audit trail, and any clamped shortfall would stay stranded,
        forever (Codex review: "Reconcile every unresolved claim instead
        of only the first"). The caller is expected to attempt
        reconciliation for each entry independently.
        """
        if not idempotency_key:
            return []
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return []
        state_path = self.root / symbol / "ledger.json"
        suffix = f":{idempotency_key}"
        with cross_process_lock(self.root / symbol / "ledger.lock"):
            state = self._read_state(state_path, symbol)
            matches = [
                (dedup_key[: -len(suffix)], dict(cached))
                for dedup_key, cached in state.get("recorded_idempotency_keys", {}).items()
                if dedup_key.endswith(suffix) and not cached.get("resolved")
            ]
        matches.sort(key=lambda item: item[0])
        return matches

    def still_claimed(
        self,
        ticker: str,
        trading_day: str,
        idempotency_key: Optional[str],
        recorded_at: Optional[str],
        claim_token: Optional[str] = None,
        already_locked: bool = False,
    ) -> bool:
        """Whether ``recorded_at`` is still the CURRENT claim on this fire.

        ``already_locked=True`` skips this call's own ``cross_process_lock``
        acquisition and reads the state directly: a caller that already holds
        this exact ticker's ledger lock (e.g. to atomically re-check ownership
        and then publish a correction under that SAME lock -- see
        ``near_close_dca``'s live-recovery-lock revalidation) would otherwise
        self-deadlock reacquiring a non-reentrant ``flock`` from the SAME
        process (flock's per-open-file-description semantics mean a second
        ``open()`` + lock attempt on the identical file blocks forever, even
        though it is "the same process" -- the exact self-deadlock class this
        codebase hit and fixed once already this same review round, in
        ``morning_dca._invoke``'s own outer-lock removal). Default ``False``
        preserves the original self-locking behavior for every other caller.

        Elapsed time alone does not prove the original claimant exited
        (Codex review: "Fence the previous owner before reclaiming stale
        fires") -- ``record_contribution``'s stale-claim reclaim only
        establishes that a NEW caller is now free to try; it does not stop
        the ORIGINAL caller from later placing an order it no longer
        exclusively owns. A caller MUST call this immediately before the
        one truly irreversible step (the broker placement call) and abstain
        if it returns ``False``: a reclaim in between updated the cache
        entry's ``claimed_at`` to the NEW caller's own ``recorded_at``, so
        the original caller's un-updated ``recorded_at`` no longer matches.

        Returns ``True`` when there is no idempotency-scoped fire to check at
        all (``idempotency_key`` unset, or no cached entry exists yet for
        it) -- those calls were never subject to the claim mechanism to
        begin with. Returns ``False`` when the cached entry is ALREADY
        resolved (settled or preserved) -- by definition of the SAME window
        this method exists to close: a retry reclaiming a stale claim,
        submitting, and settling it while the ORIGINAL caller is paused
        leaves that entry resolved by the time the original reaches this
        check. ``resolved`` alone does not identify the original caller as
        the settler, so treating it as "fine, go ahead" (the old behavior)
        let the original submit the SAME quantity again -- a duplicate
        order past this method's whole purpose. No caller may place once an
        entry is resolved, whoever resolved it (Codex review: "Reject
        resolved fire claims before placement").
        """
        if not idempotency_key:
            return True
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return True
        state_path = self.root / symbol / "ledger.json"
        dedup_key = f"{trading_day}:{idempotency_key}"
        if already_locked:
            state = self._read_state(state_path, symbol)
            cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
        else:
            with cross_process_lock(self.root / symbol / "ledger.lock"):
                state = self._read_state(state_path, symbol)
                cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
        if cached is None:
            return True
        if cached.get("resolved"):
            return False
        return self._claim_matches(cached, claim_token=claim_token, recorded_at=recorded_at)

    @staticmethod
    def _claim_matches(cached: dict[str, Any], *, claim_token: Optional[str], recorded_at: Optional[str]) -> bool:
        """Validate ownership of a cached fire claim.

        A caller that presents a ``claim_token`` must match the stored one
        exactly -- this is the only check immune to two attempts sharing the
        same ``recorded_at`` (Codex review: "A logical run timestamp is not
        an ownership token"). A cached entry that already carries a stored
        ``claim_token`` -- true of every fire recorded since this protocol
        shipped -- REQUIRES the caller to present one too: falling back to
        the pre-token ``claimed_at`` comparison for a tokenless caller would
        let a caller that omits the token match purely on a shared
        ``recorded_at``, silently reopening the exact same-timestamp
        ownership hole this token protocol exists to close, and previously
        left the entry silently unresolved instead (a stale-claim reclaim
        after ``UNRESOLVED_CLAIM_STALE_SECONDS`` could then act on the same
        fire the original, still-live order already settled) (Codex review:
        "Reject missing tokens instead of silently leaving fires
        unresolved"). Every in-repo caller
        (``near_close_dca._process_whole_share_candidate``) always supplies
        the token it was returned, so this is a hard requirement, not a
        best-effort check. Only a cached entry with NO stored token at all
        -- a pre-token ledger entry persisted before this protocol existed
        -- falls back to the legacy ``claimed_at`` comparison.
        """
        stored_token = cached.get("claim_token")
        if stored_token is not None and claim_token is None:
            raise ValueError(
                "claim_token is required to settle/preserve/verify this whole-share fire; "
                "propagate the claim_token returned by record_contribution()"
            )
        if stored_token is not None and claim_token is not None:
            return claim_token == stored_token
        return cached.get("claimed_at") == recorded_at

    def execute_claimed(
        self,
        ticker: str,
        trading_day: str,
        idempotency_key: Optional[str],
        recorded_at: Optional[str],
        operation: Callable[[], T],
        *,
        claim_token: Optional[str] = None,
    ) -> tuple[bool, Optional[T]]:
        """Run an irreversible operation while exclusively owning its claim.

        The per-ticker cross-process lock spans both the final ownership
        check and ``operation``.  A crashed owner releases the OS lock, so a
        scheduled retry can reclaim immediately; a live owner cannot be
        reclaimed while its broker request is in flight.

        The ownership check rejects a claim that is already ``resolved``,
        already ``placed``, or already ``placing`` (see below) -- not just
        ``resolved`` -- and validates ownership with
        :meth:`_claim_matches`, the SAME token-aware predicate
        ``still_claimed``/``preserve_pending``/``settle`` use, rather than a
        bare ``claimed_at`` comparison. A timestamp-only comparison is not
        enough: when overlapping scheduler attempts share the same
        ``recorded_at`` (two attempts racing with an identical
        caller-supplied timestamp), a LATER ``record_contribution()`` call
        for the same dedup_key replaces the stored ``claim_token`` (see that
        method's reclaim/redispatch branches) while ``claimed_at`` can stay
        unchanged; a ``claimed_at``-only check here would then let the
        FENCED-OUT caller place the order anyway, and its subsequent
        ``settle()`` -- which DOES check the token -- would fail the
        ownership check and leave the entry unresolved (and, for a
        ceiling-clamped order, leave the unfilled shortfall deducted until a
        later broker-history reconciliation happens to succeed) (Codex
        review discussion_r3818217238, "Validate the claim token before
        placing the order"). Every in-repo caller
        (``near_close_dca._process_whole_share_candidate``) passes the
        ``claim_token`` off the same ``WholeShareAccumulationDecision`` it
        used to reach this call.

        Checking ``placed``/``placing`` here (on top of the token check)
        closes a window two overlapping callers could otherwise both pass
        through: if both record the SAME ``recorded_at``/``claim_token`` for
        this dedup_key before either reaches this method (e.g. a genuine
        retry of a crashed caller, replaying the same claim), the first call
        to actually acquire this lock marks the entry ``placing``/``placed``
        and submits the order; without also checking those flags, the second
        call's token match would ALSO still pass, letting it submit a SECOND
        live order for the same fire (Codex review discussion_r3804235552,
        "Reject already-placed claims inside the execution lock").

        Before calling ``operation`` (the irreversible broker call), this
        durably persists ``cached["placing"] = True`` for the claim, still
        holding the lock. This closes a gap the ``placed`` marker alone left
        open: if the process is terminated (SIGKILL, OOM kill, host
        eviction) after ``operation()`` is accepted by the broker but before
        the ``placed`` write below runs, the OS lock releases on exit and
        the durable claim would otherwise still look exactly like a fresh,
        never-attempted claim -- a retry could then pass every check here
        and submit the SAME fire again while the first order is still
        absent from eventually-consistent broker history (Codex review
        discussion_r3818217230, "Persist placement intent before invoking
        the broker"). Because the TRUE broker outcome after such a crash is
        unknown (the order may or may not have gone through), a claim left
        with ``placing`` still ``True`` (i.e. this process never got back
        around to writing ``placed``) is deliberately NOT reclaimable by
        ``record_contribution``'s immediate-reclaim path or by
        :meth:`cached_fire_decision`'s preview of it -- see both methods'
        own "placing" handling -- until a caller reconciles it against
        broker order history (the same ``unresolved_claim`` /
        ``unresolved_claims_any_day`` / cross-day recovery pre-pass machinery
        every other unresolved claim already goes through; a claim with
        ``placing`` set is still reported by both of those as unresolved,
        since neither inspects ``placed``/``placing``) and calls
        :meth:`settle` or :meth:`preserve_pending`/
        :meth:`preserve_pending_if_claimed`, either of which clears
        ``placing`` as part of resolving the claim.

        Raises :class:`WholeShareClaimPersistenceError` -- rather than
        letting a bare write failure propagate as an ambiguous exception --
        when ``operation`` succeeds but persisting the resulting ``placed``
        marker afterward fails: ``operation`` (the broker call) and this
        persistence step are DELIBERATELY isolated from each other so a
        caller can tell "nothing was submitted" (any other exception here)
        apart from "something WAS submitted, but this ledger could not
        durably record it" (Codex review discussion_r3804235587, "Preserve
        broker acceptance when persisting `placed` fails") -- conflating the
        two would let a caller's generic placement-failure handling call
        ``preserve_pending()`` for an order that is actually live, restoring
        money a later fire could then spend a second time. A failure to
        persist the EARLIER ``placing`` marker (before ``operation`` even
        runs) is NOT wrapped this way -- nothing irreversible has happened
        yet, so it is safe to let that write failure propagate as an
        ordinary exception and abstain, exactly like any other pre-placement
        failure.
        """
        symbol = ticker.strip().upper()
        # Validate BEFORE any path is constructed or the lock is acquired --
        # every other public method on this class enforces _SYMBOL_PATTERN
        # up front for exactly this reason (see the pattern's own module-
        # level comment). This method used to only guard against an EMPTY
        # symbol, so a caller passing an unvalidated value (e.g.
        # idempotency_key=None skips the dedup_key-scoped ownership checks
        # below entirely, and even a claimed dedup_key never checks the
        # SYMBOL itself) could reach ``self.root / symbol / "ledger.lock"``
        # with something like ``"../other-stream"`` and have
        # cross_process_lock create that parent path outside this ledger's
        # own whole-share stream tree -- bypassing this method's claimed-
        # ownership guarantee entirely, since ``operation()`` would then run
        # with no claim validation at all for a ticker that was never a
        # legitimate whole-share symbol to begin with (Codex review
        # discussion_r3821705216, "Validate ticker before acquiring the
        # execution lock").
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            raise ValueError("ticker is required to execute a claimed whole-share fire")
        dedup_key = f"{trading_day}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(self.root / symbol / "ledger.lock"):
            state = None
            cached = None
            if dedup_key is not None:
                state = self._read_state(self.root / symbol / "ledger.json", symbol)
                cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
                if (
                    cached is None
                    or cached.get("resolved")
                    or cached.get("placed")
                    or cached.get("placing")
                    or not self._claim_matches(cached, claim_token=claim_token, recorded_at=recorded_at)
                ):
                    return False, None
                # Durable placement-intent marker, written and fsynced
                # (atomic_write_text) BEFORE the irreversible operation()
                # call below -- see this method's own docstring for why.
                cached["placing"] = True
                atomic_write_text(
                    self.root / symbol / "ledger.json", json.dumps(state, indent=2, sort_keys=True) + "\n"
                )
            result = operation()
            if dedup_key is not None and cached is not None and state is not None:
                cached["placing"] = False
                cached["placed"] = True
                try:
                    atomic_write_text(
                        self.root / symbol / "ledger.json", json.dumps(state, indent=2, sort_keys=True) + "\n"
                    )
                except Exception as exc:  # noqa: BLE001 - operation() already
                    # succeeded above; a write failure here must not be
                    # indistinguishable from operation() itself failing (see
                    # this method's own docstring) -- raise a DISTINCT
                    # exception type carrying the already-accepted result so
                    # the caller can keep it instead of restoring credit for
                    # a live order.
                    raise WholeShareClaimPersistenceError(
                        f"whole-share claim persistence failed after broker acceptance: {exc}", result
                    ) from exc
            return True, result

    def settle(
        self,
        ticker: str,
        trading_day: str,
        shares_purchased: float,
        submitted_notional_usd: float,
        *,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        claim_token: Optional[str] = None,
        require_claim_ownership: bool = False,
        on_claim_won: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Durably confirm a whole-share order that has actually been placed.

        ``on_claim_won``, when given, is invoked EXACTLY ONCE, and only when
        this call is the one that actually transitions the cached entry from
        unresolved to resolved -- the SAME condition captured by this
        method's own ``resolved_by_this_call``/return value below. It fires
        STRICTLY BEFORE the ``atomic_write_text`` at the end of this method,
        while the per-ticker ``cross_process_lock`` is still held, so a
        caller that needs its own durable evidence (e.g. a run-state
        artifact recording the booked order) to land before the claim's
        resolution becomes irreversible can do so from inside the hook --
        see ``near_close_dca._reconcile_unresolved_whole_share_claim``'s
        ``before_settle`` docstring for the full caller-side motivation
        (Codex review: "Persist evidence before irreversibly settling the
        claim" / "Fence evidence writes to the winning reconciler" -- calling
        this unconditionally regardless of claim ownership would let two
        concurrent reconcilers racing the SAME unresolved claim both persist
        recovery evidence even though only one of them can actually win the
        ownership check just below). A caller that loses the ownership race
        (``resolved_by_this_call`` stays ``False``, whether because no
        cached entry/idempotency_key was known, the entry was already
        resolved, ``_claim_matches`` rejects it, or -- with
        ``require_claim_ownership=True`` -- this call returns early before
        even reaching that check) never invokes ``on_claim_won``. A raised
        exception from ``on_claim_won`` is DELIBERATELY left uncaught: it
        propagates out of this method before ``atomic_write_text`` runs, so
        the shortfall restoration, the cached-entry resolution,
        ``shares_purchased_total``, and the audit event are all left
        uncommitted, and the claim remains exactly as unresolved/retryable
        as it was before this call -- callers wrap this in their own
        best-effort ``except Exception`` (see the ``on_claim_won`` call site
        in ``near_close_dca.py``) to turn that into a safe outcome.

        Bumps ``shares_purchased_total`` for the audit trail. If
        ``idempotency_key`` matches the key :meth:`record_contribution` used
        for this fire, this looks up that fire's PLANNED share count and its
        own cent-rounded ``current_share_price_usd`` and, when fewer shares
        were purchased than planned (a downstream cap clamped the order),
        automatically adds ``(planned_shares - shares_purchased) *
        cached_price`` back to the pending balance -- the caller does not
        need to separately call :meth:`preserve_pending` for the unfilled
        portion (Codex review: "Restore unsubmitted notional during
        settlement").

        The shortfall is deliberately computed from the CACHED price and
        share counts, not from ``submitted_notional_usd`` minus the cached
        planned notional: a caller that computes ``submitted_notional_usd``
        from a raw, un-rounded share price (rather than the SAME cent-rounded
        price ``record_contribution`` actually deducted at) would otherwise
        corrupt the shortfall by the rounding difference -- e.g. $20 pending
        at a raw $0.996 price deducts 20 shares at the ledger's rounded
        $1.00; clamping submission to 10 shares must restore exactly
        $10.00 (10 unbought shares * the $1.00 ledger price), not a
        raw-price-derived $10.04 (Codex review: "Restore clamped credit
        using the ledger price"). ``submitted_notional_usd`` is still
        recorded on the audit event, just no longer used for this math.

        Without ``idempotency_key`` (or with no matching cached entry), no
        planned share count is known here, so no shortfall can be computed
        or restored -- the caller is responsible for calling
        :meth:`preserve_pending` itself in that case.

        When the cached entry is ALREADY resolved, or this caller's
        ``recorded_at`` no longer matches its ``claimed_at`` (a stale-claim
        reclaim -- see :meth:`still_claimed` -- happened after this caller's
        own pre-placement check but before this settlement), the shortfall
        is skipped entirely: neither crediting a shortfall back nor marking
        the entry resolved here, since both belong to whichever attempt
        currently owns the claim, not this one (Codex review: "Reject
        settlements from callers that lost the claim" -- a caller in this
        state can still have placed a real order, so ``shares_purchased_total``
        and the audit event are still recorded; only the cached-entry
        mutation that could corrupt or double-resolve a DIFFERENT fire's
        claim is skipped).

        Returns ``True`` when THIS call is the one that actually transitioned
        the cache entry from unresolved to resolved (i.e. it owned the claim
        and the cached-entry mutation above ran), ``False`` otherwise
        (unknown/no ``idempotency_key``, no cached entry, or the entry was
        ALREADY resolved/fenced-out by someone else). Every normal, in-line
        placement caller can safely ignore this -- it already knows it
        placed a real order and always wants ``shares_purchased_total``
        credited, whether or not it happened to also win the claim.
        A RECONCILIATION caller settling an order it merely DISCOVERED
        (rather than just placed) is different: two concurrent reconciling
        callers can both read the SAME unresolved claim before either
        settles it, and both then call this method for the SAME physical
        broker order. ``shares_purchased_total`` was previously bumped here
        UNCONDITIONALLY regardless of which caller actually held the claim,
        so the second caller double-counted a purchase that only happened
        once (Codex review: "Make recovered-claim settlement idempotent").
        This return value lets such a caller check whether IT was the one
        that resolved the claim before booking anything further (state,
        budget, execution records) on the strength of this call.

        ``require_claim_ownership``, when ``True``, makes a RECONCILIATION
        caller's loss of the claim-ownership race skip EVERY mutation this
        call would otherwise make -- not just the shortfall (already
        skipped in that case), but also ``shares_purchased_total`` and the
        audit event -- and returns ``False`` without writing anything.
        Without this, a second reconciler discovering the SAME physical
        broker order double-counted it in the ledger's own audit trail even
        though its caller already (correctly) skips strategy-state booking
        for it (Codex review: "Update ledger totals only for the winning
        reconciliation"). Leave this ``False`` (the default) for a genuine
        in-line placement -- a caller that just placed a REAL, brand-new
        order always wants it counted, whether or not it also happens to
        win a claim-ownership race.
        """
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            raise ValueError("ticker is required to settle a whole-share purchase")
        if shares_purchased < 0:
            raise ValueError("shares_purchased must not be negative")
        if not math.isfinite(shares_purchased):
            # NaN and +/-inf both fail the `< 0` guard above cleanly (every
            # comparison against NaN is False, and inf is not negative), so
            # a malformed broker result or a buggy caller can reach the
            # float-based accumulation below and permanently corrupt
            # `shares_purchased_total` with a non-finite value that every
            # later settlement then propagates -- unlike the int-based
            # accumulator this module replaced, which raised on the same
            # input via int()'s own rejection of NaN/inf (Codex review
            # discussion_r3825818002, "Reject non-finite settled share
            # quantities"). Reject before any state mutation, exactly like
            # the negative-shares guard just above.
            raise ValueError("shares_purchased must be a finite number")
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"{trading_day}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            shortfall = 0.0
            resolved_by_this_call = False
            cached = None
            if dedup_key is not None:
                recorded_keys = state.setdefault("recorded_idempotency_keys", {})
                cached = recorded_keys.get(dedup_key)
                if cached is not None and not (
                    cached.get("resolved")
                    or not self._claim_matches(cached, claim_token=claim_token, recorded_at=recorded_at)
                ):
                    planned_shares = int(cached.get("shares_to_buy", 0))
                    cached_price = float(cached.get("current_share_price_usd", 0.0))
                    unbought_shares = max(0.0, planned_shares - shares_purchased)
                    shortfall = round(unbought_shares * cached_price, 2)
                    cached["resolved"] = True
                    # Clear "placing" -- once settled, this claim is no
                    # longer "attempted but outcome unknown" by definition
                    # (this very call IS the confirmed outcome). Deliberately
                    # does NOT clear "placed" the way preserve_pending()'s
                    # terminal-zero-fill fix does: a settled claim's "placed"
                    # marker reflects a REAL purchase that happened, accurate
                    # terminal placement history worth keeping, not a stale
                    # guard over dead money that redispatch should ignore
                    # (Codex review discussion_r3818217243's own "or
                    # distinguish terminal placement history from an
                    # in-flight placement" alternative).
                    cached["placing"] = False
                    # EXCEPT when shares_purchased is EXACTLY zero -- an
                    # explicit, broker-confirmed zero fill (e.g. a terminal
                    # "filled" order whose own cumulative_quantity is 0, or
                    # an ambiguous-placement lookup that turns up zero real
                    # shares) bought NOTHING, so "placed" no longer reflects
                    # a real purchase worth keeping as terminal history --
                    # the reasoning above only holds when THIS call's own
                    # settlement represents an actual, non-zero purchase.
                    # Leaving "placed" set here blocked every later
                    # zero-contribution retry under this same idempotency
                    # slot from ever redispatching the fully-restored credit
                    # (the resolved-and-not-"placed" gate in
                    # record_contribution()/cached_fire_decision() never
                    # opened), even though every dollar was already restored
                    # by the shortfall math just below (discussion_r3838771878,
                    # "Release placed claims for explicit zero fills").
                    if float(shares_purchased) == 0:
                        cached["placed"] = False
                    else:
                        # A CONFIRMED positive fill must mark "placed"
                        # True explicitly, not merely leave it at
                        # whatever it already was: a process killed after
                        # the broker accepted the order but before
                        # execute_claimed() persisted "placed" leaves the
                        # durable claim at placing=true, placed=false. A
                        # later reconciliation's settle() call already
                        # clears "placing" above -- if "placed" is left
                        # untouched here, the claim now reads
                        # resolved=true, placed=false, placing=false,
                        # which is exactly the shape record_contribution()/
                        # cached_fire_decision()'s zero-contribution
                        # redispatch gate treats as "safe to retry", even
                        # though real shares were just confirmed bought
                        # (Codex review: "Mark recovered positive fills as
                        # placed").
                        cached["placed"] = True
                    resolved_by_this_call = True
                    # This call is the one winning the claim -- fire the
                    # caller's hook now, still inside the lock and strictly
                    # before the atomic_write_text at the end of this
                    # method, so its own durable write (if any) is
                    # guaranteed to land before this resolution becomes
                    # irreversible. Left uncaught on purpose: a raise here
                    # propagates straight out of settle() before anything
                    # (shortfall restoration, cached-entry resolution,
                    # shares_purchased_total, the audit event) is committed,
                    # so the claim remains exactly as unresolved/retryable
                    # as it was before this call.
                    if on_claim_won is not None:
                        on_claim_won()
            if require_claim_ownership and not resolved_by_this_call:
                # A RECONCILIATION caller (settling an order it merely
                # DISCOVERED, never one it just placed) sets this. Two
                # concurrent reconcilers can both read the SAME unresolved
                # claim before either settles it and both call this method
                # for the SAME physical broker order -- the second caller's
                # own claim-ownership check above already correctly leaves
                # `resolved_by_this_call` False, but shares_purchased_total
                # and the audit event were previously still mutated
                # unconditionally below, double-counting that one physical
                # order in the ledger audit even though the caller's own
                # `_settle_whole_share_claim` already skips strategy-state
                # booking for it (Codex review: "Update ledger totals only
                # for the winning reconciliation"). Skip ALL mutation --
                # not just the shortfall -- and leave the state exactly as
                # it was; nothing here belongs to a caller that lost the
                # claim.
                return False
            if shortfall > 0:
                state["pending_usd"] = round(state["pending_usd"] + shortfall, 2)
            # A DIFFERENT settle() call for this SAME dedup_key can already
            # have credited shares_purchased_total for this exact physical
            # broker order -- e.g. the caller that genuinely PLACED it,
            # fenced out of "resolved"/shortfall bookkeeping by a reclaim
            # that happened in between (see the sibling test documenting
            # that the reclaiming attempt's own claim must stay live for
            # ITS OWN eventual placement/reconciliation), while a LATER
            # reconciliation call -- legitimately winning that still-live
            # reclaimed token -- discovers the exact same broker order via
            # an exact ref-id match and would otherwise re-credit the SAME
            # physical shares a second time (Codex review: "Handle a
            # fenced post-placement settlement idempotently"). Shortfall/
            # pending_usd and `resolved` bookkeeping above are unaffected
            # (per-claim-owner money accounting is orthogonal to this
            # physical-shares audit counter) -- only the one-time
            # shares_purchased_total credit is guarded.
            #
            # `cached` above is whatever CURRENTLY occupies this dedup_key
            # slot -- which, after a reclaim/redispatch, can be a DIFFERENT,
            # newer claim_token than the one THIS settle() call is actually
            # reporting on. The fenced-out-caller idempotency guarantee this
            # block exists to provide (two concurrent reconcilers settling
            # the SAME physical order must credit shares_purchased_total
            # only once) has to be scoped to the physical order/claim
            # ATTEMPT -- i.e. this call's own `claim_token` argument -- not
            # to "whatever is currently in the slot". Checking/writing only
            # `cached["shares_credited"]` did both at once and conflated
            # them: a fenced caller settling a real, already-executed order
            # would stamp `shares_credited=True` onto a DIFFERENT, current
            # (and possibly not-yet-placed) claim's slot, wrongly
            # suppressing THAT claim's own later legitimate credit once its
            # own order settles (Codex review discussion_r3825818004, "Do
            # not mark the new owner's order as already credited").
            # `credited_claim_tokens` is a durable, state-level record --
            # independent of `cached` being overwritten by a later reclaim
            # or redispatch -- of which `claim_token`s have already been
            # credited for THIS ticker's ledger, so idempotency for a
            # SPECIFIC physical attempt survives the slot moving on to a
            # new claim entirely.
            credited_tokens = state.setdefault("credited_claim_tokens", [])
            if claim_token is not None:
                already_credited = claim_token in credited_tokens
            else:
                # No claim_token to key a durable record by (a caller that
                # settles without one at all) -- fall back to the prior,
                # slot-scoped behavior. Every whole-share settlement call
                # site in near_close_dca.py passes claim_token, so this
                # fallback is purely defensive for a caller that doesn't.
                already_credited = cached is not None and cached.get("shares_credited")
            if not already_credited:
                # float(), not int() -- shares_purchased is a float end to
                # end since PR #515 widened settle()'s signature to carry a
                # broker-confirmed fill quantity (including partial fills)
                # rather than assuming the full requested quantity always
                # filled; rounded to 8 places purely to avoid float noise
                # accumulating across many small settlements.
                state["shares_purchased_total"] = round(
                    float(state.get("shares_purchased_total", 0)) + shares_purchased, 8
                )
                if claim_token is not None:
                    credited_tokens.append(claim_token)
                # Still write the slot-scoped marker, but ONLY when this
                # call currently owns the claim -- `cached["shares_credited"]`
                # is read directly elsewhere on the assumption it reflects
                # the CURRENT claim: `record_contribution`'s redispatch/
                # reclaim reset (grep "Reset the credit marker when
                # redispatching a new fire") and `cached_fire_decision()`'s
                # preview logic both depend on that. A fenced-out caller
                # (claim_token no longer matches, or the slot is already
                # resolved by someone else) must NOT write it -- that would
                # be exactly the bug this fix closes, stamping a foreign,
                # possibly-still-unplaced claim as already credited.
                if resolved_by_this_call and cached is not None:
                    cached["shares_credited"] = True
            state["events"].append(
                {
                    "trading_day": trading_day,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "shares_purchased": shares_purchased,
                    "submitted_notional_usd": submitted_notional_usd,
                    "restored_shortfall_usd": shortfall,
                    "shares_purchased_total": state["shares_purchased_total"],
                    "decision": "settled",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return resolved_by_this_call

    def pending_usd(self, ticker: str) -> float:
        """Return this ticker's current pending accumulation balance."""
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return 0.0
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            return float(state["pending_usd"])

    def already_recorded(self, ticker: str, trading_day: str, idempotency_key: Optional[str]) -> bool:
        """Whether :meth:`record_contribution` has already been called for
        this exact ``(trading_day, idempotency_key)`` pair, resolved or not.

        A caller previewing what a run WOULD do (e.g. tier-planning's
        read-only budget-tier preview) must not add today's contribution a
        SECOND time on top of :meth:`pending_usd` when this returns ``True``
        -- :meth:`pending_usd` already reflects that contribution (deducted
        at record time, whether or not the fire has since resolved), so
        blindly re-applying it again would double-count on a same-day retry
        of an already-recorded slot (Codex review: "Make tier previews honor
        resolved ledger contributions"). Prefer :meth:`cached_fire_decision`
        for a preview that also needs to know WHAT `record_contribution`
        would actually return right now -- this boolean alone cannot
        distinguish a resolved/reserved entry (which returns zero shares)
        from a stale, reclaimable one (which returns its cached POSITIVE
        decision).
        """
        if not idempotency_key:
            return False
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return False
        state_path = self.root / symbol / "ledger.json"
        dedup_key = f"{trading_day}:{idempotency_key}"
        with cross_process_lock(self.root / symbol / "ledger.lock"):
            state = self._read_state(state_path, symbol)
            return dedup_key in state.get("recorded_idempotency_keys", {})

    def cached_fire_decision(
        self,
        ticker: str,
        trading_day: str,
        idempotency_key: Optional[str],
        recorded_at: Optional[str],
        current_share_price_usd: Optional[float] = None,
        contribution_usd: float = 0.0,
    ) -> Optional[WholeShareAccumulationDecision]:
        """Read-only preview of what :meth:`record_contribution` would
        return RIGHT NOW for this exact ``(trading_day, idempotency_key)``
        slot, without mutating anything. Returns ``None`` when the slot has
        never been recorded at all -- the caller should compute its own
        normal preview instead.

        Mirrors :meth:`record_contribution`'s own read branches exactly:

        - An ALREADY-RESOLVED entry, or one currently marked ``placed``
          (broker-accepted, awaiting :meth:`settle`), returns a zero-shares
          decision -- the same "nothing to do" outcome
          :meth:`record_contribution` itself returns for either case.
        - An UNRESOLVED, not-yet-``placed`` entry is ALWAYS previewed as
          reclaimable, RE-PRICED against ``current_share_price_usd`` (the
          caller's own current quote) rather than the original attempt's
          cached price: a real ``record_contribution`` call right now would
          reclaim and reprice it the exact same way -- ownership since PR
          #515 is enforced by :meth:`execute_claimed`'s cross-process lock,
          not by a wall-clock staleness wait here (Codex review: "Reprice
          reclaimed stale fires before placement"), so a preview using the
          stale price could report a share count/notional a real reclaim
          would size differently. ``current_share_price_usd=None`` (a caller
          with no live price on hand) falls back to the cached price
          unchanged, matching the pre-existing behavior. A preview that
          always treats "already recorded" as "$0, no order" is wrong for
          this case -- the retry this preview is FOR will actually attempt
          (and may need budget-tier headroom for) that recovered fire (Codex
          review: "Account for actionable stale claims in tier planning").
        - An ALREADY-RESOLVED-but-not-``placed`` entry additionally
          redispatches -- returns the restored balance's own actionable
          decision instead of the flat zero-shares "nothing to do" outcome
          -- but ONLY when ``contribution_usd<=0``, mirroring EXACTLY which
          real ``record_contribution`` calls are eligible to redispatch a
          resolved slot (see that method's own "Allow same-day redispatch
          of preserved fires" fix). A caller previewing a GENUINE fresh BUY
          (``contribution_usd`` positive) against a resolved slot must NOT
          see a redispatch here: the real call for that same retry would
          NOT reopen the slot (redispatch is scoped to zero-contribution
          callers only, to avoid double-counting a fresh contribution), so
          an unconditional preview would report an order the live retry
          would actually skip, or inflate a tier-planning cap with phantom
          planned spend (Codex review: "Distinguish fresh BUY retries in
          cached previews"). That redispatch preview is sized off the same
          ``restored_credit_usd`` cap the real call applies, so a claim whose
          restored credit alone cannot cover a share is previewed as zero
          shares even when unrelated pending contributions make the pool
          balance sufficient (Codex review discussion_r3849268544).
        """
        if not idempotency_key:
            return None
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return None
        state_path = self.root / symbol / "ledger.json"
        dedup_key = f"{trading_day}:{idempotency_key}"
        with cross_process_lock(self.root / symbol / "ledger.lock"):
            state = self._read_state(state_path, symbol)
            cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
            if cached is None:
                return None
            pending_after = float(state["pending_usd"])
            if cached.get("resolved") or cached.get("placed") or cached.get("placing"):
                # Mirrors record_contribution's own resolved-entry redispatch
                # fix (Codex review: "Allow same-day redispatch of preserved
                # fires"), scoped the SAME way: only a zero-contribution
                # retry against a resolved-and-NOT-placed-and-NOT-placing
                # slot redispatches -- a "placed" entry is a live/just-settled
                # broker submission, and a "placing" entry is a submission
                # this ledger cannot yet confirm the outcome of (see
                # execute_claimed()'s and record_contribution()'s own
                # "placing" handling, Codex review discussion_r3818217230) --
                # neither is a preserve_pending() restoration, so there is no
                # freed balance to preview redispatching (mirrors
                # record_contribution's own discussion_r3804235552 fix). A
                # fresh BUY preview (contribution_usd positive) against a
                # resolved slot must fall through to the flat zero-shares
                # return below, matching what the real record_contribution
                # call would actually do for that same retry (Codex review:
                # "Distinguish fresh BUY retries in cached previews").
                if (
                    cached.get("resolved")
                    and not cached.get("placed")
                    and not cached.get("placing")
                    and contribution_usd <= 0
                ):
                    price_for_preview = (
                        current_share_price_usd
                        if current_share_price_usd is not None
                        else cached["current_share_price_usd"]
                    )
                    # Size the preview off the SAME capped credit the real
                    # record_contribution() redispatch uses, not off the whole
                    # shared pending pool. Previewing the full balance reports
                    # an order the live call would skip whenever this claim's
                    # restored credit alone falls short of a share but
                    # unrelated pending contributions push the total over it
                    # -- a phantom planned deployment that can select a higher
                    # budget tier and authorize unrelated spending (Codex
                    # review discussion_r3849268544, "Mirror the restored-
                    # credit cap in cached previews").
                    restored_credit = _redispatchable_credit_usd(
                        cached, pending_after, float(state.get(_WITHDRAWN_TOTAL_FIELD, 0.0))
                    )
                    redispatch = apply_contribution(restored_credit, 0.0, price_for_preview)
                    if redispatch.shares_to_buy > 0:
                        # The balances reported to the caller stay the FULL
                        # pool's, exactly as record_contribution() rewrites
                        # them after sizing off the capped credit -- only the
                        # SIZING is capped, and a preview whose
                        # pending_before/after disagreed with the real call's
                        # would misreport the post-fire balance.
                        return replace(
                            redispatch,
                            pending_before_usd=pending_after,
                            pending_after_usd=round(pending_after - redispatch.order_notional_usd, 2),
                        )
                # Mirrors record_contribution's own resolved-entry logic: this
                # preview call makes no new contribution, so contribution_usd
                # must be 0.0, not the original cached amount -- otherwise a
                # dry-run preview of a resolved-slot retry would report a
                # contribution that never actually happened this call (Codex
                # review: "Report zero contribution for resolved slot
                # retries").
                return WholeShareAccumulationDecision(
                    pending_before_usd=cached["pending_before_usd"],
                    contribution_usd=0.0,
                    current_share_price_usd=cached["current_share_price_usd"],
                    shares_to_buy=0,
                    order_notional_usd=0.0,
                    pending_after_usd=pending_after,
                )
            # Unresolved and not yet placed -- record_contribution() would
            # reclaim this immediately (no staleness wait since PR #515), so
            # this preview always previews the reclaim/reprice outcome too.
            if current_share_price_usd is None:
                return WholeShareAccumulationDecision(
                    pending_before_usd=cached["pending_before_usd"],
                    contribution_usd=cached["contribution_usd"],
                    current_share_price_usd=cached["current_share_price_usd"],
                    shares_to_buy=cached["shares_to_buy"],
                    order_notional_usd=cached["order_notional_usd"],
                    pending_after_usd=cached["pending_after_usd"],
                )
            # Sized off the claim's own basis, exactly as the real reclaim
            # sizes it -- see _claim_sizing_basis_usd() -- but ALSO capped by
            # this claim's own surviving remainder, mirroring
            # record_contribution()'s live reclaim path (the reprice_total /
            # frozen_total / had_prior_remainder computation just above the
            # equivalent `repriced = apply_contribution(reprice_total, 0.0,
            # ...)` call there). Previewing from the uncapped basis let a
            # preview report an order the live call would size smaller, or
            # would fire at all: the pool-level check further below only
            # proves the shared pool as a whole can cover an UPWARD reprice's
            # extra draw, not that the draw is THIS claim's own money -- a
            # different claim can have already spent this claim's remainder,
            # with a third, unrelated contribution then padding the pool back
            # up, and an uncapped preview would report a phantom order that
            # can select a higher budget tier and authorize spending the real
            # retry will not place (Codex review: "Cap cached reprice previews
            # by the surviving remainder").
            surviving_remainder = _claim_credit_remainder_usd(
                cached, pending_after, state[_WITHDRAWN_TOTAL_FIELD]
            )
            had_prior_remainder = _CLAIM_REMAINDER_FIELD in cached
            reserved_usd = _finite_non_negative(cached.get("order_notional_usd")) or 0.0
            frozen_total = _claim_sizing_basis_usd(cached) + float(cached["contribution_usd"])
            if had_prior_remainder:
                # An entry with no remainder field predates it; there is
                # nothing to age, so it keeps the frozen total it has always
                # used -- same reasoning as the live reclaim path.
                reprice_total = min(frozen_total, reserved_usd + surviving_remainder)
            else:
                reprice_total = frozen_total
            repriced = apply_contribution(reprice_total, 0.0, current_share_price_usd)
            # Mirrors record_contribution's own deferral guard: a downward
            # reprice that would need to draw more from the shared pool than
            # is currently unspent must not be previewed as actionable
            # either -- a real reclaim right now would defer, not fire
            # (Codex review: "Prevent downward repricing from overdrawing
            # pending credit").
            # The SAME basis-independent delta the real reclaim applies: the
            # change in what this claim reserves from the pool.
            pending_delta = cached["order_notional_usd"] - repriced.order_notional_usd
            # Mirrors record_contribution's own claim-ownership check too:
            # `repriced` is already sized off the CAPPED reprice_total (this
            # claim's own reserved + surviving remainder), so it can never
            # ask for anyone else's money. A capped zero-share result is
            # therefore previewed as resolved (releasing the reservation),
            # exactly like the live call, rather than deferred -- an earlier
            # version of both this preview and the live call deferred a
            # capped zero-share reprice instead, leaving the reservation
            # neither placed nor released until the price happened to fall
            # back to what the (possibly permanently smaller, after another
            # claim withdrew the remainder) capped total could afford (Codex
            # review: "Release the surviving reservation after a zero-share
            # reprice"). Only a genuine POOL-level liquidity shortfall still
            # defers below.
            if pending_after + pending_delta < 0:
                return WholeShareAccumulationDecision(
                    pending_before_usd=cached["pending_before_usd"],
                    contribution_usd=0.0,
                    current_share_price_usd=cached["current_share_price_usd"],
                    shares_to_buy=0,
                    order_notional_usd=0.0,
                    pending_after_usd=pending_after,
                )
            return WholeShareAccumulationDecision(
                pending_before_usd=cached["pending_before_usd"],
                contribution_usd=cached["contribution_usd"],
                current_share_price_usd=repriced.current_share_price_usd,
                shares_to_buy=repriced.shares_to_buy,
                order_notional_usd=repriced.order_notional_usd,
                # Mirrors record_contribution's own reconciliation: the
                # RECONCILED balance a real reclaim would persist right now
                # (pending_after + the SAME delta a real reclaim would apply),
                # not the isolated repriced.pending_after_usd computed solely
                # from the stale entry's own frozen total. A DIFFERENT
                # idempotency key can have recorded its own contribution
                # between this claim's original recording and now -- e.g. a
                # $15 fire at $10 leaves $5 pending; a later $4 contribution
                # under another key makes it $9; previewing a reclaim at $20
                # must report $9 + ($15 repriced remainder - $5 original
                # remainder) = $19, not a bare $15, matching exactly what a
                # real record_contribution() call would return and persist
                # (Codex review: "Return the reconciled balance from
                # stale-claim previews").
                pending_after_usd=round(pending_after + pending_delta, 2),
            )

    def shares_purchased_total(self, ticker: str) -> float:
        """Return the running count of whole shares bought via this ledger."""
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return 0.0
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            return float(state.get("shares_purchased_total", 0))

    def events(self, ticker: str) -> list[dict[str, Any]]:
        """Return a copy of this ticker's structured decision history."""
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return []
        state = self._read_state(self.root / symbol / "ledger.json", symbol)
        return list(state["events"])

    @staticmethod
    def _read_state(path: Path, ticker: str) -> dict[str, Any]:
        if not path.exists():
            return {
                "version": 1,
                "ticker": ticker,
                "pending_usd": 0.0,
                "shares_purchased_total": 0,
                "events": [],
                "recorded_idempotency_keys": {},
                "credited_claim_tokens": [],
                _WITHDRAWN_TOTAL_FIELD: 0.0,
            }
        try:
            state = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read whole-share accumulation ledger {path}") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("ticker") != ticker
            or isinstance(state.get("pending_usd"), bool)
            or not isinstance(state.get("pending_usd"), (int, float))
            or not isinstance(state.get("events"), list)
            # setdefault only fills this field in when it is ABSENT -- a
            # PRESENT but malformed value (null, a list, ...) survived
            # untouched, and every read site below (including the
            # legacy-snapshot migration a few lines down) unconditionally
            # calls .values()/.items()/.get() on it, raising AttributeError
            # out of _read_state() itself and breaking even read-only
            # callers like pending_usd() and events() (Codex review:
            # "Validate the idempotency-key map before iterating"). An
            # earlier fix reset the field to {} instead of raising here --
            # but that map is the ONLY durable record that a given
            # (trading_day, idempotency_key) contribution already happened.
            # pending_usd already reflects the original contribution, so
            # discarding the map makes a scheduler retry of the same claim
            # look brand new and re-apply it, doubling the credit (Codex
            # review: "Fail closed when the claim cache map is malformed").
            # Raising here, alongside the other core-shape checks, fails the
            # whole ticker closed rather than silently forgetting its claim
            # history.
            or (
                "recorded_idempotency_keys" in state
                and not isinstance(state.get("recorded_idempotency_keys"), dict)
            )
        ):
            raise ValueError(f"invalid whole-share accumulation ledger {path}")
        state.setdefault("recorded_idempotency_keys", {})
        state.setdefault("shares_purchased_total", 0)
        state.setdefault("credited_claim_tokens", [])
        # Normalized, not merely defaulted. setdefault only fills the field in
        # when it is ABSENT, so an existing entry carrying "bad" (or None, or
        # NaN) survived untouched -- and every later reservation calls float()
        # on it in _record_withdrawal(), which then raises and aborts
        # processing for the whole ticker on the next fire or preserved-fire
        # retry. Failing the malformed entitlement CLOSED is the behaviour this
        # counter exists for; taking the ticker down with it is not (Codex
        # review: "Validate the withdrawal counter when loading ledgers").
        #
        # Resetting the counter ALONE is not conservative, it is the hazard.
        # Every per-claim snapshot was taken against the old, larger counter,
        # so a reset to 0.0 makes `withdrawn_now - as_of` negative for all of
        # them and the aging subtraction becomes zero: each claim then reads
        # its full recorded entitlement, bounded only by the pool clamp -- and
        # that clamp is exactly what cannot tell "never spent" from "spent and
        # refilled". Claim A restores $10, B withdraws it, C contributes a
        # fresh $10, the counter is corrupted, and A's retry spends C's money:
        # the precise breach _redispatchable_credit_usd()'s aging exists to
        # prevent, reopened by the repair (Codex review: "Fail corrupted
        # withdrawal counters closed").
        #
        # So the entitlements the lost counter was aging are invalidated with
        # it. Zeroed rather than deleted: a MISSING restored_credit_usd falls
        # back to order_notional_usd, while a field PRESENT and 0.0 is honored
        # as a genuine "restored nothing" cap (see
        # _redispatchable_credit_usd()). A claim loses a redispatch it might
        # have been owed, which costs it a later retry; the alternative spends
        # money that was never its.
        # ABSENT is not the same thing as PRESENT-but-malformed. A ledger
        # written before _WITHDRAWN_TOTAL_FIELD existed at all has never had
        # a counter to lose -- initializing it to 0.0 (what the field's own
        # setdefault used to do) is the intentionally supported upgrade path,
        # not a corruption to invalidate entitlements over. Routing an
        # absent field through the same invalidation as a genuinely
        # unreadable one zeroed every cached restored_credit_usd/claim
        # remainder on the very first load of a legacy ledger -- a resolved
        # claim with $10 restored into a $10 pending pool then returned zero
        # shares on its zero-contribution retry instead of redispatching,
        # silently suppressing a purchase the entitlement legitimately owned
        # (Codex review: "Preserve entitlements when the counter is
        # absent"). Only a field that IS present and fails
        # _finite_non_negative() is the "lost counter" case the invalidation
        # below exists for.
        if _WITHDRAWN_TOTAL_FIELD not in state:
            withdrawn_total = 0.0
            # A pre-counter ledger can legitimately contain snapshots from a
            # later-compatible writer (for example an exported/restored
            # ledger whose counter field was absent).  With no historical
            # counter to compare them to, establish this load as the upgrade
            # boundary.  Otherwise the snapshot-ahead invariant below would
            # write off every legacy entitlement solely because the newly
            # initialized counter starts at zero.
            #
            # Tie offset here is a flat $0, not
            # _allocate_credit_tie_offset() -- this loop can re-anchor an
            # entry whose OWN tie-batch fields (on this same ``state``) were
            # already advanced by its own earlier, unrelated writes before
            # the counter itself went missing; trusting that stale batch
            # state would re-derive an inflated offset from the entry's own
            # history rather than a genuine tie against a DIFFERENT entry
            # (see _initialize_restored_credit_aging()'s identical reasoning
            # and the regression test that caught it,
            # test_a_legacy_restored_credit_is_snapshotted_before_future_withdrawals).
            # Two DIFFERENT entries re-anchored together in this SAME loop
            # can still tie on the identical as_of=0.0 value -- narrower than
            # the general case (reachable only once, on the first load after
            # this exact pre-counter upgrade path, and only when more than
            # one entry needs it) and no worse than how this repair behaved
            # before this fix (Codex review: "Avoid charging one withdrawal
            # to every restored claim" targets the ordinary
            # preserve_pending/fresh-contribution ties fixed at their own
            # write sites, which do not carry this stale-batch hazard).
            state[_WITHDRAWN_TOTAL_FIELD] = withdrawn_total
            for _dedup_key, cached in state.get("recorded_idempotency_keys", {}).items():
                if not isinstance(cached, dict):
                    continue
                if _RESTORED_CREDIT_AS_OF_FIELD in cached:
                    # Forget the OLD (claim, field, as_of) audit entry before
                    # re-anchoring: this re-anchoring can leave the AS_OF
                    # value UNCHANGED (e.g. a ledger whose entries already
                    # sat at withdrawn_total==0.0) while forcibly resetting
                    # the offset to $0 regardless of what it legitimately
                    # was -- at the SAME as_of, that collides with a still-
                    # present audit entry from before, and the next load
                    # would see the mismatch and write off a perfectly
                    # legitimate credit, not merely log a stale warning
                    # (Codex review: "Synchronize audits when migration
                    # rewrites offsets"). Read BEFORE the field is
                    # overwritten below.
                    _old_restored_as_of = _finite_non_negative(cached.get(_RESTORED_CREDIT_AS_OF_FIELD))
                    if _old_restored_as_of is not None:
                        _forget_tie_offset_audit(state, _dedup_key, _RESTORED_CREDIT_FIELD, _old_restored_as_of)
                    cached[_RESTORED_CREDIT_AS_OF_FIELD] = withdrawn_total
                    cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = 0.0
                if _CLAIM_REMAINDER_AS_OF_FIELD in cached:
                    _old_remainder_as_of = _finite_non_negative(cached.get(_CLAIM_REMAINDER_AS_OF_FIELD))
                    if _old_remainder_as_of is not None:
                        _forget_tie_offset_audit(state, _dedup_key, _CLAIM_REMAINDER_FIELD, _old_remainder_as_of)
                    cached[_CLAIM_REMAINDER_AS_OF_FIELD] = withdrawn_total
                    cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = 0.0
        else:
            withdrawn_total = _finite_non_negative(state.get(_WITHDRAWN_TOTAL_FIELD))
            if withdrawn_total is None:
                _log.warning(
                    "whole-share ledger %s carries an unusable %s (%r); resetting it to 0.00 "
                    "and writing off every claim entitlement it was aging, since none of their "
                    "snapshots can be trusted against a counter that was lost",
                    path,
                    _WITHDRAWN_TOTAL_FIELD,
                    state.get(_WITHDRAWN_TOTAL_FIELD),
                )
                withdrawn_total = 0.0
                for _dedup_key, cached in state.get("recorded_idempotency_keys", {}).items():
                    if not isinstance(cached, dict):
                        continue
                    # Forget each field's OLD (claim, field, as_of) audit
                    # entry before its AS_OF is rewritten to 0.0 below: a
                    # ledger whose entries already sat at as_of==0.0 would
                    # otherwise have this reset collide with a still-present
                    # audit entry from before, and the next load would see
                    # the mismatch and write off a perfectly legitimate
                    # credit (Codex review: "Synchronize audits when
                    # migration rewrites offsets"). Read BEFORE each field
                    # is overwritten; a no-op when unreadable/absent, since
                    # no legitimate audit entry could exist under it anyway.
                    _old_restored_as_of = _finite_non_negative(cached.get(_RESTORED_CREDIT_AS_OF_FIELD))
                    if _old_restored_as_of is not None:
                        _forget_tie_offset_audit(state, _dedup_key, _RESTORED_CREDIT_FIELD, _old_restored_as_of)
                    _old_remainder_as_of = _finite_non_negative(cached.get(_CLAIM_REMAINDER_AS_OF_FIELD))
                    if _old_remainder_as_of is not None:
                        _forget_tie_offset_audit(state, _dedup_key, _CLAIM_REMAINDER_FIELD, _old_remainder_as_of)
                    if _RESTORED_CREDIT_FIELD in cached:
                        cached[_RESTORED_CREDIT_FIELD] = 0.0
                        cached[_RESTORED_CREDIT_AS_OF_FIELD] = 0.0
                        cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = 0.0
                    else:
                        # A resolved legacy claim written BEFORE
                        # restored_credit_usd existed carries no such field
                        # at all, so this loop left it untouched --
                        # _redispatchable_credit_usd() then takes its
                        # legacy order_notional_usd fallback with NO
                        # withdrawal aging applied whatsoever, since the
                        # very counter that aging depends on was just lost.
                        # A zero-contribution retry could therefore reserve
                        # that entry's full original notional from a pool
                        # replenished entirely by unrelated later claims
                        # (Codex review: "Invalidate legacy fallback
                        # credits after counter corruption"). Stamp an
                        # explicit zero restored credit for a legacy entry
                        # that WOULD have taken that fallback (a positive
                        # order_notional_usd -- a preserved fire, not a
                        # never-attempted zero-order contribution whose own
                        # claim_credit_remainder_usd aging is unaffected
                        # here), so it now reads as PRESENT-and-written-off
                        # instead of silently exempt.
                        order_notional = _finite_non_negative(cached.get("order_notional_usd"))
                        if order_notional is not None and order_notional > 0:
                            cached[_RESTORED_CREDIT_FIELD] = 0.0
                            cached[_RESTORED_CREDIT_AS_OF_FIELD] = 0.0
                            cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = 0.0
                    if _CLAIM_REMAINDER_FIELD in cached:
                        cached[_CLAIM_REMAINDER_FIELD] = 0.0
                        cached[_CLAIM_REMAINDER_AS_OF_FIELD] = 0.0
                        cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = 0.0
                # The tie-offset batch (see _allocate_credit_tie_offset()) is
                # anchored against the SAME counter that was just lost -- a
                # stale anchor/width could sit anywhere relative to the
                # rebuilt-from-zero counter above, giving a future claim a
                # meaningless offset. Reset it alongside the counter it is
                # derived from; every entitlement it was tracking was just
                # written off above anyway.
                state[_CREDIT_TIE_BATCH_ANCHOR_FIELD] = 0.0
                state[_CREDIT_TIE_BATCH_WIDTH_FIELD] = 0.0
        state[_WITHDRAWN_TOTAL_FIELD] = withdrawn_total
        # An entry written by a PARENT version that had restored_credit_usd/
        # claim_credit_remainder_usd but not yet their _AS_OF snapshot
        # fields carries the value with no snapshot at all -- and
        # _redispatchable_credit_usd()/_claim_credit_remainder_usd() both
        # read an absent snapshot as "charged no withdrawal, fall back to
        # the balance clamp alone" FOREVER, not just for draws that
        # happened before this upgrade. Nothing else in this module ever
        # backfills an EXISTING entry's snapshot after the fact (every
        # other write site sets both the value and its snapshot together,
        # only for a freshly resolved entry), so without this the entry
        # stays exempt from aging indefinitely: claim A restores $10, claim
        # B later withdraws it, claim C replenishes the pool, and retrying
        # A still reads its un-aged $10 and spends C's contribution (Codex
        # review: "Snapshot legacy restored credits before future
        # withdrawals"). Snapshotting to the counter AS OF THIS LOAD is the
        # correct, conservative boundary: draws that already happened
        # before the upgrade cannot be proven and are (as documented above)
        # not charged, but every withdrawal from this point forward now is.
        #
        # as_of is still the plain, un-modified withdrawn_total -- "AS OF
        # THIS LOAD", a single well-defined instant before any write this
        # same call goes on to make. Tie offset here is a flat $0, not
        # _allocate_credit_tie_offset() -- an entry reaching this backfill
        # can have its OWN tie-batch fields on ``state`` already advanced by
        # its own earlier, unrelated writes (an upgrade/rollback, or a
        # partial edit, that cleared only this entry's newest _AS_OF field
        # and left its tie-batch anchor/width from when it was originally
        # recorded untouched); trusting that stale batch state here would
        # re-derive an inflated offset from THIS entry's own history rather
        # than a genuine tie against a DIFFERENT entry, reopening the very
        # self-collision aging bug this fix exists to close (caught by
        # test_a_legacy_restored_credit_is_snapshotted_before_future_withdrawals).
        # Two DIFFERENT legacy entries backfilled together in this SAME loop
        # can still tie on the identical as_of value -- narrower than the
        # general case (reachable only once, on the first load after an
        # upgrade, and only when more than one entry predates the _AS_OF
        # fields) and no worse than how this exact backfill behaved before
        # this fix (Codex review: "Avoid charging one withdrawal to every
        # restored claim" targets the ordinary preserve_pending/fresh-
        # contribution ties fixed at their own write sites, which do not
        # carry this stale-batch hazard).
        # One shared local counter, private to THIS load's backfill pass
        # only (never persisted, never read from ``state``'s own tie-batch
        # fields -- see the self-collision reasoning just above): two
        # legacy entries with restored/remainder credit but no snapshot
        # both landing on this same ``withdrawn_total`` used to both read
        # tie offset $0, so a single later withdrawal aged BOTH of them to
        # zero instead of just the one it could actually have drawn from
        # (Codex review: "Stagger tie offsets when loading legacy
        # credits"). Staggering them against EACH OTHER here -- not against
        # the live global batch -- gives the same tie protection an
        # ordinary (non-legacy) same-instant tie already gets, without
        # reopening the self-collision hazard.
        #
        # ONE counter shared across BOTH restored-credit and claim-remainder
        # backfills, not two separate ones per field type: both fields are
        # slices of the exact same fungible ``pending_usd`` pool, and the
        # live (non-migration) write sites already tie them against each
        # other through the one shared
        # state[_CREDIT_TIE_BATCH_WIDTH_FIELD]/_CREDIT_TIE_BATCH_ANCHOR_FIELD
        # axis (see _allocate_credit_tie_offset(), called from both
        # record_contribution()'s remainder registration and
        # preserve_pending()'s restored-credit registration). A restored
        # entry X and a still-open remainder entry Y can both predate the
        # _AS_OF fields and share this same load's backfilled snapshot; two
        # separate per-type counters would give X and Y independent $0
        # starting points despite drawing on the same pool, reopening
        # exactly the cross-entry collision this fix exists to close --
        # just between the two field TYPES instead of between two same-type
        # entries.
        # Deliberately still starts at a bare 0.0, NOT from the live
        # state[_CREDIT_TIE_BATCH_WIDTH_FIELD] -- an entry reaching this
        # backfill can be the self-collision case documented just above
        # (a partial edit that cleared only ITS OWN newest _AS_OF field
        # and left the live tie-batch anchor/width from when it was
        # originally recorded untouched), and starting from that stale,
        # self-referential width would re-derive an inflated offset from
        # this entry's own history and then ALSO re-add its own amount
        # into `migration_stagger_width` below, double-counting it.
        migration_stagger_width = 0.0
        _migrated_any = False
        for _dedup_key, _cached in state.get("recorded_idempotency_keys", {}).items():
            if not isinstance(_cached, dict):
                continue
            if (
                _RESTORED_CREDIT_FIELD in _cached
                and _RESTORED_CREDIT_AS_OF_FIELD not in _cached
            ):
                # This entry's AS_OF is ABSENT, so nothing here proves what
                # it used to be -- but the (dedup_key, field, withdrawn_total)
                # key this backfill is ABOUT to write into can still
                # coincidentally already hold a stale audit entry from an
                # unrelated earlier registration for this same claim/field
                # at this same counter value. Forgetting it first keeps the
                # freshly-staggered (not _allocate_credit_tie_offset()-
                # audited) offset from mismatching one on the very next load
                # (Codex review: "Synchronize audits when migration rewrites
                # offsets").
                _forget_tie_offset_audit(state, _dedup_key, _RESTORED_CREDIT_FIELD, withdrawn_total)
                _cached[_RESTORED_CREDIT_AS_OF_FIELD] = withdrawn_total
                _cached[_RESTORED_CREDIT_TIE_OFFSET_FIELD] = migration_stagger_width
                migration_stagger_width += _finite_non_negative(_cached.get(_RESTORED_CREDIT_FIELD)) or 0.0
                _migrated_any = True
            if (
                _CLAIM_REMAINDER_FIELD in _cached
                and _CLAIM_REMAINDER_AS_OF_FIELD not in _cached
            ):
                _forget_tie_offset_audit(state, _dedup_key, _CLAIM_REMAINDER_FIELD, withdrawn_total)
                _cached[_CLAIM_REMAINDER_AS_OF_FIELD] = withdrawn_total
                _cached[_CLAIM_REMAINDER_TIE_OFFSET_FIELD] = migration_stagger_width
                migration_stagger_width += _finite_non_negative(_cached.get(_CLAIM_REMAINDER_FIELD)) or 0.0
                _migrated_any = True
        if _migrated_any:
            # Seed the LIVE tracker from the migrated intervals so the next
            # _allocate_credit_tie_offset() call at this same instant --
            # e.g. a fresh sub-share contribution recorded before any
            # withdrawal moves the counter -- continues after them instead
            # of independently resetting to offset $0 and colliding with
            # the first migrated entry (Codex review: "Seed the live tie
            # batch after migrating legacy credits"). Takes the MAX against
            # whatever live width was already there for this exact
            # `withdrawn_total`, rather than blindly overwriting it: a
            # genuinely different, still-live registration (not one of the
            # entries just migrated) can already have claimed space past
            # `migration_stagger_width`, and shrinking the tracker back
            # down to the migrated total alone would let a LATER call
            # collide with that untouched live entry. Taking the max also
            # keeps the self-collision case above harmless -- there, the
            # live width was entirely the migrated entry's OWN stale
            # figure, so it never exceeds `migration_stagger_width` and the
            # max is simply `migration_stagger_width` itself.
            live_anchor = _finite_non_negative(state.get(_CREDIT_TIE_BATCH_ANCHOR_FIELD))
            live_width = (
                _finite_non_negative(state.get(_CREDIT_TIE_BATCH_WIDTH_FIELD)) or 0.0
                if live_anchor is not None and live_anchor == withdrawn_total
                else 0.0
            )
            state[_CREDIT_TIE_BATCH_ANCHOR_FIELD] = withdrawn_total
            state[_CREDIT_TIE_BATCH_WIDTH_FIELD] = round(max(live_width, migration_stagger_width), 2)
            # Deliberately does NOT write a _CREDIT_TIE_OFFSET_AUDIT_FIELD
            # entry for the offsets just staggered above: this migration
            # path assigns them directly rather than through
            # _allocate_credit_tie_offset(), and an absent audit entry is
            # exactly what tells the validation pass below "cannot be
            # proven, so trust it" -- the correct, conservative stance for
            # an offset this loader itself just computed from imperfect
            # legacy information, not a live, fully-provable registration.
        # A tie offset (_RESTORED_CREDIT_TIE_OFFSET_FIELD /
        # _CLAIM_REMAINDER_TIE_OFFSET_FIELD) records how much of THIS claim's
        # exact _AS_OF snapshot instant was already claimed by OTHER claims
        # tied at that identical instant (see _allocate_credit_tie_offset()).
        # Every prior check in this loader validated an offset only for
        # being finite and non-negative (_finite_non_negative()), never
        # against what it actually represents -- and a corrupted-but-finite
        # offset still passes those checks while
        # _redispatchable_credit_usd()/_claim_credit_remainder_usd() subtract
        # it from `drawn_since_snapshot` before aging: an oversized offset
        # can drive that subtraction to zero regardless of how much the
        # pool was actually drawn since, suppressing withdrawal aging
        # entirely and leaving the claim redispatchable from money a later,
        # unrelated contribution put into the pool (Codex review
        # https://github.com/badMade/all-stock-dca/pull/1168#discussion_r3900047284,
        # "Validate the tie offset against reconstructable same-snapshot
        # intervals").
        #
        # THREE successive attempts at a bound DERIVED from other claims'
        # state each proved unsound under further review on this same PR:
        #
        # * Grouping by claims whose _AS_OF CURRENTLY matches this one broke
        #   the moment an earlier-tied claim moved to a NEWER snapshot of its
        #   own ("Preserve offsets after preceding claims move snapshots").
        # * Summing every OTHER entry's CURRENT recorded amount ledger-wide
        #   broke the moment a claim's CURRENT amount later shrank below
        #   what it was when a sibling's offset was earned against it, and
        #   risked double-counting a single claim's own two credit fields
        #   against each other ("Preserve historical width after preceding
        #   credits shrink" / "Exclude the claim's sibling credit from its
        #   offset bound").
        # * A durable per-anchor high-water mark, with this entry's own
        #   CURRENT amount excluded, still let a corrupted offset expand to
        #   swallow whatever a DIFFERENT claim registered into the same
        #   anchor AFTER this one ("Record the bound before later
        #   same-anchor registrations").
        #
        # Every one of those was an attempt to DERIVE the legitimate bound
        # from aggregate ledger state at read time. This replaces derivation
        # entirely: _allocate_credit_tie_offset() now writes the EXACT
        # offset it computes, for the EXACT (claim, field, snapshot) it
        # computed it for, into a durable, write-once
        # _CREDIT_TIE_OFFSET_AUDIT_FIELD entry the moment it is legitimately
        # assigned (see that field's own docstring). Validating a live
        # offset is then a plain equality check against its own audit
        # record -- not a bound derived from anything else in the ledger,
        # so nothing about any OTHER claim (current, historical, before, or
        # after) can loosen or tighten it. A mismatch of ANY kind -- too
        # high, too low, or simply different -- proves the live field was
        # altered after the fact, since the audit copy is written
        # atomically alongside it and never touched again.
        #
        # A ledger written before this field existed, or an entry that
        # reached its current offset through a migration path that
        # deliberately assigns offsets directly rather than through
        # _allocate_credit_tie_offset() (see the legacy-upgrade blocks
        # above), carries no audit entry for it at all -- exactly the same
        # "cannot prove, but also cannot disprove" position this loader
        # already takes for an absent (as opposed to a present-but-corrupt)
        # _WITHDRAWN_TOTAL_FIELD. Such an offset is left untouched (trusted)
        # here, never treated as unbounded or written off; every
        # subsequently-computed offset for that same claim/field IS audited,
        # so coverage only grows from here.
        for _dedup_key, _entry in state.get("recorded_idempotency_keys", {}).items():
            if not isinstance(_entry, dict):
                continue
            for _field, _as_of_field, _offset_field in (
                (_RESTORED_CREDIT_FIELD, _RESTORED_CREDIT_AS_OF_FIELD, _RESTORED_CREDIT_TIE_OFFSET_FIELD),
                (_CLAIM_REMAINDER_FIELD, _CLAIM_REMAINDER_AS_OF_FIELD, _CLAIM_REMAINDER_TIE_OFFSET_FIELD),
            ):
                if _offset_field not in _entry or _as_of_field not in _entry:
                    continue
                _as_of = _finite_non_negative(_entry.get(_as_of_field))
                _offset = _finite_non_negative(_entry.get(_offset_field))
                if _as_of is None or _offset is None:
                    # Already unusable by the checks above -- that path (an
                    # unreadable snapshot/offset) already fails the
                    # entitlement closed inside _redispatchable_credit_usd()/
                    # _claim_credit_remainder_usd() on its own; nothing
                    # further to validate here.
                    continue
                _audit = state.get(_CREDIT_TIE_OFFSET_AUDIT_FIELD)
                _audit_key = _tie_offset_audit_key(_dedup_key, _field, _as_of)
                # ABSENT (no _CREDIT_TIE_OFFSET_AUDIT_FIELD at all, not a
                # dict, or this exact key missing from it) is not the same
                # thing as PRESENT-but-unreadable -- the distinction this
                # loader draws everywhere else for every other field. A key
                # that genuinely never existed cannot be disproven, so it is
                # trusted; but a key that IS present with a value
                # _finite_non_negative() rejects (null, "NaN", a JSON
                # boolean, ...) is a DIFFERENT thing -- an audit record was
                # written for this exact registration and has since been
                # corrupted, not one that was never written at all. Routing
                # that through the same "no audit, trust it" path as a
                # genuinely absent key would let a corrupted-but-plausible
                # live offset stand unchallenged simply by ALSO corrupting
                # its audit entry to something unreadable -- reopening
                # exactly the ownership breach this whole audit trail exists
                # to close (Codex review: "Reject malformed present audit
                # records"). Only a genuinely missing key gets the trust
                # exemption; a present-and-unreadable one fails closed like
                # a mismatch.
                if not (isinstance(_audit, dict) and _audit_key in _audit):
                    continue
                _audited_offset = _finite_non_negative(_audit.get(_audit_key))
                if _audited_offset is None or round(_offset, 2) != round(_audited_offset, 2):
                    _log.warning(
                        "whole-share ledger %s carries a %s of %r for claim %r "
                        "snapshotted at withdrawn_usd_total=%r that does not match "
                        "its own recorded audit value of %r; writing off the "
                        "entitlement instead of trusting an offset that could "
                        "suppress withdrawal aging",
                        path,
                        _offset_field,
                        _entry.get(_offset_field),
                        _dedup_key,
                        _as_of,
                        _audit.get(_audit_key),
                    )
                    _entry[_field] = 0.0
                    _entry[_offset_field] = 0.0
                    # Retire the now-provably-wrong audit entry too, not
                    # just the live field -- otherwise a caller that DOES
                    # go on to persist this corrected state (most mutating
                    # callers read via _read_state() and later write the
                    # same dict back) still carries the stale audit
                    # forward, and the NEXT load repeats this exact
                    # warning/write-off forever even after the live value
                    # is durably $0 (Codex review: "Persist or retire the
                    # audit after writing off an entitlement").
                    _forget_tie_offset_audit(state, _dedup_key, _field, _as_of)
        return state


__all__ = [
    "WholeShareAccumulationLedger",
    "WholeShareAccumulationDecision",
    "apply_contribution",
    "compute_virtual_contribution_usd",
]
