"""Read-only, schema-aware view of ``config/*.json`` for the local console.

Backs the Config page (GUI expansion, Phase 2) and the Today page's live-money
banner and trading-window countdown. Builders take explicit inputs, return a
JSON-safe dict with ``warnings`` always present and ``None`` meaning
unavailable, and never emit an absolute path, order id or account number.
Files are parsed through :func:`dca_config._read_json` (duplicate keys and
non-objects rejected exactly as the live loaders do) after
:class:`dca_console_reader.BoundedReader` confines and bounds the read.
Nothing here writes, locks, or constructs an executor or broker; the only
``os.environ`` access is a variable NAME's membership, never its value.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import math
import os
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import dca_config
import near_close_dca
from dca_console_jobs import redact_value
from dca_console_reader import BoundedReader

MAX_TEXT = 500
MAX_ROWS = 200
MAX_VIEW_CHARS = 200_000
GRID_MAX_DROP_PCT = 12.0
GRID_STEP_PCT = 0.25
BANNER = ("This console never places, reviews or cancels orders; "
          "live placement happens only in external agent sessions.")
PARITY_NOTE = ("Structural, in-process comparison only; the authoritative gate is the "
               "config-invariants job (tests/test_config_invariants.py).")
STREAMS = ("market_open", "morning", "midday", "intraday")

# Every config/*.json stem, the schemas/ stem it validates against (None for the
# seven files that carry no "$schema"), and whether a publisher writes it.
CONFIG_FILES: tuple[tuple[str, str | None, bool], ...] = (
    ("backtest_intraday_cap_curve", None, False),
    ("backtest_sizing_ladder", "sizing_ladder", False),
    ("budgets", "budgets", False),
    ("intraday_dca", None, False),
    ("inverse_martingale", "inverse_martingale", False),
    ("market_calendar", "market_calendar", False),
    ("market_open_dca", None, False),
    ("midday_dca", None, False),
    ("ml_sizing_adjustment", "ml_sizing_adjustment", True),
    ("morning_dca", None, False),
    ("near_close_dca", None, False),
    ("notifications", "notifications", False),
    ("notion_issue_loop", "notion_issue_loop", False),
    ("per_ticker_sizing", None, True),
    ("reporting", "reporting", False),
    ("run_classification", "run_classification_policy", False),
    ("scheduled_tasks", "scheduled_tasks", False),
    ("self_heal", "self_heal", False),
    ("sizing_ladder", "sizing_ladder", False),
    ("tickers", "tickers", False),
    ("trading_window", "trading_window", False),
)
STEMS = frozenset(stem for stem, _schema, _publisher in CONFIG_FILES)

# Which loader reads each file. Files with no dca_config loader name the
# consumer that does read them so the page never shows a bare "unknown".
LOADERS: dict[str, str] = {
    "tickers": "dca_config.load_tickers", "budgets": "dca_config.load_budgets",
    "sizing_ladder": "dca_config.load_sizing_tiers", "per_ticker_sizing": "dca_config.load_per_ticker_sizing",
    "inverse_martingale": "dca_config.load_inverse_martingale_config",
    "ml_sizing_adjustment": "dca_config.load_ml_sizing_adjustment",
    "trading_window": "dca_config.load_approved_dates", "near_close_dca": "dca_config.load_near_close_overrides",
    "backtest_sizing_ladder": "dca_config.load_backtest_sizing_tiers",
    "market_calendar": "dca_runner (Gate 0 market_day_status)",
    "morning_dca": "morning_dca.load_morning_config", "market_open_dca": "market_open_dca.load_market_open_config",
    "midday_dca": "midday_dca.load_midday_config", "intraday_dca": "intraday_dca.load_intraday_config",
    "scheduled_tasks": "dca_console_data (schedule) / scripts", "run_classification": "scripts.run_classification",
    "notifications": "dca_alerts", "reporting": "dca_reporting", "self_heal": "self-heal workflow",
    "notion_issue_loop": "selfheal.notion_issue_loop.load_config",
    "backtest_intraday_cap_curve": "analytics only; never loaded live",
}
# What a malformed file does to a live start. Line references are dca_config.py:
# _read_json raises on any unparsable/duplicate-key/non-object file (199-201);
# budgets raises when present but missing the breaker (530-535); sizing_ladder
# (630-648), per_ticker_sizing (660-684), inverse_martingale (705-868) and
# trading_window (1108-1125) each raise on their own malformed shapes;
# ml_sizing_adjustment alone folds a parse failure into {} (905-908).
FAIL_MODES: dict[str, str] = {
    "tickers": "raises", "budgets": "raises", "sizing_ladder": "raises", "per_ticker_sizing": "raises",
    "inverse_martingale": "raises", "trading_window": "raises", "near_close_dca": "raises",
    "backtest_sizing_ladder": "raises", "ml_sizing_adjustment": "degrades", "scheduled_tasks": "degrades",
}


# An absolute-path-shaped token: two or more "/segment" runs not glued to a
# word, a URL scheme's "//", or a relative "../" prefix. The committed stream
# policies document artifact roots as prose ("/home/user/workspace/..."), and
# a fixture can seed this machine's own tmp path; either way the location is
# dropped and only the final segment survives, so a documented file name
# stays readable while no directory ever reaches the browser.
_PATH_TOKEN = re.compile(r"(?<![\w:/.])(?:/[\w.@+~-]+){2,}")


def _scrub_paths(text: str) -> str:
    return _PATH_TOKEN.sub(lambda match: "[path]/" + match.group(0).rsplit("/", 1)[1], text)


# How much of an unbounded source string is scanned before it is bounded.
# Redaction needs the whole token (an account-shaped digit run, an exported
# secret value, a path) so it must run BEFORE the length cut; the pre-slice
# only caps the regex cost on a pathological multi-megabyte field.
_SCAN_LIMIT_FACTOR = 64


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """Bound one free-text field and strip any secret value or path before it leaves.

    Redact and scrub first, bound last (adversarial review, config area): a cut
    made before ``redact_value`` splits a 12-digit account literal into a
    9-digit tail that its regex no longer recognises, and splits a documented
    "/home/user/..." path so the scrubber never sees the second segment.
    """
    if value is None:
        return None
    scanned = str(value)[:limit * _SCAN_LIMIT_FACTOR].replace("\n", " ")
    return redact_value(_scrub_paths(scanned))[:limit]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _reader(config_dir: Path, warnings: list[str]) -> BoundedReader:
    # Resolved so a symlinked config dir (macOS /tmp -> /private/tmp) still
    # passes the reader's is_relative_to confinement on every read.
    return BoundedReader(Path(config_dir).resolve(), warnings)


def _loader_error(exc: BaseException, *paths: Path) -> str:
    # dca_config's messages embed the absolute config path; name the file only.
    message = str(exc)
    for path in paths:
        message = message.replace(str(path), path.name)
    return _text(message) or "error"


def _parse(config_dir: Path, stem: str, warnings: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one config file the way the live loaders do, inside the reader's confinement.

    Returns ``(object, None)``, ``(None, None)`` when absent, or ``(None, error)``
    when unreadable, oversized, or rejected by :func:`dca_config._read_json`.
    """
    reader = _reader(config_dir, warnings)
    name = f"{stem}.json"
    path = reader.confined(name)
    if path is None or not path.is_file():
        return None, None
    try:
        if path.stat().st_size > reader.max_bytes:
            return None, "exceeds the 32 MiB read limit"
        return dca_config._read_json(path), None
    except (OSError, ValueError) as exc:
        return None, _loader_error(exc, path, Path(reader.root))


