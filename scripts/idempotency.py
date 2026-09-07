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
from datetime import date
from typing import Any, Dict, Optional
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


def parse_idempotency_key(key: Any) -> Optional[Dict[str, str]]:
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


# The dca_runner slot ids each stream fires under (mirrors dca_runner.SLOTS'
# labels; tests/test_dca_runner.py pins the two to each other). Kept here
# rather than imported so this module stays importable by every entry point
# without pulling the runner in.
STREAM_SLOT_IDS = {
    "market_open": ("0935",),
    "morning": ("1030",),
    "intraday": ("1130", "1330", "1430"),
    "midday": ("1230",),
    "afternoon": ("1530",),
}


def _graded_dca_prefix(stream: str, kind: str = "") -> str:
    # Mirrors near_close_dca._graded_dca_ref_id_prefix: the afternoon keeps the
    # unprefixed legacy "graded-dca[-kind]" literal that the 10ce362c task's
    # own order history depends on; any other stream gets a stream token.
    parts = ["graded-dca"]
    if stream and stream != "afternoon":
        parts.append(stream)
    if kind:
        parts.append(kind)
    return "-".join(parts)


def same_stream_ref_id_prefixes(stream: str, trading_day: date, ticker: str) -> tuple:
    """Every ref-id prefix a scheduled order for ``stream``/``trading_day``/
    ``ticker`` can carry, across all three entry points.

    Three incompatible ref-id families coexist for the same slot (2026-09-07
    diagnostic, C:F5): the class-based executors write
    ``{stream}-dca[-topup]-{day}-{ticker}``, the afternoon engine behind
    ``run_dca.run_slot()`` writes ``graded-dca[-{stream}][-topup]-{day}-{ticker}``,
    and ``dca_runner`` writes ``dca-runner-{slot}-{day}-{ticker}``. Each path's
    own same-stream duplicate check recognized only its own family, so a retry
    or recovery of the same slot through a different entry point derived a
    different key and could submit the same buy twice. Both duplicate checks
    now ask for this whole set.

    Manual (``-manual-``) and inverse-Martingale (``-imart-``) kinds are
    deliberately NOT included: a manual fill never collides with a scheduled
    retry by contract, and the two existing checks never matched them either.
    Nothing here changes which ref id any path EMITS.
    """
    day = trading_day.isoformat()
    prefixes = [
        f"{stream}-dca-topup-{day}-{ticker}",
        f"{stream}-dca-{day}-{ticker}",
        f"{_graded_dca_prefix(stream, 'topup')}-{day}-{ticker}",
        f"{_graded_dca_prefix(stream)}-{day}-{ticker}",
    ]
    # IntradayDCAExecutor passes its slot-qualified namespace ("intraday-1330")
    # rather than the bare stream, so the runner family is resolved for that
    # one slot; a bare multi-slot stream still covers every slot (Copilot
    # review, PR #1394: "Normalize slot-qualified intraday streams").
    base, _, qualifier = stream.partition("-")
    if qualifier and qualifier in STREAM_SLOT_IDS.get(base, ()):
        runner_slots: tuple = (qualifier,)
    else:
        runner_slots = STREAM_SLOT_IDS.get(stream, ())
    prefixes.extend(f"dca-runner-{slot}-{day}-{ticker}" for slot in runner_slots)
    return tuple(prefixes)


__all__ = [
    "STREAM_SLOT_IDS",
    "build_idempotency_key",
    "deterministic_ref_id",
    "parse_idempotency_key",
    "same_stream_ref_id_prefixes",
]
