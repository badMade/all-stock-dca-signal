"""Operator goals and the shadow-only loss hedge (docs/adr-goals-and-loss-hedge-shadow.md).

Goals are DATA the operator edits (``config/goals.json``, validated against
``schemas/goals.schema.json``). They are evaluation inputs: they change how suggesters are
scored and what the console and dashboard report. Nothing in this module reaches a live
dollar amount, and nothing here can place, preview or review an order.

Three properties the tests pin (``tests/test_dca_goals.py``):

* **Fail closed.** ``load_goals`` never raises into a scheduled slot. A missing, malformed,
  inactive or not-yet-effective document resolves to ``NEUTRAL_GOALS`` (no growth target,
  loss hedge ``off``); a broken goal entry resolves only ITS kind to neutral; a broken hedge
  instrument is dropped without disabling the others.
* **Hedge execution is locked in code.** ``HEDGE_EXECUTION_ENABLED`` is a module literal that
  no config key, env var or console field reads or sets. Hedge ``mode`` can only
  be ``off`` or ``shadow``; there is no value that means "trade".
* **Goals can never name a hard limit.** Spend caps, position caps, the kill switch and the
  ladder ceiling stay in code and their own config files; the schema has no field for them.

Standard library only (CLAUDE.md): the schema is the contract for CI and the console form,
and this module re-implements the same rules by hand so the runtime never needs jsonschema.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# The one switch for hedge execution. A literal, assigned exactly once (test 7). Turning it on
# needs a separate ADR, an operator confirmation, and an order tool that does not exist today.
HEDGE_EXECUTION_ENABLED = False

GOALS_FILENAME = "goals.json"
HEDGE_MODES: Tuple[str, ...] = ("off", "shadow")
CRYPTO_ASSETS: Tuple[str, ...] = ("BTC", "ETH")
GROWTH_METRICS: Tuple[str, ...] = ("twr_cagr", "mwr_irr")
OPTION_INSTRUMENTS: Tuple[str, ...] = ("protective_put", "collar")
CRYPTO_INSTRUMENT = "crypto_allocation"

_GOAL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BENCHMARK = re.compile(r"^[A-Z0-9_]{1,32}$")
_UNDERLYING = re.compile(r"^[A-Z]{1,5}$")
# Seconds and up to nine fractional digits are optional; the offset is not. Python's
# fromisoformat accepts more shapes than the schema does, so match the schema first.
_AWARE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?(Z|[+-]\d{2}:\d{2})$")

_DOCUMENT_KEYS = {"$schema", "version", "status", "goals"}
_GROWTH_REQUIRED = {"goal_id", "goal_version", "kind", "metric", "horizon_years", "target", "effective_from"}
_GROWTH_KEYS = _GROWTH_REQUIRED | {"benchmark"}
_HEDGE_KEYS = {"goal_id", "goal_version", "kind", "mode", "max_drawdown", "hedge_budget_pct", "instruments",
               "effective_from"}
_OPTION_KEYS = {"instrument", "mode", "underlying", "horizon_days"}
_CRYPTO_KEYS = {"instrument", "mode", "assets", "max_alloc_pct", "hold_days"}
_MAX_GOALS = 16
_MAX_INSTRUMENTS = 4


@dataclass(frozen=True)
class GrowthGoal:
    """An aspirational return goal: reported against, never chased with extra risk (ADR D1)."""

    goal_id: str
    goal_version: int
    metric: str
    horizon_years: int
    target: float
    benchmark: Optional[str]
    effective_from: datetime


@dataclass(frozen=True)
class HedgeInstrument:
    """One hedge leg. ``mode`` is already combined with the goal's mode (both must be shadow)."""

    instrument: str
    mode: str
    underlying: Optional[str] = None
    horizon_days: Optional[int] = None
    assets: Tuple[str, ...] = ()
    max_alloc_pct: Optional[float] = None
    hold_days: Optional[int] = None


@dataclass(frozen=True)
class HedgeGoal:
    goal_id: str
    goal_version: int
    mode: str
    max_drawdown: Optional[float]
    hedge_budget_pct: float
    instruments: Tuple[HedgeInstrument, ...]


@dataclass(frozen=True)
class GoalSet:
    growth: Optional[GrowthGoal]
    hedge: HedgeGoal


