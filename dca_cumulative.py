"""Cent-precise, same-day order-credit math for the DCA streams.

Broker orders must be at least one dollar, but the per-ticker model can produce
an incremental top-up below that amount.  Those increments are *credits*, not
orders: they accumulate within one trading day, never become negative, and the
final 3:30 PM ET run closes any positive balance by rounding it up to a whole
dollar order.

This module deliberately contains only deterministic math.  Stream executors
remain responsible for their normal price, spread, budget, and broker checks
before submitting an order returned by this policy.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
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

        ``pending_baseline_usd`` is only used when ``idempotency_key`` is
        unavailable. With a slot key present, this method rebases from the
        CURRENT locked ledger state by subtracting this slot's own
        unconsumed ``preserve_pending`` entry (if any) from the ticker's
        current pending balance before applying this run's incremental credit.
        This keeps retries atomic under the lock even if another stream
        changed pending balance between the caller's pre-lock snapshot and
        this write (Codex review: "Rebase retry credits from the locked
        ledger state").
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
            if idempotency_key:
                # Recompute baseline from CURRENT locked state, not the
                # caller's pre-lock snapshot: a concurrent stream may have
                # recorded or settled this ticker after that snapshot. This
                # slot's own unconsumed preserve entry (if any) is then netted
                # out so a retry can recompute from fresh market data without
                # double-counting its prior blocked contribution.
                pending = float(state["pending_usd_by_ticker"].get(symbol, 0.0))
                preserve_key = f"preserve:{symbol}:{idempotency_key}"
                preserved = recorded_keys.get(preserve_key)
                if preserved is not None and not preserved.get("consumed_by_settlement"):
                    pending = max(0.0, pending - float(preserved.get("incremental_credit_usd", 0.0)))
            elif pending_baseline_usd is None:
                pending = state["pending_usd_by_ticker"].get(symbol, 0.0)
            else:
                pending = float(pending_baseline_usd)
            decision = apply_cumulative_credit(
                pending, incremental_credit_usd, final_session=final_session
            )
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
                    "decision": "order" if decision.order_amount_usd > 0 else "pending",
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
            pending_after = round(pending_before + float(additional_credit_usd), 2)
            if pending_after > 0:
                state["pending_usd_by_ticker"][symbol] = pending_after
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
            if dedup_key is not None:
                recorded_keys[dedup_key] = {
                    "pending_before_usd": pending_before,
                    "incremental_credit_usd": float(additional_credit_usd),
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
                    "incremental_credit_usd": float(additional_credit_usd),
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
            submitted = float(submitted_amount_usd)
            pending_after = round(max(0.0, pending_before - submitted), 2)
            if pending_after > 0:
                state["pending_usd_by_ticker"][symbol] = pending_after
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
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
            # partially covered by the remaining submitted amount is reduced
            # to its true uncovered remainder (still unconsumed): this keeps
            # preserved_credit_for_slot() aligned with the post-settlement
            # pending balance instead of overstating this slot's own
            # outstanding contribution.
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
                amount = float(cached.get("incremental_credit_usd", 0.0))
                remaining = round(submitted - covered, 2)
                if remaining <= 0.001:
                    break
                # Cent-precision tolerance: these are all round(x, 2) dollar
                # amounts, but summing floats can land a hair past the
                # target (e.g. 0.1 + 0.2 != 0.3 to the last bit).
                if amount > remaining + 0.001:
                    cached["incremental_credit_usd"] = round(max(0.0, amount - remaining), 2)
                    break
                covered = round(covered + amount, 2)
                cached["consumed_by_settlement"] = True
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


def _money(value: float) -> Decimal:
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

    At the final 3:30 PM ET session, every positive pending total is rounded up
    to the next whole dollar.  No negative remainder is ever retained.
    """
    pending = _money(pending_usd)
    credit = _money(incremental_credit_usd)
    total = (pending + credit).quantize(CENT, rounding=ROUND_HALF_UP)

    if total == 0:
        order = Decimal("0")
        remaining = Decimal("0")
    elif final_session:
        order = total.quantize(Decimal("1"), rounding=ROUND_CEILING)
        remaining = Decimal("0")
    elif total < ROUND_UP_THRESHOLD_USD:
        order = Decimal("0")
        remaining = total
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
