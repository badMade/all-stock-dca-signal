"""Load the graded DCA :class:`~near_close_dca.StrategyConfig` from ``config/*.json``.

The strategy math in :mod:`near_close_dca` ships with hardcoded dataclass defaults so
the library works with zero configuration. This module overlays the version-controlled
backup JSON (``config/tickers.json``, ``budgets.json``, ``sizing_ladder.json``,
``trading_window.json``) onto those defaults so the runtime and the documented backup
never drift. Any missing file leaves the corresponding defaults untouched — the loader
never fails just because a config file is absent.

No secrets, tokens, or account credentials live in these files or here.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from near_close_dca import StrategyConfig

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"could not parse config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a JSON object, got {type(data).__name__}")
    return data


def load_tickers(config_dir: Path) -> Dict[str, Any]:
    """Return ``{'tickers': (...), 'excluded_tickers': frozenset(...)}`` from tickers.json."""
    data = _read_json(config_dir / "tickers.json")
    if data is None:
        return {}
    out: Dict[str, Any] = {}
    active = data.get("active")
    if isinstance(active, list):
        out["tickers"] = tuple(str(t) for t in active)
    excluded = data.get("excluded")
    if isinstance(excluded, list):
        out["excluded_tickers"] = frozenset(str(t) for t in excluded)
    return out


def load_budgets(config_dir: Path) -> Dict[str, Any]:
    """Map budgets.json onto the StrategyConfig budget fields."""
    data = _read_json(config_dir / "budgets.json")
    if data is None:
        return {}
    field_map = {
        "base_amount_usd": "base_buy_amount_usd",
        "daily_base_cap_usd": "daily_base_cap_usd",
        "carry_forward_reserve_cap_usd": "reserve_cap_usd",
        "hard_daily_max_usd": "hard_daily_max_usd",
        "weekly_hard_cap_usd": "weekly_notional_cap_usd",
    }
    out: Dict[str, Any] = {}
    for json_key, config_field in field_map.items():
        if json_key in data:
            out[config_field] = float(data[json_key])
    # Keep the backward-compatible alias in lockstep with the base cap.
    if "daily_base_cap_usd" in out:
        out["daily_notional_cap_usd"] = out["daily_base_cap_usd"]
    return out


def load_sizing_tiers(config_dir: Path) -> Dict[str, Any]:
    """Convert sizing_ladder.json tiers into StrategyConfig ``sizing_tiers`` (fractions)."""
    data = _read_json(config_dir / "sizing_ladder.json")
    if data is None:
        return {}
    tiers_raw = data.get("tiers")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        return {}
    tiers: List[tuple] = []
    for tier in tiers_raw:
        if "min_drop_pct" not in tier or "multiplier" not in tier:
            raise ValueError(f"sizing tier missing min_drop_pct/multiplier: {tier}")
        threshold = float(tier["min_drop_pct"]) / 100.0
        multiplier = float(tier["multiplier"])
        if threshold < 0 or multiplier < 0:
            raise ValueError(f"sizing tier has negative threshold/multiplier: {tier}")
        tiers.append((threshold, multiplier))
    tiers.sort()
    out: Dict[str, Any] = {"sizing_tiers": tuple(tiers)}
    out["minimum_drop_vs_previous_close"] = tiers[0][0]
    if "base_amount_usd" in data:
        out["base_buy_amount_usd"] = float(data["base_amount_usd"])
    return out


def load_approved_dates(config_dir: Path) -> Dict[str, Any]:
    """Expand trading_window.json into the ``approved_test_dates`` frozenset.

    Weekdays (Mon–Fri) in the inclusive ``[start_date, end_date]`` range are approved.
    An absent window file leaves the hardcoded defaults in place, so callers who do not
    ship a window keep the exact prior behaviour.
    """
    data = _read_json(config_dir / "trading_window.json")
    if data is None:
        return {}
    start = date.fromisoformat(str(data["start_date"]))
    end = date.fromisoformat(str(data["end_date"]))
    if end < start:
        raise ValueError(f"trading_window end_date {end} precedes start_date {start}")
    weekdays_only = bool(data.get("weekdays_only", True))
    approved = set()
    current = start
    while current <= end:
        if not weekdays_only or current.weekday() < 5:
            approved.add(current)
        current += timedelta(days=1)
    return {"approved_test_dates": frozenset(approved)}


def load_strategy_config(
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    *,
    base: Optional[StrategyConfig] = None,
    **overrides: Any,
) -> StrategyConfig:
    """Return a :class:`StrategyConfig` with ``config/*.json`` overlaid on ``base``.

    Precedence (lowest to highest): ``base`` defaults < config JSON < explicit
    ``overrides``. ``dry_run`` and ``broker`` are never set from JSON — they stay at
    their ``base`` values unless passed as explicit overrides — so loading config can
    never by itself enable live trading.
    """
    config_dir = Path(config_dir)
    base = base or StrategyConfig()

    merged: Dict[str, Any] = {}
    merged.update(load_tickers(config_dir))
    merged.update(load_budgets(config_dir))
    merged.update(load_sizing_tiers(config_dir))
    merged.update(load_approved_dates(config_dir))
    merged.update(overrides)

    valid_fields = {f.name for f in dataclasses.fields(StrategyConfig)}
    unknown = set(merged) - valid_fields
    if unknown:
        raise ValueError(f"unknown StrategyConfig fields from config/overrides: {sorted(unknown)}")

    return dataclasses.replace(base, **merged)
