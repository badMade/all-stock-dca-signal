"""Strict data contracts for plan-first recommendation inputs and outputs."""

from __future__ import annotations

import hashlib
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


_ACCOUNT_KEYS = frozenset({"account_number", "account", "account_id", "account_url"})


def _redact_account(value: Any, account: str) -> Any:
    """A copy of ``value`` with every account identifier replaced by ``<redacted>``.

    Redacts by key (``account_number``, ``account``, ...) at any depth, and by value: any string that
    contains the known account number, such as a broker account url, is rewritten too.
    """
    if isinstance(value, dict):
        return {key: "<redacted>" if key in _ACCOUNT_KEYS and isinstance(item, (str, int)) and not isinstance(item, bool)
                else _redact_account(item, account) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_account(item, account) for item in value]
    if isinstance(value, str) and account and account in value:
        return value.replace(account, "<redacted>")
    return value


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
        # The account number is needed in-process for assembly, but serialized records can escape. The
        # assembler also stamps it into the envelope (and raw pages carry account urls), so redact it
        # THROUGHOUT the serialized copy; the in-memory broker evidence is never modified (PR #1815 review).
        return {"trading_day": self.trading_day.isoformat(), "as_of": self.as_of.isoformat(),
                "account_number": "<redacted>",
                "orders_envelope": _redact_account(_json_copy(self.orders_envelope), self.account_number),
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


# A carried universe is at most this many symbols (docs/adr-backup-planner-daily-universe.md).
MAX_CARRIED_UNIVERSE = 100


def _universe_hash(tickers: tuple[str, ...]) -> str:
    """sha256 over the sorted, de-duplicated tickers joined by newlines plus a trailing newline.

    The same rule as ``dca_order_plan.universe_hash``, restated because that module imports this one's world;
    ``tests/test_dca_rec_models.py`` pins the two together.
    """
    return hashlib.sha256(("\n".join(sorted(set(tickers))) + "\n").encode()).hexdigest()


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
    # What the kill switch looked like when this set was sized: {"max_multiplier": the largest ladder multiplier
    # among the items, "deployed_7d_usd": the trailing-7-day figure it was sized under}. The agent payload is
    # price-blind and carries no multiplier, so without this stamp the backup cannot tell whether a set sized above
    # 1x is still safe to place after other slots' spend engages the switch. None = an unstamped (older) set; the
    # backup fails closed on it. Optional on the wire so readers of older payloads still parse.
    sizing_guard: dict[str, float] | None = None
    # The day's buy list, sorted, when the set carries it (docs/adr-backup-planner-daily-universe.md). The list changes
    # daily, so the backup planner cannot rely on a copy bundled at build time. When present it must hash to
    # ``universe_hash`` and every item must be in it. None = an older set that carries no list.
    universe: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _valid_day(self.trading_day)
        _aware(self.as_of)
        if not isinstance(self.slot, str) or self.slot not in SLOT_IDS:
            raise ValueError("unknown slot")
        _string(self.source, "source")
        if self.sizing_guard is not None:
            guard = self.sizing_guard
            if not isinstance(guard, dict) or set(guard) != {"max_multiplier", "deployed_7d_usd"}:
                raise ValueError("sizing_guard must carry exactly max_multiplier and deployed_7d_usd")
            if _finite(guard["max_multiplier"], "max_multiplier") < 1.0:
                raise ValueError("max_multiplier must be at least 1")
            if _finite(guard["deployed_7d_usd"], "deployed_7d_usd") < 0:
                raise ValueError("deployed_7d_usd cannot be negative")
        _string(self.ladder_version, "ladder_version")
        _string(self.universe_hash, "universe_hash")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported schema_version")
        if not isinstance(self.items, tuple) or any(not isinstance(item, Recommendation) for item in self.items):
            raise ValueError("items must be a tuple of recommendations")
        tickers = [item.ticker for item in self.items]
        if len(tickers) != len(set(tickers)):
            raise ValueError("items cannot repeat a ticker")
        if self.universe is not None:
            universe = self.universe
            if not isinstance(universe, tuple) or not universe or len(universe) > MAX_CARRIED_UNIVERSE:
                raise ValueError(f"universe must be a non-empty tuple of at most {MAX_CARRIED_UNIVERSE} tickers")
            if not all(_is_ticker(t) for t in universe) or list(universe) != sorted(set(universe)):
                raise ValueError("universe must be sorted, unique tickers that match the ticker contract")
            if self.universe_hash != _universe_hash(universe):
                raise ValueError("universe_hash does not match the carried universe")
            if any(t not in set(universe) for t in tickers):
                raise ValueError("every recommended ticker must be in the carried universe")
        # This boundary is deliberately price-blind; even future fields must not leak to buyers.
        if _price_keys({"items": [item.to_json() for item in self.items]}):
            raise ValueError("recommendation contains a forbidden price field")

    def to_json(self) -> dict[str, Any]:
        data = {"schema_version": self.schema_version, "trading_day": self.trading_day.isoformat(),
                "as_of": self.as_of.isoformat(), "slot": self.slot, "source": self.source,
                "ladder_version": self.ladder_version, "universe_hash": self.universe_hash,
                "items": [item.to_json() for item in self.items]}
        if self.sizing_guard is not None:
            data["sizing_guard"] = _json_copy(self.sizing_guard)
        if self.universe is not None:
            data["universe"] = list(self.universe)
        if _price_keys(data):
            raise ValueError("recommendation contains a forbidden price field")
        return data

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> RecommendationSet:
        required = {"schema_version", "trading_day", "as_of", "slot", "source", "ladder_version", "universe_hash", "items"}
        # sizing_guard and universe are the optional keys: absent on a set written before they existed.
        optional = {"sizing_guard", "universe"} & set(value) if isinstance(value, dict) else set()
        obj = _object(value, required | optional)
        if not isinstance(obj["items"], list):
            raise ValueError("items must be a list")
        if "universe" in obj and not isinstance(obj["universe"], list):
            raise ValueError("universe must be a list")
        items = tuple(Recommendation.from_json(item) for item in obj["items"])
        return cls(_day(obj["trading_day"]), _time(obj["as_of"]), obj["slot"], obj["source"],
                   obj["ladder_version"], obj["universe_hash"], items, obj["schema_version"],
                   obj.get("sizing_guard"), tuple(obj["universe"]) if "universe" in obj else None)


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
