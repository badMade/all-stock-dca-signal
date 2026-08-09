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
import math
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from near_close_dca import StrategyConfig

logger = logging.getLogger("near_close_dca.config")

# Where a wheel install places the bundled config (see pyproject [tool.setuptools.data-files]).
_INSTALLED_CONFIG_DIR = Path(sys.prefix) / "share" / "all-stock-dca" / "config"


def get_config_dir() -> Path:
    """Resolve the default config directory across checkout and installed-wheel layouts.

    Order: ``$DCA_CONFIG_DIR`` -> ``config/`` beside this module (checkout) -> the wheel's
    installed ``share/all-stock-dca/config`` -> ``./config`` in the working directory. The
    wheel ships ``config/*.json`` as data files (so an installed ``run-dca`` finds the live
    window); ``load_strategy_config`` still warns if none of these resolve.
    """
    env = os.environ.get("DCA_CONFIG_DIR")
    if env:
        return Path(env)
    for candidate in (Path(__file__).resolve().parent / "config", _INSTALLED_CONFIG_DIR, Path.cwd() / "config"):
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "config"


DEFAULT_CONFIG_DIR = get_config_dir()

# ``${VAR}`` references inside config string values, expanded by
# :func:`expand_config_env`. Deliberately narrower than ``os.path.expandvars``:
# only the explicit braced form is recognised, so a literal ``$`` in a note or a
# Windows-style path can never be mistaken for a variable reference.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingConfigEnvError(ValueError):
    """A config value references an environment variable that is not set."""


def expand_config_env(value: str, *, source: str) -> str:
    """Expand ``${VAR}`` references in a config string, or raise if one is unset.

    Sensitive identifiers (the live brokerage account number) must not be
    committed to this repository, so ``config/*.json`` stores them as
    ``"${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}"`` and the value is supplied by the
    environment at run time.

    Raises :class:`MissingConfigEnvError` rather than substituting an empty
    string: an empty account number is exactly the value that used to disable
    account pinning (see ``near_close_dca.account_pinning_failure``), so a
    missing variable must fail loudly at config-load time instead of producing a
    config that silently permits trading any account.
    """

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if not resolved:
            raise MissingConfigEnvError(
                f"{source} references ${{{name}}} but that environment variable is "
                f"unset or empty. Export {name} before loading config."
            )
        return resolved

    return _ENV_PLACEHOLDER.sub(_sub, value)


# The digit shape schemas/*_run_classification.schema.json pins account_number
# to. Shared so the loader's validation and the artifact's schema cannot drift.
ACCOUNT_NUMBER_SHAPE = re.compile(r"^[0-9]{6,12}$")

# The literal the read-only morning modes carry instead of an account.
READ_ONLY_ACCOUNT = "read_only"


class InvalidAccountNumberError(ValueError):
    """A configured broker account is present but not a usable account number."""