NEUTRAL_HEDGE = HedgeGoal(goal_id="loss-hedge", goal_version=0, mode="off", max_drawdown=None,
                          hedge_budget_pct=0.0, instruments=())
NEUTRAL_GOALS = GoalSet(growth=None, hedge=NEUTRAL_HEDGE)


# --------------------------------------------------------------------------------------------
# Small validators. Each raises ValueError with a reason the console can show the operator.
# --------------------------------------------------------------------------------------------

def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")


def _number(value: Any, name: str, low: float, high: float, *, low_inclusive: bool) -> float:
    # bool is an int subclass; True must not pass for 1.0. A string must not pass either.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if (value < low) if low_inclusive else (value <= low):
        raise ValueError(f"{name} is below its lower bound {low}")
    if value > high:
        raise ValueError(f"{name} is above its ceiling {high}")
    return value


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _aware_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not _AWARE_ISO.match(value):
        raise ValueError(f"{name} must be a timezone-aware ISO 8601 date-time")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    # fromisoformat stops at microseconds; a 7 to 9 digit fraction is trimmed to 6. A goal's
    # effective instant does not need nanoseconds, and trimming moves it earlier by < 1 us.
    match = re.match(r"^(.*\.\d{6})\d{1,3}([+-].*)$", text)
    if match:
        text = match.group(1) + match.group(2)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _closed_keys(entry: Mapping[str, Any], allowed: set, required: set, what: str) -> None:
    unknown = set(entry) - allowed
    if unknown:
        raise ValueError(f"{what} has unknown field(s): {sorted(unknown)}")
    missing = required - set(entry)
    if missing:
        raise ValueError(f"{what} is missing field(s): {sorted(missing)}")


def _goal_id(value: Any) -> str:
    if not isinstance(value, str) or not _GOAL_ID.match(value):
        raise ValueError("goal_id must be lowercase letters, digits and hyphens (1 to 64 characters)")
    return value


def _mode(value: Any, what: str) -> str:
    # Exact match only: "ACTIVE", "on", True, 1 and None are all refused, not normalised.
    if not isinstance(value, str) or value not in HEDGE_MODES:
        raise ValueError(f"{what} mode must be one of {list(HEDGE_MODES)}")
    return value


def _instrument(raw: Any, goal_mode: str) -> HedgeInstrument:
    if not isinstance(raw, dict):
        raise ValueError("an instrument must be an object")
    name = raw.get("instrument")
    if name in OPTION_INSTRUMENTS:
        _closed_keys(raw, _OPTION_KEYS, _OPTION_KEYS, f"instrument {name}")
        mode = _mode(raw["mode"], f"instrument {name}")
        underlying = raw["underlying"]
        if not isinstance(underlying, str) or not _UNDERLYING.match(underlying):
            raise ValueError("underlying must be an upper-case ticker")
        return HedgeInstrument(instrument=name, mode=_combined(goal_mode, mode), underlying=underlying,
                               horizon_days=_integer(raw["horizon_days"], "horizon_days", 7, 365))
    if name == CRYPTO_INSTRUMENT:
        _closed_keys(raw, _CRYPTO_KEYS, _CRYPTO_KEYS, f"instrument {name}")
        mode = _mode(raw["mode"], f"instrument {name}")
        assets = raw["assets"]
        if (not isinstance(assets, list) or not assets or len(set(assets)) != len(assets)
                or not all(isinstance(a, str) and a in CRYPTO_ASSETS for a in assets)):
            raise ValueError(f"assets must be a non-empty list of distinct values from {list(CRYPTO_ASSETS)}")
        return HedgeInstrument(instrument=name, mode=_combined(goal_mode, mode), assets=tuple(assets),
                               max_alloc_pct=_number(raw["max_alloc_pct"], "max_alloc_pct", 0.0, 0.05,
                                                     low_inclusive=False),
                               hold_days=_integer(raw["hold_days"], "hold_days", 1, 60))
    raise ValueError(f"unknown instrument {name!r}")


def _combined(goal_mode: str, instrument_mode: str) -> str:
    # A leg is shadowed only when BOTH the goal and the leg say shadow (ADR D3).
    return "shadow" if goal_mode == "shadow" and instrument_mode == "shadow" else "off"


