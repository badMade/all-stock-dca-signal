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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from near_close_dca import WHOLE_SHARE_TICKERS, StrategyConfig

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

# Same canonical ticker-symbol shape schemas/tickers.schema.json enforces and
# dca_whole_share_accumulator.py's own _SYMBOL_PATTERN validates against
# before ever using a ticker as a filesystem path component. A supported
# alternate $DCA_CONFIG_DIR is not schema-validated at all (that only
# happens for the bundled config/tickers.json, via a separate CI/test
# check) -- without validating here too, a noncanonical entry like
# "byddy" would pass straight through into StrategyConfig.whole_share_tickers
# verbatim, and the afternoon evaluator would then request market data using
# that lowercase value while RobinhoodDataProvider's own quote/bar lookups
# compare the broker's canonical UPPERCASE response symbol exactly -- so the
# configured ticker silently receives no Robinhood data and never reaches
# accumulation or placement at all (Codex review: "Reject noncanonical
# whole-share symbols at config load").
# Requires at least one letter (the positive lookahead), matching
# dca_whole_share_accumulator._SYMBOL_PATTERN's own identical hardening --
# a dots-only value like "." or ".." otherwise still passes (both this
# regex and schemas/tickers.schema.json's own pattern) even though the
# ledger itself now rejects it, and ".."  in particular resolves as a
# path-traversal-risky filesystem component once used as a ticker
# directory name (Codex review: "Reject dot-only whole-share symbols at
# config load").
_WHOLE_SHARE_SYMBOL_PATTERN = re.compile(r"^(?=.*[A-Z])[A-Z.]{1,6}$")


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


def _reject_duplicate_keys(pairs: List[Any]) -> Dict[str, Any]:
    """``object_pairs_hook`` that raises on a duplicate key in one JSON object.

    ``json.loads`` silently keeps the LAST occurrence of a duplicate object
    key by default -- a corrupt or hand-edited ``config/inverse_martingale.json``
    with two ``"AAPL"`` entries (differing only in ``enabled``), or a
    ``config/ml_sizing_adjustment.json`` with two ``"AAPL"`` adjustments, would
    otherwise load successfully with whichever entry happened to come last
    silently winning, before either loader's own case-folded duplicate check
    ever gets a chance to see the discarded one (Codex review: "Reject
    duplicate sizing-configuration keys"). Applies at every nesting level:
    ``json.loads`` invokes this hook for every JSON object it parses, not just
    the top-level document. Mirrors ``dca_runner._reject_duplicate_keys``
    exactly -- duplicated rather than imported, since ``dca_runner`` itself
    imports from this module and a reverse import would be circular.
    """
    seen: set[str] = set()
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON document")
        seen.add(key)
        result[key] = value
    return result


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not parse config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a JSON object, got {type(data).__name__}")
    return data


