"""Compare the price the program expected with the fill the agent actually got.

The buying agent only ever sees ``{ticker, usd, ref_id}``. The decision price stays on the
program side in ``expected_<slot>_<HHMM>.json`` (dca_rec_store), so this is the one place
expected and actual meet. It covers the six prompt-driven slots, which have had no
expected-vs-actual record at all; ``dca_reporting.compute_slippage_rows`` only runs on the
3:30 PM ``run_slot`` path.

Inputs, all files on disk and nothing the agent computes:

* ``handed_<slot>.json``: written by ``scripts/read_recommendation`` when it hands a slot its
  buys. It names the exact recommendation (``as_of``) the agent acted on.
* ``placements/<slot>/<TICKER>.json``: the raw ``place_equity_order`` response the agent saved.
* Raw broker order rows from a later ``get_equity_orders`` fetch. The 5-minute courier
  fetches them anyway, and they carry the fill once it happens.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import dca_config
from dca_fileio import atomic_write_text
from dca_rec_models import SLOT_IDS, RecommendationSet
from dca_rec_store import RecommendationStore
from scripts.broker_reconciliation import _to_float, normalize_order
from scripts.validate_native_placement import _order_of

_TICKER_FILE = re.compile(r"^([A-Z][A-Z0-9.\-]{0,9})\.json$")


@dataclass(frozen=True)
class FillRow:
    """One placed buy: what was expected, what happened, and the slippage when both are known."""

    slot: str
    ticker: str
    order_id: str | None
    requested_usd: float | None
    expected_price: float | None
    recommendation_as_of: str | None
    status: str  # filled | partially_filled | pending | no_fill | order_not_found | placement_unreadable
    broker_state: str | None = None
    fill_price: float | None = None
    filled_usd: float | None = None
    filled_shares: float | None = None
    slippage_pct: float | None = None


def _read(path: Path) -> Any:
    return json.loads(path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys)


def slippage_pct(fill_price: float, expected_price: float) -> float:
    """``(fill / expected - 1) * 100``: ``dca_reporting``'s convention, so the two reports agree.

    Positive means the buy paid more than the program expected; negative means it filled cheaper.
    """
    return round((fill_price / expected_price - 1.0) * 100.0, 4)


class FillComparator:
    """Join handed recommendations, saved placement responses and broker order rows."""

    def __init__(self, store: RecommendationStore) -> None:
        self.store = store

    def _handed(self, day: date, slot: str) -> RecommendationSet | None:
        path = self.store.handed_path(day, slot)
        if not path.exists():
            return None
        return RecommendationSet.from_json(_read(path))

    def compare(self, day: date, order_rows: Iterable[Mapping[str, Any]]) -> list[FillRow]:
        """Every saved placement on ``day``, matched by broker order id (never by ticker alone).

        Robinhood assigns its own ref_id (scripts/validate_native_placement.py, 2026-10-12), so the
        order id in the placement response is the only reliable join key.
        """
        by_id: dict[str, tuple[Any, Mapping[str, Any]]] = {}
        for raw in order_rows:
            normalized = normalize_order(raw)
            if normalized is not None:
                # A later page or fetch of the same order replaces the earlier one: newer state wins.
                by_id[normalized.order_id] = (normalized, raw)
        rows: list[FillRow] = []
        root = self.store.day_dir(day) / "placements"
        for slot in SLOT_IDS:
            slot_dir = root / slot
            if not slot_dir.is_dir():
                continue
            handed = self._handed(day, slot)
            expected = {item.ticker: item for item in self.store.expected_for(handed).items} if handed else {}
            requested = {item.ticker: item.usd for item in handed.items} if handed else {}
            as_of = handed.as_of.isoformat() if handed else None
            for path in sorted(slot_dir.iterdir()):
                match = _TICKER_FILE.match(path.name)
                if not match:
                    continue
                ticker = match.group(1)
                base = {"slot": slot, "ticker": ticker, "requested_usd": requested.get(ticker),
                        "expected_price": expected[ticker].decision_price if ticker in expected else None,
                        "recommendation_as_of": as_of}
                try:
                    order = _order_of(_read(path))
                except (OSError, ValueError, UnicodeError):
                    order = None
                order_id = str(order.get("id")).strip() if order and order.get("id") else None
                if not order_id:
                    rows.append(FillRow(order_id=None, status="placement_unreadable", **base))
                    continue
                found = by_id.get(order_id)
                if found is None:
                    rows.append(FillRow(order_id=order_id, status="order_not_found", **base))
                    continue
                normalized, raw = found
                rows.append(self._row(base, order_id, normalized, raw))
        return rows

    @staticmethod
    def _row(base: dict[str, Any], order_id: str, normalized: Any, raw: Mapping[str, Any]) -> FillRow:
        shares = _to_float(raw.get("cumulative_quantity"))
        fill_price = normalized.reference_price
        if normalized.is_filled and fill_price:
            status = "filled"
        elif normalized.is_live:
            # A live order's notional is its commitment, not money spent; report nothing as filled yet.
            status = "partially_filled" if shares and shares > 0 else "pending"
            fill_price = None
        else:
            status = "no_fill"
        filled_usd = normalized.notional_usd if status == "filled" else None
        expected_price = base["expected_price"]
        slip = slippage_pct(fill_price, expected_price) if fill_price and expected_price else None
        return FillRow(order_id=order_id, status=status, broker_state=normalized.state, fill_price=fill_price,
                       filled_usd=filled_usd, filled_shares=shares if status == "filled" else None,
                       slippage_pct=slip, **base)

    def write_report(self, day: date, rows: list[FillRow], *, generated_at: datetime) -> Path:
        """Atomically write ``fill_comparison.json`` for ``day``; the latest run replaces the last."""
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        filled = [row for row in rows if row.slippage_pct is not None]
        document = {
            "trading_day": day.isoformat(),
            "generated_at": generated_at.isoformat(),
            "orders": [asdict(row) for row in rows],
            "summary": {
                "placed": len(rows),
                "filled_with_slippage": len(filled),
                "mean_slippage_pct": round(sum(r.slippage_pct for r in filled) / len(filled), 4) if filled else None,
            },
        }
        directory = self.store.day_dir(day)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "fill_comparison.json"
        atomic_write_text(path, json.dumps(document, sort_keys=True, indent=1, allow_nan=False) + "\n")
        return path
