#!/usr/bin/env python3
"""Broker-sourced, fail-closed enforcement of ``same_day_top_up_policy``.

WHAT THIS IS FOR
----------------
``config/scheduled_tasks.json``'s ``same_day_top_up_policy`` PERMITS buying the
same ticker at more than one slot in a day, sized down: each later run buys only
``max(0, desired - prior_same_day)``, and only when its decision price is
strictly below the prior same-day reference price. That policy is correct. What
was broken was its enforcement.

The previous enforcement path derived "what has already been bought today" from
run-state artifacts on disk (``load_prior_same_day_allocations``,
``load_same_day_top_up_context``). Those loaders treat a state root that does not
exist as identical to one that is empty -- ``near_close_dca.py:3724``
(``if root.exists()``) and ``scripts/run_classification.py:1776-1777``
(``if not root.exists(): return []``) both drop a missing root silently, with no
log and no ``skip_sink`` entry, so ``scan_had_failures`` stays False. The guard
therefore concluded "no prior allocation" and every slot bought full size. It
failed OPEN. Two further gaps compounded it: only three legacy streams were ever
folded into the prior-allocation chain, leaving four of the seven roster slots
invisible to it, and the terminal handling of an unproven scan
(``run_dca.py:2121-2147``) logs a warning and continues.

This module replaces that input. Prior same-day exposure is read from the broker
-- the same ``get_equity_orders`` history Gate 6 already trusts for budget. That
is stateless (nothing to relocate or lose when a machine changes), and it sees
all seven slots automatically because it asks the account what it bought rather
than asking each stream to have written a file.

WHAT THIS IS NOT
----------------
It does not re-implement the policy. The strictly-below invariant and the
delta arithmetic stay in :func:`scripts.run_classification.incremental_top_up_amount`;
the effective-prior dollar fold and the fail-closed completeness gate stay in
:mod:`scripts.broker_reconciliation`. Both are already tested. This module is
the missing wiring: replay saved order pages, reconcile with NO artifact input,
and hand each candidate to the existing decision function.

One piece IS overridden: ``reconcile_effective_prior``'s own reference price is
the MINIMUM confirmed same-day fill price, a conservative default tuned for its
other (artifact-combining) callers. ``config/scheduled_tasks.json``'s
``same_day_top_up_policy`` (see ``final_override_top_up``) defines this gate's
own comparison reference as the MOST RECENT valid same-day buy, ordered by
time -- so this module recomputes just that one field per ticker before
handing it to the decision function (Codex review: "Compare against the most
recent valid reference price").

That recomputed value is still only the average FILL price, never the
documented preference's higher tiers (logged ``target_price``/
``decision_price``) -- this gate has no artifact access to them. A buy that
filled above its own decision price (the common direction for a market
order) inflates the reference, which can wrongly authorize a top-up whose
decision price sits between the true reference and the inflated one (Codex
review: "Compare top-ups against the configured reference price"). Recovering
target/decision price by reading a prior run's own artifacts would reintroduce
the exact fragility (a missing/unreachable state root read as "nothing to
recover") this replacement was built to remove -- so instead, a candidate may
supply ``prior_logged_reference_price`` to override the fill-price fallback
with the true logged reference WHEN THE CALLER ALREADY HAS IT (e.g. from its
own or an earlier slot's orders.csv/run_state.json), without this gate ever
reading an artifact itself or treating one's absence as "no prior exists";
see ``evaluate_slot``'s docstring. Omitted, the gate still falls back to the
broker-observed fill price, matching the documented preference's own
last-resort tier.

It also does not decide share-class equivalence. ``GOOG`` and ``GOOGL`` are
scored as independent tickers here, exactly as ``config/tickers.json`` lists
them. Changing that is a portfolio-construction decision for a human.

FAIL CLOSED
-----------
If same-day broker history cannot be proven complete -- an unfollowed pagination
cursor, an error-shaped page, an unnormalizable row, an unsized live order, or
no saved pages at all -- the verdict is ``ABORT``, every candidate is $0, and
the reason is ``BROKER_HISTORY_INCOMPLETE``. There is deliberately no path that
falls back to the full desired amount on an unproven-empty history: that is the
exact failure this module exists to remove.

Standard library only. No network: the caller fetches order pages with its own
``get_equity_orders`` tool and passes them in.

USAGE (from an executor prompt)
-------------------------------
    python3 -m scripts.same_day_top_up_gate \\
        --account ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER} \\
        --trading-day 2026-07-31 \\
        --orders-pages artifacts/orders_today.json \\
        --candidates artifacts/cands.json

``--orders-pages`` is a JSON OBJECT binding the saved evidence to the query it
was fetched for: ``{"account_number": "...", "trading_day": "YYYY-MM-DD",
"pages": [...]}``, where ``pages`` is the RAW ``get_equity_orders`` responses,
one element per page, in the order they were fetched, including the final
page. ``account_number``/``trading_day`` must match ``--account``/
``--trading-day`` exactly -- a saved snapshot is evidence about a specific
account and day, and replaying it against a mismatched (or missing) claim
proves nothing about the account/day actually being traded (Codex review:
"Bind replayed pages to the requested account and day"). A bare JSON array
(the pre-pinning shape) is rejected rather than trusted.
``--candidates`` is a JSON array of ``{"ticker", "desired_usd", "current_price"}``.
Exit status is 0 on ``PROCEED`` and 2 on ``ABORT``; the decision document is
written to stdout either way.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from scripts.broker_reconciliation import (
    BrokerOrder,
    EffectivePrior,
    decide_effective_top_up,
    reconcile_effective_prior,
    same_day_order_snapshot,
)
from scripts.reason_codes import BROKER_HISTORY_INCOMPLETE, MISSING_CURRENT_PRICE, MISSING_PRIOR_PRICE

__all__ = ["evaluate_slot", "main"]

#: Reasons that mean "$0 and a human should look at this", as distinct from
#: "$0 because the policy's arithmetic legitimately landed on zero".
_MANUAL_REVIEW_REASONS = frozenset({MISSING_PRIOR_PRICE, MISSING_CURRENT_PRICE, BROKER_HISTORY_INCOMPLETE})


class _PageReplayer:
    """Serves saved ``get_equity_orders`` responses to ``paginate_same_day_orders``.

    Pages are returned in the order they were saved. Running out of pages while
    the pager is still following a cursor raises, which
    ``paginate_same_day_orders`` records as a fetch exception and treats as
    incomplete -- the correct outcome, since a caller who saved only part of a
    paginated history has not proven what the account bought today.

    The other direction must also fail closed: if the FIRST saved page already
    carries no continuation pointer, pagination stops there and reports
    ``complete=True`` without ever asking this replayer for a second page --
    silently discarding any remaining saved pages instead of treating them as
    the malformed evidence they are (e.g. an out-of-order or duplicated
    ``[empty_final_page, page_containing_prior_fill]`` array would otherwise
    read as a clean, complete, empty history and authorize a full first
    allocation). :attr:`fully_consumed` lets the caller verify every supplied
    page was actually served before trusting pagination's own completeness
    verdict (Codex review: "Reject unconsumed saved order pages").
    """

    def __init__(self, pages: Sequence[Mapping[str, Any]]) -> None:
        self._pages = list(pages)
        self._index = 0

    def __call__(self, **_kwargs: Any) -> Any:
        if self._index >= len(self._pages):
            raise RuntimeError(
                "no saved order page available for this cursor; the saved history is "
                "truncated and cannot prove today's prior fills"
            )
        page = self._pages[self._index]
        self._index += 1
        return page

    @property
    def fully_consumed(self) -> bool:
        """True only if every saved page was served to the paginator."""
        return self._index == len(self._pages)


def _abort(reason: str, candidates: Sequence[Mapping[str, Any]], detail: Sequence[str]) -> Dict[str, Any]:
    """Every candidate to $0 with a single shared reason."""
    return {
        "verdict": "ABORT",
        "reason": reason,
        "detail": list(detail),
        "coverage_complete": False,
        "decisions": [
            {
                "ticker": str(c.get("ticker", "")).strip().upper(),
                "desired_usd": _f(c.get("desired_usd")),
                "current_price": _f(c.get("current_price")),
                "effective_prior_usd": None,
                "reference_price": None,
                "buy_usd": 0.0,
                "reason": reason,
                "manual_review": True,
            }
            for c in candidates
        ],
    }


def _f(value: Any) -> Optional[float]:
    """Best-effort float parse; None for missing/nonnumeric/non-finite.

    inf/-inf/nan all parse successfully through a bare ``float()`` call, so
    without the finiteness check a malformed ``current_price`` (or
    ``desired_usd``) would silently survive as a value the arithmetic below
    can compare against, rather than being treated as absent (Codex review:
    "Reject first allocations without a current price").
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return result


