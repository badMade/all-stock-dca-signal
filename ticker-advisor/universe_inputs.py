"""The adapter between the two feeds and the ranking engine (feed step 6).

Specified first by tests/test_universe_inputs.py (docs/fundamentals_feed_scope.md). It reads the fundamentals
SNAPSHOT file (the contract written by ``dca-sizing-signals``' ``fundamentals_snapshot``; this package does not
import that one, so the hash rule is restated in :func:`_digest` and pinned by tests) and the price-derived inputs
from ``price_factors``, and produces what ``universe.py`` consumes: pool ``Candidate`` records, ``FactorInputs`` and
``TickerStatus`` records, plus a reason for every name left out of the factor inputs.

Definitions:

* profitability = gross profit / total assets; gross profit is the ``GrossProfit`` fact, or revenue minus cost of
  revenue when ``GrossProfit`` is absent and both facts cover the same period. Assets must be positive. The assets
  figure is the latest reported instant, which can be later than the profit period.
* value = book-to-price = stockholders' equity / market cap, market cap = shares outstanding times the last price.
  Negative equity gives a negative value, not None.
* sector and industry come from the SIC fields. A name with none is left out of the factor inputs ("unscored"),
  never given a made-up sector. Missing data is None, never zero.
* pinned names are removed from every output; they are never ranked.
* fractional eligibility is not checked in the ranking, so every candidate carries ``fractional_eligible=None``;
  pair it with ``PoolThresholds(require_fractional_eligible=False)``.
* ``security_type`` is ``common_stock`` only for a name with SEC facts AND a SIC code; otherwise it stays None (an
  unknown value holds an incumbent, it never demotes it). ``nms_listed`` comes from a caller-supplied exchange map.

Pure stdlib, no network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from price_factors import PriceInputs
from universe import Candidate, FactorInputs, TickerStatus

SNAPSHOT_KIND = "fundamentals_snapshot"
SNAPSHOT_SCHEMA_VERSION = 1
# Exchanges that are part of the National Market System. Compared upper-cased.
NMS_EXCHANGES = frozenset({"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE ARCA", "CBOE"})
_GROSS_PROFIT = "us-gaap:GrossProfit"
_REVENUES = "us-gaap:Revenues"
_COST_OF_REVENUE = "us-gaap:CostOfRevenue"
_ASSETS = "us-gaap:Assets"
_EQUITY = "us-gaap:StockholdersEquity"
_SHARES = "dei:EntityCommonStockSharesOutstanding"


@dataclass(frozen=True)
class RankingInputs:
    """What the engine consumes, plus the reasons some names were left out and the snapshot's identity."""

    candidates: tuple[Candidate, ...]
    factor_inputs: tuple[FactorInputs, ...]
    statuses: tuple[TickerStatus, ...]
    unscored: Mapping[str, str]
    snapshot_fetched_at: datetime
    snapshot_sha256: str


