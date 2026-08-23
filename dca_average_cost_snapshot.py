"""Weekly average-cost-basis snapshot across every Robinhood account.

Positions and average cost basis live only in Robinhood, and this repo makes no
network calls (see ``CLAUDE.md``'s "Standard library only at runtime" /
"library driven from outside" rules) -- so, like ``dca_weekly_dashboard.py``,
this module is connector-agnostic: it never calls a broker itself. An external
agent session holding Robinhood MCP access fetches each account's positions
(``get_accounts`` + ``get_equity_positions`` per account_number), passes the
raw results in here, and this module does the aggregation and the durable
write. There is no in-repo caller and no CLI entry point -- see
"Three entry points coexist" in ``CLAUDE.md`` for why that is normal for a
module in this category, alongside ``dca_cost_tracker.py`` and
``dca_weekly_dashboard.py``.

WHAT THIS BUILDS
-----------------
Per account: every held position's ticker, quantity, and average cost.
Across all accounts: one combined per-ticker rollup -- total quantity held and
a quantity-weighted average cost -- for a stock the account holds in more than
one account. This mirrors ``scripts/purchase_history.py``'s
``build_ticker_rollup``, except the inputs are Robinhood's own live average
cost basis rather than this repo's own confirmed-fill ledger; the two are
independent cross-checks of each other, not the same computation.

Standard library only.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from dca_fileio import atomic_open_w

__all__ = [
    "AVERAGE_COST_SNAPSHOT_FILENAME",
    "build_average_cost_snapshot",
    "write_average_cost_snapshot",
]

AVERAGE_COST_SNAPSHOT_FILENAME = "average_cost_snapshot.json"


def _optional_float(value: Any) -> Optional[float]:
    """Best-effort float parse. ``None`` (never a fabricated ``0.0``) when the
    source position carries no usable number -- matching
    ``scripts/purchase_history.py``'s own rule that an unresolved field must
    never silently corrupt a downstream average-cost computation.

    A non-finite parse (``"NaN"``, ``"Infinity"``, ``"-Infinity"`` -- all
    valid input to ``float()``) is treated the same as unparseable: a NaN
    quantity would otherwise pass the caller's ``quantity <= 0`` check
    unfiltered, and an infinite cost would poison the weighted-average sum
    for every account holding that ticker, plus make
    ``write_average_cost_snapshot`` emit non-standard ``NaN``/``Infinity``
    tokens into JSON (Codex review: "Reject non-finite numeric values before
    aggregation").
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _position_ticker(position: Mapping[str, Any]) -> Optional[str]:
    raw = position.get("symbol", position.get("ticker"))
    return raw.strip().upper() if isinstance(raw, str) and raw.strip() else None


def _position_average_cost(position: Mapping[str, Any]) -> Optional[float]:
    # Robinhood's get_equity_positions response shape uses
    # "average_buy_price"; accept "average_cost" too so a caller normalizing
    # its own payload shape ahead of time (or a test fixture) is not forced
    # to match one exact key name.
    return _optional_float(position.get("average_buy_price", position.get("average_cost")))


def build_average_cost_snapshot(
    accounts: Sequence[Mapping[str, Any]],
    positions_by_account: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    generated_at: str,
) -> Dict[str, Any]:
    """Build the snapshot document from already-fetched account/position data.

    ``accounts`` is the raw list from ``get_accounts`` (used only for display
    metadata: nickname / account type / brokerage trading type -- never for
    positions). ``positions_by_account`` maps each account_number to that
    account's raw ``get_equity_positions`` rows. An account with no entry in
    ``positions_by_account`` (never fetched, or fetch failed) is simply
    absent from the output rather than recorded as zero holdings -- silently
    publishing "no positions" for an account that was actually never queried
    would be indistinguishable from a genuinely empty account.

    A position with no resolvable quantity, or a non-positive quantity (a
    fully-closed position some responses still list), is skipped: it
    contributes nothing to either the per-account listing or the combined
    rollup. A position with a resolvable quantity but no resolvable average
    cost is still listed per-account (so the holding itself is visible) and
    still counted in the combined rollup's ``total_quantity``/``account_count``
    (it is real, held quantity), but excluded from the numerator/denominator
    behind ``average_cost_usd``, matching ``build_ticker_rollup``'s "exclude
    unpaired amounts from average cost basis" rule -- an unpriced holding's
    quantity would otherwise deflate the weighted average cost for that
    ticker if it were folded into that division (Codex/Copilot review:
    "total_quantity is not actually the total quantity held" /
    "Track total held quantity separately from the priced quantity used as
    the weighted-average denominator").
    """
    account_meta = {
        str(account["account_number"]): account
        for account in accounts
        if account.get("account_number") is not None
    }

    per_account: Dict[str, Any] = {}
    combined_total_quantity: Dict[str, float] = {}
    combined_account_numbers: Dict[str, set] = {}
    # "_priced_*" back ONLY average_cost_usd's weighted-average division --
    # kept separate from combined_total_quantity above so an unpriced
    # holding's real, held quantity still counts toward total_quantity and
    # account_count without also diluting the average cost it has no price
    # to contribute to.
    combined_priced_quantity: Dict[str, float] = {}
    combined_priced_invested_usd: Dict[str, float] = {}

    for account_number, positions in positions_by_account.items():
        meta = account_meta.get(account_number, {})
        account_positions: List[Dict[str, Any]] = []
        for position in positions:
            ticker = _position_ticker(position)
            quantity = _optional_float(position.get("quantity"))
            if ticker is None or quantity is None or quantity <= 0:
                continue
            average_cost = _position_average_cost(position)
            account_positions.append(
                {
                    "ticker": ticker,
                    "quantity": quantity,
                    "average_cost_usd": average_cost,
                }
            )
            combined_total_quantity[ticker] = combined_total_quantity.get(ticker, 0.0) + quantity
            combined_account_numbers.setdefault(ticker, set()).add(account_number)
            if average_cost is not None:
                combined_priced_quantity[ticker] = combined_priced_quantity.get(ticker, 0.0) + quantity
                combined_priced_invested_usd[ticker] = (
                    combined_priced_invested_usd.get(ticker, 0.0) + quantity * average_cost
                )

        per_account[account_number] = {
            "nickname": meta.get("nickname"),
            "brokerage_account_type": meta.get("brokerage_account_type"),
            "type": meta.get("type"),
            "is_default": meta.get("is_default", False),
            "positions": sorted(account_positions, key=lambda p: p["ticker"]),
        }

    combined_by_ticker = {
        ticker: {
            "total_quantity": round(combined_total_quantity[ticker], 8),
            # None when NO account's holding of this ticker resolved a price
            # -- never a fabricated 0.0/division-by-zero, matching this
            # module's "never fabricate an unresolved number" rule.
            "average_cost_usd": (
                round(combined_priced_invested_usd[ticker] / combined_priced_quantity[ticker], 6)
                if ticker in combined_priced_quantity
                else None
            ),
            # Every account actually holding this ticker (priced or not) --
            # matches total_quantity's own scope, not only the subset behind
            # average_cost_usd's weighted average.
            "account_count": len(combined_account_numbers[ticker]),
        }
        for ticker in sorted(combined_total_quantity)
    }

    return {
        "generated_at": generated_at,
        "account_count": len(per_account),
        "accounts": per_account,
        "combined_by_ticker": combined_by_ticker,
    }


def write_average_cost_snapshot(snapshot: Dict[str, Any], *, output_dir: str | Path) -> Path:
    """Write ``snapshot`` to ``<output_dir>/average_cost_snapshot.json`` atomically.

    Full replace every call, not an incremental merge -- a weekly cadence makes
    a full rebuild cheap, and it can never drift from a partially-applied
    update the way an incremental writer could (mirrors
    ``scripts/purchase_history.py.write_purchase_history``'s identical
    rationale). ``atomic_open_w`` is what makes each write all-or-nothing: a
    concurrent reader never observes an empty or half-written file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_path / AVERAGE_COST_SNAPSHOT_FILENAME
    with atomic_open_w(snapshot_path) as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return snapshot_path
