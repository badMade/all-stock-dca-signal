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
see ``evaluate_slot``'s docstring. The override is honored ONLY when the
candidate also supplies ``prior_logged_reference_order_id`` matching the
broker's own latest qualifying order_id for that ticker -- an unbound
override could describe a stale, older buy rather than the latest one,
over-crediting prior exposure (Codex review: "Bind logged references to the
latest broker order").

FIXED (2026-09-18): a prior fill with no bound logged reference used to fall
back silently to the broker-observed fill price and auto-authorize a top-up
off it -- a documented known limitation, since a market order commonly fills
above its own decision price and that inflated reference could wrongly
authorize a top-up a true target/decision-price comparison would have
blocked. It no longer auto-authorizes: when the fill-price fallback would
authorize a nonzero top-up (``EFFECTIVE_TOP_UP``) and no bound logged
reference was supplied, the outcome is ``UNLOGGED_REFERENCE_PRICE`` -- $0,
flagged ``manual_review``. Every other outcome (a legitimate $0 from
``NO_LOWER_PRICE``/``ALREADY_MET_OR_OVERFILLED``/etc., or any bound-reference
decision) is unaffected. Only the 3:30 PM live-mirror prompt currently
threads the override through (via ``scripts/prior_reference_bindings.py``);
until the other six do too, a real same-day top-up in those slots holds for
manual review rather than auto-firing off an unproven reference.

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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from scripts.account_arg import add_account_argument, resolve_account
from scripts.broker_reconciliation import (
    BrokerOrder,
    EffectivePrior,
    decide_effective_top_up,
    reconcile_effective_prior,
    same_day_order_snapshot,
)
from scripts.reason_codes import (
    BROKER_HISTORY_INCOMPLETE,
    EFFECTIVE_TOP_UP,
    MISSING_CURRENT_PRICE,
    MISSING_PRIOR_PRICE,
    UNLOGGED_REFERENCE_PRICE,
)

__all__ = ["evaluate_slot", "main"]

#: Reasons that mean "$0 and a human should look at this", as distinct from
#: "$0 because the policy's arithmetic legitimately landed on zero".
_MANUAL_REVIEW_REASONS = frozenset(
    {MISSING_PRIOR_PRICE, MISSING_CURRENT_PRICE, BROKER_HISTORY_INCOMPLETE, UNLOGGED_REFERENCE_PRICE}
)


def _resolve_minimum_order_usd(
    minimum_order_usd: Optional[float] = None,
    config_dir: Optional[Union[str, Path]] = None,
) -> float:
    """Resolve the active broker minimum order size.

    If ``minimum_order_usd`` is explicitly supplied, validate and return it.
    Otherwise, load the configured value from ``config/per_ticker_sizing.json``
    (falling back to $1.00 if absent or unreadable).
    """
    if minimum_order_usd is not None:
        if isinstance(minimum_order_usd, bool) or minimum_order_usd <= 0:
            raise ValueError(
                f"minimum_order_usd must be a positive number, got {minimum_order_usd!r}"
            )
        return minimum_order_usd

    try:
        from dca_config import DEFAULT_CONFIG_DIR, load_per_ticker_sizing

        cd = Path(config_dir) if config_dir is not None else DEFAULT_CONFIG_DIR
        loaded = load_per_ticker_sizing(cd)
        if "minimum_order_usd" in loaded:
            return float(loaded["minimum_order_usd"])
    except Exception:
        pass
    return 1.0


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


def _most_recent_reference(orders: Sequence[BrokerOrder]) -> "tuple[Optional[float], Optional[str]]":
    """Like :func:`_most_recent_reference_price`, but also returns the specific
    order_id the selected price came from.

    Added so a caller-supplied ``prior_logged_reference_price`` can be bound to
    the exact broker order it claims to describe (see ``evaluate_slot``'s
    docstring and Codex review: "Bind logged references to the latest broker
    order") rather than trusted purely on the caller's say-so.
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


def _reference_source(logged_reference_bound: bool, reference_price: Optional[float]) -> Optional[str]:
    """Report where a decision's reference price actually came from.

    ``reference_price`` is ``None`` on a ticker's first same-day allocation --
    there is no qualifying prior broker fill and no logged reference to bind,
    so labeling it ``broker_fill`` would claim provenance from a fill that
    never happened, making this audit field unreliable for downstream
    reporting (Codex review: "Report no source when no reference exists").
    """
    if logged_reference_bound:
        return "logged"
    if reference_price is None:
        return None
    return "broker_fill"


def _most_recent_reference_price(orders: Sequence[BrokerOrder]) -> Optional[float]:
    """This policy's comparison reference: the most recent valid same-day buy.

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

    Thin wrapper over :func:`_most_recent_reference`, which also returns the
    order_id the price came from (needed to bind a caller-supplied
    ``prior_logged_reference_price`` to a specific order -- see
    ``evaluate_slot``).
    """
    price, _order_id = _most_recent_reference(orders)
    return price


def evaluate_slot(
    *,
    pages: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    account_number: str,
    trading_day: str,
    minimum_order_usd: Optional[float] = None,
    config_dir: Optional[Union[str, Path]] = None,
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
    supply it, ALONG WITH ``prior_logged_reference_order_id`` -- the specific
    broker order_id that logged reference describes. The override is honored
    only when that order_id matches the broker's own latest qualifying
    order_id for the ticker, proving the logged reference describes the SAME
    order the broker itself would otherwise use, not some older buy (Codex
    review: "Bind logged references to the latest broker order" -- an
    unbound override could belong to an earlier, stale fill and over-credit
    prior exposure). When omitted, or unbound, this falls back to the
    broker-observed fill price for COMPARISON purposes, but no longer
    auto-authorizes off it: if that fallback reference would authorize a
    nonzero top-up, the decision is ``UNLOGGED_REFERENCE_PRICE`` -- $0,
    ``manual_review=True`` -- instead of firing on an unproven, possibly
    fill-price-inflated reference. A legitimate $0 outcome the fallback would
    have produced anyway (``NO_LOWER_PRICE``, ``ALREADY_MET_OR_OVERFILLED``,
    etc.) is unaffected.

    Raises ``ValueError`` if ``account_number`` is empty: an order snapshot that
    is not pinned to the account being traded proves nothing about it.
    """
    if not account_number.strip():
        raise ValueError("account_number is required (account pinning) to evaluate the top-up gate")
    if not trading_day.strip():
        raise ValueError("trading_day is required (day pinning) to evaluate the top-up gate")

    effective_minimum_order_usd = _resolve_minimum_order_usd(
        minimum_order_usd, config_dir=config_dir
    )

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
        account_number=account_number.strip(),
        created_at_gte=trading_day,
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
        # ONLY when it is bound to the specific broker order it claims to
        # describe: an unbound override could belong to an OLDER buy (e.g. a
        # stale $100 logged reference from a ticker's first same-day fill,
        # while the broker's own history shows a more recent $90 fill), which
        # would authorize a top-up (e.g. at $95) the true latest-reference
        # policy would refuse (Codex review: "Bind logged references to the
        # latest broker order"). Compute the broker's own latest reference
        # unconditionally so there is always a fallback and a value to bind
        # against.
        broker_reference_price, broker_reference_order_id = _most_recent_reference(
            orders_by_symbol.get(ticker, [])
        )
        logged_reference_price = _f(candidate.get("prior_logged_reference_price"))
        if logged_reference_price is not None and logged_reference_price <= 0:
            logged_reference_price = None
        logged_reference_order_id = candidate.get("prior_logged_reference_order_id")
        reference_price = broker_reference_price
        logged_reference_bound = False
        if (
            logged_reference_price is not None
            and logged_reference_order_id
            and broker_reference_order_id is not None
            and logged_reference_order_id == broker_reference_order_id
        ):
            # The caller's logged reference is proven to describe the SAME
            # order the broker itself identifies as the latest qualifying
            # buy -- safe to prefer it over the (possibly fill-price-inflated)
            # broker reference for this order.
            reference_price = logged_reference_price
            logged_reference_bound = True
        # Normalize effective_prior_usd to cents BEFORE it feeds the desired -
        # prior subtraction below. Reconstructing dollars from cumulative_quantity
        # * average_price (both broker-reported at share/price precision) lands a
        # small fraction of a cent off the requested notional even for whole-cent
        # fills -- e.g. three genuine $1.00 buys can reconstruct as
        # $3.0000041175. buy_usd's ROUND_DOWN below is a hard ceiling with no
        # tolerance, so that noise previously survived all the way to the final
        # answer and turned an intended $2.00 top-up into $1.99 (Codex review:
        # "Preserve exact cents when rounding the top-up delta"). Rounding here
        # -- not just at display time -- keeps the value used for arithmetic and
        # the value reported in effective_prior_usd consistent.
        #
        # CONSIDERED AND REJECTED: rounding only the display value and keeping
        # the raw value for arithmetic (Codex review, P2: "Keep raw prior
        # exposure for top-up arithmetic" -- worried a raw $3.004 exposure
        # rounding to $3.00 could over-credit headroom on a genuinely
        # fractional-cent exposure). That does not apply to this domain: every
        # real Robinhood notional amount is cent-quantized by construction --
        # there is no such thing as a genuine $3.004 fill -- so any sub-cent
        # residue in effective_prior_usd is PROVABLY float-reconstruction noise
        # from the quantity * average_price multiplication, never real
        # exposure. Rounding it away before arithmetic cannot lose genuine
        # exposure in this domain, and skipping it reintroduces the original
        # $1.99-instead-of-$2.00 bug this rounding was added to fix (see the
        # test suite's ``test_prior_exposure_accumulates_across_every_slot_
        # not_just_the_last_one``, which pins the $2.00 answer).
        prior = replace(
            prior,
            reference_price=reference_price,
            effective_prior_usd=round(prior.effective_prior_usd, 2),
        )
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
                    "effective_prior_usd": prior.effective_prior_usd,
                    "reference_price": prior.reference_price,
                    "reference_order_id": broker_reference_order_id,
                    "reference_source": _reference_source(logged_reference_bound, prior.reference_price),
                    "logged_reference_bound": logged_reference_bound,
                    "buy_usd": 0.0,
                    "reason": MISSING_CURRENT_PRICE,
                    "manual_review": True,
                }
            )
            continue
        decision = decide_effective_top_up(
            ticker, desired, prior, current_price, effective_minimum_order_usd
        )
        buy_usd = float(
            Decimal(str(decision.incremental_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        )
        reason = decision.reason
        # A prior fill exists (broker_reference_price is not None) and the
        # policy's own arithmetic would authorize a nonzero top-up off it --
        # but the reference behind that arithmetic is the unproven, possibly
        # fill-price-inflated broker reference, never a caller-bound logged
        # target/decision price (see this function's docstring). Authorizing
        # the buy anyway risks exactly the over-purchase this gate's own
        # module docstring names as a known limitation. Redirect to manual
        # review instead of auto-firing; every OTHER outcome (a legitimate
        # $0 from NO_LOWER_PRICE/ALREADY_MET_OR_OVERFILLED/etc., or any
        # bound-reference decision) is unaffected.
        if reason == EFFECTIVE_TOP_UP and broker_reference_price is not None and not logged_reference_bound:
            reason = UNLOGGED_REFERENCE_PRICE
            buy_usd = 0.0
        decisions.append(
            {
                "ticker": ticker,
                "desired_usd": desired,
                "current_price": current_price,
                "effective_prior_usd": prior.effective_prior_usd,
                "reference_price": prior.reference_price,
                "reference_order_id": broker_reference_order_id,
                "reference_source": _reference_source(logged_reference_bound, prior.reference_price),
                "logged_reference_bound": logged_reference_bound,
                "buy_usd": buy_usd,
                "reason": reason,
                "manual_review": reason in _MANUAL_REVIEW_REASONS,
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
    if saved_account != account_number.strip():
        return None, "orders_pages_account_mismatch"
    if saved_day != trading_day.strip():
        return None, "orders_pages_trading_day_mismatch"
    return pages, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_account_argument(parser, "Robinhood account number the orders were fetched for.")
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
            "instead of this gate's own broker-fill-price fallback. It is used "
            'ONLY when the entry ALSO carries a matching "prior_logged_reference_'
            'order_id" -- the broker order_id that logged reference describes -- '
            "proving it belongs to the broker's own latest qualifying buy for "
            "that ticker; otherwise the broker-observed fill price is used "
            "instead, ignoring the unbound override (see evaluate_slot's "
            "docstring)."
        ),
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Optional path to directory containing config JSON files.",
    )
    parser.add_argument(
        "--minimum-order-usd",
        type=float,
        default=None,
        help="The minimum order size allowed by the broker (defaults to minimum_order_usd in config/per_ticker_sizing.json, or 1.0).",
    )
    args = parser.parse_args(argv)
    resolve_account(parser, args)

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

    assert pages is not None
    result = evaluate_slot(
        pages=pages,
        candidates=candidates,
        account_number=args.account,
        trading_day=args.trading_day,
        minimum_order_usd=args.minimum_order_usd,
        config_dir=args.config_dir,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "PROCEED" else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
