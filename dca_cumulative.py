"""Cent-precise, same-day order-credit math for the DCA streams.

Broker orders must be at least one dollar, but the per-ticker model can produce
an incremental top-up below that amount.  Those increments are *credits*, not
orders: they accumulate within one trading day and never become negative.  At
the final 3:30 PM ET run, totals below 50 cents are discarded, totals from 50
cents through 99 cents become exactly $1, and larger totals retain cent
precision.

This module deliberately contains only deterministic math.  Stream executors
remain responsible for their normal price, spread, budget, and broker checks
before submitting an order returned by this policy.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, NamedTuple, Optional

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from scripts.file_lock import cross_process_lock

_log = logging.getLogger(__name__)

MINIMUM_ORDER_USD = Decimal("1.00")
ROUND_UP_THRESHOLD_USD = Decimal("0.50")
CENT = Decimal("0.01")


class MinimumOrderDiscard(NamedTuple):
    """Outcome of one final-session minimum-order discard attempt.

    ``removed_usd`` is what THIS call cleared under its own lock -- 0.0 when
    the balance was already absent, so nothing of this call's may later be
    restored. ``refused`` is True when a concurrent update meant the ledger
    was left untouched entirely. ``revision`` is the ledger's mutation
    counter as observed by this call, the compare-and-swap token for a later
    rollback.
    """

    removed_usd: float
    revision: int
    refused: bool


@dataclass(frozen=True)
class CumulativeCreditDecision:
    """The result of applying one qualified top-up increment.

    ``order_amount_usd`` is zero when the credit remains pending.  A positive
    value is always at least one dollar.  ``pending_after_usd`` is never
    negative: a 50-cent credit that qualifies for a $1 order is cleared rather
    than carrying a synthetic negative 50-cent balance forward.
    """

    pending_before_usd: float
    incremental_credit_usd: float
    order_amount_usd: float
    pending_after_usd: float
    final_session: bool


@dataclass(frozen=True)
class CumulativeCreditProvenanceSnapshot:
    """One locked view of a ticker's balance and one slot's provenance."""

    pending_usd: float
    preserved_credit_usd: float
    settled_credit_usd: float
    # Whether the slot's preserve_pending() dedup key exists AT ALL,
    # regardless of its current outstanding amount -- the same distinction
    # :meth:`CumulativeCreditLedger.has_preserved_entry_for_slot` draws
    # (a zeroed-but-existing entry still needs reconciling rather than a
    # fresh preserve_pending() call that its own dedup would swallow).
    # Read under the same lock as the three amounts so a retry's
    # reconcile-vs-skip decision cannot straddle a concurrent write.
    has_preserved_entry: bool = False


_DECISION_FIELDS = (
    "pending_before_usd",
    "incremental_credit_usd",
    "order_amount_usd",
    "pending_after_usd",
    "final_session",
)


def _decision_fields(cached: dict[str, Any]) -> dict[str, Any]:
    """Filter a cached ``recorded_idempotency_keys`` entry to decision fields.

    A cached entry also carries ledger-internal bookkeeping (e.g.
    ``sequence``) that is not part of :class:`CumulativeCreditDecision`'s
    shape and would otherwise raise a ``TypeError`` when reconstructing one.
    """
    return {field: cached[field] for field in _DECISION_FIELDS}


def _next_sequence(state: dict[str, Any]) -> int:
    """Return the next monotonic sequence number for this ledger, advancing it.

    Stamped onto each ``record()``/``preserve_pending()`` cache entry so
    :meth:`CumulativeCreditLedger.settle` can recover true preservation order
    even though the ledger is durably written with ``sort_keys=True`` and
    therefore re-read in lexicographic key order, not chronological order
    (Codex review: "Preserve chronological credit settlement order" -- a
    10:30 ``morning...`` preserve sorts AFTER a 12:30 ``midday...`` preserve
    purely by key text, which would otherwise consume the wrong, later
    entry first on a partial settlement).
    """
    sequence = int(state.get("next_sequence", 0))
    state["next_sequence"] = sequence + 1
    return sequence


class CumulativeCreditLedger:
    """Durably retain same-day pending credits and their decision history.

    One compact JSON ledger is written per trading day.  It contains both the
    pending balance by ticker and an append-only-in-practice event history, so
    a later analysis can explain *why* an order was submitted, deferred, or
    rounded at the final 3:30 PM ET session.  Updates are serialized and
    atomically written; a simultaneous stream cannot lose another stream's
    credit or logging record.
    """

    def __init__(self, root: Optional[str | Path] = None) -> None:
        self.root = Path(root) if root is not None else Path(stream_root("cumulative_dca_credits"))

    def record(
        self,
        ticker: str,
        trading_day: str,
        incremental_credit_usd: float,
        *,
        final_session: bool = False,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        pending_baseline_usd: Optional[float] = None,
    ) -> CumulativeCreditDecision:
        """Apply a credit and retain a structured event for it.

        ``trading_day`` is an ISO-8601 date (``YYYY-MM-DD``).  Keeping each
        day separate guarantees that no positive or negative balance can leak
        into a later trading day.

        ``idempotency_key`` should identify one slot's single logical firing
        (e.g. a scheduled run's dated state-directory name, which is stable
        across retries of that same slot but differs for a new day or a
        different slot/stream). When provided, a second ``record()`` call for
        the same ``(ticker, idempotency_key)`` pair is a no-op that returns the
        FIRST call's decision unchanged, instead of applying the credit again.
        This closes a gap where a process crash after this method durably
        wrote the ledger but before the caller's own run-state artifact was
        persisted left a scheduler retry with no artifact-based way to detect
        the already-recorded decline, double-applying it (Codex review: "store
        a slot/ticker idempotency key in the ledger rather than relying only
        on the later run artifact"). ``None`` (the default) disables this
        check, preserving the original always-apply behavior for callers that
        cannot supply a stable key.

        ``pending_baseline_usd``, when provided together with
        ``idempotency_key``, signals that a retry of a slot which earlier
        called :meth:`preserve_pending` wants its OWN prior contribution
        netted out of the ledger's balance before applying a fresh,
        possibly SMALLER decline for this same slot -- otherwise this call
        would silently combine the fresh decline with the ledger's raw
        (still includes this slot's stale, larger contribution) balance and
        could cross the order threshold purely from the retry, clearing the
        pending balance even though no broker order was actually submitted
        this run (Codex review: "Do not re-record adjusted credits against
        the full ledger").

        The caller's own pre-lock ``pending_usd()`` /
        ``preserved_credit_for_slot()`` reads (whose difference is
        typically passed here) happen BEFORE this call acquires the ledger
        lock, so a concurrent stream can record/settle/preserve this same
        ticker in between; using that stale snapshot as an absolute
        replacement value would silently discard the concurrent update
        despite the cross-process lock. So whenever ``idempotency_key`` is
        also provided, the netted baseline is instead RECOMPUTED here,
        atomically, from the state already loaded under this call's own
        lock: this slot's own unconsumed preserved contribution (looked up
        fresh by ``idempotency_key``) is subtracted from the ledger's
        CURRENT balance, not the value the caller computed earlier (Codex
        review: "Rebase retry adjustments inside the ledger lock"). The
        caller-supplied ``pending_baseline_usd`` is used verbatim only when
        ``idempotency_key`` is omitted, since there is then no slot key to
        rebase against. ``None`` (the default) preserves the original
        read-the-ledger's-own-balance behavior.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required for a cumulative credit")
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"record:{symbol}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            if dedup_key is not None and dedup_key in recorded_keys:
                cached = recorded_keys[dedup_key]
                return CumulativeCreditDecision(**_decision_fields(cached))
            rebased = False
            if idempotency_key:
                # Rebase atomically against the state just loaded under this
                # call's own lock instead of trusting the caller's pre-lock
                # snapshot (see the docstring above). This netting applies
                # whenever an idempotency_key is present and the ledger holds
                # a matching unconsumed preserve_pending() entry for it --
                # NOT only when the caller also happened to pass
                # pending_baseline_usd. Skipping the netting whenever the
                # (optional) baseline was omitted was a regression versus the
                # pre-PR parent behavior, which always netted the matching
                # preserved contribution for the same idempotency key: a
                # retry that calls record() without recomputing its own
                # baseline would otherwise double-count its earlier
                # preserve_pending() contribution against the ledger's raw
                # balance (Codex review: "Rebase retries even without an
                # explicit pending_baseline_usd").
                preserve_key = f"preserve:{symbol}:{idempotency_key}"
                cached_preserve = recorded_keys.get(preserve_key)
                own_contribution = (
                    0.0
                    if cached_preserve is None or cached_preserve.get("consumed_by_settlement")
                    else float(cached_preserve.get("incremental_credit_usd", 0.0))
                )
                # `rebased` (which later suppresses this call from ever
                # returning an order below) must track whether this call
                # actually had something to rebase against -- either a
                # preserved contribution that was netted out above, or an
                # explicit `pending_baseline_usd` from a caller that is
                # documented to never inspect the return value (see the
                # docstring) -- not merely whether an idempotency_key was
                # supplied. A plain record() retry with an idempotency_key
                # but no matching preserve_pending() entry AND no explicit
                # baseline has nothing to rebase against, so treating it as
                # rebased would wrongly convert every threshold-crossing
                # decision into pending credit and starve this call of the
                # order the policy actually earned (Codex review: "Limit
                # rebase suppression to preserved retries" /
                # "Rebase only when a matching preserved credit exists").
                rebased = own_contribution > 0.0 or pending_baseline_usd is not None
                pending = max(
                    0.0,
                    round(state["pending_usd_by_ticker"].get(symbol, 0.0) - own_contribution, 2),
                )
            elif pending_baseline_usd is not None:
                # No idempotency_key to rebase against: the caller-supplied
                # baseline is used verbatim, exactly as documented.
                pending = float(pending_baseline_usd)
            else:
                pending = state["pending_usd_by_ticker"].get(symbol, 0.0)
            decision = apply_cumulative_credit(
                pending, incremental_credit_usd, final_session=final_session
            )
            # Track the subset whose provenance is specifically the broker's
            # minimum-order threshold.  preserve_pending() deliberately does
            # not contribute here: it is used after spread, budget, duplicate,
            # tradability, buying-power, review, and placement failures, none
            # of which the 15:30 minimum-order policy may turn into a buy.
            minimum_pending = state.setdefault("minimum_order_pending_usd_by_ticker", {})
            qualifying_before = float(minimum_pending.get(symbol, 0.0))
            qualifying_decision = apply_cumulative_credit(
                qualifying_before, incremental_credit_usd, final_session=final_session
            )
            if rebased and not final_session and decision.order_amount_usd > 0:
                # The only production call site for the rebase branch above
                # (morning_dca.py's stays-pending write) is reached ONLY
                # after the caller has already committed, using its own
                # (potentially staler) baseline, to placing no order this
                # run -- it never inspects this method's return value,
                # relying on the documented invariant that this call always
                # yields order_amount_usd == 0. The atomic rebase can now see
                # a fresher, larger balance (e.g. a concurrent stream's
                # contribution the caller's own decision never knew about)
                # that crosses the regular-session threshold on its own;
                # silently honoring that here would clear the pending
                # balance as though an order were submitted when none will
                # be, permanently losing the credit (Codex review: "Prevent
                # rebasing from creating an unsubmitted order"). Keep the
                # full combined total pending instead -- exactly as
                # :meth:`preserve_pending` would -- and defer the
                # order-or-not decision to whichever later evaluation (a
                # retry of this slot or a later slot) actually has the
                # opportunity to place an order.
                total = round(pending + incremental_credit_usd, 2)
                decision = CumulativeCreditDecision(
                    pending_before_usd=pending,
                    incremental_credit_usd=incremental_credit_usd,
                    order_amount_usd=0.0,
                    pending_after_usd=total,
                    final_session=final_session,
                )
                # The minimum-order-provenance pool must be rebased the exact
                # same way as the main pending balance above: qualifying_decision
                # was computed against the same rebase-vulnerable baseline as
                # the original (now-overridden) `decision`, so leaving it
                # un-rebased would still treat this call's qualifying
                # contribution as consumed by an order that this override just
                # decided will NOT happen (Codex review discussion_r3793071617:
                # "Preserve minimum provenance after a rebased threshold
                # crossing" -- e.g. an existing $0.30 plus two concurrently
                # evaluated $0.10 credits leaves $0.50 pending but no
                # qualifying ticker without this correction).
                qualifying_total = round(qualifying_before + incremental_credit_usd, 2)
                qualifying_decision = CumulativeCreditDecision(
                    pending_before_usd=qualifying_before,
                    incremental_credit_usd=incremental_credit_usd,
                    order_amount_usd=0.0,
                    pending_after_usd=qualifying_total,
                    final_session=final_session,
                )
            if qualifying_decision.pending_after_usd > 0:
                minimum_pending[symbol] = qualifying_decision.pending_after_usd
            else:
                minimum_pending.pop(symbol, None)
            if decision.pending_after_usd > 0:
                state["pending_usd_by_ticker"][symbol] = decision.pending_after_usd
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
            if dedup_key is not None:
                recorded_keys[dedup_key] = {
                    "pending_before_usd": decision.pending_before_usd,
                    "incremental_credit_usd": decision.incremental_credit_usd,
                    "order_amount_usd": decision.order_amount_usd,
                    "pending_after_usd": decision.pending_after_usd,
                    "final_session": decision.final_session,
                    "sequence": _next_sequence(state),
                }
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "final_session": final_session,
                    "pending_before_usd": decision.pending_before_usd,
                    "incremental_credit_usd": decision.incremental_credit_usd,
                    "order_amount_usd": decision.order_amount_usd,
                    "pending_after_usd": decision.pending_after_usd,
                    "decision": (
                        "order"
                        if decision.order_amount_usd > 0
                        else "discarded"
                        if final_session and decision.pending_before_usd + decision.incremental_credit_usd > 0
                        else "pending"
                    ),
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            self._write_state(state_path, state)
        return decision

    def preserve_pending(
        self,
        ticker: str,
        trading_day: str,
        additional_credit_usd: float,
        *,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> float:
        """Add an unresolved decline to the pending balance without deciding
        whether it now crosses the order threshold.

        Used when a candidate's combined pending-plus-this-run credit (as
        computed by :func:`apply_cumulative_credit` during evaluation)
        crossed the order threshold -- the caller planned to submit an order
        for it -- but placement never actually happened this run, blocked by
        a per-order ceiling, the shared attempt cap, a duplicate-check error,
        the daily/weekly cap, the spread gate, tradability, buying power,
        review, or the broker call itself. Unlike :meth:`record`, this never
        rounds up to an order or clears the pending balance: since no order
        was actually confirmed submitted, the combined balance must stay
        FULLY pending for a later attempt (a retry of this slot, a later
        slot, or the 3:30 PM final session) to decide again from scratch --
        exactly as if this run's own new decline had simply stayed pending on
        its own (Codex review: "Preserve threshold-crossing credits when
        placement is blocked" -- persist the combined pending credit on
        non-submission paths, clearing it only after confirmed placement via
        :meth:`settle`).

        Returns the new pending total.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required for a cumulative credit")
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        dedup_key = f"preserve:{symbol}:{idempotency_key}" if idempotency_key else None
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            if dedup_key is not None and dedup_key in recorded_keys:
                existing = recorded_keys[dedup_key]
                if not existing.get("consumed_by_settlement"):
                    return float(existing["pending_after_usd"])
                # else: this key's earlier contribution was already fully
                # paid out by a LATER slot's settle() (see settle()'s
                # chronological preserve consumption) -- preserved_credit_
                # for_slot() and recorded_decision() both already correctly
                # treat that as "nothing left to track", but returning the
                # STALE pending_after_usd here would silently drop THIS
                # call's brand new decline instead of adding it to the
                # ledger, permanently losing it: the dedup key still exists,
                # but the outstanding amount it once described is zero, and
                # a plain "key exists" check cannot tell those apart (Codex
                # review discussion_r3838839294: "Reconcile zeroed preserve
                # entries before re-preserving"). Fall through and treat
                # this as a fresh preserve cycle for the same slot.
            pending_before = float(state["pending_usd_by_ticker"].get(symbol, 0.0))
            pending_after = round(pending_before + additional_credit_usd, 2)
            if pending_after > 0:
                state["pending_usd_by_ticker"][symbol] = pending_after
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
            if dedup_key is not None:
                recorded_keys[dedup_key] = {
                    "pending_before_usd": pending_before,
                    "incremental_credit_usd": additional_credit_usd,
                    "pending_after_usd": pending_after,
                    "sequence": _next_sequence(state),
                    # A fresh preserve cycle: the PREVIOUS cycle's settled
                    # amount was already fully paid and is done, not part of
                    # this new, unrelated decline's own provenance.
                    "settled_credit_usd": 0.0,
                }
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "final_session": False,
                    "pending_before_usd": pending_before,
                    "incremental_credit_usd": additional_credit_usd,
                    "order_amount_usd": 0.0,
                    "pending_after_usd": pending_after,
                    "decision": "preserved_pending",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            self._write_state(state_path, state)
        return pending_after

    def recorded_decision(
        self, ticker: str, trading_day: str, idempotency_key: Optional[str]
    ) -> Optional[CumulativeCreditDecision]:
        """Return the decision an earlier :meth:`record` call already made for
        ``(ticker, idempotency_key)`` this trading day, or ``None`` if it was
        never recorded.

        Lets a caller check retry-idempotency BEFORE recomputing this slot's
        incremental credit from current price data. Without this, a caller
        that reads :meth:`pending_usd` (already reflecting an earlier crashed
        attempt's persisted decline) and independently recomputes the SAME
        slot's decline via :func:`apply_cumulative_credit` can combine the two
        and cross the order threshold purely from being retried -- and
        because the combined total is now positive, the caller's own
        stays-pending branch (the only one that passes ``idempotency_key`` to
        :meth:`record`) is skipped entirely, so ``record``'s own dedup check
        never runs (Codex review: "Apply retry idempotency before
        recomputing the credit" -- "check the slot key before applying the
        current decline"). Callers should look this up first and, when it
        returns a decision, reuse it unchanged instead of calling
        :func:`apply_cumulative_credit` again for this slot.
        """
        if not idempotency_key:
            return None
        symbol = ticker.strip().upper()
        if not symbol:
            return None
        record_key = f"record:{symbol}:{idempotency_key}"
        preserve_key = f"preserve:{symbol}:{idempotency_key}"
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            recorded_keys = state.get("recorded_idempotency_keys", {})
            cached = recorded_keys.get(record_key)
            if cached is not None:
                return CumulativeCreditDecision(**_decision_fields(cached))
            preserved = recorded_keys.get(preserve_key)
            if preserved is not None and "settled_credit_usd" not in preserved:
                # Ledgers written before partial-settlement accounting cannot
                # distinguish an untouched preserve from a reduced remainder.
                # Retrying it could therefore buy already-paid credit.  Fail
                # closed; newly written preserves always carry the field.
                return CumulativeCreditDecision(
                    pending_before_usd=preserved.get("pending_before_usd", 0.0),
                    incremental_credit_usd=preserved.get("incremental_credit_usd", 0.0),
                    order_amount_usd=0.0,
                    pending_after_usd=0.0,
                    final_session=False,
                )
            if preserved is not None and preserved.get("consumed_by_settlement"):
                # A LATER slot's settle() already paid out this ticker's
                # combined balance, which included what THIS slot preserved
                # here. This slot's own contribution is therefore fully
                # resolved, not merely still-blocked-and-retriable: unlike
                # the ordinary preserved-but-unconsumed case (see
                # preserved_credit_for_slot), recomputing a fresh decline for
                # this slot now would evaluate against a decline that has
                # already been bought, and no same-stream duplicate check
                # would catch a cross-stream settlement (Codex review:
                # "Prevent settled credits from reviving on old-slot
                # retries" -- "track whether the preserved contribution has
                # already been consumed by settlement"). Report this slot as
                # fully decided with nothing further to contribute, exactly
                # like a stays-pending record() decision.
                return CumulativeCreditDecision(
                    pending_before_usd=preserved.get("pending_before_usd", 0.0),
                    incremental_credit_usd=preserved.get("incremental_credit_usd", 0.0),
                    order_amount_usd=0.0,
                    pending_after_usd=0.0,
                    final_session=False,
                )
            return None

    def preserved_credit_for_slot(
        self, ticker: str, trading_day: str, idempotency_key: Optional[str]
    ) -> float:
        """Return how much of the CURRENT pending balance THIS slot's own
        earlier (blocked) attempt already contributed via :meth:`preserve_pending`,
        or ``0.0`` if it never called it.

        A caller re-evaluating this ticker on a retry reads :meth:`pending_usd`
        for its baseline, but that durable balance already includes whatever
        THIS SAME slot preserved on an earlier, blocked attempt. Recomputing a
        fresh decline for this slot and combining it with that balance as-is
        would double-count this slot's own contribution -- unlike the
        stays-pending case :meth:`recorded_decision` guards, a blocked
        placement must still be free to retry (a later gate check might now
        pass), so it cannot simply reuse a cached final decision (Codex
        review: "Recognize preserved credits before recomputing the slot").
        Callers should subtract this from :meth:`pending_usd`'s result before
        passing it to :func:`apply_cumulative_credit` for a fresh decline.
        """
        if not idempotency_key:
            return 0.0
        symbol = ticker.strip().upper()
        if not symbol:
            return 0.0
        dedup_key = f"preserve:{symbol}:{idempotency_key}"
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            recorded_keys = state.get("recorded_idempotency_keys", {})
            cached = recorded_keys.get(dedup_key)
            if cached is None or cached.get("consumed_by_settlement"):
                # A consumed entry's contribution has already been paid out
                # by a later slot's settle() -- it is no longer part of the
                # CURRENT pending balance, so there is nothing left for a
                # caller to subtract. (recorded_decision short-circuits this
                # case entirely before a caller would even reach this call.)
                return 0.0
            return float(cached.get("incremental_credit_usd", 0.0))

    def has_preserved_entry_for_slot(
        self, ticker: str, trading_day: str, idempotency_key: Optional[str]
    ) -> bool:
        """True if THIS slot's own :meth:`preserve_pending` dedup key already
        exists in the ledger, regardless of its CURRENT outstanding amount.

        Distinct from :meth:`preserved_credit_for_slot`, which collapses two
        different situations into the same ``0.0``: a slot that never called
        :meth:`preserve_pending` at all, and one whose entry was legitimately
        reconciled down to zero (e.g. a temporary price recovery via
        :meth:`reconcile_preserved_credit`) but still EXISTS. A caller
        deciding whether to reconcile-vs-skip on a retry must tell these
        apart: gating on ``preserved_credit_for_slot() > 0`` alone treats a
        zeroed-but-existing entry as "no preserve to reconcile," so if this
        retry's price declines again and recomputes a positive,
        threshold-crossing, again-blocked credit, the caller falls through to
        a fresh :meth:`preserve_pending` call with the SAME idempotency key --
        whose own dedup check finds the (zeroed, stale) entry already present
        and returns early WITHOUT adding the revived decline to the shared
        balance, silently losing it (Codex review, PR #656 discussion_
        r3838839294: "Reconcile zeroed preserve entries before
        re-preserving"). Callers should reconcile whenever this returns
        True, not only when :meth:`preserved_credit_for_slot` is positive.
        """
        if not idempotency_key:
            return False
        symbol = ticker.strip().upper()
        if not symbol:
            return False
        dedup_key = f"preserve:{symbol}:{idempotency_key}"
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            recorded_keys = state.get("recorded_idempotency_keys", {})
            return dedup_key in recorded_keys

    def reconcile_preserved_credit(
        self,
        ticker: str,
        trading_day: str,
        idempotency_key: str,
        outstanding_credit_usd: float,
        *,
        expected_settled_credit_usd: float = 0.0,
        expected_outstanding_credit_usd: float,
        expected_pending_usd: Optional[float] = None,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
    ) -> float:
        """Replace a slot's outstanding preserved contribution atomically.

        A retry can recompute a different decline after prices move.  Updating
        both the preserve entry and the shared pending balance keeps that
        amount in preserve provenance instead of duplicating it through
        :meth:`record`.  Returns the ticker's new total pending balance.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required for a cumulative credit")
        dedup_key = f"preserve:{symbol}:{idempotency_key}"
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        with cross_process_lock(day_dir / "ledger.lock"):
            state = self._read_state(state_path, trading_day)
            cached = state.setdefault("recorded_idempotency_keys", {}).get(dedup_key)
            if cached is None:
                return float(state["pending_usd_by_ticker"].get(symbol, 0.0))
            if "settled_credit_usd" not in cached:
                raise ValueError("cannot safely reconcile a legacy preserved credit")
            current_settled = round(float(cached["settled_credit_usd"]), 2)
            if current_settled != round(float(expected_settled_credit_usd), 2):
                raise RuntimeError("preserved credit changed during reconciliation")
            # A consumed entry's raw incremental_credit_usd is the OLD,
            # already-paid amount (settle() leaves it unchanged on full
            # consumption) -- callers compare against what
            # preserved_credit_for_slot() reports them, which is 0.0 for
            # any consumed entry regardless of that stale field. Match that
            # here or a caller correctly reading 0.0 outstanding would
            # always mismatch and raise before ever reaching the
            # consumed-entry handling below.
            current_outstanding = (
                0.0
                if cached.get("consumed_by_settlement")
                else round(float(cached.get("incremental_credit_usd", 0.0)), 2)
            )
            if current_outstanding != round(float(expected_outstanding_credit_usd), 2):
                raise RuntimeError("preserved credit changed during reconciliation")
            if expected_pending_usd is not None:
                # The caller's order-vs-stays-pending decision was already
                # made from a pending_usd() snapshot read before this lock
                # was acquired. The two checks above only prove THIS
                # preserve entry is unchanged; a concurrent stream can still
                # settle or record an UNRELATED contribution for the same
                # ticker in between, moving the shared balance out from
                # under that earlier decision without touching either
                # compared preserve field (Codex review: "Revalidate the
                # shared pending balance before ordering"). Reject the
                # reconciliation so the caller recomputes from scratch
                # rather than acting on a decision based on a stale total.
                current_pending = round(float(state["pending_usd_by_ticker"].get(symbol, 0.0)), 2)
                if current_pending != round(float(expected_pending_usd), 2):
                    raise RuntimeError("preserved credit changed during reconciliation")
            already_consumed = bool(cached.get("consumed_by_settlement"))
            if already_consumed:
                # This entry's earlier contribution was already fully paid
                # out by a LATER slot's settle() (see settle()'s
                # chronological preserve consumption) -- returning early
                # here without applying outstanding_credit_usd (the prior
                # behavior) silently drops a freshly recomputed decline
                # entirely: if a concurrent stream fully settles this
                # preserve between _compute_top_up's initial recorded_
                # decision() check and its later reads, this call's own
                # outstanding_credit_usd (a genuine NEW retry-time decline,
                # e.g. the price fell again after a paid $0.50 preserve)
                # is never added to the pending balance (Codex review, PR
                # #656 follow-up: "Retry reconciliation when the preserve
                # was consumed"). Treat this as a fresh preserve cycle
                # instead, mirroring preserve_pending()'s own handling of a
                # consumed-but-still-present dedup key: cached[
                # "incremental_credit_usd"] here is the OLD, already-paid
                # amount, not still-outstanding credit to net against, so
                # `previous` is reset to 0 below rather than read from it.
                if round(max(0.0, float(outstanding_credit_usd)), 2) <= 0:
                    return float(state["pending_usd_by_ticker"].get(symbol, 0.0))
                cached["consumed_by_settlement"] = False
                cached["settled_credit_usd"] = 0.0
                # settle() consumes preserves in `sequence` order -- retaining
                # this entry's original (already-paid) sequence would order
                # this genuinely NEW retry-time decline ahead of credits
                # preserved after the old cycle finished, so a later partial
                # settlement could be misattributed to this revived entry
                # instead of the older outstanding one (Codex review, PR #656
                # follow-up: "Restamp revived preserves with a new sequence").
                # Mirror preserve_pending()'s own consumed-entry handling,
                # which stamps a fresh sequence for the same reason.
                cached["sequence"] = _next_sequence(state)
            previous = 0.0 if already_consumed else float(cached.get("incremental_credit_usd", 0.0))
            outstanding = round(max(0.0, float(outstanding_credit_usd)), 2)
            pending_before = float(state["pending_usd_by_ticker"].get(symbol, 0.0))
            pending_after = round(max(0.0, pending_before - previous + outstanding), 2)
            cached["incremental_credit_usd"] = outstanding
            # A zeroed recomputation here means the current price no longer
            # shows a decline for this slot, NOT that the credit was paid --
            # only settle() may set consumed_by_settlement, since only it
            # confirms an actual submitted order (Codex review: "Keep zeroed
            # reconciliations retriable"). Marking this terminal would make
            # recorded_decision() short-circuit future retries at $0 even
            # after the price falls again and a genuine decline reappears.
            if pending_after > 0:
                state["pending_usd_by_ticker"][symbol] = pending_after
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "final_session": False,
                    "pending_before_usd": pending_before,
                    "incremental_credit_usd": round(outstanding - previous, 2),
                    "order_amount_usd": 0.0,
                    "pending_after_usd": pending_after,
                    "decision": "reconciled_preserved_pending",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            self._write_state(state_path, state)
            return pending_after

    def settled_credit_for_slot(
        self, ticker: str, trading_day: str, idempotency_key: Optional[str]
    ) -> float:
        """Return the portion of this slot's preserved decline already paid.

        A partially settled preserve entry remains retriable, but a retry must
        deduct the paid portion from its freshly recomputed decline.  Merely
        shrinking the entry's outstanding contribution is insufficient: the
        caller also removes that contribution from the shared pending balance
        before recomputing the slot, which would otherwise recreate the paid
        credit.
        """
        if not idempotency_key:
            return 0.0
        symbol = ticker.strip().upper()
        if not symbol:
            return 0.0
        dedup_key = f"preserve:{symbol}:{idempotency_key}"
        day_dir = self.root / trading_day
        with cross_process_lock(day_dir / "ledger.lock"):
            state = self._read_state(day_dir / "ledger.json", trading_day)
            cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
            if cached is None:
                return 0.0
            return float(cached.get("settled_credit_usd", 0.0))

    def provenance_snapshot(
        self, ticker: str, trading_day: str, idempotency_key: Optional[str]
    ) -> CumulativeCreditProvenanceSnapshot:
        """Return pending, preserved, and settled credit from one ledger lock.

        Retry calculations must use these three related values from the same
        state revision.  Reading them separately permits a concurrent
        settlement to mix a stale pending balance with newer provenance and
        recreate credit that has already been paid.

        ``has_preserved_entry`` rides along from the same read: it is the
        :meth:`has_preserved_entry_for_slot` answer (True for a zeroed or
        consumed entry too), so a retry's reconcile-vs-skip decision is made
        against the same state revision as the amounts it reconciles.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            return CumulativeCreditProvenanceSnapshot(0.0, 0.0, 0.0, has_preserved_entry=False)
        day_dir = self.root / trading_day
        with cross_process_lock(day_dir / "ledger.lock"):
            state = self._read_state(day_dir / "ledger.json", trading_day)
            pending = float(state["pending_usd_by_ticker"].get(symbol, 0.0))
            cached = None
            if idempotency_key:
                dedup_key = f"preserve:{symbol}:{idempotency_key}"
                cached = state.get("recorded_idempotency_keys", {}).get(dedup_key)
            if cached is None:
                return CumulativeCreditProvenanceSnapshot(pending, 0.0, 0.0, has_preserved_entry=False)
            preserved = 0.0 if cached.get("consumed_by_settlement") else float(
                cached.get("incremental_credit_usd", 0.0)
            )
            return CumulativeCreditProvenanceSnapshot(
                pending,
                preserved,
                float(cached.get("settled_credit_usd", 0.0)),
                has_preserved_entry=True,
            )

    def pending_usd(self, ticker: str, trading_day: str) -> float:
        """Return this ticker's current pending same-day credit, or 0.0.

        Read-only -- callers wanting to consider an earlier session's
        not-yet-ordered decline (e.g. the final 3:30 PM session's own
        cumulative-credit computation, which must flush ANY positive pending
        balance even when its own decision price shows no further decline)
        should read this value first and feed it into
        :func:`apply_cumulative_credit` themselves, rather than assuming a
        fresh 0.0 balance (Codex review: "Accumulate credits instead of
        resetting pending state").
        """
        symbol = ticker.strip().upper()
        if not symbol:
            return 0.0
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            return float(state["pending_usd_by_ticker"].get(symbol, 0.0))

    def pending_tickers(self, trading_day: str) -> dict[str, float]:
        """Return every ticker with a positive pending balance on ``trading_day``.

        Lets a caller enumerate credits independently of whatever candidate
        list it built for other reasons (e.g. the final 3:30 PM session's own
        buy-candidate list, which only contains tickers whose OWN decision is
        BUY). A ticker that earned a pending credit earlier but whose final
        decision is not BUY -- e.g. its price recovered to or above the
        previous close -- would otherwise never have its ledger balance
        inspected at all, permanently stranding it (Codex review: "Flush
        credits even when the final signal is not BUY").
        """
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            return {
                ticker: float(amount)
                for ticker, amount in state["pending_usd_by_ticker"].items()
                if float(amount) > 0
            }

    def minimum_order_pending_tickers(self, trading_day: str) -> dict[str, float]:
        """Return credits deferred solely because they were below broker minimum.

        This provenance-filtered view is the only balance the 15:30 aggregate
        top-up may flush.  Legacy ledgers without the field fail closed and
        return no qualifying credits rather than reclassifying unrelated
        placement blocks as broker-minimum blocks.
        """
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            return {
                ticker: float(amount)
                for ticker, amount in state.get("minimum_order_pending_usd_by_ticker", {}).items()
                if float(amount) > 0
            }

    def discard_minimum_order_pending(
        self, ticker: str, trading_day: str, observed_amount_usd: Optional[float] = None
    ) -> bool:
        """Forget a sub-threshold minimum-order credit at the final session.

        The caller typically decides to discard based on a balance it read
        BEFORE acquiring this call's own lock (e.g. the 15:30 final pass's
        pre-scan of every pending ticker). A concurrent stream can raise that
        balance to or above the discard threshold in between -- e.g. a
        stale-baseline ``record()`` retry landing after the scan but before
        this call's lock. Blindly popping whatever is present under the lock
        would then silently discard a balance that should have produced an
        order instead (Codex review discussion on commit 0af4768: "Guard
        final discards against concurrent credit updates"). Re-check the
        CURRENT balance, under this call's own lock, against both the observed
        amount and the same final-session discard threshold before clearing
        it; refuse (and leave the ledger untouched) if it changed or no longer
        qualifies. The exact-amount comparison also preserves a concurrent
        credit that increases one sub-threshold balance to another (for
        example, $0.20 to $0.30).

        ``observed_amount_usd`` is the caller's pre-lock reading. A DIFFERENT
        current balance is not on its own a reason to refuse: what matters is
        only whether the fresh amount would now produce an order. A
        concurrent contribution that moves a credit but leaves it below the
        threshold ($0.10 -> $0.20) still needs discarding, and refusing there
        stranded it -- both callers read False as "it became actionable" and
        neither can route a balance with no order in it (Codex review: "Retry
        discards when the balance remains sub-threshold"). The snapshot is
        therefore only logged when it differs; the threshold re-check below
        is the sole veto, and it runs against the amount read under this lock.

        Returns True if the balance was discarded, False if the current
        balance now QUALIFIES FOR AN ORDER (nothing is changed in that case --
        the caller re-evaluates so the now-qualifying order can be placed
        rather than left stranded after the final pass).

        True deliberately covers BOTH "this call removed the balance" and
        "the balance was already gone" -- for a caller that only needs to
        know whether anything is left to discard, those are the same answer.
        A caller that intends to UNDO the discard later (restore the credit
        if a required post-discard check cannot complete) must not use this
        bool: it cannot tell "I removed $0.10" apart from "a concurrent retry
        already settled that $0.10 before my lock", and restoring in the
        second case resurrects credit a real order already consumed. Use
        :meth:`discard_minimum_order_pending_amount` for that (Codex review,
        PR #680 discussion_r3925291346: "Restore only credit this call
        actually discarded").
        """
        return (
            self.discard_minimum_order_pending_amount(ticker, trading_day, observed_amount_usd)
            is not None
        )

    def discard_minimum_order_pending_amount(
        self, ticker: str, trading_day: str, observed_amount_usd: Optional[float] = None
    ) -> float | None:
        """:meth:`discard_minimum_order_pending`, reporting what was removed.

        Same locking, same observed-amount logging and threshold re-check,
        same ledger write. The return value distinguishes the three outcomes
        the bool form collapses to two:

        * a positive float -- THIS call cleared exactly that much from the
          ledger under its own lock (the FRESH amount read under the lock,
          which is what actually left the pool, not the caller's possibly
          stale ``observed_amount_usd``);
        * ``0.0`` -- nothing was left to clear (the balance was already
          absent, e.g. a concurrent retry settled it first); the ledger was
          not written;
        * ``None`` -- refused: a concurrent update raised the balance to
          order-qualifying; the ledger was left untouched.

        Only the positive case is safe to hand to
        :meth:`restore_minimum_order_pending` if a required post-discard
        step later fails -- restoring the caller's stale observed amount on
        ``0.0`` would re-credit money a concurrent order already spent.

        A caller that will decide whether to UNDO this discard after
        releasing the lock needs the ledger revision as well, to detect a
        concurrent mutation landing in between -- use
        :meth:`discard_minimum_order_pending_tracked`.
        """
        _outcome = self.discard_minimum_order_pending_tracked(ticker, trading_day, observed_amount_usd)
        return None if _outcome.refused else _outcome.removed_usd

    def discard_minimum_order_pending_tracked(
        self, ticker: str, trading_day: str, observed_amount_usd: Optional[float] = None
    ) -> MinimumOrderDiscard:
        """:meth:`discard_minimum_order_pending_amount` plus the ledger revision.

        The revision is read (or written) under this call's OWN lock, so a
        caller that later reacquires the lock can tell whether any other
        mutation -- a concurrent ``settle()`` registering the recovered
        order, a fresh ``record()`` crediting new money -- landed in the
        gap. That is the compare-and-swap token
        :meth:`restore_minimum_order_pending` accepts as
        ``expected_revision`` (Codex review, PR #680
        discussion_r3925440982: "Make post-discard restoration
        compare-and-swap").
        """
        symbol = ticker.strip().upper()
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            qualifying = state.setdefault("minimum_order_pending_usd_by_ticker", {})
            amount = float(qualifying.get(symbol, 0.0))
            if amount <= 0:
                # Already absent: nothing to discard, and -- critically --
                # nothing THIS call removed, so nothing it may later restore.
                return MinimumOrderDiscard(0.0, self._state_revision(state), False)
            if observed_amount_usd is not None and round(amount, 2) != round(float(observed_amount_usd), 2):
                # The balance MOVED since the caller's pre-lock scan, but a
                # move is not by itself a reason to refuse. Returning a
                # refusal here for any change at all stranded the credit
                # whenever a concurrent contribution left it still BELOW the
                # threshold (e.g. $0.10 -> $0.20): both callers read False as
                # "it became actionable", and neither can route a balance
                # that still has no order in it --
                # _select_final_session_credit_flush_tickers has no decision
                # to route, and the per-candidate path re-evaluates but
                # declines a second discard -- so nothing in the final
                # session ever cleared it (Codex review: "Retry discards when
                # the balance remains sub-threshold"). The threshold re-check
                # below is the real guard, and it runs against the FRESH
                # amount read under this lock, so a changed balance is simply
                # discarded at its current value (and THAT fresh amount is
                # what removed_usd reports). Logged because a concurrent
                # write to a final-session credit is worth a trace either way.
                _log.info(
                    "cumulative credit for %s on %s moved from %.2f to %.2f between the "
                    "caller's scan and this final discard; re-evaluating at the current amount",
                    symbol,
                    trading_day,
                    round(float(observed_amount_usd), 2),
                    amount,
                )
            if apply_cumulative_credit(amount, 0.0, final_session=True).order_amount_usd > 0:
                # The current balance now qualifies for an order -- a
                # concurrent update raised it since the caller's pre-lock
                # scan. Do NOT discard; leave both pools untouched so the
                # money is not silently lost, even though nothing in this
                # final session will retry it.
                return MinimumOrderDiscard(0.0, self._state_revision(state), True)
            qualifying.pop(symbol, None)
            pending_before = float(state["pending_usd_by_ticker"].get(symbol, 0.0))
            pending_after = round(max(0.0, pending_before - amount), 2)
            if pending_after > 0:
                state["pending_usd_by_ticker"][symbol] = pending_after
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": "afternoon",
                    "recorded_at": None,
                    "final_session": True,
                    "pending_before_usd": pending_before,
                    "incremental_credit_usd": 0.0,
                    "order_amount_usd": 0.0,
                    "pending_after_usd": pending_after,
                    "decision": "discarded",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            _revision_after_discard = self._write_state(state_path, state)
        return MinimumOrderDiscard(round(amount, 2), _revision_after_discard, False)

    def restore_minimum_order_pending(
        self, ticker: str, trading_day: str, discarded_amount_usd: float, *, expected_revision: Optional[int] = None
    ) -> bool:
        """Restore a discard when its required post-discard check cannot finish.

        Returns True when the credit was restored, False when the rollback
        was refused.

        ``expected_revision`` makes this a COMPARE-AND-SWAP against the
        revision the discard reported: the rollback is applied only if no
        other mutation landed in between. Without it, a concurrent retry
        that settled the recovered broker order after this invocation
        released the discard lock -- observing zero pending credit and
        durably recording its ``order_id`` -- would be followed by this
        unconditional restore, resurrecting spendable credit that the same
        order can no longer consume, because its settlement is now
        deduplicated by that very ``order_id`` (Codex review, PR #680
        discussion_r3925440982: "Make post-discard restoration
        compare-and-swap"). Callers must pass it whenever they released the
        lock between discarding and deciding to roll back.

        Restore both the general pending balance and its minimum-order
        provenance under one lock so a later retry can reconcile the credit.
        """
        symbol = ticker.strip().upper()
        amount = round(float(discarded_amount_usd), 2)
        if not symbol or amount <= 0:
            return False
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        with cross_process_lock(day_dir / "ledger.lock"):
            state = self._read_state(state_path, trading_day)
            if expected_revision is not None and self._state_revision(state) != expected_revision:
                # Something else mutated the ledger after this caller's own
                # discard. Whatever it was -- a settlement of the very order
                # this rollback exists to protect, or fresh credit for the
                # ticker -- it observed the post-discard state, so re-adding
                # the discarded dollars now would create credit no longer
                # backed by an unreconciled order. Fail closed: leave the
                # ledger as the concurrent writer left it.
                return False
            pending = state["pending_usd_by_ticker"]
            qualifying = state.setdefault("minimum_order_pending_usd_by_ticker", {})
            pending_before = float(pending.get(symbol, 0.0))
            pending_after = round(pending_before + amount, 2)
            pending[symbol] = pending_after
            qualifying[symbol] = round(float(qualifying.get(symbol, 0.0)) + amount, 2)
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": "afternoon",
                    "recorded_at": None,
                    "final_session": True,
                    "pending_before_usd": pending_before,
                    "incremental_credit_usd": amount,
                    "order_amount_usd": 0.0,
                    "pending_after_usd": pending_after,
                    "decision": "discard_restored",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            self._write_state(state_path, state)
        return True

    def settle(
        self,
        ticker: str,
        trading_day: str,
        submitted_amount_usd: float,
        *,
        final_session: bool = True,
        stream: str = "unknown",
        recorded_at: Optional[str] = None,
        order_id: Optional[str] = None,
        already_debited_usd: Optional[float] = None,
    ) -> None:
        """Durably confirm a credit-derived order that has actually been placed.

        ``already_debited_usd`` is for the one caller whose dollars were
        removed from the ledger BEFORE the order was matched to them -- the
        final-session discard that then discovers a concurrently placed
        duplicate. Passing it records the settlement as provenance only (order
        id registered for later dedup, audit event appended) and debits
        nothing, because the balance was already reduced. See the branch's own
        note below.

        Unlike :meth:`record`, this makes no sizing decision of its own -- the
        decision (whether/how much to order) was already made by the caller
        using a pure, stateless computation *before* the broker call, so that
        no credit is durably flushed for an order that a later cap, spread,
        buying-power, or review gate could still block or shrink. Call this
        ONLY once the resulting order is confirmed submitted, passing the
        ACTUAL submitted notional (which may differ from what was originally
        computed, e.g. a kill-switch base cap), so the audit trail and any
        pending balance reflect what really happened rather than a decision
        that was later blocked or clamped (Codex review: "Defer ledger
        updates until the submitted amount is known").

        ``final_session`` defaults to ``True`` for backward compatibility with
        the original (afternoon-only) caller, but every non-final stream
        (morning, midday, market-open, intraday) must pass ``False``: a
        regular-session threshold order is otherwise indistinguishable from
        the special 3:30 PM rounding flush in the ledger's audit history
        (Codex review: "Record the actual session when settling credits").

        Clears this ticker's pending balance ONLY up to the submitted amount:
        a confirmed order is usually for at least the broker's $1 minimum,
        fully resolving any sub-dollar credit that was pending for it, but a
        downstream kill-switch or per-order cap can clamp the ACTUAL
        submitted notional below the amount the pending balance called for
        (see the module-level comment above and near_close_dca's caller).
        Any uncovered remainder stays pending rather than being silently
        destroyed (Codex review: "Retain credits not covered by a clamped
        order").

        ``order_id`` makes settlement IDEMPOTENT per broker order. A placement
        can succeed at the broker and the process then crash before it
        publishes its run artifact; the retry's duplicate-recovery path finds
        that same live order and settles it a second time. Without a dedup key
        the second call subtracts the notional again -- a $1 settlement against
        a $2 balance erases the remaining $1 and consumes its provenance twice,
        so a genuinely outstanding credit vanishes with no view left able to
        explain or retry it (Codex review: "Make cumulative-credit settlement
        idempotent by order ID"). The id of every order settled here is
        persisted on the ledger, and a repeat call for the same order is
        recorded as an ignored duplicate and otherwise does nothing. Callers
        that cannot supply an id keep the previous at-least-once behavior.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required to settle a cumulative credit")
        # Broker adapters are not guaranteed to normalize identifiers. Keep
        # the canonicalization at the ledger boundary so original-placement
        # and duplicate-recovery callers cannot persist two spellings of the
        # same order and bypass the idempotency guard.
        order_id = _normalized_order_id(order_id)
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            # The persisted shape is a MAPPING of normalized broker order id ->
            # what that settlement moved, not a bare list: the metadata is what
            # makes an ignored duplicate explicable after the fact (which ticker,
            # how much) without replaying the whole event log. order_id was
            # canonicalized above; the persisted keys are re-canonicalized here
            # too so an id persisted under an older spelling still dedups.
            if order_id:
                persisted_order_ids = state.setdefault("settled_order_ids", {})
                settled_order_ids: dict[str, dict[str, Any]] = {}
                for persisted_order_id, persisted_order_meta in persisted_order_ids.items():
                    normalized = _normalized_order_id(persisted_order_id)
                    if normalized is None:
                        continue
                    if normalized in settled_order_ids:
                        continue
                    settled_order_ids[normalized] = (
                        persisted_order_meta if isinstance(persisted_order_meta, dict) else {}
                    )
                state["settled_order_ids"] = settled_order_ids
                normalized_order_id = order_id
                if normalized_order_id in settled_order_ids:
                    # This exact broker order was already settled -- almost always
                    # the duplicate-recovery path re-settling an order whose
                    # original placement succeeded but whose process died before
                    # publishing. Subtracting again would destroy a real remaining
                    # credit. Recorded rather than silently skipped so the audit
                    # trail shows the recovery happened and was correctly ignored.
                    state["events"].append(
                        {
                            "ticker": symbol,
                            "stream": stream,
                            "recorded_at": recorded_at,
                            "final_session": final_session,
                            "pending_before_usd": float(state["pending_usd_by_ticker"].get(symbol, 0.0)),
                            "submitted_amount_usd": submitted_amount_usd,
                            "pending_after_usd": float(state["pending_usd_by_ticker"].get(symbol, 0.0)),
                            "order_id": order_id,
                            "decision": "settle_ignored_duplicate_order",
                        }
                    )
                    day_dir.mkdir(parents=True, exist_ok=True)
                    self._write_state(state_path, state)
                    return
            else:
                settled_order_ids = state.setdefault("settled_order_ids", {})
                normalized_order_id = ""
            if already_debited_usd is not None:
                # The dollars this order consumed were ALREADY removed from
                # both pools -- by this caller's own successful
                # discard_minimum_order_pending_*() -- so there is nothing
                # left to debit. Debiting `submitted` from the CURRENT
                # balance here would charge whatever credit happens to be
                # pending NOW: if another stream recorded fresh credit for
                # the ticker between that discard and this call, the order
                # would consume money that did not exist when it was
                # accepted, and `order_id` idempotence cannot prevent the
                # FIRST wrong application (Codex review, PR #680
                # discussion_r3925440968: "Keep post-discard settlement from
                # consuming newer credit").
                #
                # Record the settlement as pure provenance instead: register
                # the order id (in the same mapping shape the ordinary
                # settlement path below persists, so a later recovery pass
                # still dedups) and append the audit event naming the
                # already-removed amount, leaving every balance untouched.
                _already = round(float(already_debited_usd), 2)
                if normalized_order_id:
                    settled_order_ids[normalized_order_id] = {
                        "ticker": symbol,
                        "submitted_amount_usd": round(float(submitted_amount_usd), 2),
                    }
                state["events"].append(
                    {
                        "ticker": symbol,
                        "stream": stream,
                        "recorded_at": recorded_at,
                        "final_session": final_session,
                        "pending_before_usd": float(state["pending_usd_by_ticker"].get(symbol, 0.0)),
                        "incremental_credit_usd": 0.0,
                        "order_amount_usd": round(float(submitted_amount_usd), 2),
                        "pending_after_usd": float(state["pending_usd_by_ticker"].get(symbol, 0.0)),
                        "decision": "settled_already_discarded",
                        "already_discarded_usd": _already,
                        "order_id": order_id,
                    }
                )
                day_dir.mkdir(parents=True, exist_ok=True)
                self._write_state(state_path, state)
                return
            pending_before = float(state["pending_usd_by_ticker"].get(symbol, 0.0))
            submitted = submitted_amount_usd
            pending_after = round(max(0.0, pending_before - submitted), 2)
            if pending_after > 0:
                state["pending_usd_by_ticker"][symbol] = pending_after
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
            # minimum_pending and the preserve: entries below are DISJOINT
            # provenance views of the SAME underlying pending_usd_by_ticker
            # balance (record() funds minimum_pending, preserve_pending()
            # funds a preserve: entry -- never both for the same dollar).
            # `submitted` is therefore one shared budget to partition between
            # them, not a full independent budget each pool gets to spend on
            # its own: letting each subtract the FULL submitted amount from
            # itself, unpartitioned, can settle more total credit across the
            # two views than was actually submitted (e.g. a $0.40 minimum
            # credit plus an unrelated $0.80 preserved credit, settled by a
            # single clamped $1.00 order, would otherwise fully clear BOTH
            # the $0.40 minimum pool AND the entire $0.80 preserve entry --
            # $1.20 of credit accounted for against only $1.00 actually
            # submitted -- silently orphaning the real $0.20 remainder that
            # stays in pending_usd_by_ticker with no provenance view left
            # able to explain or retry it. Codex review discussion on commit
            # 0af4768: "Allocate settlement once across provenance pools".
            # Pay down minimum_pending FIRST from the shared budget (it is
            # the pool whose provenance the 15:30 aggregate rule specifically
            # reads), then spend whatever remains on the preserve entries.
            minimum_pending = state.setdefault("minimum_order_pending_usd_by_ticker", {})
            qualifying_before = float(minimum_pending.get(symbol, 0.0))
            minimum_consumed = round(min(submitted, qualifying_before), 2)
            qualifying_after = round(max(0.0, qualifying_before - minimum_consumed), 2)
            if qualifying_after > 0:
                minimum_pending[symbol] = qualifying_after
            else:
                minimum_pending.pop(symbol, None)
            preserve_budget = round(max(0.0, submitted - minimum_consumed), 2)
            # Every UNCONSUMED preserve() entry for this ticker described a
            # contribution to the balance this settlement is paying out.
            # Mark as many as fit within the submitted amount as fully
            # consumed, in the order they were originally preserved: a
            # clamped/partial settlement (e.g. two blocked $0.80 credits
            # followed by a $1 clamped final order) still fully covers the
            # FIRST $0.80 contribution even though $0.60 remains genuinely
            # outstanding, and leaving that first entry unconsumed just
            # because the total wasn't fully paid would let a delayed retry
            # of ITS OWN slot recompute and resubmit for credit that was
            # already bought (Codex review: "Mark settled preserve keys on
            # partial settlement" -- "track consumption up to
            # submitted_amount_usd instead of waiting for the balance to
            # reach zero"). "Originally preserved" order is NOT the dict's
            # iteration order: the ledger is durably written with
            # sort_keys=True and re-read on every call, so after any write
            # the dict's key order is lexicographic by idempotency key, not
            # chronological (Codex review: "Preserve chronological credit
            # settlement order" -- a 10:30 morning preserve sorting after a
            # 12:30 midday preserve would otherwise consume the WRONG,
            # later-preserved entry first). Each entry instead carries its
            # own monotonic "sequence" stamped by _next_sequence() at
            # preserve time, so consumption here is explicitly sorted by
            # that instead of relying on dict order. An entry only
            # PARTIALLY covered by the remaining submitted amount is not
            # marked consumed (a later retry of that exact slot must still
            # be free to attempt the genuinely-outstanding remainder), but
            # its recorded incremental_credit_usd IS reduced to that
            # remainder: leaving the stale full amount in place would make
            # preserved_credit_for_slot keep reporting the ORIGINAL
            # preserved total even though part of it was just paid by this
            # settlement, letting a delayed retry of that slot recompute
            # and resubmit for credit already bought (Codex review: "Track
            # partially consumed preserve entries").
            recorded_keys = state.setdefault("recorded_idempotency_keys", {})
            preserve_prefix = f"preserve:{symbol}:"
            candidates = [
                (cached.get("sequence", 0), key, cached)
                for key, cached in recorded_keys.items()
                if key.startswith(preserve_prefix) and not cached.get("consumed_by_settlement")
            ]
            candidates.sort(key=lambda item: (item[0], item[1]))
            covered = 0.0
            for _sequence, _key, cached in candidates:
                # Cent-precision tolerance: these are all round(x, 2) dollar
                # amounts, but summing floats can land a hair past the
                # target (e.g. 0.1 + 0.2 != 0.3 to the last bit). Budgeted
                # from preserve_budget (submitted minus whatever minimum_
                # pending already consumed above), not the full submitted
                # amount -- see the partition comment above minimum_pending.
                available = round(preserve_budget - covered, 2)
                if available <= 0:
                    break
                amount = float(cached.get("incremental_credit_usd", 0.0))
                if round(amount, 2) <= available + 0.001:
                    covered = round(covered + amount, 2)
                    if "settled_credit_usd" in cached:
                        cached["settled_credit_usd"] = round(float(cached["settled_credit_usd"]) + amount, 2)
                    cached["consumed_by_settlement"] = True
                else:
                    cached["incremental_credit_usd"] = round(amount - available, 2)
                    if "settled_credit_usd" in cached:
                        cached["settled_credit_usd"] = round(float(cached["settled_credit_usd"]) + available, 2)
                    covered = round(covered + available, 2)
                    break
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "final_session": final_session,
                    "pending_before_usd": pending_before,
                    "submitted_amount_usd": submitted,
                    "pending_after_usd": pending_after,
                    "order_id": order_id,
                    "decision": "settled",
                }
            )
            if normalized_order_id:
                settled_order_ids[normalized_order_id] = {
                    "ticker": symbol,
                    "submitted_amount_usd": submitted,
                }
            day_dir.mkdir(parents=True, exist_ok=True)
            self._write_state(state_path, state)

    def events(self, trading_day: str) -> list[dict[str, Any]]:
        """Return a copy of one day's structured credit-decision history."""
        state = self._read_state(self.root / trading_day / "ledger.json", trading_day)
        return list(state["events"])

    @staticmethod
    def _state_revision(state: dict[str, Any]) -> int:
        """This state's mutation counter (0 for a ledger written before it).

        Distinct from ``version``, which is the SCHEMA version and is pinned
        at 1 by :meth:`_read_state`. ``revision`` instead increments on every
        durable write, so a caller that released the lock between two
        operations can tell whether ANY concurrent mutation landed in
        between -- the compare-and-swap
        :meth:`restore_minimum_order_pending` needs (Codex review, PR #680
        discussion_r3925440982: "Make post-discard restoration
        compare-and-swap").
        """
        try:
            return int(state.get("revision", 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _write_state(cls, state_path: Path, state: dict[str, Any]) -> int:
        """Durably write ``state``, bumping and returning its new revision.

        The single write choke point for this ledger, so no mutation can
        land without advancing the counter the compare-and-swap depends on.
        """
        state["revision"] = cls._state_revision(state) + 1
        atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        return state["revision"]

    @staticmethod
    def _read_state(path: Path, trading_day: str) -> dict[str, Any]:
        if not path.exists():
            return {
                "version": 1,
                "trading_day": trading_day,
                "pending_usd_by_ticker": {},
                "events": [],
            }
        try:
            state = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read cumulative credit ledger {path}") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != 1
            or state.get("trading_day") != trading_day
            or not isinstance(state.get("pending_usd_by_ticker"), dict)
            or not isinstance(state.get("events"), list)
        ):
            raise ValueError(f"invalid cumulative credit ledger {path}")
        # `settled_order_ids` is optional (older ledgers predate it) but MUST be
        # a dict when present. `settle()` reaches it via a bare `setdefault()`,
        # so a malformed value here (e.g. `[]` or `null` from hand edits or a
        # partial write) would only surface as an AttributeError/TypeError
        # mid-way through `settle()` -- AFTER pending/provenance balances were
        # already updated in memory but BEFORE the atomic write. That would
        # leave a broker-accepted order's credit "pending" forever with no
        # ledger record of it, so a later slot could buy the same ticker again
        # (Codex review: "Validate the settled-order index before mutating
        # credits"). Validated here, at load time, so `settle()` can assume
        # the shape is already sound and fail closed before touching any
        # balance rather than partway through.
        # ABSENT is not the same thing as PRESENT-but-null. A legacy ledger
        # that predates this field never had the key at all -- the
        # supported upgrade path, defaulted to {} below. A ledger that DOES
        # have the key but carries an explicit JSON null is a field that
        # existed and was corrupted (e.g. by a partial/interrupted write),
        # not one that was never written; the prior code conflated the two
        # and silently re-initialized it to {} either way, erasing the
        # durable order-ID index. Duplicate recovery of the same broker
        # order then treated it as new and subtracted its notional from the
        # remaining pending balance and provenance a second time (Codex
        # review: "Reject a null settled-order index"). Only true absence
        # gets the conservative default; a present-but-malformed value
        # fails the ledger closed like every other shape check above.
        if "settled_order_ids" not in state:
            state["settled_order_ids"] = {}
        elif not isinstance(state.get("settled_order_ids"), dict):
            raise ValueError(f"invalid cumulative credit ledger {path}: settled_order_ids must be a mapping")
        return state


def _money(value: float | Decimal | int | str) -> Decimal:
    """Return a non-negative cent amount, rejecting invalid inputs."""
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("credit amounts must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError("credit amounts must be finite")
    return max(Decimal("0"), Decimal(str(numeric))).quantize(CENT, rounding=ROUND_HALF_UP)


def _normalized_order_id(value: Any) -> Optional[str]:
    """Return the canonical form of a usable string or numeric broker order ID."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, (int, float)) and (value == 0 or not math.isfinite(value)):
        return None
    return str(value).strip() or None


def apply_cumulative_credit(
    pending_usd: float,
    incremental_credit_usd: float,
    *,
    final_session: bool = False,
) -> CumulativeCreditDecision:
    """Apply one positive incremental top-up credit.

    During regular sessions, a credit below 50 cents stays pending.  Credits of
    50 cents through 99 cents round up to a $1 order; credits already at least
    $1 are ordered exactly to the cent.  This permits $1.01, $2.99, and $4.50
    orders while ensuring Robinhood's $1 minimum is always met.

    The final 3:30 PM ET session uses the same thresholds: totals below 50
    cents are discarded, totals below $1 round to exactly $1, and totals of at
    least $1 are ordered exactly to the cent.  No negative remainder is ever
    retained.
    """
    pending = _money(pending_usd)
    credit = _money(incremental_credit_usd)
    total = (pending + credit).quantize(CENT, rounding=ROUND_HALF_UP)

    if total == 0:
        order = Decimal("0")
        remaining = Decimal("0")
    elif total < ROUND_UP_THRESHOLD_USD:
        order = Decimal("0")
        remaining = Decimal("0") if final_session else total
    elif total < MINIMUM_ORDER_USD:
        order = MINIMUM_ORDER_USD
        remaining = Decimal("0")
    else:
        order = total
        remaining = Decimal("0")

    return CumulativeCreditDecision(
        pending_before_usd=float(pending),
        incremental_credit_usd=float(credit),
        order_amount_usd=float(order),
        pending_after_usd=float(remaining),
        final_session=final_session,
    )


__all__ = [
    "CumulativeCreditLedger",
    "CumulativeCreditDecision",
    "MINIMUM_ORDER_USD",
    "ROUND_UP_THRESHOLD_USD",
    "apply_cumulative_credit",
]
