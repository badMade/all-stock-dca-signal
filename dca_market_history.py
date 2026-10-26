"""Persist and assemble the courier's raw Robinhood responses for one trading day."""

from __future__ import annotations

import json
import math
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import dca_config
from dca_fileio import atomic_write_text
from dca_live_adapters import _is_error_response, _to_float, _unwrap_traversed_layers
from dca_market_sources import ET, SourceUnavailable, _quote_sides, _raw_response
from dca_rec_models import BrokerState, MarketSnapshot
from scripts import assemble_market_data, assemble_order_pages


def orders_window_start(trading_day: date) -> datetime:
    """00:00 ET on the earlier of this ISO week's Monday and seven days back.

    The weekly cap sums the ISO week, but the kill switch's broker fallback
    (``dca_order_plan.trailing_deployed_usd``) sums the trailing seven days. A Monday start
    would drop last Thursday and Friday on a Wednesday and undercount it, leaving the 1x cap
    off when it should engage (PR #1667 review). ``budget_snapshot`` still limits weekly
    accounting to the ISO week.
    """
    monday = trading_day - timedelta(days=trading_day.weekday())
    return datetime.combine(min(monday, trading_day - timedelta(days=7)), time.min, ET)


class MarketHistoryStore:
    """Own the raw response tree and merge each courier run into a daily snapshot."""

    def __init__(self, day_dir: Path) -> None:
        self.day_dir = Path(day_dir)
        self.raw_dir = self.day_dir / "raw"

    @staticmethod
    def _strict_json(text: str) -> Any:
        return json.loads(
            text,
            object_pairs_hook=dca_config._reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"non-finite token {token}")),
        )

    @classmethod
    def _load_response(cls, path: Path) -> dict[str, Any]:
        try:
            value = cls._strict_json(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and "content" in value and "structuredContent" not in value:
                blocks = value.get("content")
                text_blocks = [block for block in blocks if isinstance(block, dict) and block.get("type") == "text"]
                if not isinstance(blocks, list) or len(text_blocks) != 1:
                    raise ValueError
            response = _raw_response(value) if "structuredContent" in value or "content" in value else value
            if not isinstance(response, dict):
                raise ValueError
            # Structured MCP content has already been decoded, so validate it again as strict JSON.
            json.dumps(response, allow_nan=False)
            return response
        except (OSError, UnicodeError, ValueError, TypeError):
            raise SourceUnavailable("courier response is unavailable or invalid") from None

    @staticmethod
    def _indexed(run_dir: Path, prefix: str, *, required: bool = True) -> list[Path]:
        try:
            files = sorted(run_dir.glob(f"{prefix}_*.json"), key=lambda path: int(path.stem.rsplit("_", 1)[1]))
        except (IndexError, ValueError):
            raise SourceUnavailable("courier response sequence is invalid") from None
        if required and not files:
            raise SourceUnavailable("courier run is incomplete")
        expected = [f"{prefix}_{index}.json" for index in range(len(files))]
        if [path.name for path in files] != expected:
            raise SourceUnavailable("courier response sequence is incomplete")
        return files

    def _read_merged_bars(self) -> dict[str, dict[str, Any]]:
        path = self.raw_dir / "day_bars.json"
        if not path.exists():
            return {}
        try:
            value = self._strict_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise SourceUnavailable("stored market history is invalid") from None
        if not isinstance(value, list):
            raise SourceUnavailable("stored market history is invalid")
        merged = {}
        for response in value:
            try:
                normalized = _raw_response(response) if "structuredContent" in response or "content" in response else response
                body = normalized.get("data", normalized)
                results = body["results"]
                if len(results) != 1 or not isinstance(results[0].get("symbol"), str):
                    raise ValueError
                symbol = results[0]["symbol"].upper()
                bars = results[0].get("bars")
                if not isinstance(bars, list) or not all(
                    isinstance(bar, dict) and isinstance(bar.get("begins_at"), str) for bar in bars
                ):
                    raise ValueError
                merged[symbol] = {"symbol": symbol, "bars": bars}
            except (KeyError, TypeError, ValueError):
                raise SourceUnavailable("stored market history is invalid") from None
        return merged

    def _merge_bars(self, run_dir: Path) -> list[dict[str, Any]]:
        merged = self._read_merged_bars()
        for path in self._indexed(run_dir, "bars5m"):
            response = self._load_response(path)
            body = response.get("data", response)
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list):
                raise SourceUnavailable("courier market history is invalid")
            for row in results:
                if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or not isinstance(row.get("bars"), list):
                    raise SourceUnavailable("courier market history is invalid")
                symbol = row["symbol"].upper()
                by_start = {bar.get("begins_at"): bar for bar in merged.get(symbol, {}).get("bars", [])
                            if isinstance(bar, dict) and isinstance(bar.get("begins_at"), str)}
                for bar in row["bars"]:
                    if not isinstance(bar, dict) or not isinstance(bar.get("begins_at"), str):
                        raise SourceUnavailable("courier market history is invalid")
                    by_start[bar["begins_at"]] = bar  # Later fetch replaces an incomplete overlapping bar.
                merged[symbol] = {"symbol": symbol, "bars": [by_start[key] for key in sorted(by_start)]}
        result = [{"results": [merged[symbol]]} for symbol in sorted(merged)]
        atomic_write_text(self.raw_dir / "day_bars.json", json.dumps(result, allow_nan=False, separators=(",", ":")))
        return result

    def _daily(self, run_dir: Path) -> list[dict[str, Any]]:
        stored = self.raw_dir / "daily.json"
        if stored.exists():
            try:
                saved = self._strict_json(stored.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                raise SourceUnavailable("stored daily history is invalid") from None
            if not isinstance(saved, list) or not all(isinstance(item, dict) for item in saved):
                raise SourceUnavailable("stored daily history is invalid")
            return saved
        paths = self._indexed(run_dir, "daily", required=False)
        if not paths:
            return []
        responses = [self._load_response(path) for path in paths]
        # Keep every first-run chunk; the day-level snapshot must cover the whole universe.
        atomic_write_text(stored, json.dumps(responses, allow_nan=False, separators=(",", ":")))
        return responses

    @staticmethod
    def _buying_power(response: dict[str, Any]) -> float | None:
        # Match RobinhoodBroker's documented balance aliases while refusing ambiguous values.
        aliases = ("buying_power", "cash", "unallocated_buying_power", "portfolio_cash", "withdrawable_amount")
        layers = [response, *_unwrap_traversed_layers(response)]
        if any(_is_error_response(layer) is not None for layer in layers):
            return None
        for layer in layers:
            for name in aliases:
                if name not in layer:
                    continue
                value = _to_float(layer[name], default=math.nan)
                if not math.isfinite(value):
                    return None
                return float(value)
        return None

    def ingest_run(
        self, run_dir: Path, *, trading_day: date, now: datetime, universe: list[str], account_number: str,
    ) -> tuple[MarketSnapshot, BrokerState]:
        """Validate one courier run, update raw history, and return the assembled market/broker state."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(account_number, str) or not account_number.strip():
            raise ValueError("account_number is required")
        run_dir = Path(run_dir)
        bars = self._merge_bars(run_dir)
        daily = self._daily(run_dir)
        quote_paths = self._indexed(run_dir, "quotes")
        quotes = [self._load_response(path) for path in quote_paths]
        raw = {"historicals": bars, "daily": daily, "quotes": quotes}
        payload = assemble_market_data.assemble(raw, trading_day, list(dict.fromkeys(universe)))
        errors = (payload.get("ticker_errors") or {}).get("robinhood")
        if universe and isinstance(errors, list) and len(errors) >= len(set(universe)):
            raise SourceUnavailable("market source returned no usable universe data")

        order_paths = self._indexed(run_dir, "orders")
        order_pages = [self._load_response(path) for path in order_paths]
        with tempfile.TemporaryDirectory(prefix="dca-market-orders-") as temp_dir:
            page_files = []
            for index, page in enumerate(order_pages):
                path = Path(temp_dir) / f"page-{index}.json"
                atomic_write_text(path, json.dumps(page, allow_nan=False))
                page_files.append(str(path))
            try:
                envelope, _ = assemble_order_pages.assemble(
                    page_files, account_number=account_number, trading_day=trading_day.isoformat(),
                    snapshot_at=now.isoformat(),
                )
            except (ValueError, assemble_order_pages.AssemblyRefused):
                raise SourceUnavailable("order history incomplete") from None
        try:
            portfolio = self._load_response(run_dir / "portfolio.json")
        except SourceUnavailable:
            portfolio = {}
        broker = BrokerState(trading_day, now, account_number, envelope, self._buying_power(portfolio))
        snapshot = MarketSnapshot(trading_day, now, "robinhood_agent_fetch", payload, _quote_sides(quotes))
        return snapshot, broker

    def fetch_plan(
        self, *, trading_day: date, now: datetime, universe: list[str], account_number: str,
    ) -> dict[str, Any]:
        """Return the courier's exact MCP calls and verbatim response destinations."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(account_number, str) or not account_number.strip():
            raise ValueError("account_number is required")
        now_et = now.astimezone(ET)
        # "15h05m00s", not bare HHMMSS digits: a six-digit run id trips the account-number invariant
        # (tests/test_config_invariants.py) wherever a fixture or doc spells one out.
        run_dir = (self.raw_dir / "incoming" / now_et.strftime("%Hh%Mm%Ss")).resolve()
        symbols = list(dict.fromkeys(universe))
        # get_equity_quotes takes up to 25 symbols per call; get_equity_historicals rejects anything over 10
        # ("too many symbols ... max 10", 2026-10-26), so the two tools need separate chunk sizes.
        chunks = [symbols[index:index + 25] for index in range(0, len(symbols), 25)]
        history_chunks = [symbols[index:index + 10] for index in range(0, len(symbols), 10)]
        stored_bars = self._read_merged_bars()
        latest: datetime | None = None
        for row in stored_bars.values():
            for bar in row["bars"]:
                stamp = bar.get("begins_at") if isinstance(bar, dict) else None
                if isinstance(stamp, str):
                    try:
                        parsed = datetime.fromisoformat(stamp)
                        if parsed.tzinfo is None or parsed.utcoffset() is None:
                            raise ValueError
                    except ValueError:
                        raise SourceUnavailable("stored market history is invalid") from None
                    parsed = parsed.astimezone(ET)
                    if latest is None or parsed > latest:
                        latest = parsed
        open_time = datetime.combine(trading_day, time(9, 30), ET)
        start = open_time
        if latest is not None:
            start = max(open_time, latest - timedelta(minutes=10))
        calls = []
        for index, chunk in enumerate(chunks):
            calls.append({"tool": "get_equity_quotes", "args": {"symbols": chunk}, "save_as": f"quotes_{index}.json", "paginate": False})
        for index, chunk in enumerate(history_chunks):
            calls.append({"tool": "get_equity_historicals", "args": {
                "symbols": chunk, "start_time": start.isoformat(), "end_time": now_et.isoformat(),
                "interval": "5minute", "bounds": "regular", "adjustment_type": "none",
            }, "save_as": f"bars5m_{index}.json", "paginate": False})
        if not (self.raw_dir / "daily.json").exists():
            daily_start = datetime.combine(trading_day - timedelta(days=14), time.min, ET)
            for index, chunk in enumerate(history_chunks):
                calls.append({"tool": "get_equity_historicals", "args": {
                    "symbols": chunk, "start_time": daily_start.isoformat(), "end_time": now_et.isoformat(),
                    "interval": "day", "bounds": "regular", "adjustment_type": "none",
                }, "save_as": f"daily_{index}.json", "paginate": False})
        calls.extend([
            {"tool": "get_equity_orders", "args": {
                "account_number": account_number,
                "created_at_gte": orders_window_start(trading_day).isoformat(),
            }, "save_as": "orders_0.json", "paginate": True},
            {"tool": "get_portfolio", "args": {"account_number": account_number}, "save_as": "portfolio.json", "paginate": False},
        ])
        return {"run_dir": str(run_dir), "calls": calls}
