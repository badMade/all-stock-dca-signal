"""Composite score (D13): one pooled probability per event across venues.

Method ``logodds-liq-spread-v1``:

* Pool in log-odds: ``logit(p) = sum(w_j * logit(p_j)) / sum(w_j)``.
* Weight ``w_j = liquidity_usd_j / max(spread_j, MIN_SPREAD)``: more resting dollars and a
  tighter book both mean a more trustworthy price. A missing spread counts as ``MAX_SPREAD``.
* No extremizing. Cross-venue prices are arbitraged, not independent, so a weighted mean in
  log-odds -- which always lands between the most and least extreme inputs -- is the ceiling.
* ``usable = false`` inputs get zero weight (ADR §7). No usable input means no composite.

``compute_composite`` is point-in-time: at ``t`` it reads only reviewed event links valid at
``t`` and quotes with ``ts_utc <= t`` that were already ingested by ``t``. It writes
``pm_composite`` only; ``pm_quotes`` is read, never written.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

from .models import CompositeRow

METHOD_VERSION = "logodds-liq-spread-v1"
MIN_SPREAD = 0.001  # one tick on a 0.001-tick book; stops a zero spread dominating
MAX_SPREAD = 1.0
EPS = 1e-6  # keeps logit finite for a 0 or 1 price


@dataclass(frozen=True)
class CompositeInput:
    prob: float
    liquidity_usd: float | None
    spread: float | None
    usable: bool
    label: str = ""


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def _weight(inp: CompositeInput) -> float:
    if not inp.usable or inp.liquidity_usd is None or inp.liquidity_usd <= 0:
        return 0.0
    spread = MAX_SPREAD if inp.spread is None else max(inp.spread, MIN_SPREAD)
    return inp.liquidity_usd / spread


def pool_weights(inputs: list[CompositeInput]) -> list[float]:
    raw = [_weight(i) if 0.0 <= i.prob <= 1.0 else 0.0 for i in inputs]
    total = sum(raw)
    return [w / total for w in raw] if total > 0 else [0.0 for _ in raw]


def pool_logodds(inputs: list[CompositeInput]) -> float | None:
    weights = pool_weights(inputs)
    if not any(weights):
        return None
    x = sum(w * _logit(i.prob) for w, i in zip(weights, inputs, strict=True) if w > 0)
    return 1 / (1 + math.exp(-x))


def compute_composite(
    store, event_id: str, *, at: datetime, computed_at: datetime, outcome: str = "Yes"
) -> list[CompositeRow]:
    inputs: list[CompositeInput] = []
    venues: list[str] = []
    for link in store.event_links(event_id):
        if not link["reviewed"] or link["relation"] not in ("same", "inverse"):
            continue  # "subset" links describe a different event; they never pool
        if link["valid_from"] > at or (link["valid_to"] is not None and link["valid_to"] <= at):
            continue
        q = store.latest_quote(link["venue"], link["market_id"], link["outcome"], at=at)
        if q is None or q.mid is None:
            continue
        prob = 1 - q.mid if link["relation"] == "inverse" else q.mid
        spread = q.ask - q.bid if q.ask is not None and q.bid is not None else None
        inputs.append(CompositeInput(prob, q.liquidity_usd, spread, q.usable, f"{q.venue}:{q.market_id}"))
        venues.append(q.venue)
    pooled = pool_logodds(inputs)
    if pooled is None:
        return []
    weights = pool_weights(inputs)
    used = {v for v, w in zip(venues, weights, strict=True) if w > 0}
    row = CompositeRow(
        event_id=event_id,
        outcome=outcome,
        ts_utc=at,
        prob=pooled,
        n_venues=len(used),
        weights_json=json.dumps(
            {i.label: round(w, 6) for i, w in zip(inputs, weights, strict=True)}, sort_keys=True
        ),
        method_version=METHOD_VERSION,
        computed_at=computed_at,
    )
    store.write_composite([row])
    return [row]
