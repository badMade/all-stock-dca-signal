"""Budget-exhaustion opportunity count (global ladder): the first estimate for the Markov gate.

Read-only research query. It answers one question before anything gets built (see
claude/markov-extensions.md): how often did a LATER scheduled slot see a buyable dip when the
day's budget was already too spent to fund the buy the ladder would have made? If that almost
never happened, an MDP that reserves cash for later slots has nothing to save, and both the MDP
and the absorbing tier chain drop down the roadmap.

Nothing here is a suggester and nothing reaches live sizing. Spec: revision 2 of
tests/ledger/test_budget_exhaustion_opportunity.py, operator-approved 2026-11-23.

SCOPE, stated loudly because it is the main limit: this prices each quote with the GLOBAL ladder
(config/sizing_ladder.json) only. Per-ticker calibrations, the ML nudge, the kill-switch cap,
inverse-Martingale suppression and the same-day top-up guard are NOT replayed, and every report
says ``"ladder": "global"``. The faithful replay that calls near_close_dca's own sizing code is a
separate, proposed design (docs/adr-budget-exhaustion-replay.md); this module is its extract half
plus a first estimate.

Rules the spec pins, and why:

* The ladder mirrors dca_ladder_core, which live sizing delegates to: the largest ``min_drop_pct``
  <= the drop wins with a raw float compare (no epsilon, so 10.00 -> 9.80 reads as 1.9999...%, as
  live does); a non-finite drop is no buy; the $2 base needs the price STRICTLY below the prior
  low (select_base_usd), otherwise $1 below the close, nothing at or above it. It is copied, not
  imported, because dca-sizing-signals may not reach into the repo root
  (tests/test_architecture_boundaries.py); the tests pin it to the real config files instead.
* The daily cap moves between the budgets.json tiers with the day's 3x-signal count, which the
  ledger does not store, so a report runs at ONE cap and echoes it; ``..._all_caps`` runs them all.
* Spend counts SCHEDULED fills only. An order id tagged manual in ANY visible version is excluded,
  the same ambiguity rule as the ``scheduled_outcomes`` macro (docs/run_classification.md).
* Spend is summed across every ticker (the cap is account-wide) and counts only fills with
  ``filled_at`` strictly BEFORE the quote's ``ts``.
* Point in time: fills, quotes and bars are visible only when their ``as_of <= known_at``.
* Fail closed: a quote with no prior-session bar is skipped and counted, never guessed. Bad
  parameters raise ``ValueError``; a missing config file raises ``FileNotFoundError``. Never writes.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..prices.store import to_utc_naive

# Session dates are US/Eastern trading days; quote timestamps are stored as naive UTC.
_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# dca_ladder_core.MIN_BASE_USD: "a model must never produce a sub-dollar notional".
_MIN_BASE_USD = 1.0

BELOW_LOW = "below_low"
BELOW_CLOSE_ONLY = "below_close_only"
DIP_KINDS = (BELOW_LOW, BELOW_CLOSE_ONLY)


def default_config_dir() -> Path:
    """``$DCA_CONFIG_DIR`` if set (same variable the core honours), else the repo's ``config/``."""
    env = os.environ.get("DCA_CONFIG_DIR")
    # parents: [0] ledger, [1] market_data_ingest, [2] dca-sizing-signals, [3] repo root.
    return Path(env) if env else Path(__file__).resolve().parents[3] / "config"


def _read_json(path: Path) -> Any:
    # read_text raises FileNotFoundError on a missing file: a gate count with a guessed ladder
    # would be worse than no count, so there is deliberately no fallback here.
    return json.loads(Path(path).read_text())