def load_tickers(config_dir: Path) -> Dict[str, Any]:
    """Return ``{'tickers': (...), 'excluded_tickers': frozenset(...),
    'whole_share_tickers': frozenset(...)}`` from tickers.json.

    ``whole_share_tickers`` (see ``near_close_dca.WHOLE_SHARE_TICKERS`` and
    ``docs/whole_share_accumulation.md``) is a ticker category distinct from
    both ``tickers``/``excluded_tickers``: a whole-share ticker IS evaluated
    by the graded ladder and actively bought, just via a quantity-based
    whole-share order instead of the normal dollar-amount one. Overlaid from
    tickers.json's ``"whole_share"`` array exactly like ``"active"``/
    ``"excluded"`` overlay ``tickers``/``excluded_tickers``.
    """
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
    whole_share = data.get("whole_share")
    if whole_share is not None:
        # Unlike "active"/"excluded" above, a present-but-malformed
        # "whole_share" value must be rejected outright rather than silently
        # skipped: silently ignoring it leaves whole_share_tickers at
        # StrategyConfig's empty-set default, so the afternoon run starts
        # successfully but never evaluates or accumulates the intended
        # whole-share ticker, with no configuration error explaining the
        # omission. Mirrors the same validation the whole-share-aware
        # maintenance and ML loaders (ticker-advisor, ml-forecaster,
        # dca_market_graph) already apply to this same field (Codex review:
        # "Reject a malformed whole-share collection at config load").
        if not isinstance(whole_share, list):
            raise ValueError(
                f"tickers.json's 'whole_share' field must be a list, got {type(whole_share).__name__}"
            )
        invalid_whole_share = [
            item for item in whole_share if not isinstance(item, str) or not _WHOLE_SHARE_SYMBOL_PATTERN.match(item)
        ]
        if invalid_whole_share:
            raise ValueError(
                f"tickers.json's 'whole_share' field contains invalid ticker symbol(s): {invalid_whole_share!r}"
            )
        out["whole_share_tickers"] = frozenset(str(t) for t in whole_share)
    # active/excluded/whole_share are a THREE-WAY DISJOINT partition of the
    # universe (see docs/whole_share_accumulation.md) -- a symbol in more
    # than one category leaves near_close_dca._run_once_impl's ticker-list
    # construction and _process_buy_candidate's dispatch ambiguous, and can
    # silently evaluate/submit the SAME ticker twice in one run, one copy
    # overwriting the other's candidate_outcomes entry. test_config_invariants.py
    # only checks the bundled config/tickers.json; a caller pointing
    # DCA_CONFIG_DIR at an alternate config directory got no such guarantee
    # (Codex review: "Reject overlapping ticker categories at config load").
    # Compared case-insensitively (not just verbatim): the categories are
    # stored as-is elsewhere (existing convention -- entries are expected to
    # already be uppercase in tickers.json), but a symbol differing only in
    # case (e.g. "AAPL" active, "aapl" whole_share) is still the SAME
    # instrument to every consumer here -- near_close_dca.WHOLE_SHARE_TICKERS
    # membership, broker calls, etc. all normalize a ticker before use. A
    # verbatim-only comparison would let that exact collision slip through
    # (Codex review: "Normalize symbols before checking category overlap").
    active_set = {str(t).upper() for t in out.get("tickers", ())}
    excluded_set = {str(t).upper() for t in out.get("excluded_tickers", frozenset())}
    whole_share_list = list(out.get("whole_share_tickers", frozenset()))
    whole_share_set = {str(t).upper() for t in whole_share_list}
    # A case-folded duplicate WITHIN whole_share (e.g. ["BYDDY", "byddy"]) is
    # invisible to the cross-category overlap check below -- both spellings
    # collapse to one entry in whole_share_set, so the set-difference math
    # never sees a collision, but BOTH spellings remain in
    # whole_share_tickers (a frozenset built verbatim, not case-insensitively).
    # Unlike "active"/"excluded" -- whose own downstream non-canonical checks
    # in scripts/generate_buy_candidates.py already catch a lower-case entry
    # (it never even reaches a duplicate-count check) -- nothing downstream
    # validates whole_share tickers at all (it is not part of that script's
    # universe; see docs/whole_share_accumulation.md's "Scope" section on the
    # Gate 5 entry point gap), so a case-folded duplicate here would silently
    # reach near_close_dca._run_once_impl's whole-share evaluation twice:
    # both spellings normalize to the SAME WholeShareAccumulationLedger entry,
    # so the accumulator's contribution gets recorded twice in one run,
    # inflating the balance twice as fast as the graded ladder actually
    # intends (Codex review: "Reject case-folded duplicates within each
    # ticker category").
    if len(whole_share_set) != len(whole_share_list):
        dupes = sorted({t for t in whole_share_list if sum(1 for u in whole_share_list if u.upper() == t.upper()) > 1})
        raise ValueError(
            f"tickers.json's whole_share list contains a case-folded duplicate symbol: {dupes} "
            "(e.g. 'BYDDY' and 'byddy' are the same instrument)"
        )
    overlaps = {
        "active/excluded": active_set & excluded_set,
        "active/whole_share": active_set & whole_share_set,
        "excluded/whole_share": excluded_set & whole_share_set,
    }
    conflicting = {pair: tickers for pair, tickers in overlaps.items() if tickers}
    if conflicting:
        details = "; ".join(f"{pair}: {sorted(tickers)}" for pair, tickers in conflicting.items())
        raise ValueError(
            f"tickers.json's active/excluded/whole_share categories must be pairwise disjoint "
            f"(case-insensitive): {details}"
        )
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


def _reject_bool_numeric(raw_value: Any, description: str) -> float:
    """``float(raw_value)``, but explicitly rejecting a JSON boolean first.

    ``bool`` is a subclass of ``int`` in Python, so ``float(True) == 1.0``
    and ``float(False) == 0.0`` would otherwise silently coerce a
    schema-invalid boolean into a real, plausible-looking numeric value --
    e.g. ``"unit_usd": true`` becomes a real $1/unit rate, or
    ``"max_fire_attempts": true`` becomes a real 1-attempt retry budget,
    both loading successfully instead of failing closed on the
    schema-invalid input they actually are (Codex review: "Reject booleans
    in inverse-Martingale numeric fields").
    """
    if isinstance(raw_value, bool):
        raise ValueError(f"{description} must be a number, not a boolean")
    return float(raw_value)


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
    if "minimum_order_usd" in data:
        min_order = _reject_bool_numeric(data["minimum_order_usd"], "sizing_ladder.json minimum_order_usd")
        if min_order <= 0:
            raise ValueError(f"sizing_ladder.json minimum_order_usd must be positive, got {min_order}")
        out["minimum_order_usd"] = min_order
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
    out: Dict[str, Any] = {"per_ticker_sizing": normalized}
    if "minimum_order_usd" in data:
        min_order = _reject_bool_numeric(data["minimum_order_usd"], "per_ticker_sizing.json minimum_order_usd")
        if min_order <= 0:
            raise ValueError(f"per_ticker_sizing.json minimum_order_usd must be positive, got {min_order}")
        out["minimum_order_usd"] = min_order
    return out