def _growth(entry: Mapping[str, Any], *, versioned: bool = True) -> GrowthGoal:
    required = _GROWTH_REQUIRED if versioned else _GROWTH_REQUIRED - {"goal_version"}
    allowed = _GROWTH_KEYS if versioned else _GROWTH_KEYS - {"goal_version"}
    _closed_keys(entry, allowed, required, "growth goal")
    if entry["kind"] != "aspirational":
        raise ValueError("growth goal kind must be 'aspirational'")
    metric = entry["metric"]
    if metric not in GROWTH_METRICS:
        # Raw account growth is refused on purpose: DCA deposits alone would clear any target.
        raise ValueError(f"metric must be one of {list(GROWTH_METRICS)}")
    benchmark = entry.get("benchmark")
    if benchmark is not None and (not isinstance(benchmark, str) or not _BENCHMARK.match(benchmark)):
        raise ValueError("benchmark must be an upper-case identifier")
    return GrowthGoal(goal_id=_goal_id(entry["goal_id"]),
                      goal_version=_integer(entry["goal_version"], "goal_version", 1, 10**9) if versioned else 0,
                      metric=metric,
                      horizon_years=_integer(entry["horizon_years"], "horizon_years", 1, 30),
                      target=_number(entry["target"], "target", 0.0, 0.5, low_inclusive=False),
                      benchmark=benchmark,
                      effective_from=_aware_datetime(entry["effective_from"], "effective_from"))


def _hedge(entry: Mapping[str, Any], *, versioned: bool = True, strict_instruments: bool = False
           ) -> Tuple[HedgeGoal, datetime]:
    required = _HEDGE_KEYS if versioned else _HEDGE_KEYS - {"goal_version"}
    _closed_keys(entry, required, required, "loss hedge goal")
    if entry["kind"] != "loss_hedge":
        raise ValueError("loss hedge goal kind must be 'loss_hedge'")
    mode = _mode(entry["mode"], "loss hedge")
    raw_instruments = entry["instruments"]
    if not isinstance(raw_instruments, list) or len(raw_instruments) > _MAX_INSTRUMENTS:
        raise ValueError(f"instruments must be a list of at most {_MAX_INSTRUMENTS}")
    instruments: List[HedgeInstrument] = []
    for raw in raw_instruments:
        try:
            instruments.append(_instrument(raw, mode))
        except ValueError:
            if strict_instruments:
                raise
            # Loading: a broken leg resolves to off by being dropped; the others stand (test 9).
            continue
    goal = HedgeGoal(goal_id=_goal_id(entry["goal_id"]),
                     goal_version=_integer(entry["goal_version"], "goal_version", 1, 10**9) if versioned else 0,
                     mode=mode,
                     max_drawdown=_number(entry["max_drawdown"], "max_drawdown", 0.0, 0.9, low_inclusive=False),
                     hedge_budget_pct=_number(entry["hedge_budget_pct"], "hedge_budget_pct", 0.0, 0.05,
                                              low_inclusive=True),
                     instruments=tuple(instruments))
    return goal, _aware_datetime(entry["effective_from"], "effective_from")


# --------------------------------------------------------------------------------------------
# Loading (scheduled slots, dashboard, suggester scoring): never raises on bad data.
# --------------------------------------------------------------------------------------------

def _reject_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def parse_document(text: str) -> Dict[str, Any]:
    """Parse goals JSON, rejecting duplicate keys.

    NaN and Infinity tokens are NOT rejected here on purpose: they parse to non-finite floats that
    the per-field validators refuse, so one bad number neutralises only its own goal (test 5),
    not the whole document.
    """
    data = json.loads(text, object_pairs_hook=_reject_duplicates)
    if not isinstance(data, dict):
        raise ValueError("goals document must be a JSON object")
    return data


