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
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from near_close_dca import StrategyConfig

logger = logging.getLogger("near_close_dca.config")


def _default_config_dir() -> Path:
    """Resolve the default config directory, tolerant of installed-wheel layouts.

    Order: ``$DCA_CONFIG_DIR`` env var -> the ``config/`` beside this module (checkout
    layout) -> ``./config`` in the current working directory. The flat single-module
    packaging does not ship ``config/*.json`` inside the wheel, so an installed CLI relies
    on the env var or a checkout; ``load_strategy_config`` warns if none resolves.
    """
    env = os.environ.get("DCA_CONFIG_DIR")
    if env:
        return Path(env)
    sibling = Path(__file__).resolve().parent / "config"
    if sibling.is_dir():
        return sibling
    cwd = Path.cwd() / "config"
    return cwd if cwd.is_dir() else sibling


DEFAULT_CONFIG_DIR = _default_config_dir()


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
    """Map budgets.json onto the surviving StrategyConfig budget fields.

    The daily/weekly caps are governed by the module-level ``BUDGET_TIERS`` ladder
    (see ``near_close_dca.select_budget_tier``), not by flat StrategyConfig fields, so
    only the base buy amount and the weekly rollover-reserve cap are overlaid here. The
    ``budget_tiers`` block in budgets.json is documentation of that ladder; it is not a
    StrategyConfig field and is intentionally not applied.
    """
    data = _read_json(config_dir / "budgets.json")
    if data is None:
        return {}
    field_map = {
        "base_amount_usd": "base_buy_amount_usd",
        "weekly_rollover_reserve_cap_usd": "weekly_rollover_reserve_cap_usd",
    }
    out: Dict[str, Any] = {}
    for json_key, config_field in field_map.items():
        if json_key in data:
            out[config_field] = float(data[json_key])
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

    The optional ``decision_time_et`` field (``"HH:MM"``, 24-hour ET) overlays
    ``StrategyConfig.decision_time_et`` so the scheduled task and the strategy agree on the
    decision anchor. It is omitted by default, leaving the 3:45 PM ET default in place; set
    it explicitly if the live task decides at a different time (e.g. ``"15:30"``).
    """
    data = _read_json(config_dir / "trading_window.json")
    if data is None:
        return {}
    if "start_date" not in data or "end_date" not in data:
        raise ValueError("trading_window.json must contain both 'start_date' and 'end_date'")
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
    out: Dict[str, Any] = {"approved_test_dates": frozenset(approved)}
    if "decision_time_et" in data:
        try:
            out["decision_time_et"] = datetime.strptime(str(data["decision_time_et"]), "%H:%M").time()
        except ValueError as exc:
            raise ValueError(
                f"trading_window.json decision_time_et must be 'HH:MM', got {data['decision_time_et']!r}"
            ) from exc
    return out


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

    # Surface the fallback-to-defaults case loudly. If the strategy is installed as a
    # wheel and run outside a checkout, the bundled config/ may be absent; without this
    # warning the CLI would silently use the hardcoded approved-date window.
    if not config_dir.is_dir():
        logger.warning(
            "config dir %s not found; using hardcoded StrategyConfig defaults "
            "(approved trading window falls back to the built-in dates). "
            "Pass --config-dir or run from a checkout that contains config/*.json.",
            config_dir,
        )

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
