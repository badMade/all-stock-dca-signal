#!/usr/bin/env python3
"""Validate a native ``place_equity_order`` response before the batch continues.

The live executor prompts place orders through the host's native Robinhood
tool, not through ``dca_runner.execute_plan()``. Two broker facts observed on
2026-10-12 break the prompts' earlier "returned ref_id must equal the sent
ref_id" rule:

* the tool only accepts a UUID ``ref_id``, so the literal
  ``dca-runner-<slot>-<day>-<TICKER>`` key cannot be sent; and
* the response echoes a broker-assigned ``ref_id``, never the one sent.

So the echo can never match, and the rule halted every batch after its first
order. This module replaces the echo with a proof the broker CAN satisfy: the
response names a NEW order (not in the history fetched before the call),
created at or after the call started, for the requested symbol, side and
dollar amount, in a non-terminal or filled state. Every failure shape
``dca_runner._placement_failure_reason`` already rejects still halts.

CLI::

    python3 -m scripts.validate_native_placement ref-id --slot 1030 \
        --trading-day 2026-10-12 --ticker XLG
    python3 -m scripts.validate_native_placement check --response resp.json \
        --symbol XLG --side buy --amount 1.90 --sent-ref-id <uuid> \
        --prior-order-ids prior.json --submitted-after 2026-10-12T14:43:10Z

``check`` exits 0 when the order is confirmed and 1 when the batch must stop;
it prints a JSON verdict and never prints an account number.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, Mapping, Optional, Sequence

from dca_config import _reject_duplicate_keys
from dca_fileio import atomic_write_text
from dca_runner import _accepted_response_names_unaccounted_orders, _placement_failure_reason
from near_close_dca import _redacted_diagnostic_text
from scripts.idempotency import deterministic_ref_id

#: States in which the broker has accepted the order and it can still fill,
#: or has filled. Anything else (rejected, failed, cancelled, unknown) halts.
_ACCEPTED_STATES = frozenset(
    {"new", "queued", "confirmed", "unconfirmed", "partially_filled", "filled"}
)

#: Allowed clock skew between this host's call-start sample and the broker's
#: created_at, so a fast fill is not misread as a pre-existing order.
_CLOCK_SKEW_SECONDS = 5.0


def native_ref_id(slot: str, trading_day: str, ticker: str) -> str:
    """Deterministic UUID ref_id for one slot/day/ticker order.

    Same inputs always give the same UUID, so two instances of one slot send
    the identical idempotency key.
    """
    key = f"dca-runner-{str(slot).strip()}-{str(trading_day).strip()}-{str(ticker).strip().upper()}"
    return deterministic_ref_id(key)


def _parse_instant(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must carry a UTC offset (got naive {value!r})")
    return parsed


def _order_of(response: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(response, Mapping):
        return None
    data = response.get("data")
    if isinstance(data, Mapping) and isinstance(data.get("order"), Mapping):
        return data["order"]
    return None


def _halt(reason: str, order_id: Any = None) -> Dict[str, Any]:
    return {"ok": False, "order_id": order_id, "reason": reason}


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON constant {token!r}")


def _redact_account_values(value: Any, account_number: str) -> Any:
    if isinstance(value, str):
        return _redacted_diagnostic_text(value.replace(account_number, "[REDACTED-ACCOUNT]"))
    if isinstance(value, int) and not isinstance(value, bool) and str(value) == account_number:
        return "[REDACTED-ACCOUNT]"
    if isinstance(value, dict):
        return {
            _redact_account_values(key, account_number) if isinstance(key, str) else key:
            _redact_account_values(item, account_number)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_account_values(item, account_number) for item in value]
    return value


def validate_native_placement(
    response: Any,
    *,
    expected_symbol: str,
    expected_side: str,
    expected_amount_usd: float,
    sent_ref_id: str,
    prior_order_ids: AbstractSet[str],
    submitted_after: str,
    response_received_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return ``{"ok", "order_id", "reason"}`` for one native placement response.

    Raises ``ValueError`` when either clock is not offset-aware: a freshness
    test against an unzoned clock proves nothing.
    """
    call_start = _parse_instant(submitted_after, label="submitted_after")
    response_received_at = response_received_at or datetime.now(timezone.utc)
    if response_received_at.tzinfo is None:
        raise ValueError("response_received_at must carry a UTC offset")
    if response_received_at.utcoffset() is None:
        raise ValueError("response_received_at must carry a UTC offset")

    base_failure = _placement_failure_reason(
        response,
        expected_symbol=expected_symbol,
        expected_side=expected_side,
        expected_amount_usd=expected_amount_usd,
        expected_ref_id=None,
    )
    if base_failure is not None:
        return _halt(base_failure)

    order = _order_of(response)
    if order is None:
        return _halt("response carries no data.order object")
    order_id = order.get("id")
    if not isinstance(order_id, str) or not order_id.strip():
        return _halt("order has no usable id")

    state = str(order.get("state") or "").strip().lower()
    if state not in _ACCEPTED_STATES:
        return _halt(f"order state {state or 'missing'!r} is not an accepted state", order_id)

    if str(order.get("symbol") or "").strip().upper() != expected_symbol.strip().upper():
        return _halt(f"order symbol {order.get('symbol')!r} != requested {expected_symbol!r}", order_id)
    if str(order.get("side") or "").strip().lower() != expected_side.strip().lower():
        return _halt(f"order side {order.get('side')!r} != requested {expected_side!r}", order_id)

    amount_block = order.get("dollar_based_amount")
    amount_text = amount_block.get("amount") if isinstance(amount_block, Mapping) else None
    if isinstance(amount_text, bool) or isinstance(expected_amount_usd, bool):
        return _halt("order or requested dollar amount is not numeric", order_id)
    try:
        amount = float(amount_text)
        expected_amount = float(expected_amount_usd)
    except (TypeError, ValueError, OverflowError):
        return _halt("order or requested dollar amount is not numeric", order_id)
    if (
        not math.isfinite(amount)
        or not math.isfinite(expected_amount)
        or amount <= 0
        or expected_amount <= 0
        or abs(amount - expected_amount) > 0.005
    ):
        return _halt("order amount does not match the requested amount", order_id)

    returned_ref = order.get("ref_id")
    if returned_ref is not None and returned_ref != sent_ref_id:
        try:
            uuid.UUID(str(returned_ref))
        except ValueError:
            return _halt(f"returned ref_id {returned_ref!r} is not a broker UUID", order_id)

    if order_id in prior_order_ids:
        return _halt("order id was already in the prior order history (stale or cached response)", order_id)

    try:
        created = _parse_instant(order.get("created_at"), label="order created_at")
    except ValueError as exc:
        return _halt(str(exc), order_id)
    if (call_start - created).total_seconds() > _CLOCK_SKEW_SECONDS:
        return _halt("order was created before this placement call started", order_id)
    if (created - response_received_at).total_seconds() > _CLOCK_SKEW_SECONDS:
        return _halt("order was created after this placement response was received", order_id)

    if _accepted_response_names_unaccounted_orders(
        response,
        expected_symbol=expected_symbol,
        expected_side=expected_side,
        expected_amount_usd=expected_amount_usd,
        expected_ref_id=None,
    ):
        return _halt("response names additional or unaccounted orders", order_id)

    return {"ok": True, "order_id": order_id, "reason": None}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    ref = sub.add_parser("ref-id", help="print the deterministic UUID ref_id")
    ref.add_argument("--slot", required=True)
    ref.add_argument("--trading-day", required=True)
    ref.add_argument("--ticker", required=True)

    chk = sub.add_parser("check", help="validate one placement response")
    chk.add_argument("--response", required=True)
    chk.add_argument("--symbol", required=True)
    chk.add_argument("--side", required=True)
    chk.add_argument("--amount", required=True, type=float)
    chk.add_argument("--sent-ref-id", required=True)
    chk.add_argument("--prior-order-ids", required=True, help="JSON array of order ids fetched before the call")
    chk.add_argument("--submitted-after", required=True, help="offset-aware ISO instant sampled before the call")
    chk.add_argument(
        "--redacted-response",
        help="write an account-redacted copy of the response to this artifact path after validation",
    )

    args = parser.parse_args(argv)
    if args.command == "ref-id":
        print(native_ref_id(args.slot, args.trading_day, args.ticker))
        return 0

    if args.redacted_response and Path(args.response).resolve() == Path(args.redacted_response).resolve():
        parser.error("--response must be a temporary source file, not the redacted artifact path")
    try:
        with open(args.response, encoding="utf-8") as fh:
            response = json.load(fh, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
        with open(args.prior_order_ids, encoding="utf-8") as fh:
            prior = json.load(fh, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except (OSError, ValueError, json.JSONDecodeError):
        parser.error("response or prior order history is unavailable or invalid")
    if not isinstance(prior, list):
        parser.error("--prior-order-ids must hold a JSON array")
    if any(not isinstance(item, str) or not item.strip() for item in prior):
        parser.error("--prior-order-ids must contain only nonblank string order ids")
    normalized_prior = [item.strip() for item in prior]
    if len(set(normalized_prior)) != len(normalized_prior):
        parser.error("--prior-order-ids must not contain duplicate order ids")
    verdict = validate_native_placement(
        response,
        expected_symbol=args.symbol,
        expected_side=args.side,
        expected_amount_usd=args.amount,
        sent_ref_id=args.sent_ref_id,
        prior_order_ids=frozenset(normalized_prior),
        submitted_after=args.submitted_after,
        response_received_at=datetime.now(timezone.utc),
    )
    if args.redacted_response:
        from dca_env import load_dotenv

        load_dotenv()
        account_number = os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER")
        if not account_number or not account_number.strip():
            parser.error("cannot persist response safely: brokerage account is unavailable")
        sanitized = _redact_account_values(response, account_number)
        atomic_write_text(
            Path(args.redacted_response),
            json.dumps(sanitized, sort_keys=True, indent=2) + "\n",
        )
    print(json.dumps(verdict, sort_keys=True))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