def _warn_loader(warnings: list[str], config_dir: Path, label: str, exc: BaseException) -> None:
    """Record a loader rejection under a file name or label, never under a path."""
    message = f"{label}: {_loader_error(exc, Path(config_dir).resolve())}"
    if message not in warnings:
        warnings.append(message)


def _raw(config_dir: Path, stem: str, warnings: list[str]) -> dict[str, Any] | None:
    data, error = _parse(config_dir, stem, warnings)
    if error is not None and f"{stem}.json: {error}" not in warnings:
        warnings.append(f"{stem}.json: {error}")
    return data


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Make a loader value JSON-safe: tuples/sets to lists, dates to ISO, bounded."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # Before the str branch: BrokerName is a str-mixin Enum whose str() is the
    # member name, and the page wants the wire value ("robinhood").
    if isinstance(value, enum.Enum):
        return _json_safe(value.value, depth)
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if depth >= 8:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(key)[:80]: _json_safe(item, depth + 1) for key, item in list(value.items())[:MAX_ROWS]}
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:MAX_ROWS]]
    return _text(repr(value))


def _env_reference(text: str) -> str:
    # The operator sees that a placeholder exists and whether it resolves;
    # the value never enters the response (set == non-empty, exactly the rule
    # dca_config.expand_config_env applies).
    return dca_config._ENV_PLACEHOLDER.sub(
        lambda match: f"env reference {match.group(1)} ({'set' if os.environ.get(match.group(1)) else 'unset'})", text)


