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
    ) -> CumulativeCreditDecision:
        """Apply a credit and retain a structured event for it.

        ``trading_day`` is an ISO-8601 date (``YYYY-MM-DD``).  Keeping each
        day separate guarantees that no positive or negative balance can leak
        into a later trading day.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required for a cumulative credit")
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            pending = state["pending_usd_by_ticker"].get(symbol, 0.0)
            decision = apply_cumulative_credit(
                pending, incremental_credit_usd, final_session=final_session
            )
            if decision.pending_after_usd > 0:
                state["pending_usd_by_ticker"][symbol] = decision.pending_after_usd
            else:
                state["pending_usd_by_ticker"].pop(symbol, None)
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

    def settle(
        self,
        ticker: str,
        trading_day: str,
        submitted_amount_usd: float,
        *,
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

        Clears this ticker's pending balance outright: a confirmed order is
        always for at least the broker's $1 minimum, which fully resolves any
        sub-dollar credit that was pending for it.
        """
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required to settle a cumulative credit")
        day_dir = self.root / trading_day
        state_path = day_dir / "ledger.json"
        lock_path = day_dir / "ledger.lock"
        with cross_process_lock(lock_path):
            state = self._read_state(state_path, trading_day)
            pending_before = state["pending_usd_by_ticker"].pop(symbol, 0.0)
            state["events"].append(
                {
                    "ticker": symbol,
                    "stream": stream,
                    "recorded_at": recorded_at,
                    "final_session": True,
                    "pending_before_usd": pending_before,
                    "submitted_amount_usd": float(submitted_amount_usd),
                    "pending_after_usd": 0.0,
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
            state = json.loads(path.read_text(encoding="utf-8"))
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