def _require_positive_finite(name: str, value: Any) -> float:
    # bool is an int subclass; refuse it so True cannot pass as a $1 cap.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class GlobalLadder:
    """The global graded ladder: ``(drop_fraction, multiplier)`` tiers plus the two bases."""

    tiers: tuple[tuple[float, float], ...]
    base_below_low_usd: float
    base_below_close_usd: float

    def multiplier(self, drop: float) -> float:
        """Stepped match, as dca_ladder_core.ladder_multiplier: 0.0 below the first tier."""
        if not math.isfinite(drop):
            return 0.0
        out = 0.0
        for threshold, tier_multiplier in self.tiers:  # sorted ascending at load
            if drop >= threshold:
                out = tier_multiplier
        return out

    def planned_usd(self, price: float, *, prior_close: float, prior_low: float | None) -> tuple[str | None, float]:
        """``(dip_kind, dollars)`` the global ladder would plan for a first allocation at ``price``.

        ``(None, 0.0)`` means no buy: at or above the prior close, or a drop under the first tier.
        """
        if not (price < prior_close):
            return None, 0.0
        multiplier = self.multiplier((prior_close - price) / prior_close)
        if multiplier <= 0:
            return None, 0.0
        # Same guard as dca_ladder_core.select_base_usd: an unusable low can never upgrade the base.
        below_low = (
            prior_low is not None and math.isfinite(prior_low) and 0 < prior_low <= prior_close and price < prior_low
        )
        if below_low:
            return BELOW_LOW, round(max(_MIN_BASE_USD, self.base_below_low_usd) * multiplier, 2)
        return BELOW_CLOSE_ONLY, round(max(_MIN_BASE_USD, self.base_below_close_usd) * multiplier, 2)


def load_global_ladder(config_dir: Path | str | None = None) -> GlobalLadder:
    """Read ``sizing_ladder.json`` the way dca_config.load_sizing_tiers does (pct -> fraction)."""
    data = _read_json(Path(config_dir or default_config_dir()) / "sizing_ladder.json")
    tiers = []
    for tier in data["tiers"]:
        threshold = _require_positive_finite("min_drop_pct", tier["min_drop_pct"]) / 100.0
        multiplier = _require_positive_finite("multiplier", tier["multiplier"])
        tiers.append((threshold, multiplier))
    return GlobalLadder(
        tiers=tuple(sorted(tiers)),
        base_below_low_usd=_require_positive_finite("base_amount_usd", data["base_amount_usd"]),
        base_below_close_usd=_require_positive_finite(
            "base_amount_below_close_usd", data["base_amount_below_close_usd"]
        ),
    )


def load_daily_caps(config_dir: Path | str | None = None) -> tuple[float, ...]:
    """The ``daily_cap_usd`` of every ``budgets.json`` budget tier, in file order."""
    data = _read_json(Path(config_dir or default_config_dir()) / "budgets.json")
    return tuple(_require_positive_finite("daily_cap_usd", t["daily_cap_usd"]) for t in data["budget_tiers"])


def _session_of(ts_utc_naive: datetime) -> date:
    return ts_utc_naive.replace(tzinfo=_UTC).astimezone(_ET).date()


def _scheduled_fills(con: Any, known: datetime) -> list[tuple[date, datetime, float]]:
    # fills_known_at() gives the latest version of each order; the manual set looks at EVERY
    # visible version so a later scheduled-tagged correction cannot launder a manual order.
    return con.execute(
        """
        SELECT f.session, f.filled_at, f.notional
        FROM fills_known_at(?) AS f
        WHERE f.run_kind = 'scheduled'
          AND f.order_id NOT IN (SELECT order_id FROM fills WHERE run_kind = 'manual' AND as_of <= ?)
        """,
        [known, known],
    ).fetchall()


def _visible_quotes(con: Any, known: datetime) -> list[tuple[str, datetime, float, str]]:
    return con.execute(
        "SELECT symbol, ts, price, slot FROM intraday_quotes WHERE as_of <= ? ORDER BY ts, symbol, slot",
        [known],
    ).fetchall()


def _bars_by_symbol(con: Any, known: datetime) -> dict[str, list[tuple[date, float, float]]]:
    out: dict[str, list[tuple[date, float, float]]] = defaultdict(list)
    for symbol, session, close, low in con.execute(
        "SELECT symbol, session, close, low FROM bars_known_at(?) ORDER BY symbol, session", [known]
    ).fetchall():
        out[symbol].append((session, close, low))
    return out