def load_goals(config_dir: Path, *, now: datetime) -> GoalSet:
    """Return the goals in effect at ``now`` from ``<config_dir>/goals.json``. Never raises on data.

    ``now`` must be timezone-aware (a programming error otherwise, so that one does raise).
    """
    _require_aware(now)
    try:
        doc = parse_document((Path(config_dir) / GOALS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return NEUTRAL_GOALS
    if (set(doc) - _DOCUMENT_KEYS or doc.get("status") != "active"
            or isinstance(doc.get("version"), bool) or not isinstance(doc.get("version"), int)
            or not isinstance(doc.get("goals"), list) or len(doc["goals"]) > _MAX_GOALS):
        return NEUTRAL_GOALS
    growth: List[GrowthGoal] = []
    hedges: List[HedgeGoal] = []
    broken_kinds: set = set()
    for entry in doc["goals"]:
        kind = entry.get("kind") if isinstance(entry, dict) else None
        try:
            if kind == "aspirational":
                goal = _growth(entry)
                if goal.effective_from <= now:
                    growth.append(goal)
            elif kind == "loss_hedge":
                hedge, effective_from = _hedge(entry)
                if effective_from <= now:
                    hedges.append(hedge)
        except (ValueError, KeyError, TypeError):
            # A broken entry neutralises its own kind only; the other kind is unaffected (test 5).
            if isinstance(kind, str):
                broken_kinds.add(kind)
    # Two in-effect entries of one kind are ambiguous: fail closed for that kind.
    chosen_growth = growth[0] if len(growth) == 1 and "aspirational" not in broken_kinds else None
    chosen_hedge = hedges[0] if len(hedges) == 1 and "loss_hedge" not in broken_kinds else NEUTRAL_HEDGE
    return GoalSet(growth=chosen_growth, hedge=chosen_hedge)


def hedge_execution_allowed(goals: GoalSet) -> bool:
    """Always False today. The lock is checked first, so no goal content is ever consulted.

    Even with the lock flipped this stays False: no hedge mode means "trade" (only off and
    shadow exist), so activation needs a new mode, a new ADR and an order tool, not a toggle.
    """
    if not HEDGE_EXECUTION_ENABLED:
        return False
    return False


# --------------------------------------------------------------------------------------------
# Proposals (console Goals page): strict, raises ValueError with a reason to show the operator.
# --------------------------------------------------------------------------------------------

def validate_goal_proposal(payload: Any, *, now: datetime) -> Dict[str, Any]:
    """Validate one console proposal ``{"goal": {...}}`` and return a copy of the goal entry.

    ``goal_version`` is server-assigned (``build_goals_document``), so a proposal carrying one is
    refused. ``effective_from`` must be strictly after ``now``: a goal never changes retroactively,
    which is what keeps backtests point-in-time.
    """
    _require_aware(now)
    if not isinstance(payload, dict) or set(payload) != {"goal"} or not isinstance(payload["goal"], dict):
        raise ValueError('proposal must be exactly {"goal": {...}}')
    entry = payload["goal"]
    if "goal_version" in entry:
        raise ValueError("goal_version is assigned by the server")
    try:
        kind = entry.get("kind")
        if kind == "aspirational":
            effective_from = _growth(entry, versioned=False).effective_from
        elif kind == "loss_hedge":
            effective_from = _hedge(entry, versioned=False, strict_instruments=True)[1]
        else:
            raise ValueError("kind must be 'aspirational' or 'loss_hedge'")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"malformed goal: {exc}") from exc
    if effective_from <= now:
        raise ValueError("effective_from must be in the future")
    return copy.deepcopy(entry)


def empty_document() -> Dict[str, Any]:
    return {"$schema": "../schemas/goals.schema.json", "version": 0, "status": "active", "goals": []}


def build_goals_document(current: Mapping[str, Any], entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the next ``config/goals.json``: only ``entry``'s goal and the two versions change.

    An existing goal with the same ``goal_id`` is replaced in place (its ``goal_version`` + 1),
    keeping list order so the PR diff stays minimal; a new goal is appended at version 1.
    Neither input is mutated.
    """
    result = copy.deepcopy(dict(current))
    goals = result.setdefault("goals", [])
    current_version = result.get("version", 0)
    result["version"] = (current_version if isinstance(current_version, int) else 0) + 1
    for index, existing in enumerate(goals):
        if isinstance(existing, dict) and existing.get("goal_id") == entry["goal_id"]:
            goals[index] = _with_version(entry, int(existing.get("goal_version", 0)) + 1)
            break
    else:
        goals.append(_with_version(entry, 1))
    return result


def _with_version(entry: Mapping[str, Any], version: int) -> Dict[str, Any]:
    # goal_version sits right after goal_id, matching how the operator's file is written.
    out: Dict[str, Any] = {}
    for key, value in copy.deepcopy(dict(entry)).items():
        out[key] = value
        if key == "goal_id":
            out["goal_version"] = version
    out.setdefault("goal_version", version)
    return out
