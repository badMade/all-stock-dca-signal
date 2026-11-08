"""Lives on the live-DCA side of the boundary. This is the ONLY module in
the proposal allowed to know that more than one suggester exists --
neither suggester package is aware of the other, and neither is aware of
this reader either. Dependency flows one way:

    suggester -> its own config file -> this reader -> graded_buy_multiplier()

Nothing flows back. If this file did not exist, near_close_dca would read
one adjustment file directly, same as it does today for ml_forecaster;
this reader only becomes necessary once a second independent suggester is
added and something has to decide how to combine them -- and that
decision belongs here, not in either suggester.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dca_config import _reject_duplicate_keys


@dataclass(frozen=True, slots=True)
class CombinedAdjustment:
    multiplier_adjustment_pct: float
    sources_used: tuple[str, ...]


def load_combined_adjustment(
    ticker: str,
    adjustment_files: dict[str, Path],
    max_staleness_hours: float,
    combine: str = "most_conservative",
) -> CombinedAdjustment | None:
    """Fail-closed combination of every currently-fresh, currently-valid
    suggestion file. Assumption flagged for review: "most_conservative"
    (smallest absolute adjustment) is the default here because it matches
    the fail-closed posture used everywhere else in this repo -- "average"
    and "require_all_agree" are sketched below as the two other realistic
    options. This default is easy to change in one place and does not
    require touching either suggester.

    Args:
        ticker: the equity symbol to look up.
        adjustment_files: mapping of source name to that source's own
            config file, e.g. {"ml_forecaster": ..., "connector_signals": ...}.
        max_staleness_hours: same staleness budget near_close_dca already
            applies to ml_sizing_adjustment.json today.
        combine: "most_conservative" (default), "average", or
            "require_all_agree".

    Returns:
        None if no source has a fresh, valid entry for this ticker --
        callers should fall back to the unadjusted graded-ladder
        multiplier, the same fail-closed behaviour as today.
    """
    values: list[tuple[str, float]] = []
    for source, path in adjustment_files.items():
        adj = _read_if_fresh(path, ticker, max_staleness_hours)
        if adj is not None:
            values.append((source, adj))

    if not values:
        return None

    if combine == "most_conservative":
        source, adj = min(values, key=lambda pair: abs(pair[1]))
        return CombinedAdjustment(adj, (source,))

    if combine == "average":
        adj = sum(v for _, v in values) / len(values)
        return CombinedAdjustment(adj, tuple(s for s, _ in values))

    if combine == "require_all_agree":
        if len(values) < len(adjustment_files):
            return None  # not every source has a fresh entry -- fail closed
        signs = {1 if v >= 0 else -1 for _, v in values}
        if len(signs) > 1:
            return None  # sources disagree on direction -- fail closed
        adj = min(values, key=lambda pair: abs(pair[1]))[1]
        return CombinedAdjustment(adj, tuple(s for s, _ in values))

    raise ValueError(f"unknown combine policy: {combine}")


def _read_if_fresh(path: Path, ticker: str, max_staleness_hours: float) -> float | None:
    """Mirrors dca_config.load_ml_sizing_adjustment's existing shape and
    freshness checks -- a missing, malformed, wrong-ticker, or stale file
    is treated as absent, never as an error."""
    if not path.exists():
        return None
    if (
        isinstance(max_staleness_hours, bool)
        or not isinstance(max_staleness_hours, (int, float))
        or not math.isfinite(max_staleness_hours)
        or max_staleness_hours <= 0
    ):
        return None
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(data, dict) or data.get("ticker") != ticker:
            return None
        raw_dt = data.get("generated_at")
        if not isinstance(raw_dt, str):
            return None
        generated_at = datetime.fromisoformat(raw_dt)
        if generated_at.tzinfo is None:
            return None
        age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
        if age_hours < 0 or age_hours > max_staleness_hours:
            return None
        raw_pct = data.get("multiplier_adjustment_pct")
        if isinstance(raw_pct, bool) or not isinstance(raw_pct, (int, float)):
            return None
        pct = float(raw_pct)
        if not math.isfinite(pct):
            return None
        return pct
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, OverflowError, OSError):
        return None