def _digest(document: Mapping[str, Any]) -> str:
    # The snapshot writer's rule: everything except fetched_at and the hash itself, canonical JSON.
    content = {k: v for k, v in document.items() if k not in ("fetched_at", "content_sha256")}
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _validate(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot is not a JSON object")
    if snapshot.get("kind") != SNAPSHOT_KIND:
        raise ValueError("snapshot has the wrong kind")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot has an unknown schema version")
    try:
        matches = isinstance(snapshot.get("content_sha256"), str) and _digest(snapshot) == snapshot["content_sha256"]
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError("snapshot hash does not match its content")
    if not isinstance(snapshot.get("symbols"), dict):
        raise ValueError("snapshot has no symbols object")
    return snapshot


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """Read and validate a snapshot file. Raises ``ValueError`` (bad JSON, kind, version or hash), ``OSError``."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise ValueError("snapshot file is not strict JSON") from exc
    return _validate(document)


def _number(fact: Mapping[str, Any] | None) -> float | None:
    if not fact:
        return None
    value = fact.get("val")
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)


def _profitability(facts: Mapping[str, Any]) -> float | None:
    gross = _number(facts.get(_GROSS_PROFIT))
    if gross is None:
        revenue, cost = facts.get(_REVENUES), facts.get(_COST_OF_REVENUE)
        revenue_value, cost_value = _number(revenue), _number(cost)
        same_period = bool(revenue and cost) and (revenue.get("start"), revenue.get("end")) == (
            cost.get("start"),
            cost.get("end"),
        )
        has_both = revenue_value is not None and cost_value is not None
        gross = revenue_value - cost_value if same_period and has_both else None
    assets = _number(facts.get(_ASSETS))
    if gross is None or assets is None or assets <= 0:
        return None
    return gross / assets


def _market_cap(facts: Mapping[str, Any], price: float | None) -> float | None:
    shares = _number(facts.get(_SHARES))
    if shares is None or price is None:
        return None
    cap = shares * price
    return cap if cap > 0 else None


def _is_nms(exchange_by_symbol: Mapping[str, str | None] | None, symbol: str) -> bool | None:
    exchange = (exchange_by_symbol or {}).get(symbol)
    return None if exchange is None else exchange.strip().upper() in NMS_EXCHANGES


def build_ranking_inputs(
    snapshot: Mapping[str, Any],
    price_inputs: Mapping[str, PriceInputs],
    *,
    pinned: Iterable[str] = (),
    exchange_by_symbol: Mapping[str, str | None] | None = None,
) -> RankingInputs:
    """Turn a validated snapshot and the price inputs into the engine's inputs (``ValueError``: bad snapshot)."""
    document = _validate(dict(snapshot))
    fetched_at = datetime.fromisoformat(document["fetched_at"])
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("snapshot fetched_at must be timezone-aware")
    skip = set(pinned)
    candidates: list[Candidate] = []
    factors: list[FactorInputs] = []
    statuses: list[TickerStatus] = []
    unscored: dict[str, str] = {}
    for symbol in sorted(s for s in document["symbols"] if s not in skip):
        record = document["symbols"][symbol]
        status = record.get("status")
        px = price_inputs.get(symbol)
        facts = record.get("facts", {}) if status == "ok" else {}
        price = px.last_price if px else None
        sector, industry = record.get("sector"), record.get("industry")
        candidates.append(
            Candidate(
                ticker=symbol,
                nms_listed=_is_nms(exchange_by_symbol, symbol),
                security_type="common_stock" if status == "ok" and record.get("sic") is not None else None,
                price=price,
                market_cap=_market_cap(facts, price),
                median_dollar_volume_60d=px.median_dollar_volume_60d if px else None,
                history_sessions=px.history_sessions if px else None,
                fractional_eligible=None,
            )
        )
        statuses.append(
            TickerStatus(
                ticker=symbol,
                fractional_eligible=None,
                sessions_below_min_price=px.sessions_below_min_price if px else 0,
            )
        )
        if status != "ok":
            unscored[symbol] = f"snapshot:{status}"
            continue
        if sector is None or industry is None:
            unscored[symbol] = "no_sector"
            continue
        equity, cap = _number(facts.get(_EQUITY)), _market_cap(facts, price)
        factors.append(
            FactorInputs(
                ticker=symbol,
                sector=sector,
                industry=industry,
                momentum_6m=px.momentum_6m if px else None,
                momentum_12m=px.momentum_12m if px else None,
                profitability=_profitability(facts),
                volatility_1y=px.volatility_1y if px else None,
                value=None if equity is None or cap is None else equity / cap,
            )
        )
    return RankingInputs(
        candidates=tuple(candidates),
        factor_inputs=tuple(factors),
        statuses=tuple(statuses),
        unscored=unscored,
        snapshot_fetched_at=fetched_at,
        snapshot_sha256=str(document["content_sha256"]),
    )