def _most_recent_reference(
    orders: Sequence[BrokerOrder],
) -> tuple[Optional[float], Optional[str]]:
    """This policy's comparison reference: the most recent valid same-day buy.

    Returns ``(reference_price, order_id)`` for that buy so a caller-supplied
    ``prior_logged_reference_price`` override can be validated against the
    SAME order before being trusted (see ``evaluate_slot``) rather than being
    accepted on its own say-so -- an override sourced from an OLDER same-day
    buy would silently replace the correct, more recent broker reference
    (Codex review: "Bind the reference override to the latest buy").

    ``reconcile_effective_prior`` computes ``min(known_prices)`` -- a more
    conservative default tuned for its other (artifact-combining) callers, not
    this gate's own documented contract. ``config/scheduled_tasks.json``'s
    ``same_day_top_up_policy`` (``final_override_top_up``) defines the
    comparison reference as "that ticker's most recent valid same-day buy
    reference (ordered by generated_at)": for fills at $90 then $100, a later
    $95 decision is below the most recent $100 reference and should top up,
    even though $90 -- the minimum -- is also below $95 (Codex review:
    "Compare against the most recent valid reference price").

    Mirrors ``reconcile_effective_prior``'s own poisoning rule exactly (with
    artifact_priors always ``{}`` for this stateless gate, every qualifying
    order without a confirmed fill price poisons unconditionally): a buy-side
    order that is live-and-unfilled, or a terminal order whose fill could not
    be priced, means the true prior price is not yet knowable, so the
    reference must be ``None`` rather than compared against. A contributing
    fill with no usable ``created_at`` poisons the same way: substituting an
    empty string would silently treat it as the OLDEST fill regardless of
    when it actually happened, so a timestamped $100 fill plus an undated $90
    fill that was in fact the most recent would compare a new $95 decision
    against $100 and wrongly authorize a top-up (Codex review: "Fail closed
    when fill chronology is unavailable").
    """
    from datetime import datetime, timezone

    latest_created_at: Optional[datetime] = None
    latest_price: Optional[float] = None
    latest_order_id: Optional[str] = None
    for order in orders:
        if order.side == "sell":
            continue
        if not order.is_live and order.notional_usd <= 0:
            continue
        if order.reference_price is None:
            return None, None
        created_at_raw = order.created_at
        if not created_at_raw:
            return None, None
        try:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError:
            return None, None
        if created_at.tzinfo is None:
            return None, None
        created_at = created_at.astimezone(timezone.utc)
        if latest_created_at is None or created_at >= latest_created_at:
            latest_created_at = created_at
            latest_price = order.reference_price
            latest_order_id = order.order_id
    return latest_price, latest_order_id


