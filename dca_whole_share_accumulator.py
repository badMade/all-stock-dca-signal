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
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from scripts.file_lock import cross_process_lock

if TYPE_CHECKING:
    from near_close_dca import StrategyConfig

#: How long an unresolved fire (recorded but not yet settle()d/preserve_pending()d)
#: reserves its idempotency key against a second concurrent caller before it is
#: treated as abandoned. Two overlapping scheduler attempts for the SAME slot
#: within this window must not both treat the same cached positive decision as
#: independently actionable -- both could pass the broker same-day check before
#: either places an order, so both could submit the quantity order (or, if both
#: are blocked, both could restore the same deduction and inflate pending_usd)
#: (Codex review: "Claim unresolved fires before returning cached decisions").
#: Long enough to cover the real gap between record_contribution and its
#: settle()/preserve_pending() call within one run (broker round-trip plus
#: several risk-gate checks); short enough that a genuinely crashed process's
#: abandoned claim does not durably strand the deducted pending balance --
#: a retry attempted after this window elapses reclaims the SAME cached fire
#: (recorded_at is caller-supplied, e.g. ctx.now.isoformat(), matching the
#: rest of this module's no-internal-clock convention).
UNRESOLVED_CLAIM_STALE_SECONDS = 300

CENT = Decimal("0.01")


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


def _claim_is_stale(claimed_at: Optional[str], now: Optional[str]) -> bool:
    """Whether an unresolved fire's reservation (see
    ``UNRESOLVED_CLAIM_STALE_SECONDS``) has aged out and may be reclaimed.

    Defaults to NOT stale (still reserved) whenever either timestamp is
    missing or unparseable -- an unresolved fire guarding against a
    duplicate submission is the safer failure mode than silently handing it
    to a second caller; blocking a legitimate retry just delays it, while a
    wrongly-permitted concurrent fire can submit real money twice.
    """
    if not claimed_at or not now:
        return False
    try:
        claimed_dt = datetime.fromisoformat(claimed_at)
        now_dt = datetime.fromisoformat(now)
    except ValueError:
        return False
    try:
        elapsed = (now_dt - claimed_dt).total_seconds()
    except TypeError:
        # One timestamp is naive and the other aware -- cannot compare safely.
        return False
    return elapsed >= UNRESOLVED_CLAIM_STALE_SECONDS


