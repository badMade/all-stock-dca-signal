"""Read-only market and broker data sources for the recommendation pipeline."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import dca_config
from dca_rec_models import MarketSnapshot
from scripts import assemble_market_data

ET = ZoneInfo("America/New_York")


class SourceUnavailable(Exception):
    """A required upstream source did not provide usable data."""


class MarketSource(Protocol):
    def fetch(self, trading_day: date, now: datetime, universe: list[str]) -> MarketSnapshot: ...


def _raw_response(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("MCP tool result is not an object")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    decoded = json.loads(block["text"], object_pairs_hook=dca_config._reject_duplicate_keys,
                                         parse_constant=_reject_constant)
                except ValueError:
                    raise ValueError("MCP text result is not valid JSON") from None
                if isinstance(decoded, dict):
                    return decoded
                raise ValueError("MCP text result is not an object")
    raise ValueError("MCP tool result has no object response")


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON token: {token}")


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now


def _quote_sides(raw_quotes: list[Any]) -> dict[str, list[float] | None]:
    """Preserve the raw two-sided quote fields because Gate 6.7 needs both sides."""
    sides: dict[str, list[float] | None] = {}
    for response in raw_quotes:
        body = response.get("data", response) if isinstance(response, dict) else {}
        results = body.get("results", []) if isinstance(body, dict) else []
        for row in results:
            if not isinstance(row, dict):
                continue
            quote = row.get("quote") if isinstance(row.get("quote"), dict) else row
            symbol = row.get("symbol", quote.get("symbol"))
            if not isinstance(symbol, str):
                continue
            bid = quote.get("bid_price", quote.get("best_bid"))
            ask = quote.get("ask_price", quote.get("best_ask"))
            try:
                if isinstance(bid, bool) or isinstance(ask, bool):
                    raise ValueError
                pair = [float(bid), float(ask)]
                if not all(math.isfinite(value) for value in pair):
                    pair = None
            except (OverflowError, TypeError, ValueError):
                pair = None
            if pair is not None and (pair[0] <= 0 or pair[1] <= 0 or pair[1] < pair[0]):
                pair = None
            sides[symbol.upper()] = pair
    return sides


class AgentFetchSource:
    """Read a fresh raw response file written by the tiny agent fetch task."""

    def __init__(self, path: str | Path, *, max_age: timedelta = timedelta(minutes=15)) -> None:
        if max_age.total_seconds() <= 0:
            raise ValueError("max_age must be positive")
        self.path = Path(path)
        self.max_age = max_age

    def fetch(self, trading_day: date, now: datetime, universe: list[str]) -> MarketSnapshot:
        now = _aware(now)
        try:
            age = now.timestamp() - self.path.stat().st_mtime
            if age < 0 or age > self.max_age.total_seconds():
                raise SourceUnavailable("agent market file is stale")
            raw = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=dca_config._reject_duplicate_keys,
                             parse_constant=_reject_constant)
        except SourceUnavailable:
            raise
        except (OSError, ValueError, UnicodeError):
            raise SourceUnavailable("agent market file is unavailable or invalid") from None
        if not isinstance(raw, dict) or set(raw) - {"historicals", "daily", "quotes"}:
            raise SourceUnavailable("agent market file has an invalid shape")
        if not all(isinstance(raw.get(key), list) for key in ("historicals", "quotes")):
            raise SourceUnavailable("agent market file has an invalid shape")
        raw.setdefault("daily", [])
        if not isinstance(raw["daily"], list):
            raise SourceUnavailable("agent market file has an invalid shape")
        payload = assemble_market_data.assemble(raw, trading_day, list(dict.fromkeys(universe)))
        errors = payload.get("ticker_errors", {}).get("robinhood")
        if universe and isinstance(errors, list) and len(errors) >= len(set(universe)):
            raise SourceUnavailable("market source returned no usable universe data")
        return MarketSnapshot(trading_day, now, "robinhood_agent_fetch", payload, _quote_sides(raw["quotes"]))