def _view_safe(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return redact_value(_scrub_paths(_env_reference(value)))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if depth >= 32:
        return None
    if isinstance(value, dict):
        return {str(key): _view_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_view_safe(item, depth + 1) for item in value]
    return value


def config_catalog(config_dir: Path, repo: Path) -> dict[str, Any]:
    """Describe every known config file: presence, size, parse result, schema and loader.

    ``parse_ok``/``error`` come from :func:`dca_config._read_json`; ``schema_exists``
    is None for a file that has no schema, ``modified`` is UTC ISO or None.
    """
    warnings: list[str] = []
    reader = _reader(config_dir, warnings)
    schemas_dir = Path(repo).resolve() / "schemas"
    rows = []
    for stem, schema_stem, publisher_only in CONFIG_FILES:
        name = f"{stem}.json"
        path = reader.confined(name)
        exists = path is not None and path.is_file()
        size = modified = None
        if exists:
            try:
                stat = path.stat()
                size, modified = stat.st_size, datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                reader.warn(f"{name}: metadata could not be read.")
        data, error = _parse(config_dir, stem, warnings) if exists else (None, None)
        ref = data.get("$schema") if isinstance(data, dict) else None
        schema_ref = _text(ref, 120) if isinstance(ref, str) else None
        if schema_ref and schema_stem and not schema_ref.endswith(f"{schema_stem}.schema.json"):
            reader.warn(f"{name} names a different schema than the catalog expects.")
        rows.append({
            "stem": stem, "exists": exists, "size": size, "modified": modified, "schema_ref": schema_ref,
            "schema_stem": schema_stem,
            "schema_exists": (schemas_dir / f"{schema_stem}.schema.json").is_file() if schema_stem else None,
            "parse_ok": data is not None if exists else None, "error": error, "publisher_only": publisher_only,
            "loader": LOADERS.get(stem, "unknown"), "fail_mode": FAIL_MODES.get(stem, "unknown"),
        })
    return {"files": rows, "warnings": warnings}


def config_file_view(config_dir: Path, stem: str) -> dict[str, Any]:
    """Pretty-print one catalogued config file with every secret value and ``${VAR}`` neutralised.

    ``found`` is False for an unknown stem or an absent file; ``text`` is None
    when the file is present but rejected (see ``error``); ``truncated`` marks a
    view cut at 200 KB.
    """
    warnings: list[str] = []
    result: dict[str, Any] = {"stem": _text(stem, 80), "found": False, "text": None, "truncated": False,
                              "error": None, "warnings": warnings}
    if stem not in STEMS:
        warnings.append("Unknown config file.")
        return result
    data, error = _parse(config_dir, stem, warnings)
    result["error"] = error
    result["found"] = data is not None or error is not None
    if data is None:
        return result
    text = json.dumps(_view_safe(data), indent=2, allow_nan=False)
    if len(text) > MAX_VIEW_CHARS:
        text, result["truncated"] = text[:MAX_VIEW_CHARS], True
        warnings.append("The view was truncated at 200 KB.")
    result["text"] = text
    return result


def _check(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": _text(detail)}


def _universe_check(name: str, documented: list[str] | None, constant: Any, ordered: bool) -> dict[str, Any]:
    """Compare one tickers.json list with its near_close_dca constant, by order or by set."""
    if documented is None:
        return _check(name, None, "tickers.json value unavailable")
    ok = documented == list(constant) if ordered else set(documented) == set(constant)
    return _check(name, ok, f"tickers.json has {len(documented)}, near_close_dca has {len(constant)}; "
                            f"only in json: {sorted(set(documented) - set(constant)) or 'none'}; "
                            f"only in constant: {sorted(set(constant) - set(documented)) or 'none'}")


def _schema_exists(config_dir: Path, ref: str) -> bool | None:
    """Resolve a relative ``$schema`` beside the config dir, then beside this checkout.

    A ``$DCA_CONFIG_DIR`` override (or a test's tmp copy) carries no schemas/
    of its own; the invariants job validates such a directory against the
    checkout's schemas, so the fallback mirrors it. Neither lookup may leave
    its base directory.
    """
    for base in (Path(config_dir).resolve(), Path(__file__).resolve().parent / "config"):
        try:
            target = (base / ref).resolve()
        except (OSError, RuntimeError):
            return None
        if target.is_relative_to(base.parent) and target.is_file():
            return True
    return False


def parity_checks(config_dir: Path) -> dict[str, Any]:
    """Mirror the config-invariants parity tests structurally, in process.

    ``ok`` is None when the file a check needs is unreadable. Mirrors
    tests/test_config_invariants.py's universe, breaker and schema checks only.
    """
    warnings: list[str] = []
    tickers = _raw(config_dir, "tickers", warnings)
    sizing = _raw(config_dir, "per_ticker_sizing", warnings)
    budgets = _raw(config_dir, "budgets", warnings)
    checks = []

    def lst(data: dict[str, Any] | None, key: str) -> list[str] | None:
        value = data.get(key) if data else None
        return [str(item) for item in value] if isinstance(value, list) else None

    active, excluded, whole = lst(tickers, "active"), lst(tickers, "excluded"), lst(tickers, "whole_share")
    checks.append(_universe_check("active_tickers_order", active, near_close_dca.ACTIVE_TICKERS, True))
    checks.append(_universe_check("excluded_tickers_set", excluded, near_close_dca.EXCLUDED_TICKERS, False))
    checks.append(_universe_check("whole_share_tickers_set", whole, near_close_dca.WHOLE_SHARE_TICKERS, False))
    if active is None or excluded is None or whole is None:
        checks.append(_check("categories_disjoint", None, "tickers.json categories unavailable"))
    else:
        overlap = (set(active) & set(excluded)) | (set(active) & set(whole)) | (set(excluded) & set(whole))
        checks.append(_check("categories_disjoint", not overlap, f"overlap: {sorted(overlap) or 'none'}"))
    entries = sizing.get("tickers") if sizing else None
    if not isinstance(entries, dict):
        checks.append(_check("whole_share_sizing_entries", None, "per_ticker_sizing.json tickers unavailable"))
    else:
        missing = sorted(t for t in near_close_dca.WHOLE_SHARE_TICKERS if not isinstance(entries.get(t), dict)
                         or entries[t].get("base_amount_usd") != 1.0 or entries[t].get("base_amount_below_close_usd") != 1.0)
        checks.append(_check("whole_share_sizing_entries", not missing, f"missing or not $1-based: {missing or 'none'}"))
    breakers = budgets.get("circuit_breakers") if budgets else None
    if budgets is None:
        checks.append(_check("budgets_breaker", None, "budgets.json unavailable"))
    else:
        streak = breakers.get("max_consecutive_placement_failures") if isinstance(breakers, dict) else None
        ok = isinstance(streak, int) and not isinstance(streak, bool) and streak >= 2
        checks.append(_check("budgets_breaker", ok, f"circuit_breakers.max_consecutive_placement_failures = {streak!r}"))
    bad = []
    for stem, _schema, _publisher in CONFIG_FILES:
        data = _raw(config_dir, stem, warnings)
        ref = data.get("$schema") if data else None
        if isinstance(ref, str) and _schema_exists(config_dir, ref) is not True:
            bad.append(stem)
    checks.append(_check("schema_files_exist", not bad, f"configs whose $schema is missing: {bad or 'none'}"))
    return {"checks": checks, "note": PARITY_NOTE, "warnings": warnings}


def _load_config(config_dir: Path, now: datetime, warnings: list[str]) -> tuple[Any, list[str]]:
    """Run load_strategy_config; report an unset ``${VAR}`` by name, never by value."""
    try:
        return dca_config.load_strategy_config(Path(config_dir), now=now), []
    except dca_config.MissingConfigEnvError as exc:
        names = sorted(set(dca_config._ENV_PLACEHOLDER.findall(str(exc))))
        warnings.append("The strategy config references an unset environment variable.")
        return None, names
    except (ValueError, TypeError, OSError) as exc:
        _warn_loader(warnings, config_dir, "The strategy config could not be loaded", exc)
        return None, []


def effective_config(config_dir: Path, now: datetime) -> dict[str, Any]:
    """Every StrategyConfig field after the JSON overlay, next to whether it left its default.

    ``source`` is ``json`` when the value differs from a bare ``StrategyConfig()``;
    ``dry_run``/``broker`` carry a note because no file can set them; the account
    number is reported as ``configured``/``unset`` only.
    """
    warnings: list[str] = []
    config, unresolved = _load_config(config_dir, now, warnings)
    fields: list[dict[str, Any]] = []
    if config is not None:
        defaults = near_close_dca.StrategyConfig()
        for field in dataclasses.fields(near_close_dca.StrategyConfig):
            value, default = getattr(config, field.name), getattr(defaults, field.name)
            differs, note = value != default, None
            if field.name == "target_account_number":
                # The class default already carries the env value when it was
                # exported at import time, so neither side may be echoed: label
                # only. A literal "${VAR}" that survived the overlay is unset.
                text = value if isinstance(value, str) else ""
                unresolved.extend(n for n in dca_config._ENV_PLACEHOLDER.findall(text) if not os.environ.get(n))
                placeholder = text in ("", near_close_dca._UNSET_ACCOUNT_SENTINEL) or bool(dca_config._ENV_PLACEHOLDER.fullmatch(text))
                value = "unset" if placeholder else "configured"
            elif field.name in ("dry_run", "broker"):
                note = "never settable from JSON"
            fields.append({"name": field.name, "value": _json_safe(value), "default_differs": differs,
                           "source": "json" if differs else "default", "note": note})
    return {"fields": fields, "unresolved_env": sorted(set(unresolved)), "warnings": warnings}


def _grid_row(drop_pct: float, config: Any, ticker: str | None) -> dict[str, Any]:
    # Prices are synthetic: previous_close=100, decision_price=100*(1-drop/100),
    # previous_low=None. select_first_allocation_base_usd then answers with the
    # reduced ($1-scale) base for any positive drop and $0 at zero drop; the
    # full base needs a prior low the preview deliberately does not invent.
    base, reason = near_close_dca.select_first_allocation_base_usd(
        100.0 * (1.0 - drop_pct / 100.0), 100.0, None, config, ticker)
    multiplier = near_close_dca.graded_buy_multiplier(drop_pct / 100.0, config, ticker=ticker)
    return {"drop_pct": drop_pct, "multiplier": _finite(multiplier), "base_usd": _finite(base),
            "base_reason": reason, "candidate_usd": _finite(round(base * multiplier, 4))}


def _config_or_defaults(config_dir: Path, now: datetime, warnings: list[str]) -> Any:
    config, _names = _load_config(config_dir, now, warnings)
    if config is None:
        warnings.append("Ladder values use built-in StrategyConfig defaults because the config did not load.")
        config = near_close_dca.StrategyConfig()
    return config


def ladder_preview(config_dir: Path) -> dict[str, Any]:
    """The graded ladder over a fixed 0-12% drop grid plus the budget tiers and per-ticker summary."""
    warnings: list[str] = []
    config = _config_or_defaults(config_dir, datetime.now(timezone.utc), warnings)
    steps = int(GRID_MAX_DROP_PCT / GRID_STEP_PCT) + 1
    grid = [_grid_row(index * GRID_STEP_PCT, config, None) for index in range(steps)]
    tiers = [_json_safe(tier) for tier in near_close_dca.BUDGET_TIERS]
    raw = _raw(config_dir, "per_ticker_sizing", warnings)
    version = _text(raw.get("version"), 40) if raw else None
    per_ticker = []
    try:
        loaded = dca_config.load_per_ticker_sizing(Path(config_dir)).get("per_ticker_sizing", {})
    except (ValueError, TypeError) as exc:
        # TypeError too: the loader does float(tier["min_drop_pct"]) (dca_config.py
        # 672) and a publisher that emits null or an object there raises TypeError,
        # not ValueError. A bad publish must degrade the page, never 500 it.
        _warn_loader(warnings, config_dir, "per_ticker_sizing.json", exc)
        loaded = {}
    for ticker, model in sorted(loaded.items())[:MAX_ROWS]:
        per_ticker.append({"ticker": _text(ticker, 16), "entries": len(model.get("tiers", ())), "version": version})
    return {"grid": grid, "tiers": tiers, "per_ticker": per_ticker, "warnings": warnings}


def ladder_point(config_dir: Path, drop_pct: Any, ticker: Any) -> dict[str, Any]:
    """One ladder row for a validated drop (0-50) and a ticker in tickers.json's active list."""
    warnings: list[str] = []
    drop = _finite(drop_pct)
    symbol = str(ticker).strip().upper() if isinstance(ticker, str) else ""
    try:
        universe = dca_config.load_tickers(Path(config_dir)).get("tickers", ())
    except (ValueError, TypeError) as exc:
        _warn_loader(warnings, config_dir, "tickers.json", exc)
        universe = ()
    if drop is None or not 0 <= drop <= 50 or symbol not in universe:
        return {"found": False, "warnings": warnings}
    config = _config_or_defaults(config_dir, datetime.now(timezone.utc), warnings)
    return {"found": True, "ticker": symbol, **_grid_row(drop, config, symbol), "warnings": warnings}


def _slot_ids(stream: str, data: dict[str, Any]) -> list[str]:
    slots = data.get("slots")
    if isinstance(slots, dict):
        return sorted(key for key in slots if isinstance(key, str) and key.isdigit())[:MAX_ROWS]
    task = data.get("live_task")
    when = task.get("schedule_et") if isinstance(task, dict) else None
    return [when[:5]] if isinstance(when, str) else []


def stream_flags(config_dir: Path) -> dict[str, Any]:
    """Per-stream live flags from raw JSON (no executor import) plus the run-classification summary.

    ``live_execution_enabled`` is None when the file does not declare it (the
    morning file selects live vs watchlist by executor mode instead).
    """
    warnings: list[str] = []
    streams = []
    for stream in STREAMS:
        data = _raw(config_dir, f"{stream}_dca", warnings)
        flag = data.get("live_execution_enabled") if data else None
        note = data.get("live_execution_enabled_note") if data else None
        if data is not None and flag is None:
            note = note or "live_execution_enabled is not declared in this file; the executor mode decides."
        streams.append({
            "stream": stream, "file": f"{stream}_dca.json", "found": data is not None,
            "live_execution_enabled": flag if isinstance(flag, bool) else None, "note": _text(note),
            "confidence_rule": _text(data.get("confidence_rule")) if data else None,
            "trend_min_bars": data.get("trend_min_bars") if data and isinstance(data.get("trend_min_bars"), int)
            and not isinstance(data.get("trend_min_bars"), bool) else None,
            "slots": _slot_ids(stream, data) if data else [],
        })
    policy = _raw(config_dir, "run_classification", warnings)
    summary = None
    if policy is not None:
        scheduled = policy.get("scheduled_run_classification")
        manual = policy.get("manual_run_classification")
        pick = lambda block, keys: {key: _json_safe(block.get(key)) for key in keys} if isinstance(block, dict) else None  # noqa: E731
        summary = {
            "policy_id": _text(policy.get("policy_id"), 80), "version": _text(policy.get("version"), 40),
            "state_schema_version": _text(policy.get("state_schema_version"), 80),
            "accepted_state_schema_versions": _json_safe(policy.get("accepted_state_schema_versions")),
            "scheduled": pick(scheduled, ("run_origin", "cron_id", "supersedes_cron_id", "scheduled_for_et", "run_id_pattern")),
            "manual": pick(manual, ("run_origin", "manual_run", "run_id_pattern", "required_fields")),
        }
    return {"streams": streams, "run_classification": summary, "warnings": warnings}


def sizing_sources(config_dir: Path, now: datetime) -> dict[str, Any]:
    """Status of the two publisher-written sizing inputs; ``stale`` is None when not computable."""
    warnings: list[str] = []
    ml_raw = _raw(config_dir, "ml_sizing_adjustment", warnings) or {}
    ml_loaded = dca_config.load_ml_sizing_adjustment(Path(config_dir), now=now).get("ml_sizing_adjustment", {})
    stale = None
    hours = _finite(ml_raw.get("max_staleness_hours"))
    generated = ml_raw.get("generated_at")
    if isinstance(generated, str) and hours is not None:
        try:
            stamp = datetime.fromisoformat(generated)
            stale = (now - stamp).total_seconds() > hours * 3600 if stamp.tzinfo else None
        except ValueError:
            stale = None
    adjustments = ml_raw.get("adjustments")
    ml = {"status": _text(ml_raw.get("status"), 40), "generated_at": _text(generated, 40),
          "model_version": _text(ml_raw.get("model_version"), 80), "bounds": _json_safe(ml_raw.get("bounds")),
          "max_staleness_hours": hours, "stale": stale,
          "adjustment_count": len(adjustments) if isinstance(adjustments, dict) else None,
          "applied_count": len(ml_loaded)}
    pt_raw = _raw(config_dir, "per_ticker_sizing", warnings) or {}
    entries, fallback = pt_raw.get("tickers"), pt_raw.get("fallback_tickers")
    try:
        loaded_count = len(dca_config.load_per_ticker_sizing(Path(config_dir)).get("per_ticker_sizing", {}))
    except (ValueError, TypeError) as exc:
        # Same TypeError shape as ladder_preview: float(None) inside the loader.
        _warn_loader(warnings, config_dir, "per_ticker_sizing.json", exc)
        loaded_count = None
    per_ticker = {"status": _text(pt_raw.get("status"), 40), "version": _text(pt_raw.get("version"), 40),
                  "entry_count": len(entries) if isinstance(entries, dict) else None,
                  "fallback_count": len(fallback) if isinstance(fallback, list) else None, "loaded_count": loaded_count}
    return {"ml": ml, "per_ticker": per_ticker, "publisher_only": True, "warnings": warnings}


def _gate(name: str, state: str, detail: str) -> dict[str, Any]:
    return {"name": name, "state": state, "detail": _text(detail)}


def live_gate_summary(config_dir: Path, day: date) -> dict[str, Any]:
    """The six independent live gates as configured/unconfigured/unknown from a checkout.

    ``configured`` means the gate currently permits live money on ``day``; a
    checkout can never observe the broker's ``enable_live_orders``, so that gate
    is always ``unknown``. The account gate reports a variable NAME only.
    """
    warnings: list[str] = []
    overrides = _raw(config_dir, "near_close_dca", warnings)
    empty = overrides == {}
    gates = [_gate("strategy_dry_run", "unconfigured",
                   f"StrategyConfig.dry_run defaults to {near_close_dca.StrategyConfig().dry_run} and no JSON can set it; "
                   f"near_close_dca.json is {'literally {}' if empty else 'not empty' if overrides is not None else 'absent'}."),
             _gate("broker_enable_live_orders", "unknown", "RobinhoodBroker.enable_live_orders is not observable from a checkout.")]
    flags = stream_flags(config_dir)
    warnings.extend(w for w in flags["warnings"] if w not in warnings)
    # Only a stream file that was actually read is evidence. Zero readable files
    # is "unknown", never "unconfigured": a banner that said "blocks live money"
    # from no observations is the None-vs-false confusion the brief forbids
    # (adversarial review, config area).
    observed = [row for row in flags["streams"] if row["found"]]
    enabled = [row["stream"] for row in observed if row["live_execution_enabled"] is True]
    if not observed:
        gates.append(_gate("stream_live_execution_enabled", "unknown", "no stream policy file could be read."))
    else:
        gates.append(_gate("stream_live_execution_enabled", "configured" if enabled else "unconfigured",
                           f"enabled for: {', '.join(enabled) or 'no stream'} "
                           f"({len(observed)} of {len(STREAMS)} stream files read)."))
    present = bool(os.environ.get("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"))
    gates.append(_gate("account_number_env", "configured" if present else "unconfigured",
                       f"ROBINHOOD_AGENTIC_ACCOUNT_NUMBER is {'present' if present else 'absent'} in this process."))
    window = window_countdown(config_dir, day)
    warnings.extend(w for w in window["warnings"] if w not in warnings)
    covered = window["in_window"]
    gates.append(_gate("approved_trading_window", "unknown" if covered is None else "configured" if covered else "unconfigured",
                       f"window {window['start']} to {window['end']}; {day.isoformat()} {'is' if covered else 'is not'} approved."))
    try:
        from dca_runner import SLOTS
        gates.append(_gate("slot_windows", "configured" if SLOTS else "unconfigured", f"{len(SLOTS)} slot windows in dca_runner.SLOTS."))
    except Exception as exc:  # noqa: BLE001 - dca_runner reads config at import; report, never raise
        warnings.append("dca_runner could not be imported; the slot roster is unavailable.")
        gates.append(_gate("slot_windows", "unknown", f"import failed: {type(exc).__name__}"))
    return {"gates": gates, "banner": BANNER, "warnings": warnings}


def window_countdown(config_dir: Path, day: date) -> dict[str, Any]:
    """Approved-window bounds and countdown for ``day`` plus market-calendar coverage.

    Every field is None when trading_window.json is absent or rejected;
    ``expiring_warning`` mirrors near_close_dca.warn_if_window_expiring's rule.
    """
    warnings: list[str] = []
    result: dict[str, Any] = {"start": None, "end": None, "days_remaining": None, "in_window": None,
                              "expired": None, "expiring_warning": None, "calendar_years": None,
                              "calendar_covers_day": None, "warnings": warnings}
    try:
        approved = dca_config.load_approved_dates(Path(config_dir)).get("approved_test_dates")
    except (ValueError, TypeError) as exc:
        _warn_loader(warnings, config_dir, "trading_window.json", exc)
        approved = None
    if approved:
        in_window, end, remaining = near_close_dca.approved_window_status(approved, day)
        expired = end is not None and day > end
        result.update({"start": min(approved).isoformat(), "end": end.isoformat() if end else None,
                       "days_remaining": remaining, "in_window": in_window, "expired": expired,
                       "expiring_warning": expired or (in_window and remaining <= near_close_dca.APPROVED_WINDOW_EXPIRY_WARN_DAYS)})
    elif approved is not None:
        warnings.append("trading_window.json approves no dates.")
    calendar = _raw(config_dir, "market_calendar", warnings)
    years = calendar.get("years_covered") if calendar else None
    if isinstance(years, list):
        result["calendar_years"] = [y for y in years if isinstance(y, int) and not isinstance(y, bool)][:MAX_ROWS]
        result["calendar_covers_day"] = day.year in result["calendar_years"]
    return result


def build_config_snapshot(config_dir: Path, repo: Path, now: datetime, day: date) -> dict[str, Any]:
    """Compose the Config page's panels (no file views, no ladder grid) with merged warnings."""
    parts = {"catalog": config_catalog(config_dir, repo), "parity": parity_checks(config_dir),
             "streams": stream_flags(config_dir), "sizing": sizing_sources(config_dir, now),
             "gates": live_gate_summary(config_dir, day), "countdown": window_countdown(config_dir, day)}
    warnings: list[str] = []
    for part in parts.values():
        warnings.extend(w for w in part["warnings"] if w not in warnings)
    return {**parts, "warnings": warnings}
