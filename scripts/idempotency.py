#!/usr/bin/env python3
"""Idempotency keys for DCA orders.

The plan requires orders to be keyed by trade date, execution window, symbol,
strategy version, AND allocation tier, so that two runs (or a run and its
recovery) computing the same intended order produce the SAME key and the broker's
client-order-id dedup rejects the duplicate. The tracked ref-id today
(``graded-dca[-topup]-{trading_day}-{ticker}``) carries only date + symbol + a
top-up flag, so a morning and an afternoon order for the same symbol, or two runs
at different strategy versions, collide or fail to dedup as intended.

This module builds the richer key as a pure function, in two forms:

  * :func:`build_idempotency_key` -- a readable, parseable string.
  * :func:`deterministic_ref_id` -- a deterministic UUIDv5 derived from that
    string, for brokers that require a UUID-shaped client order id.

Standard library only. No trading logic, no network, no secrets. This module does
NOT change any live ref id on its own; a runner must adopt it explicitly.
"""

from __future__ import annotations

import uuid
from typing import Dict, Optional
from urllib.parse import quote, unquote

#: Namespace for deterministic UUIDv5 ref ids (a fixed, arbitrary UUID constant).
_REF_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

_PREFIX = "graded-dca"
_SEP = ":"


def _field(value: object, *, upper: bool = False) -> str:
    """Canonicalize + LOSSLESSLY encode one key field.

    Case is canonicalized (upper for symbols, lower otherwise); the value is then
    percent-encoded (``quote(..., safe="")``) so distinct inputs always produce
    distinct output and the ``:`` separator can never appear inside a field. This
    replaces the earlier collapse-unsafe-chars-to-hyphen normalization, under
    which distinct idempotency dimensions like ``v2/a`` and ``v2-a`` mapped to the
    same key (and thus the same ref id) and could be wrongly deduplicated.
    """
    text = str(value).strip()
    text = text.upper() if upper else text.lower()
    if not text:
        raise ValueError("idempotency key fields must be non-empty")
    return quote(text, safe="")


def build_idempotency_key(
    *,
    trade_date: str,
    window: str,
    symbol: str,
    strategy_version: str,
    tier: str,
    kind: str = "first",
) -> str:
    """Build the canonical idempotency key string.

    ``kind`` distinguishes a first allocation (``first``) from a top-up
    (``topup``) so the two are keyed distinctly for the same symbol/day.

    Example::

        graded-dca:buy:first:2026-07-23:1230et:TXN:v2-preauthorized:elevated
    """
    parts = [
        _PREFIX,
        "buy",
        _field(kind),
        _field(trade_date),
        _field(window),
        _field(symbol, upper=True),
        _field(strategy_version),
        _field(tier),
    ]
    return _SEP.join(parts)


def deterministic_ref_id(key: str) -> str:
    """Deterministic UUIDv5 ref id derived from an idempotency key string.

    Same key -> same UUID, so a run and its recovery produce an identical client
    order id and the broker rejects the duplicate.
    """
    if not key:
        raise ValueError("key is required")
    return str(uuid.uuid5(_REF_ID_NAMESPACE, key))


def parse_idempotency_key(key: str) -> Optional[Dict[str, str]]:
    """Parse a key string back into its fields, or None if it isn't one of ours."""
    if not isinstance(key, str):
        return None
    parts = key.split(_SEP)
    if len(parts) != 8 or parts[0] != _PREFIX or parts[1] != "buy":
        return None
    return {
        "kind": unquote(parts[2]),
        "trade_date": unquote(parts[3]),
        "window": unquote(parts[4]),
        "symbol": unquote(parts[5]),
        "strategy_version": unquote(parts[6]),
        "tier": unquote(parts[7]),
    }


__all__ = [
    "build_idempotency_key",
    "deterministic_ref_id",
    "parse_idempotency_key",
]