def load_inverse_martingale_config(config_dir: Path) -> Dict[str, Any]:
    """Load the opt-in inverse-Martingale accumulator config, fail-closed.

    Returns ``{}`` (mode fully disabled for every ticker) when the file is
    absent, malformed, or ``status`` is not ``"active"`` -- exactly the same
    contract as :func:`load_per_ticker_sizing`. A malformed per-ticker entry
    raises rather than being silently dropped, matching this repo's existing
    "fail closed, loudly" convention for calibration data (see
    ``load_per_ticker_sizing`` above) -- a config typo should break config
    loading, not silently disable one ticker's accumulator.
    """
    data = _read_json(config_dir / "inverse_martingale.json")
    if data is None or data.get("status") != "active":
        return {}
    raw = data.get("tickers", {})
    if not isinstance(raw, dict):
        raise ValueError("inverse_martingale.json must contain a tickers object")
    # The per-ticker `enabled` fallback below: previously every ticker
    # entry defaulted to False regardless of this value, so a
    # schema-valid "enabled_default": true had NO effect anywhere at
    # runtime -- enrollment is decided solely by presence in the returned
    # `normalized` mapping, and every ticker omitting its own `enabled`
    # field was silently dropped from it either way (Codex review: "Honor
    # the configured default enrollment switch"). Validated the same
    # strict-boolean way the per-ticker field is, just below.
    default_enabled_raw = data.get("enabled_default", False)
    if not isinstance(default_enabled_raw, bool):
        raise ValueError("inverse_martingale.json enabled_default must be a boolean")
    normalized: Dict[str, Dict[str, Any]] = {}
    seen_symbols: set[str] = set()
    for ticker, model in raw.items():
        if not isinstance(ticker, str) or not isinstance(model, dict):
            raise ValueError("inverse_martingale tickers entries must map a symbol to an object")
        # Detect a case-folded duplicate (e.g. "AAPL" and "aapl") BEFORE the
        # enabled/disabled filtering below -- both keys are individually
        # schema-valid, but `normalized` is keyed by the UPPERCASED symbol
        # (see the assignment at the end of this loop), so two variants of
        # the same real ticker collide there regardless of which one this
        # loop happens to reach last. Checking after the enabled filter
        # would miss the most dangerous case entirely: a disabled "aapl"
        # entry meant to suppress the ticker gets silently skip()ed via
        # `continue` before ever being compared against an earlier enabled
        # "AAPL" entry, leaving the ticker enrolled -- suppressing the
        # normal ladder and firing accumulator orders -- even though one
        # configuration entry explicitly says it should be off. Two ENABLED
        # variants are just as dangerous the other way: whichever one this
        # loop reaches last silently overwrites the other in `normalized`,
        # with no error and no way for an operator to know which
        # basis/tier/recovery configuration actually won (Codex review:
        # "Reject case-folded duplicate enrollment symbols").
        upper_ticker = ticker.upper()
        if upper_ticker in seen_symbols:
            raise ValueError(f"inverse_martingale tickers contains a case-folded duplicate symbol: {ticker!r}")
        seen_symbols.add(upper_ticker)
        enabled_raw = model.get("enabled", default_enabled_raw)
        # A truthy-but-non-bool value (e.g. the schema-invalid string
        # "false", which Python treats as truthy) would otherwise silently
        # ENABLE the accumulator against the operator's obvious intent --
        # require an actual JSON boolean and fail closed (loudly) on
        # anything else, matching this function's own "malformed entry
        # raises rather than being silently guessed at" convention (Codex
        # review: "Require a boolean before enabling the accumulator").
        if not isinstance(enabled_raw, bool):
            raise ValueError(f"inverse_martingale enabled must be a boolean for {ticker}")
        if not enabled_raw:
            continue
        basis = model.get("basis")
        if basis not in ("pct", "stddev"):
            raise ValueError(f"inverse_martingale basis missing/invalid for {ticker}")
        tiers_raw = model.get("tiers")
        if not isinstance(tiers_raw, list) or not tiers_raw:
            raise ValueError(f"inverse_martingale tiers missing for {ticker}")
        threshold_key = "drop_pct" if basis == "pct" else "sigma_multiple"
        parsed_tiers = []
        seen_thresholds: set[float] = set()
        for tier in tiers_raw:
            if not isinstance(tier, dict) or threshold_key not in tier or "units" not in tier:
                raise ValueError(f"inverse_martingale tier malformed for {ticker}")
            threshold_value = _reject_bool_numeric(tier[threshold_key], f"inverse_martingale {threshold_key} for {ticker}")
            units_value = _reject_bool_numeric(tier["units"], f"inverse_martingale tier units for {ticker}")
            # evaluate_inverse_martingale_step's tier-crossing loop credits a
            # tier when `threshold not in crossed_before` -- crossed_before is
            # only updated AFTER the whole loop finishes evaluating this run's
            # tiers (see near_close_dca.py's own comment on this), so two
            # configured tiers sharing the identical threshold both read as
            # "not yet crossed" in the SAME evaluation and both get credited,
            # double-accumulating for what is really one dip level (Codex
            # review: "Reject duplicate tier thresholds at load time"). Checked
            # before the positivity/finiteness validation below so a duplicate
            # is reported on its own terms rather than shadowed by whichever
            # error a malformed value might also trigger.
            if threshold_value in seen_thresholds:
                raise ValueError(
                    f"inverse_martingale tiers contains a duplicate {threshold_key} {threshold_value!r} for {ticker}"
                )
            seen_thresholds.add(threshold_value)
            # A nonpositive threshold/units, unit_usd, recovery offset, or
            # cycle/attempt limit could accumulate a negative balance,
            # trigger an immediate spurious fire, or loop indefinitely --
            # none of that is caught by the runtime evaluator itself
            # (evaluate_inverse_martingale_step trusts its inputs), and
            # schema validation is skipped entirely when load_strategy_config
            # reads a non-default DCA_CONFIG_DIR (test_every_config_file_...
            # only validates the shipped config/ tree). Validate here so a
            # malformed alternate config fails closed at load time regardless
            # (Codex review: "Reject nonpositive inverse-Martingale tier
            # values at load time"). Python's json module accepts the
            # non-standard NaN/Infinity/-Infinity tokens by default, and
            # both compare false against "<= 0" -- a NaN threshold would
            # never cross, and an infinite one would suppress the ladder
            # (enrollment alone does that) while never firing; check
            # finiteness explicitly rather than relying on the positivity
            # check alone to catch it (Codex review: "Reject non-finite
            # inverse-Martingale values").
            if not math.isfinite(threshold_value) or threshold_value <= 0:
                raise ValueError(f"inverse_martingale {threshold_key} must be positive and finite for {ticker}")
            if not math.isfinite(units_value) or units_value <= 0:
                raise ValueError(f"inverse_martingale tier units must be positive and finite for {ticker}")
            parsed_tiers.append((threshold_value, units_value))
        entry: Dict[str, Any] = {
            "basis": basis,
            "tiers": tuple(sorted(parsed_tiers)),
        }
        if basis == "pct":
            if "recovery_offset_pct" not in model:
                raise ValueError(f"inverse_martingale recovery_offset_pct missing for {ticker}")
            recovery_offset = _reject_bool_numeric(
                model["recovery_offset_pct"], f"inverse_martingale recovery_offset_pct for {ticker}"
            )
            if not math.isfinite(recovery_offset) or recovery_offset <= 0:
                raise ValueError(f"inverse_martingale recovery_offset_pct must be positive and finite for {ticker}")
            entry["recovery_offset"] = recovery_offset
        else:
            if "recovery_offset_sigma" not in model or "stddev_window_days" not in model:
                raise ValueError(f"inverse_martingale stddev fields missing for {ticker}")
            recovery_offset = _reject_bool_numeric(
                model["recovery_offset_sigma"], f"inverse_martingale recovery_offset_sigma for {ticker}"
            )
            stddev_window_days_raw = _reject_bool_numeric(
                model["stddev_window_days"], f"inverse_martingale stddev_window_days for {ticker}"
            )
            # A fractional value (e.g. 2.9) silently truncates to a smaller
            # whole number of days via int() -- reject anything that is not
            # already an exact integer rather than guessing which whole
            # number the operator meant (Codex review: "Reject fractional
            # integer limits before truncating").
            if not math.isfinite(stddev_window_days_raw) or stddev_window_days_raw != int(stddev_window_days_raw):
                raise ValueError(f"inverse_martingale stddev_window_days must be a finite whole number for {ticker}")
            stddev_window_days = int(stddev_window_days_raw)
            if not math.isfinite(recovery_offset) or recovery_offset <= 0:
                raise ValueError(
                    f"inverse_martingale recovery_offset_sigma must be positive and finite for {ticker}"
                )
            if stddev_window_days <= 1:
                raise ValueError(f"inverse_martingale stddev_window_days must be > 1 for {ticker}")
            entry["recovery_offset"] = recovery_offset
            entry["stddev_window_days"] = stddev_window_days
        normalized[ticker.upper()] = entry
    unit_usd = _reject_bool_numeric(data.get("unit_usd", 1.0), "inverse_martingale.json unit_usd")
    max_cycle_days_raw = _reject_bool_numeric(data.get("max_cycle_days", 30), "inverse_martingale.json max_cycle_days")
    max_fire_attempts_raw = _reject_bool_numeric(
        data.get("max_fire_attempts", 5), "inverse_martingale.json max_fire_attempts"
    )
    if not math.isfinite(unit_usd) or unit_usd <= 0:
        raise ValueError("inverse_martingale.json unit_usd must be positive and finite")
    # A positive-but-fractional value (e.g. 0.5) passes the ">0" check above
    # but truncates to 0 via int() below -- silently defeating the very
    # positivity this just validated (an enrolled ticker's normal ladder is
    # already suppressed, so a cycle that can abandon itself after 0 days or
    # 0 attempts is a real, not cosmetic, misconfiguration). Require an exact
    # whole number rather than truncating a fraction (Codex review: "Reject
    # fractional integer limits before truncating").
    if not math.isfinite(max_cycle_days_raw) or max_cycle_days_raw <= 0 or max_cycle_days_raw != int(max_cycle_days_raw):
        raise ValueError("inverse_martingale.json max_cycle_days must be positive and finite, and a whole number")
    if (
        not math.isfinite(max_fire_attempts_raw)
        or max_fire_attempts_raw <= 0
        or max_fire_attempts_raw != int(max_fire_attempts_raw)
    ):
        raise ValueError("inverse_martingale.json max_fire_attempts must be positive and finite, and a whole number")
    max_cycle_days = int(max_cycle_days_raw)
    max_fire_attempts = int(max_fire_attempts_raw)
    return {
        "inverse_martingale_enabled_default": default_enabled_raw,
        "inverse_martingale_unit_usd": unit_usd,
        "inverse_martingale_max_cycle_days": max_cycle_days,
        "inverse_martingale_max_fire_attempts": max_fire_attempts,
        "inverse_martingale": normalized,
    }