def resolve_broker_account(value: Any, *, source: str) -> str:
    """Expand and validate a configured ``broker_account``.

    Rejecting a malformed account here, at pinning time, is what keeps the
    artifact honest. ``expand_config_env`` only guarantees the variable was set
    to something non-empty, so a value like ``" 123456789 "`` or ``"acct-12"``
    used to flow through into execution and into the run artifact, and the live
    classification schema requires ``account_number`` to match
    :data:`ACCOUNT_NUMBER_SHAPE` for every ``*_dca_live`` run. Emitting a
    malformed value (or omitting the field to avoid emitting one) produced
    schema-invalid evidence for exactly the runs that matter most -- an
    account-mismatch or connector failure -- so a downstream watchdog would
    report the scheduled run as missing or invalid instead of surfacing its real
    block reason (Codex review, PR #219).

    Surrounding whitespace is stripped rather than rejected: an account exported
    with a stray newline is unambiguous, and failing on it would be a needless
    outage. Anything else that does not match the shape raises.
    """
    expanded = expand_config_env(str(value), source=source).strip()
    if expanded == READ_ONLY_ACCOUNT:
        return expanded
    if not ACCOUNT_NUMBER_SHAPE.fullmatch(expanded):
        # The VALUE is deliberately absent from this message. It comes from the
        # environment, and a malformed one is often malformed precisely because
        # something sensitive landed in the wrong variable -- a token, a
        # password. Embedding it copied that straight into any scheduler, CLI, or
        # CI log that records the exception (Codex review, PR #219). The length
        # is enough to debug a truncated or padded value without reproducing it.
        raise InvalidAccountNumberError(
            f"{source} resolved to a {len(expanded)}-character value that is not a "
            f"valid account number (expected 6-12 digits, or {READ_ONLY_ACCOUNT!r} "
            f"for a read-only mode). The value is withheld from this message "
            f"because it may be a secret pasted into the wrong variable. Refusing "
            f"to trade or to write an artifact claiming an account that does not "
            f"exist."
        )
    return expanded


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
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
        "base_amount_below_close_usd": "base_buy_amount_below_close_usd",
        "weekly_rollover_reserve_cap_usd": "weekly_rollover_reserve_cap_usd",
        "monthly_cash_allocation_usd": "monthly_cash_allocation_usd",
    }
    out: Dict[str, Any] = {}
    for json_key, config_field in field_map.items():
        if json_key in data:
            out[config_field] = float(data[json_key])
    # The dynamic risk guard's tunables live under a nested ``risk_guard`` block.
    # deployed_last_7d_usd is intentionally never sourced from config — it is a
    # runtime figure injected per run by dca_risk.apply_risk_guard.
    risk = data.get("risk_guard")
    if isinstance(risk, dict):
        if "monthly_cash_allocation_usd" in risk:
            nested_allocation = float(risk["monthly_cash_allocation_usd"])
            if (
                "monthly_cash_allocation_usd" in out
                and out["monthly_cash_allocation_usd"] != nested_allocation
            ):
                raise ValueError(
                    "budgets.json monthly_cash_allocation_usd differs between top-level and risk_guard"
                )
            out["monthly_cash_allocation_usd"] = nested_allocation
        if "kill_switch_7d_deploy_fraction" in risk:
            fraction = float(risk["kill_switch_7d_deploy_fraction"])
            if not 0 <= fraction <= 1:
                raise ValueError(
                    f"kill_switch_7d_deploy_fraction must be between 0 and 1, got {fraction}"
                )
            out["kill_switch_7d_deploy_fraction"] = fraction
    circuit_breakers = data.get("circuit_breakers")
    if isinstance(circuit_breakers, dict) and "max_per_order_usd" in circuit_breakers:
        max_per_order = float(circuit_breakers["max_per_order_usd"])
        if not (math.isfinite(max_per_order) and max_per_order > 0):
            raise ValueError(f"max_per_order_usd must be a finite positive number, got {max_per_order}")
        out["max_per_order_usd"] = max_per_order
    if isinstance(circuit_breakers, dict) and "max_orders_per_day" in circuit_breakers:
        # Shared daily order-count circuit breaker (StrategyConfig.max_orders_per_day,
        # enforced by near_close_dca.within_caps): the one gate every scheduled
        # slot and manual run actually places orders through. A bool/non-int/
        # non-positive value is refused rather than silently defaulting to
        # uncapped -- this is exactly the kind of limit that should stop
        # binding loudly, not quietly (Codex review: "Apply the order-count
        # cap to every live placement path").
        max_orders = circuit_breakers["max_orders_per_day"]
        if isinstance(max_orders, bool) or not isinstance(max_orders, int) or max_orders <= 0:
            raise ValueError(f"max_orders_per_day must be a positive integer, got {max_orders!r}")
        out["max_orders_per_day"] = max_orders
    # order_rules.one_order_per_ticker_per_day is intentionally NOT overlaid here.
    # StrategyConfig.one_order_per_ticker_per_day defaults to False specifically to
    # preserve the legacy manual-morning -> scheduled-afternoon lower-price top-up
    # path (see near_close_dca.py's _process_buy_candidate and
    # test_one_order_per_ticker_per_day_defaults_off_to_preserve_the_top_up_path).
    # Permanent policy (Geoff, confirmed 2026-08-02): the same ticker may be bought
    # more than once in a day, resized to the incremental delta at a strictly lower
    # price via same_day_top_up_policy / scripts/same_day_top_up_gate.py; a repeat
    # in the SAME slot/session is what stays blocked, via each stream's own
    # sole-executor interlock and one-order-per-ticker-per-batch rule, not via this
    # flag. budgets.json's own order_rules.one_order_per_ticker_per_day is false to
    # match. run_dca.run_slot() is the ONE caller that pins the flag explicitly, via
    # `replace(config, one_order_per_ticker_per_day=False)` -- reinforcing this
    # module's own default rather than overriding it -- the same way dry_run/broker
    # are excluded from JSON and only ever set by an explicit caller override (see
    # this module's docstring: "so loading config can never by itself enable live
    # trading"). Reading the flag from JSON here would overlay it onto EVERY
    # load_strategy_config() call, including the legacy afternoon live path
    # (docs/live_wiring.md), which is harmless while the JSON value agrees with this
    # default, but would silently change behavior everywhere the moment someone
    # edited budgets.json alone -- keeping the override caller-explicit (Codex
    # review, discussion on dca_config.py: "Keep first-wins policy out of legacy
    # config loads") means a future flip of the JSON value cannot reintroduce
    # first-slot-wins by accident.
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
    if "base_amount_below_close_usd" in data:
        out["base_buy_amount_below_close_usd"] = float(data["base_amount_below_close_usd"])
    return out


