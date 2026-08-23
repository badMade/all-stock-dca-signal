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
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Optional

from dca_fileio import atomic_write_text
from dca_paths import stream_root
from scripts.file_lock import cross_process_lock

MINIMUM_ORDER_USD = Decimal("1.00")
ROUND_UP_THRESHOLD_USD = Decimal("0.50")
CENT = Decimal("0.01")


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
                rebased = True
                preserve_key = f"preserve:{symbol}:{idempotency_key}"
                cached_preserve = recorded_keys.get(preserve_key)
                own_contribution = (
                    0.0
                    if cached_preserve is None or cached_preserve.get("consumed_by_settlement")
                    else float(cached_preserve.get("incremental_credit_usd", 0.0))
                )
                pending = max(
                    0.0,
                    round(state["pending_usd_by_ticker"].get(symbol, 0.0) - own_contribution, 2),
                )
            elif pending_baseline_usd is not None:
                # No idempotency_key to rebase against: the caller-supplied
                # baseline is used verbatim, exactly as documented.
                pending = pending_baseline_usd
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
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
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
                return float(recorded_keys[dedup_key]["pending_after_usd"])
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
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
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

    def discard_minimum_order_pending(self, ticker: str, trading_day: str) -> bool:
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
        CURRENT balance, under this call's own lock, against the same
        final-session discard threshold before clearing it; refuse (and
        leave the ledger untouched) if it no longer qualifies.

        Returns True if the balance was discarded, False if a concurrent
        update means the current balance no longer qualifies for discard
        (nothing is changed in that case -- the balance is left pending for
        a later, out-of-band reconciliation to resolve).
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
                return True
            if apply_cumulative_credit(amount, 0.0, final_session=True).order_amount_usd > 0:
                # The current balance now qualifies for an order -- a
                # concurrent update raised it since the caller's pre-lock
                # scan. Do NOT discard; leave both pools untouched so the
                # money is not silently lost, even though nothing in this
                # final session will retry it.
                return False
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
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
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
    ) -> None:
        """Durably confirm a credit-derived order that has actually been placed.

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
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required to settle a cumulative credit")
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
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
                    cached["consumed_by_settlement"] = True
                else:
                    cached["incremental_credit_usd"] = round(amount - available, 2)
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
                    "decision": "settled",
                }
            )
            day_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")

    def events(self, trading_day: str) -> list[dict[str, Any]]:
        """Return a copy of one day's structured credit-decision history."""
        state = self._read_state(self.root / trading_day / "ledger.json", trading_day)
        return list(state["events"])

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