def load_ml_sizing_adjustment(config_dir: Path, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Load the ML-forecaster's published sizing adjustment, fail-closed.

    Returns ``{}`` (every ticker's adjustment is the implicit 1.0 no-op) when
    the file is absent, malformed, ``status`` is not ``"active"``,
    ``generated_at`` cannot be parsed, ``generated_at`` is older than
    ``max_staleness_hours``, or any published adjustment falls outside the
    file's own declared ``bounds`` -- a corrupted or stale publish must never
    partially apply. Staleness is checked ONLY here; the multiplier lookup in
    ``near_close_dca.graded_buy_multiplier`` does no I/O and trusts whatever
    this function already validated.

    Unlike :func:`load_inverse_martingale_config` (which deliberately RAISES
    on a malformed file -- see its own docstring's "fail closed, loudly"
    rationale for calibration data), a truncated/invalid-JSON/non-object
    ``ml_sizing_adjustment.json`` is caught HERE and folded into the same
    documented ``{}`` no-op as every other malformed-payload case above,
    rather than propagating ``_read_json``'s ``ValueError``. This loader
    runs unconditionally inside ``load_strategy_config()``, the single
    entry point every scheduled run uses to build its config -- an
    unhandled parse failure here would abort strategy startup entirely over
    one optional, ancillary sizing-nudge publish, not merely disable the
    nudge itself (Codex review: "Catch malformed ML publication JSON").
    """
    try:
        data = _read_json(config_dir / "ml_sizing_adjustment.json")
    except ValueError as exc:
        logger.warning("ml_sizing_adjustment.json could not be read: %s", exc)
        return {}
    if data is None or data.get("status") != "active":
        return {}
    generated_at_raw = data.get("generated_at")
    max_staleness_hours = data.get("max_staleness_hours")
    if not isinstance(generated_at_raw, str) or not isinstance(max_staleness_hours, (int, float)):
        return {}
    if isinstance(max_staleness_hours, bool) or not math.isfinite(max_staleness_hours) or max_staleness_hours <= 0:
        # A NaN/Infinity/nonpositive value passes the bare isinstance check
        # above but would raise ValueError/OverflowError constructing the
        # timedelta below -- since this loader runs during
        # load_strategy_config, that would abort strategy startup instead of
        # returning the documented empty no-op (Codex review: "Fail closed
        # on non-finite staleness values").
        return {}
    try:
        generated_at = datetime.fromisoformat(generated_at_raw)
    except ValueError:
        return {}
    if generated_at.tzinfo is None:
        # schemas/ml_sizing_adjustment.schema.json declares generated_at as
        # "format": "date-time" -- RFC 3339's date-time format REQUIRES a
        # timezone offset ("Z" or +HH:MM). Python's datetime.fromisoformat()
        # is more permissive than RFC 3339, though: it happily parses a
        # date-only string ("2026-08-12") or a full timestamp with no
        # offset, both producing a NAIVE datetime here. Silently assuming
        # UTC for either (this function's prior behavior) would apply the
        # live sizing multipliers from a publication that never actually
        # conformed to its own declared schema -- schema validation itself
        # only runs against the shipped config/ tree, never against an
        # alternate DCA_CONFIG_DIR this loader might be pointed at, the same
        # gap already closed for out-of-range bounds/boolean values
        # elsewhere in this function (Codex review: "Reject date-only
        # publication timestamps"). The real publish pipeline
        # (ml-forecaster/publish.py) always resolves generated_at to an
        # aware datetime before writing it, so a genuinely fresh, legitimate
        # publish is never naive here -- fail closed instead of guessing.
        return {}
    reference_now = now if now is not None else datetime.now(timezone.utc)
    # A caller-injected `now` (e.g. a replay/test harness) may be naive,
    # while `generated_at` above is always normalized to aware UTC just a
    # few lines up -- subtracting a naive datetime from an aware one raises
    # TypeError instead of returning the documented fail-closed {} no-op
    # (Codex review: "Normalize the injected staleness clock").
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=timezone.utc)
    age = reference_now - generated_at
    try:
        # A finite max_staleness_hours already passed the isfinite check
        # above, but "finite" does not mean "representable" -- an extreme
        # value like 1e308 still raises OverflowError constructing the
        # timedelta below (timedelta is internally bounded to roughly
        # roughly +/-2.7 million years). Uncaught, that would abort THIS load_strategy_
        # config() call entirely instead of the documented fail-closed {}
        # no-op -- and since max_staleness_hours is read from a PERSISTED
        # file, a single malformed publish would break every subsequent
        # load until manually fixed (Codex review: "Bound staleness values
        # before constructing a timedelta").
        staleness_budget = timedelta(hours=float(max_staleness_hours))
    except OverflowError:
        return {}
    if age < timedelta(0) or age > staleness_budget:
        return {}
    bounds = data.get("bounds")
    if not isinstance(bounds, dict):
        return {}
    try:
        # bool is a subclass of int in Python -- float(True) == 1.0 -- so a
        # hand-edited or malformed publication with "min_multiplier": true
        # would otherwise silently pass as a real 1.0 bound (itself always
        # valid, since min <= 1.0 <= max is enforced below), applying a
        # live sizing adjustment from a payload that is actually
        # schema-invalid instead of the documented fail-closed no-op
        # (Codex review: "Reject booleans in ML multiplier bounds").
        # Reuses the same guard load_inverse_martingale_config()'s numeric
        # fields already apply.
        min_multiplier = _reject_bool_numeric(bounds["min_multiplier"], "ml_sizing_adjustment.json bounds.min_multiplier")
        max_multiplier = _reject_bool_numeric(bounds["max_multiplier"], "ml_sizing_adjustment.json bounds.max_multiplier")
    except (KeyError, TypeError, ValueError, OverflowError):
        # json.loads() parses an arbitrarily large JSON integer into an
        # unbounded Python int with no size limit of its own -- a 400-digit
        # "min_multiplier"/"max_multiplier" passes the isinstance(int, float)
        # check inside _reject_bool_numeric() but overflows float's finite
        # range, raising OverflowError rather than one of the other already-
        # caught exception types. This loader runs inside every
        # load_strategy_config() call, so an uncaught OverflowError here
        # aborted strategy startup entirely instead of the documented
        # fail-closed {} no-op for this optional publication (Codex review:
        # "Catch overflow while parsing multiplier bounds"). Mirrors the
        # identical guard already applied to the per-ticker adjustment
        # value's own float() conversion below.
        return {}
    # Enforce schemas/ml_sizing_adjustment.schema.json's own HARD limits here
    # too, not just internal consistency (min <= 1.0 <= max) -- schema
    # validation only ever runs against the shipped config/ tree
    # (test_every_config_file_validates_against_its_referenced_schema), never
    # against an alternate DCA_CONFIG_DIR this loader might be pointed at, so
    # a manually edited or malformed file from such a directory could
    # otherwise declare arbitrarily wide bounds (e.g. 0.01..100) and have
    # every adjustment validated against THOSE instead of the schema's real
    # 0.5..1.5 ceiling (Codex review: "Enforce the schema limits when loading
    # ML bounds").
    if not (0.5 <= min_multiplier <= 1.0 <= max_multiplier <= 1.5):
        return {}
    raw_adjustments = data.get("adjustments", {})
    if not isinstance(raw_adjustments, dict):
        return {}
    normalized: Dict[str, float] = {}
    seen_symbols: set[str] = set()
    for ticker, value in raw_adjustments.items():
        # bool is a subclass of int in Python -- isinstance(True, (int, float))
        # is True -- so without the explicit bool exclusion here (already
        # applied to max_staleness_hours above), a malformed
        # `"adjustments": {"AAPL": true}` would silently coerce to
        # float(True) == 1.0, itself always inside [min_multiplier,
        # max_multiplier] (since min <= 1.0 <= max is enforced above), and
        # pass through as a real, live 1.0 sizing adjustment instead of
        # being rejected as the invalid input it is.
        if not isinstance(ticker, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}
        # A case-folded duplicate (e.g. both "AAPL" and "aapl" published)
        # would otherwise both pass every check above and collide in
        # `normalized`, keyed by the UPPERCASED symbol -- whichever one this
        # loop reaches last silently wins, with JSON insertion order (an
        # implementation detail, not something an operator can reliably
        # reason about) deciding which live sizing multiplier actually
        # applies (Codex review: "Reject case-folded duplicate ML
        # adjustment symbols"). Matches load_inverse_martingale_config's
        # identical duplicate check, but rejects the whole publish (this
        # function's own established fail-closed contract for any malformed
        # entry) rather than raising.
        upper_ticker = ticker.upper()
        if upper_ticker in seen_symbols:
            return {}
        seen_symbols.add(upper_ticker)
        # json.loads() parses an arbitrarily large JSON integer into a
        # Python int with no size limit of its own -- a malformed or
        # externally supplied publication carrying, say, a 400-digit
        # adjustment value passes every check above (it's a real, non-bool
        # int) but overflows float()'s own finite range, raising an
        # uncaught OverflowError. Since this loader runs inside every
        # load_strategy_config() call, that would abort live strategy
        # startup entirely instead of returning this function's documented
        # fail-closed {} for a malformed publish.
        try:
            multiplier = float(value)
        except OverflowError:
            return {}
        if not (min_multiplier <= multiplier <= max_multiplier):
            # A single out-of-bounds entry rejects the whole publish rather
            # than silently dropping just that ticker's adjustment -- simpler
            # fail-closed semantics than per-ticker filtering.
            return {}
        normalized[upper_ticker] = multiplier
    return {"ml_sizing_adjustment": normalized}


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

    The inverse-Martingale and ML-sizing-adjustment fields are excluded too, for
    a different reason: those two have their OWN dedicated, validated loaders
    (:func:`load_inverse_martingale_config`, :func:`load_ml_sizing_adjustment`)
    that reject nonpositive/non-finite tier values, out-of-schema bounds, and
    stale timestamps -- and this function's own generic pass-through applies
    none of that validation. Since ``load_strategy_config`` merges this
    function's output AFTER both dedicated loaders, letting these fields
    through here would let a ``near_close_dca.json`` entry silently replace an
    already-validated ``inverse_martingale``/``ml_sizing_adjustment`` value
    with an unchecked one -- e.g. injecting a stale or out-of-range
    ``ml_sizing_adjustment``, or an ``inverse_martingale`` entry with malformed
    limits that still suppresses the normal ladder for that ticker (Codex
    review: "Prevent generic overrides from bypassing sizing validation"). An
    operator who genuinely wants to change either must edit that field's own
    config file instead.
    """
    data = _read_json(config_dir / "near_close_dca.json")
    if data is None:
        return {}
    valid_fields = {f.name for f in dataclasses.fields(StrategyConfig)}
    excluded = {
        "dry_run",
        "broker",
        "inverse_martingale_enabled_default",
        "inverse_martingale_unit_usd",
        "inverse_martingale_max_cycle_days",
        "inverse_martingale_max_fire_attempts",
        "inverse_martingale",
        "ml_sizing_adjustment",
    }
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key in valid_fields and key not in excluded:
            out[key] = value
    return out


def load_strategy_config(
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    *,
    base: Optional[StrategyConfig] = None,
    now: Optional[datetime] = None,
    **overrides: Any,
) -> StrategyConfig:
    """Return a :class:`StrategyConfig` with ``config/*.json`` overlaid on ``base``.

    Precedence (lowest to highest): ``base`` defaults < config JSON < explicit
    ``overrides``. ``dry_run`` and ``broker`` are never set from JSON — they stay at
    their ``base`` values unless passed as explicit overrides — so loading config can
    never by itself enable live trading.

    ``now`` is the reference time :func:`load_ml_sizing_adjustment` checks
    ``ml_sizing_adjustment.json``'s ``generated_at`` staleness against;
    ``None`` (the default) means the real wall clock. A caller REPLAYING a
    historical run (``run_dca.py --now <past timestamp>``) must pass that
    same timestamp here too, or the published file's freshness is judged
    against the real "today" instead of the replay's intended "now" --
    applying a publish that is from the future relative to the replay, or
    omitting one that was genuinely fresh AT the replay date, either way
    silently contaminating the replayed decisions (Codex review: "Validate
    ML staleness against the run timestamp").
    """
    config_dir = Path(config_dir)
    # Whether the CALLER supplied an explicit `base` -- decides below whether
    # the canonical WHOLE_SHARE_TICKERS fallback applies at all (see that
    # branch's own comment, after `merged` is known).
    explicit_base_given = base is not None
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
    merged.update(load_inverse_martingale_config(config_dir))
    merged.update(load_ml_sizing_adjustment(config_dir, now=now))
    merged.update(load_approved_dates(config_dir))
    merged.update(load_near_close_overrides(config_dir))

    # StrategyConfig()'s own bare class default deliberately leaves
    # whole_share_tickers at frozenset() (see that field's own docstring) --
    # unlike tickers/excluded_tickers, which default to the real
    # ACTIVE_TICKERS/EXCLUDED_TICKERS constants, defaulting this field
    # non-empty on the CLASS itself would silently evaluate BYDDY against
    # every bare `StrategyConfig(tickers=(...))` construction throughout the
    # test suite, most of which never provision a broker/data double for it.
    # But a CALLER of THIS loader who passed no `base` and whose config
    # never declared its own "whole_share" key (load_tickers's own merge
    # above left "whole_share_tickers" out of `merged` entirely) should get
    # the canonical WHOLE_SHARE_TICKERS membership here, exactly like it
    # already gets ACTIVE_TICKERS/EXCLUDED_TICKERS from StrategyConfig()'s
    # own defaults -- without this, the documented installed-wheel/missing-
    # config-dir fallback (see the warning above), or simply an existing
    # tickers.json that omits the optional "whole_share" key, silently
    # stopped evaluating and accumulating BYDDY, while the active/excluded
    # universes kept their real hardcoded fallback (Codex review: "Preserve
    # whole-share defaults when config is absent"). Subtracts whatever this
    # config's FINAL excluded_tickers turns out to be (from `merged` if the
    # config declared its own "excluded", else `base`'s) first: a config
    # that explicitly excludes BYDDY (a supported, safer do-not-buy state
    # that never mentions "whole_share" at all) must not have this implicit
    # default inject BYDDY into whole_share_tickers, overlapping
    # excluded_tickers and failing the disjointness validation just below
    # for a config that never asked for BYDDY to be evaluated in ANY
    # category (Codex review: "Honor explicit exclusions when applying
    # whole-share defaults"). A caller that DOES pass an explicit `base`
    # (e.g. a test isolating a small ad hoc universe), or whose config
    # explicitly declares its own "whole_share" list, is unaffected.
    if not explicit_base_given and "whole_share_tickers" not in merged:
        # `overrides` (the caller's own explicit keyword arguments) is the
        # HIGHEST-precedence layer -- applied via merged.update(overrides)
        # below, AFTER this default is computed. Deriving final_excluded_tickers
        # from `merged` alone (the config-file layer) missed a caller-supplied
        # `excluded_tickers=` override entirely: a caller explicitly excluding
        # BYDDY via that documented override, on a tickers.json that omits
        # "whole_share", would still get BYDDY injected into
        # whole_share_tickers here (computed from the lower-precedence
        # merged/base exclusions), which merged.update(overrides) cannot
        # retroactively undo -- the final partition validation then raised
        # for an excluded/whole-share overlap the caller's own override was
        # trying to avoid (Codex review: "Apply exclusion overrides before
        # deriving whole-share defaults"). Check `overrides` first, exactly
        # matching the precedence order `dataclasses.replace(base, **merged)`
        # itself applies.
        final_excluded_tickers = (
            overrides.get("excluded_tickers")
            or merged.get("excluded_tickers")
            or base.excluded_tickers
            or frozenset()
        )
        merged["whole_share_tickers"] = frozenset(WHOLE_SHARE_TICKERS) - final_excluded_tickers

    merged.update(overrides)

    valid_fields = {f.name for f in dataclasses.fields(StrategyConfig)}
    unknown = set(merged) - valid_fields
    if unknown:
        raise ValueError(f"unknown StrategyConfig fields from config/overrides: {sorted(unknown)}")

    result = dataclasses.replace(base, **merged)

    # load_tickers's own pairwise-disjointness check only sees the categories a
    # PARTIAL tickers.json explicitly provides -- a config defining only
    # "whole_share": ["aapl"] passes that check (its own "tickers"/"excluded"
    # sets are empty, so there is nothing to overlap with), but the merge
    # above never touches `base.tickers`, whose DEFAULT active universe
    # (ACTIVE_TICKERS) still contains "AAPL". The resulting StrategyConfig
    # then has the SAME instrument in both `tickers` and `whole_share_tickers`,
    # which near_close_dca._process_buy_candidate has no defined behavior for
    # -- both the notional and whole-share paths would evaluate/act on it in
    # the same run. Re-validate disjointness here, after every base/JSON/
    # override layer has been applied, using the FINAL resolved sets rather
    # than just what one partial config file happened to declare (Codex
    # review: "Validate the final overlaid ticker partition").
    final_active = {t.upper() for t in result.tickers}
    final_excluded = {t.upper() for t in result.excluded_tickers}
    final_whole_share_list = list(result.whole_share_tickers)
    final_whole_share = {t.upper() for t in final_whole_share_list}
    # A case-folded duplicate supplied directly through `base` or an explicit
    # keyword override (e.g. whole_share_tickers=frozenset({"BYDDY", "byddy"}))
    # bypasses load_tickers/tickers.json entirely, so its own case-folded-
    # duplicate check (dca_config.load_tickers) never runs against it. Both
    # spellings survive as distinct entries in the frozenset (frozenset only
    # dedupes exact matches), and normalizing here to check overlaps collapses
    # them into ONE entry -- silently hiding the duplicate from the overlap
    # check above, exactly like the tickers.json case this mirrors (Codex
    # review: "Reject case-folded duplicates after applying overlays").
    if len(final_whole_share) != len(final_whole_share_list):
        final_whole_share_dupes = sorted(
            {t for t in final_whole_share_list if sum(1 for u in final_whole_share_list if u.upper() == t.upper()) > 1}
        )
        raise ValueError(
            "the final resolved StrategyConfig's whole_share_tickers contains a case-folded "
            f"duplicate symbol: {final_whole_share_dupes} (e.g. 'BYDDY' and 'byddy' are the same instrument)"
        )
    # load_tickers's _WHOLE_SHARE_SYMBOL_PATTERN check only runs against
    # tickers.json's own "whole_share" list -- a symbol supplied through
    # `base` or the documented `whole_share_tickers=` keyword override
    # bypasses that loader entirely and reaches here unchecked. A lowercase
    # symbol (e.g. "byddy") survives every check above (it does not collide
    # with anything, and nothing here case-folds membership before use), but
    # near_close_dca's signal lookup and RobinhoodDataProvider._quote_for()/
    # _parse_bars() compare the broker's canonical UPPERCASE response
    # verbatim, so the ticker silently never matches and never accumulates
    # (Codex review: "Validate final whole-share symbols after overrides").
    invalid_final_whole_share = [t for t in final_whole_share_list if not _WHOLE_SHARE_SYMBOL_PATTERN.match(t)]
    if invalid_final_whole_share:
        raise ValueError(
            "the final resolved StrategyConfig's whole_share_tickers contains invalid ticker "
            f"symbol(s): {sorted(invalid_final_whole_share)!r}"
        )
    final_overlaps = {
        "active/excluded": final_active & final_excluded,
        "active/whole_share": final_active & final_whole_share,
        "excluded/whole_share": final_excluded & final_whole_share,
    }
    final_conflicting = {pair: tickers for pair, tickers in final_overlaps.items() if tickers}
    if final_conflicting:
        details = "; ".join(f"{pair}: {sorted(tickers)}" for pair, tickers in final_conflicting.items())
        raise ValueError(
            "the final resolved StrategyConfig's active/excluded/whole_share categories "
            f"must be pairwise disjoint (case-insensitive): {details}"
        )

    return result