def evaluate_slot(
    *,
    pages: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    account_number: str,
    trading_day: str,
) -> Dict[str, Any]:
    """Size every candidate against today's broker-observed prior exposure.

    Returns a decision document with a ``verdict`` of ``PROCEED`` or ``ABORT``
    and one entry per candidate carrying ``buy_usd`` -- the amount the policy
    authorizes, which may be the full desired amount (first allocation today),
    a strictly smaller top-up, or $0.

    A candidate may optionally carry ``prior_logged_reference_price``: the
    prior same-day buy's own logged ``target_price``/``decision_price`` for
    this ticker (config/scheduled_tasks.json's ``reference_price_preference``
    puts both ahead of ``average_fill_price``). This gate has no artifact
    access of its own -- ``BrokerOrder.reference_price`` is always the
    average FILL price -- so without this override, a buy that filled above
    its own decision price (the common direction for a market order) makes
    the comparison too permissive: a later decision above the true prior
    reference but still below the inflated fill-price reference wrongly
    authorizes a top-up (Codex review: "Compare top-ups against the
    configured reference price"). A caller that has the logged reference
    (e.g. from its own or an earlier slot's orders.csv/run_state.json) should
    supply it; when omitted, this falls back to the broker-observed fill
    price, matching the documented preference's own last-resort tier.

    The override must also carry ``prior_logged_reference_order_id``: the
    broker order id the logged reference price came from. It is honored ONLY
    when that id matches this ticker's chronologically most recent qualifying
    same-day buy as this gate itself resolves from the broker history --
    otherwise it is ignored and the broker-observed fill price is used
    instead. Without this binding, a ticker bought more than once today could
    supply a stale reference from an EARLIER buy (e.g. an older logged $100
    with a newer $90 fill) and override the correct, more recent broker
    reference, wrongly authorizing a top-up a current decision price below
    the true $90 reference should have blocked (Codex review: "Bind the
    reference override to the latest buy").

    Raises ``ValueError`` if ``account_number`` is empty: an order snapshot that
    is not pinned to the account being traded proves nothing about it.
    """
    if not str(account_number or "").strip():
        raise ValueError("account_number is required (account pinning) to evaluate the top-up gate")

    if not pages:
        # An empty page list is missing evidence, not evidence of no buying --
        # the same shape a missing filesystem state root produced.
        return _abort(BROKER_HISTORY_INCOMPLETE, candidates, ["no_saved_order_pages"])

    # Deliberately NO expected_order_ids and NO artifact priors: this gate is
    # stateless by design. Broker history is the sole authority on what was
    # bought today, so there is no on-disk record whose absence could be
    # mistaken for an empty day. same_day_order_snapshot also deduplicates by
    # order id -- a page-boundary repeat or a duplicated saved page must not be
    # counted as separate exposure (Codex review: "Deduplicate replayed orders
    # before reconciliation").
    replayer = _PageReplayer(pages)
    snapshot = same_day_order_snapshot(
        replayer,
        account_number=str(account_number).strip(),
        created_at_gte=str(trading_day),
        max_pages=max(len(pages) + 1, 2),
    )
    if not replayer.fully_consumed:
        # Pagination stopped (cleanly or not) before serving every saved page
        # -- e.g. a well-formed first page with no continuation pointer, ahead
        # of more pages in the file. That is malformed evidence, not a proven
        # history, regardless of what pagination itself concluded.
        return _abort(BROKER_HISTORY_INCOMPLETE, candidates, ["unconsumed_saved_order_pages"])
    if not snapshot.complete:
        return _abort(BROKER_HISTORY_INCOMPLETE, candidates, snapshot.incomplete_reasons or ["coverage_incomplete"])

    effective = reconcile_effective_prior({}, snapshot)
    orders_by_symbol = snapshot.orders_by_symbol()

    decisions: List[Dict[str, Any]] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker", "")).strip().upper()
        desired = _f(candidate.get("desired_usd")) or 0.0
        current_price = _f(candidate.get("current_price"))
        prior = effective.get(
            ticker,
            EffectivePrior(
                symbol=ticker,
                artifact_prior_usd=0.0,
                broker_prior_usd=0.0,
                effective_prior_usd=0.0,
                reference_price=None,
                coverage_complete=True,
            ),
        )
        # Prefer the caller-supplied logged target_price/decision_price (the
        # documented preference's first two tiers) over this stateless gate's
        # own broker-fill-price fallback -- see this function's docstring.
        # The override is trusted only when it is bound to the SAME order
        # this gate itself resolves as the ticker's most recent qualifying
        # same-day buy; an unbound or stale-order override falls back to that
        # broker-observed price instead of silently replacing it (Codex
        # review: "Bind the reference override to the latest buy").
        broker_latest_price, broker_latest_order_id = _most_recent_reference(
            orders_by_symbol.get(ticker, [])
        )
        logged_reference_price = _f(candidate.get("prior_logged_reference_price"))
        if logged_reference_price is not None and logged_reference_price <= 0:
            logged_reference_price = None
        logged_reference_order_id = candidate.get("prior_logged_reference_order_id")
        if (
            logged_reference_price is not None
            and logged_reference_order_id is not None
            and broker_latest_order_id is not None
            and str(logged_reference_order_id) == str(broker_latest_order_id)
        ):
            reference_price = logged_reference_price
        else:
            reference_price = broker_latest_price
        # effective_prior_usd feeds the desired - prior subtraction below at
        # its full RAW precision -- not rounded to cents first. Reconstructing
        # dollars from cumulative_quantity * average_price (both
        # broker-reported at share/price precision) can land a small fraction
        # of a cent off the requested notional in either direction; rounding
        # that DOWN before the subtraction (e.g. a genuine $3.004 exposure
        # reads as $3.00) understates prior exposure and lets buy_usd exceed
        # desired_usd - effective_prior_usd, the ceiling the policy declares
        # must never be rounded up (Codex review: "Do not round prior
        # exposure down before subtraction"). buy_usd's own ROUND_DOWN below
        # is the only rounding this arithmetic needs; cents-rounding is
        # applied separately, only for the effective_prior_usd value reported
        # below.
        prior = replace(prior, reference_price=reference_price)
        display_effective_prior_usd = round(prior.effective_prior_usd, 2)
        if current_price is None or current_price <= 0:
            # decide_effective_top_up (via incremental_top_up_amount) only
            # checks current_price AFTER finding a prior allocation --  a
            # missing/nonnumeric/non-finite price on a FIRST allocation
            # (effective_prior_usd <= 0) reaches "no_prior_run" and authorizes
            # the FULL desired amount without ever looking at the price. Gate
            # 6.5 cannot run without the decision price regardless of whether
            # a prior exists, so short-circuit here rather than delegating
            # (Codex review: "Reject first allocations without a current
            # price"). A zero or negative price is equally malformed market
            # data -- ``_f`` only rejects non-finite/NaN values, not sign --
            # and must be treated the same way (Codex review: "Reject
            # non-positive decision prices").
            decisions.append(
                {
                    "ticker": ticker,
                    "desired_usd": desired,
                    "current_price": None,
                    "effective_prior_usd": display_effective_prior_usd,
                    "reference_price": prior.reference_price,
                    "buy_usd": 0.0,
                    "reason": MISSING_CURRENT_PRICE,
                    "manual_review": True,
                }
            )
            continue
        decision = decide_effective_top_up(ticker, desired, prior, current_price)
        buy_usd = float(
            Decimal(str(decision.incremental_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        )
        decisions.append(
            {
                "ticker": ticker,
                "desired_usd": desired,
                "current_price": current_price,
                "effective_prior_usd": display_effective_prior_usd,
                "reference_price": prior.reference_price,
                "buy_usd": buy_usd,
                "reason": decision.reason,
                "manual_review": decision.reason in _MANUAL_REVIEW_REASONS,
            }
        )

    return {
        "verdict": "PROCEED",
        "reason": None,
        "detail": [],
        "coverage_complete": True,
        "orders_in_snapshot": len(snapshot.orders),
        "decisions": decisions,
    }


def _bind_saved_pages(
    payload: Any, *, account_number: str, trading_day: str
) -> tuple[Optional[List[Any]], Optional[str]]:
    """Extract ``pages`` from ``--orders-pages`` and prove it was saved for
    THIS account and day.

    Returns ``(pages, None)`` on a match, or ``(None, detail_reason)`` when the
    saved evidence does not carry matching ``account_number``/``trading_day``
    metadata. A snapshot proves nothing about the account/day actually being
    traded unless it is pinned to them -- requiring a nonempty ``--account``
    string is not account pinning, since nothing before this checked that the
    SAVED pages were ever fetched for that account (or this trading day)
    rather than replayed from an old file left over from a different account
    or an earlier day (Codex review: "Bind replayed pages to the requested
    account and day"). A bare JSON array (no metadata to check) is therefore
    rejected the same as a mismatch, not trusted as before this fix.
    """
    if not isinstance(payload, dict):
        return None, "orders_pages_missing_query_metadata"
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return None, "orders_pages_missing_query_metadata"
    saved_account = str(payload.get("account_number") or "").strip()
    saved_day = str(payload.get("trading_day") or "").strip()
    if not saved_account or not saved_day:
        return None, "orders_pages_missing_query_metadata"
    if saved_account != str(account_number).strip():
        return None, "orders_pages_account_mismatch"
    if saved_day != str(trading_day).strip():
        return None, "orders_pages_trading_day_mismatch"
    return pages, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True, help="Robinhood account number the orders were fetched for.")
    parser.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
    parser.add_argument(
        "--orders-pages",
        required=True,
        help=(
            'JSON file: {"account_number", "trading_day", "pages"}, where "pages" is '
            "an array of RAW get_equity_orders responses, one per page, in fetch order."
        ),
    )
    parser.add_argument(
        "--candidates",
        required=True,
        help=(
            'JSON file: array of {"ticker","desired_usd","current_price"}. '
            'An entry may optionally carry "prior_logged_reference_price" -- '
            "this ticker's prior same-day buy's logged target_price/"
            "decision_price, when the caller has it -- to compare against "
            "instead of this gate's own broker-fill-price fallback, plus "
            '"prior_logged_reference_order_id" binding it to that buy\'s '
            "broker order id (honored only when it matches this gate's own "
            "most-recent-buy resolution; see evaluate_slot's docstring)."
        ),
    )
    args = parser.parse_args(argv)

    try:
        with open(args.orders_pages, encoding="utf-8") as handle:
            orders_payload = json.load(handle)
        with open(args.candidates, encoding="utf-8") as handle:
            candidates = json.load(handle)
    except (OSError, ValueError) as exc:
        # Unreadable input is indistinguishable from missing input, and missing
        # input must never read as a clean slate.
        print(
            json.dumps(
                _abort(BROKER_HISTORY_INCOMPLETE, [], [f"input_unreadable:{type(exc).__name__}"]),
                indent=2,
            )
        )
        return 2

    if not isinstance(candidates, list):
        print(json.dumps(_abort(BROKER_HISTORY_INCOMPLETE, [], ["input_not_a_json_array"]), indent=2))
        return 2

    pages, binding_error = _bind_saved_pages(
        orders_payload, account_number=args.account, trading_day=args.trading_day
    )
    if binding_error is not None:
        print(json.dumps(_abort(BROKER_HISTORY_INCOMPLETE, candidates, [binding_error]), indent=2))
        return 2

    result = evaluate_slot(
        pages=pages,
        candidates=candidates,
        account_number=args.account,
        trading_day=args.trading_day,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "PROCEED" else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