def _prior_bar(bars: list[tuple[date, float, float]], session: date) -> tuple[float, float] | None:
    # Latest bar strictly before the quote's session; bars are ordered by session.
    for bar_session, close, low in reversed(bars):
        if bar_session < session:
            return close, low
    return None


def budget_exhaustion_opportunity(
    ledger: Any,
    *,
    known_at: datetime,
    daily_cap_usd: float,
    ladder: GlobalLadder,
) -> dict[str, Any]:
    """Count buyable dips that arrived after the day's budget could no longer fund the planned buy.

    Args:
        ledger: an open ``LedgerStore`` (its price store shares the same DuckDB connection).
        known_at: timezone-aware reader moment; rows with a later ``as_of`` are invisible.
        daily_cap_usd: the account-wide daily cap to test against (one budgets.json tier).
        ladder: the global ladder from :func:`load_global_ladder`.

    Returns:
        A dict with the parameters echoed back, ``ladder`` (always ``"global"``),
        ``sessions_observed``, ``quotes_considered``, ``sessions_with_event``,
        ``skipped_no_prior_bar``, the sorted ``events`` list, and ``by_tier`` (one bucket per dip
        kind with its own ``sessions_with_event`` and ``events``).

    Side effects: none. Read-only.
    """
    cap = _require_positive_finite("daily_cap_usd", daily_cap_usd)
    if not isinstance(known_at, datetime) or known_at.tzinfo is None:
        raise ValueError("known_at must be a timezone-aware datetime")
    known = to_utc_naive(known_at)

    con = ledger.con
    fills_by_session: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for session, filled_at, notional in _scheduled_fills(con, known):
        fills_by_session[session].append((filled_at, notional))
    bars = _bars_by_symbol(con, known)
    quotes = _visible_quotes(con, known)

    sessions_observed: set[date] = set()
    skipped = 0
    events: list[dict[str, Any]] = []
    for symbol, ts, price, slot in quotes:
        session = _session_of(ts)
        sessions_observed.add(session)
        prior = _prior_bar(bars.get(symbol, []), session)
        if prior is None:
            skipped += 1
            continue
        prior_close, prior_low = prior
        kind, planned = ladder.planned_usd(price, prior_close=prior_close, prior_low=prior_low)
        if kind is None:
            continue  # the ladder would not have bought here
        spent = sum(n for filled_at, n in fills_by_session.get(session, ()) if filled_at < ts)
        remaining = cap - spent
        if remaining >= planned:
            continue  # the planned buy was still fundable
        events.append(
            {
                "session": session,
                "symbol": symbol,
                "slot": slot,
                "tier": kind,
                "price": price,
                "prior_close": prior_close,
                "prior_low": prior_low,
                "planned_usd": planned,
                "spent_before_usd": spent,
                "remaining_usd": remaining,
            }
        )

    events.sort(key=lambda e: (e["session"], e["slot"], e["symbol"]))
    by_tier = {
        kind: {
            "sessions_with_event": len({e["session"] for e in events if e["tier"] == kind}),
            "events": [e for e in events if e["tier"] == kind],
        }
        for kind in DIP_KINDS
    }
    return {
        "daily_cap_usd": cap,
        "known_at": known_at,
        "ladder": "global",
        "sessions_observed": len(sessions_observed),
        "quotes_considered": len(quotes),
        "sessions_with_event": len({e["session"] for e in events}),
        "skipped_no_prior_bar": skipped,
        "events": events,
        "by_tier": by_tier,
    }


def budget_exhaustion_opportunity_all_caps(
    ledger: Any, *, known_at: datetime, config_dir: Path | str | None = None
) -> list[dict[str, Any]]:
    """One report per budgets.json daily cap, in file order, all priced with the same ladder."""
    ladder = load_global_ladder(config_dir)
    return [
        budget_exhaustion_opportunity(ledger, known_at=known_at, daily_cap_usd=cap, ladder=ladder)
        for cap in load_daily_caps(config_dir)
    ]
