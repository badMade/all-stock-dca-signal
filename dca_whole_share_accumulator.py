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

Does NOT import from ``near_close_dca`` at module level (only inside
``TYPE_CHECKING`` for the ``StrategyConfig`` type hint) -- ``near_close_dca``
imports :class:`WholeShareAccumulationLedger` from here, and a module-level
import in the other direction would be circular, exactly why
``dca_inverse_martingale.py`` (the closest precedent) does the same.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar
from uuid import uuid4

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from scripts.file_lock import cross_process_lock

if TYPE_CHECKING:
    from near_close_dca import StrategyConfig

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


def _money(value: float, *, field: str) -> Decimal:
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
    config: "StrategyConfig",
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
            if dedup_key is not None and dedup_key in recorded_keys:
                cached = recorded_keys[dedup_key]
                if cached.get("resolved") or cached.get("placed") or cached.get("placing"):
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
                        redispatch = apply_contribution(state["pending_usd"], 0.0, current_share_price_usd)
                        if redispatch.shares_to_buy > 0:
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
                repriced = apply_contribution(
                    cached["pending_before_usd"], cached["contribution_usd"], current_share_price_usd
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
                pending_delta = repriced.pending_after_usd - cached["pending_after_usd"]
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
                if state["pending_usd"] + pending_delta < 0:
                    return WholeShareAccumulationDecision(
                        pending_before_usd=cached["pending_before_usd"],
                        contribution_usd=0.0,
                        current_share_price_usd=cached["current_share_price_usd"],
                        shares_to_buy=0,
                        order_notional_usd=0.0,
                        pending_after_usd=state["pending_usd"],
                    )
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
            pending = state["pending_usd"]
            decision = apply_contribution(pending, contribution_usd, current_share_price_usd)
            state["pending_usd"] = decision.pending_after_usd
            if dedup_key is not None:
                claim_token = uuid4().hex if decision.shares_to_buy > 0 else None
                recorded_keys[dedup_key] = {
                    "pending_before_usd": decision.pending_before_usd,
                    "contribution_usd": decision.contribution_usd,
                    "current_share_price_usd": decision.current_share_price_usd,
                    "shares_to_buy": decision.shares_to_buy,
                    "order_notional_usd": decision.order_notional_usd,
                    "pending_after_usd": decision.pending_after_usd,
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
                    decision = WholeShareAccumulationDecision(**{**decision.__dict__, "claim_token": claim_token})
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
    ) -> float:
        """Restore a failed fire's deducted amount to the CURRENT balance.

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
            cached = None
            if dedup_key is not None:
                recorded_keys = state.setdefault("recorded_idempotency_keys", {})
                cached = recorded_keys.get(dedup_key)
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
            pending_after = round(pending_before + restored_amount, 2)
            state["pending_usd"] = pending_after
            if cached is not None:
                cached["resolved"] = True
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
        expected_placed: Optional[bool] = None,
    ) -> bool:
        """Atomically verify ownership and restore a CONFIRMED broker
        attempt's deduction in ONE locked operation, returning whether THIS
        call is the one that resolved the claim.

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

        ``expected_placed``, when given, must be the ``placed`` value the
        caller observed on ITS OWN snapshot of this claim (e.g. from
        :meth:`unresolved_claim`) at the time it gathered the broker
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
            cached = None
            if dedup_key is not None:
                recorded_keys = state.setdefault("recorded_idempotency_keys", {})
                cached = recorded_keys.get(dedup_key)
            if cached is None or cached.get("resolved") or not self._claim_matches(
                cached, claim_token=claim_token, recorded_at=recorded_at
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
            if expected_placed is not None and (
                bool(cached.get("placed")) != expected_placed or (not expected_placed and cached.get("placing"))
            ):
                # The caller's snapshot showed neither a placed nor an
                # in-flight order for this claim_token, but the CURRENT,
                # freshly-read-under-lock entry now shows one -- a
                # concurrent execute_claimed() placed a genuinely new live
                # order for this exact still-current claim in the gap
                # between the caller gathering its "safe to restore"
                # evidence and this call acquiring the lock. Declining here
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
            pending_after = round(pending_before + restored_amount, 2)
            state["pending_usd"] = pending_after
            cached["resolved"] = True
            # See preserve_pending()'s identical fix for the full reasoning
            # (Codex review discussion_r3818217243, "Clear placed after
            # releasing a terminal zero-fill order") -- this method exists
            # SPECIFICALLY for the confirmed-terminal-zero-fill reconciliation
            # case, so clearing both markers here is the primary case that
            # fix targets, not just a mirrored precaution.
            cached["placed"] = False
            cached["placing"] = False
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
    ) -> bool:
        """Whether ``recorded_at`` is still the CURRENT claim on this fire.

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
            if dedup_key is not None:
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
    ) -> bool:
        """Durably confirm a whole-share order that has actually been placed.

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
                    unbought_shares = max(0.0, planned_shares - float(shares_purchased))
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
                    resolved_by_this_call = True
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
                    float(state.get("shares_purchased_total", 0)) + float(shares_purchased), 8
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
                    "shares_purchased": float(shares_purchased),
                    "submitted_notional_usd": float(submitted_notional_usd),
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
          cached previews").
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
                    redispatch = apply_contribution(pending_after, 0.0, price_for_preview)
                    if redispatch.shares_to_buy > 0:
                        return redispatch
                # Mirrors record_contribution's own resolved-entry fix: this
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
            repriced = apply_contribution(
                cached["pending_before_usd"], cached["contribution_usd"], current_share_price_usd
            )
            # Mirrors record_contribution's own deferral guard: a downward
            # reprice that would need to draw more from the shared pool than
            # is currently unspent must not be previewed as actionable
            # either -- a real reclaim right now would defer, not fire
            # (Codex review: "Prevent downward repricing from overdrawing
            # pending credit").
            if pending_after + (repriced.pending_after_usd - cached["pending_after_usd"]) < 0:
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
                pending_after_usd=pending_after + (repriced.pending_after_usd - cached["pending_after_usd"]),
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
            }
        try:
            state = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read whole-share accumulation ledger {path}") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("ticker") != ticker
            or not isinstance(state.get("pending_usd"), (int, float))
            or not isinstance(state.get("events"), list)
        ):
            raise ValueError(f"invalid whole-share accumulation ledger {path}")
        state.setdefault("recorded_idempotency_keys", {})
        state.setdefault("shares_purchased_total", 0)
        state.setdefault("credited_claim_tokens", [])
        return state


__all__ = [
    "WholeShareAccumulationLedger",
    "WholeShareAccumulationDecision",
    "apply_contribution",
    "compute_virtual_contribution_usd",
]
