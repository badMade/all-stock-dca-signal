"""Strict data contracts for plan-first recommendation inputs and outputs."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

SLOT_IDS = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_PRICE_WORDS = ("price", "close", "low", "quote", "bar")


def _object(value: Any, required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("expected an object")
    if set(value) != required:
        raise ValueError("object keys do not match the contract")
    return value


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("date must be an ISO string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid date") from None
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD")
    return parsed


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid datetime") from None
    return parsed


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _is_ticker(value: Any) -> bool:
    return isinstance(value, str) and _TICKER.fullmatch(value) is not None


def _finite(value: Any, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _json_copy(value: Any) -> Any:
    # Round-trip through JSON keeps public payloads detached and rejects non-JSON values.
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ValueError("payload must contain JSON values only") from None


def _aware(value: Any, name: str = "as_of") -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _valid_day(value: Any) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError("trading_day must be a date")


def _price_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(any(word in key.lower() for word in _PRICE_WORDS) or _price_keys(child)
                   for key, child in value.items())
    if isinstance(value, (tuple, list)):
        return any(_price_keys(child) for child in value)
    return False


@dataclass(frozen=True)
class MarketSnapshot:
    trading_day: date
    as_of: datetime
    source: str
    payload: dict[str, Any]
    quote_sides: dict[str, list[float] | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _valid_day(self.trading_day)
        _aware(self.as_of)
        if not isinstance(self.source, str) or self.source != "robinhood_agent_fetch":
            raise ValueError("unsupported market source")
        if not isinstance(self.payload, dict) or set(self.payload) != {"sources", "source_errors", "ticker_errors"}:
            raise ValueError("invalid market payload shape")
        if not isinstance(self.payload["sources"], dict) or not isinstance(self.payload["source_errors"], list) or not isinstance(self.payload["ticker_errors"], dict):
            raise ValueError("invalid market payload shape")
        _json_copy(self.payload)
        sides = self.quote_sides
        if not isinstance(sides, dict):
            raise ValueError("quote_sides must be an object")
        for ticker, pair in sides.items():
            if not _is_ticker(ticker) or (pair is not None and (
                not isinstance(pair, list) or len(pair) != 2 or any(
                    isinstance(side, bool) or not isinstance(side, (int, float)) or not math.isfinite(side)
                    for side in pair
                )
            )):
                raise ValueError("invalid quote_sides")

    def to_json(self) -> dict[str, Any]:
        return {"trading_day": self.trading_day.isoformat(), "as_of": self.as_of.isoformat(), "source": self.source,
                "payload": _json_copy(self.payload), "quote_sides": _json_copy(self.quote_sides or {})}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> MarketSnapshot:
        if not isinstance(value, dict) or set(value) not in (
            {"trading_day", "as_of", "source", "payload"},
            {"trading_day", "as_of", "source", "payload", "quote_sides"},
        ):
            raise ValueError("object keys do not match the contract")
        return cls(_day(value["trading_day"]), _time(value["as_of"]), value["source"], value["payload"],
                   value.get("quote_sides", {}))


@dataclass(frozen=True)
class BrokerState:
    trading_day: date
    as_of: datetime
    account_number: str
    orders_envelope: dict[str, Any]
    buying_power_usd: float | None

    def __post_init__(self) -> None:
        _valid_day(self.trading_day)
        _aware(self.as_of)
        _string(self.account_number, "account_number")
        if self.account_number == "<redacted>":
            raise ValueError("redacted broker state cannot be restored")
        if not isinstance(self.orders_envelope, dict):
            raise ValueError("orders_envelope must be an object")
        _finite(self.buying_power_usd, "buying_power_usd", optional=True)
        _json_copy(self.orders_envelope)

    def to_json(self) -> dict[str, Any]:
        # The account number is needed in-process for assembly, but serialized records can escape.
        return {"trading_day": self.trading_day.isoformat(), "as_of": self.as_of.isoformat(),
                "account_number": "<redacted>", "orders_envelope": _json_copy(self.orders_envelope),
                "buying_power_usd": self.buying_power_usd}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> BrokerState:
        obj = _object(value, {"trading_day", "as_of", "account_number", "orders_envelope", "buying_power_usd"})
        return cls(_day(obj["trading_day"]), _time(obj["as_of"]), obj["account_number"], obj["orders_envelope"],
                   obj["buying_power_usd"])


@dataclass(frozen=True)
class Recommendation:
    ticker: str
    usd: float
    ref_id: str

    def __post_init__(self) -> None:
        if not _is_ticker(self.ticker):
            raise ValueError("ticker must be uppercase and match the ticker contract")
        amount = _finite(self.usd, "usd")
        cents = Decimal(str(self.usd)) * 100
        if amount <= 0 or cents != cents.to_integral_value():
            raise ValueError("usd must be positive whole cents")
        _string(self.ref_id, "ref_id")
        try:
            UUID(self.ref_id)
        except ValueError:
            raise ValueError("ref_id must be a UUID") from None

    def to_json(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "usd": self.usd, "ref_id": self.ref_id}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Recommendation:
        obj = _object(value, {"ticker", "usd", "ref_id"})
        amount = _finite(obj["usd"], "usd")
        return cls(obj["ticker"], amount, obj["ref_id"])


@dataclass(frozen=True)
class RecommendationSet:
    trading_day: date
    as_of: datetime
    slot: str
    source: str
    ladder_version: str
    universe_hash: str
    items: tuple[Recommendation, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _valid_day(self.trading_day)
        _aware(self.as_of)
        if not isinstance(self.slot, str) or self.slot not in SLOT_IDS:
            raise ValueError("unknown slot")
        _string(self.source, "source")
        _string(self.ladder_version, "ladder_version")
        _string(self.universe_hash, "universe_hash")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported schema_version")
        if not isinstance(self.items, tuple) or any(not isinstance(item, Recommendation) for item in self.items):
            raise ValueError("items must be a tuple of recommendations")
        tickers = [item.ticker for item in self.items]
        if len(tickers) != len(set(tickers)):
            raise ValueError("items cannot repeat a ticker")
        # This boundary is deliberately price-blind; even future fields must not leak to buyers.
        if _price_keys({"items": [item.to_json() for item in self.items]}):
            raise ValueError("recommendation contains a forbidden price field")

    def to_json(self) -> dict[str, Any]:
        data = {"schema_version": self.schema_version, "trading_day": self.trading_day.isoformat(),
                "as_of": self.as_of.isoformat(), "slot": self.slot, "source": self.source,
                "ladder_version": self.ladder_version, "universe_hash": self.universe_hash,
                "items": [item.to_json() for item in self.items]}
        if _price_keys(data):
            raise ValueError("recommendation contains a forbidden price field")
        return data

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> RecommendationSet:
        required = {"schema_version", "trading_day", "as_of", "slot", "source", "ladder_version", "universe_hash", "items"}
        obj = _object(value, required)
        if not isinstance(obj["items"], list):
            raise ValueError("items must be a list")
        items = tuple(Recommendation.from_json(item) for item in obj["items"])
        return cls(_day(obj["trading_day"]), _time(obj["as_of"]), obj["slot"], obj["source"],
                   obj["ladder_version"], obj["universe_hash"], items, obj["schema_version"])


@dataclass(frozen=True)
class ExpectedPrice:
    ticker: str
    decision_price: float
    prior_close: float
    prior_low: float | None
    multiplier: float
    drop_pct: float
    notional_usd: float

    def __post_init__(self) -> None:
        if not _is_ticker(self.ticker):
            raise ValueError("ticker must be uppercase and match the ticker contract")
        for name in ("decision_price", "prior_close", "prior_low", "multiplier", "drop_pct", "notional_usd"):
            value = _finite(getattr(self, name), name, optional=name == "prior_low")
            # Prices, multiplier and notional must be strictly positive (a known prior low too):
            # fill comparison divides by decision_price, and a zero or negative value here is a
            # corrupt record, never a real buy (PR #1667 review). A drop may be 0 only at the edge.
            if value is not None and (value < 0 if name == "drop_pct" else value <= 0):
                raise ValueError(f"{name} must be positive")

    def to_json(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "decision_price": self.decision_price, "prior_close": self.prior_close,
                "prior_low": self.prior_low, "multiplier": self.multiplier, "drop_pct": self.drop_pct,
                "notional_usd": self.notional_usd}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> ExpectedPrice:
        obj = _object(value, {"ticker", "decision_price", "prior_close", "prior_low", "multiplier", "drop_pct", "notional_usd"})
        values = {key: _finite(obj[key], key, optional=key == "prior_low") for key in
                  ("decision_price", "prior_close", "prior_low", "multiplier", "drop_pct", "notional_usd")}
        return cls(obj["ticker"], **values)


@dataclass(frozen=True)
class ExpectedPriceSet:
    trading_day: date
    as_of: datetime
    slot: str
    deployed_7d_source: str
    items: tuple[ExpectedPrice, ...]
    budget: dict[str, Any]

    def __post_init__(self) -> None:
        _valid_day(self.trading_day)
        _aware(self.as_of)
        if not isinstance(self.slot, str) or self.slot not in SLOT_IDS:
            raise ValueError("unknown slot")
        _string(self.deployed_7d_source, "deployed_7d_source")
        if not isinstance(self.items, tuple) or any(not isinstance(item, ExpectedPrice) for item in self.items):
            raise ValueError("items must be a tuple of expected prices")
        if not isinstance(self.budget, dict) or not isinstance(self.budget.get("budget_tier"), str):
            raise ValueError("budget must include budget_tier")
        _string(self.budget["budget_tier"], "budget_tier")
        for key in ("daily_cap_usd", "weekly_cap_usd"):
            value = _finite(self.budget.get(key), key)
            if value <= 0:
                raise ValueError(f"{key} must be positive")
        _json_copy(self.budget)

    def to_json(self) -> dict[str, Any]:
        return {"trading_day": self.trading_day.isoformat(), "as_of": self.as_of.isoformat(), "slot": self.slot,
                "deployed_7d_source": self.deployed_7d_source, "items": [item.to_json() for item in self.items],
                "budget": _json_copy(self.budget)}

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> ExpectedPriceSet:
        obj = _object(value, {"trading_day", "as_of", "slot", "deployed_7d_source", "items", "budget"})
        if not isinstance(obj["items"], list):
            raise ValueError("items must be a list")
        items = tuple(ExpectedPrice.from_json(item) for item in obj["items"])
        return cls(_day(obj["trading_day"]), _time(obj["as_of"]), obj["slot"], obj["deployed_7d_source"], items,
                   obj["budget"])