def load_per_ticker_sizing(config_dir: Path) -> Dict[str, Any]:
    """Load validated historical per-symbol sizing calibrations when present."""
    data = _read_json(config_dir / "per_ticker_sizing.json")
    if data is None or data.get("status") != "active":
        return {}
    raw = data.get("tickers")
    if not isinstance(raw, dict):
        raise ValueError("per_ticker_sizing.json must contain a tickers object")
    normalized: Dict[str, Dict[str, Any]] = {}
    for ticker, model in raw.items():
        if not isinstance(ticker, str) or not isinstance(model, dict):
            raise ValueError("per-ticker sizing entries must map a symbol to an object")
        tiers = model.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            raise ValueError(f"per-ticker sizing tiers missing for {ticker}")
        parsed_tiers = []
        for tier in tiers:
            if not isinstance(tier, dict) or "min_drop_pct" not in tier or "multiplier" not in tier:
                raise ValueError(f"per-ticker sizing tier malformed for {ticker}")
            parsed_tiers.append((float(tier["min_drop_pct"]) / 100.0, float(tier["multiplier"])))
        full_base = max(1.0, float(model.get("base_amount_usd", 1.0)))
        reduced_base = max(1.0, float(model.get("base_amount_below_close_usd", 1.0)))
        normalized[ticker.upper()] = {
            "tiers": tuple(sorted(parsed_tiers)),
            "base_amount_usd": full_base,
            "base_amount_below_close_usd": reduced_base,
        }
    return {"per_ticker_sizing": normalized}


def load_backtest_sizing_tiers(config_dir: Path) -> tuple[tuple[float, float], ...]:
    """Return the analytics-only backtest ladder tiers as (fraction, multiplier).

    Read from ``backtest_sizing_ladder.json``. This ladder is for the 12-month
    analytics backtest ONLY and is intentionally NOT overlaid onto StrategyConfig
    — the live/manual execution ladder is always ``sizing_ladder.json``. Returns
    an empty tuple when the file is absent.
    """
    data = _read_json(config_dir / "backtest_sizing_ladder.json")
    if data is None:
        return ()
    tiers_raw = data.get("tiers")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        return ()
    tiers: List[tuple] = []
    for tier in tiers_raw:
        if "min_drop_pct" not in tier or "multiplier" not in tier:
            raise ValueError(f"backtest sizing tier missing min_drop_pct/multiplier: {tier}")
        threshold = float(tier["min_drop_pct"]) / 100.0
        multiplier = float(tier["multiplier"])
        if threshold < 0 or multiplier < 0:
            raise ValueError(f"backtest sizing tier has negative threshold/multiplier: {tier}")
        tiers.append((threshold, multiplier))
    tiers.sort()
    return tuple(tiers)


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


def load_near_close_overrides(config_dir: Path) -> Dict[str, Any]:
    """Load per-field overrides from ``near_close_dca.json`` for the afternoon task.

    Only keys that correspond to valid :class:`~near_close_dca.StrategyConfig`
    fields are forwarded; unknown keys are silently ignored so the file can
    carry documentation-only entries without breaking the loader.

    ``dry_run`` and ``broker`` are excluded from the allowed set — they must
    never be set from file config so that loading config alone can never enable
    live trading.
    """
    data = _read_json(config_dir / "near_close_dca.json")
    if data is None:
        return {}
    valid_fields = {f.name for f in dataclasses.fields(StrategyConfig)}
    excluded = {"dry_run", "broker"}
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key in valid_fields and key not in excluded:
            out[key] = value
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
    merged.update(load_per_ticker_sizing(config_dir))
    merged.update(load_approved_dates(config_dir))
    merged.update(load_near_close_overrides(config_dir))
    merged.update(overrides)

    valid_fields = {f.name for f in dataclasses.fields(StrategyConfig)}
    unknown = set(merged) - valid_fields
    if unknown:
        raise ValueError(f"unknown StrategyConfig fields from config/overrides: {sorted(unknown)}")

    return dataclasses.replace(base, **merged)