def _claim_can_resolve(cached: dict[str, object], recorded_at: Optional[str]) -> bool:
    """Return whether this caller owns an unresolved cached fire claim."""
    return not bool(cached.get("resolved")) and cached.get("claimed_at") == recorded_at


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

        A cached fire that is UNRESOLVED (recorded but not yet settled or
        preserved) is reserved for the caller who is still acting on it: a
        second, overlapping call for the same key within
        ``UNRESOLVED_CLAIM_STALE_SECONDS`` also gets a zero-shares decision,
        rather than the same actionable fire -- two overlapping scheduler
        attempts must not both be free to submit the quantity order, or both
        independently restore the same deduction (Codex review: "Claim
        unresolved fires before returning cached decisions"). Once that
        window elapses, the claim is treated as abandoned (e.g. the original
        caller crashed before settling) and a retry reclaims the SAME cached
        fire so it can still be completed.
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
                if cached.get("resolved"):
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
                    if contribution_usd <= 0:
                        redispatch = apply_contribution(state["pending_usd"], 0.0, current_share_price_usd)
                        if redispatch.shares_to_buy > 0:
                            cached["resolved"] = False
                            cached["claimed_at"] = recorded_at
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
                if not _claim_is_stale(cached.get("claimed_at"), recorded_at):
                    # Still reserved by whoever recorded it -- do not hand
                    # out the same actionable fire to a second, overlapping
                    # caller. contribution_usd is 0.0, not the ORIGINAL
                    # cached amount -- mirrors the resolved-entry branch's
                    # own identical fix just above: this overlapping call
                    # makes NO new contribution and mutates nothing (the
                    # original caller is still the one actively processing
                    # the actionable fire). Reporting the original positive
                    # amount double-counted it: a caller (see
                    # _process_whole_share_candidate) persists this field as
                    # "what THIS call contributed", so a concurrent retry
                    # within the claim window would otherwise describe a
                    # contribution that never happened on THIS invocation,
                    # double-reporting it in run artifacts while the
                    # original invocation is separately processing the same
                    # actionable fire (Codex review: "Report zero
                    # contribution for actively claimed retries").
                    return WholeShareAccumulationDecision(
                        pending_before_usd=cached["pending_before_usd"],
                        contribution_usd=0.0,
                        current_share_price_usd=cached["current_share_price_usd"],
                        shares_to_buy=0,
                        order_notional_usd=0.0,
                        pending_after_usd=state["pending_usd"],
                    )
                # The claim is stale (abandoned) -- reclaim it for this
                # caller so a genuinely crashed process's fire can still be
                # completed rather than permanently stranding the deducted
                # pending balance. Re-price it against the CURRENT quote
                # (this call's own current_share_price_usd argument), not
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
                )
            pending = state["pending_usd"]
            decision = apply_contribution(pending, contribution_usd, current_share_price_usd)
            state["pending_usd"] = decision.pending_after_usd
            if dedup_key is not None:
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
                    # Only meaningful while unresolved (see _claim_is_stale) --
                    # when a real fire happens, this reserves the key against
                    # a second overlapping caller until settle()/preserve_pending()
                    # resolves it or the claim goes stale.
                    "claimed_at": recorded_at,
                }
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
                if cached is not None and not _claim_can_resolve(cached, recorded_at):
                    # Already resolved by someone else, or this caller no
                    # longer holds the claim -- the deduction being
                    # "restored" here belongs to a different fire attempt
                    # now. Crediting it back would double-count.
                    return state["pending_usd"]
            restored_amount = float(_money(deducted_usd, field="deducted_usd"))
            pending_before = state["pending_usd"]
            pending_after = round(pending_before + restored_amount, 2)
            state["pending_usd"] = pending_after
            if cached is not None:
                cached["resolved"] = True
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
        return _claim_can_resolve(cached, recorded_at)

    def settle(
        self,
        ticker: str,
        trading_day: str,
        shares_purchased: int,
        submitted_notional_usd: float,
        *,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
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
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"{trading_day}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            shortfall = 0.0
            resolved_by_this_call = False
            if dedup_key is not None:
                recorded_keys = state.setdefault("recorded_idempotency_keys", {})
                cached = recorded_keys.get(dedup_key)
                if cached is not None and _claim_can_resolve(cached, recorded_at):
                    planned_shares = int(cached.get("shares_to_buy", 0))
                    cached_price = float(cached.get("current_share_price_usd", 0.0))
                    unbought_shares = max(0, planned_shares - int(shares_purchased))
                    shortfall = round(unbought_shares * cached_price, 2)
                    cached["resolved"] = True
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
            state["shares_purchased_total"] = int(state.get("shares_purchased_total", 0)) + int(shares_purchased)
            state["events"].append(
                {
                    "trading_day": trading_day,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "shares_purchased": int(shares_purchased),
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

        - An ALREADY-RESOLVED entry, or one still within its claim window
          (not yet stale against ``recorded_at``), returns a zero-shares
          decision -- the same "nothing to do" outcome
          :meth:`record_contribution` itself returns for either case.
        - An UNRESOLVED entry that IS stale returns a decision for that
          reclaimed fire, RE-PRICED against ``current_share_price_usd`` (the
          caller's own current quote) rather than the original attempt's
          stale cached price: a real ``record_contribution`` call right now
          would reclaim and reprice it the same way (Codex review: "Reprice
          reclaimed stale fires before placement"), so a preview using the
          stale price could report a share count/notional a real reclaim
          would size differently. ``current_share_price_usd=None`` (a caller
          with no live price on hand) falls back to the cached price
          unchanged, matching the pre-existing behavior. A preview that
          always treats "already recorded" as "$0, no order" is wrong for
          this case -- the retry this preview is FOR will actually attempt
          (and may need budget-tier headroom for) that recovered fire (Codex
          review: "Account for actionable stale claims in tier planning").
        - An ALREADY-RESOLVED entry additionally redispatches -- returns the
          restored balance's own actionable decision instead of the flat
          zero-shares "nothing to do" outcome -- but ONLY when
          ``contribution_usd<=0``, mirroring EXACTLY which real
          ``record_contribution`` calls are eligible to redispatch a
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
            if cached.get("resolved"):
                # Mirrors record_contribution's own resolved-entry redispatch
                # fix (Codex review: "Allow same-day redispatch of preserved
                # fires"), scoped the SAME way: only a zero-contribution
                # retry redispatches. A fresh BUY preview (contribution_usd
                # positive) against a resolved slot must fall through to the
                # flat zero-shares return below, matching what the real
                # record_contribution call would actually do for that same
                # retry (Codex review: "Distinguish fresh BUY retries in
                # cached previews").
                if contribution_usd <= 0:
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
            if not _claim_is_stale(cached.get("claimed_at"), recorded_at):
                # Mirrors record_contribution's own still-reserved-branch fix
                # (Codex review: "Report zero contribution for actively
                # claimed retries"): this preview call makes no new
                # contribution either, so contribution_usd must be 0.0 here
                # too, not the original cached amount.
                return WholeShareAccumulationDecision(
                    pending_before_usd=cached["pending_before_usd"],
                    contribution_usd=0.0,
                    current_share_price_usd=cached["current_share_price_usd"],
                    shares_to_buy=0,
                    order_notional_usd=0.0,
                    pending_after_usd=pending_after,
                )
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

    def shares_purchased_total(self, ticker: str) -> int:
        """Return the running count of whole shares bought via this ledger."""
        symbol = ticker.strip().upper()
        if not symbol or not _SYMBOL_PATTERN.match(symbol):
            return 0
        day_dir = self.root / symbol
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, symbol)
            return int(state.get("shares_purchased_total", 0))

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
        return state


__all__ = [
    "WholeShareAccumulationLedger",
    "WholeShareAccumulationDecision",
    "apply_contribution",
    "compute_virtual_contribution_usd",
]
