"""Strict loader for ``dca-sizing-signals/config/prediction_markets.json``.

Fails closed like the core's ``dca_config``: duplicate keys, NaN, a non-object top level, an
unknown key, an unknown ``license_scope`` or an out-of-range number is a ``ConfigError``.
Credentials never live here (D12): a source may name the *environment variables* that hold
its keys (``credential_env``), and any other key -- ``api_key``, ``secret_key``, ``token`` ... --
is rejected by name. Error messages carry the key's name, never its value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# dca-sizing-signals/config/prediction_markets.json. Resolved from this file, so it holds in a
# checkout; an installed wheel does not ship config/, and load_config then fails closed.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "prediction_markets.json"

LICENSE_SCOPES = ("store_ok", "live_only", "manual_only")
# The CLOB rejects 21 token ids with HTTP 400 (fixture intl_clob_batch_prices_history_21_tokens_rejected.json).
VENUE_MAX_HISTORY_BATCH = 20
_SOURCE_KEYS = {"license_scope", "hosts", "budgets_per_10s", "max_history_batch", "credential_env"}
_TOP_KEYS = {"liquidity_threshold_usd", "cadence", "watchlist", "db_path_env", "sources", "_comment"}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SourceConfig:
    venue: str
    license_scope: str
    hosts: dict[str, str]
    budgets_per_10s: dict[str, dict[str, int]]
    max_history_batch: int = VENUE_MAX_HISTORY_BATCH
    credential_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CadenceConfig:
    default_interval_s: int
    watchlist_interval_s: int


@dataclass(frozen=True)
class PMConfig:
    liquidity_threshold_usd: float
    cadence: CadenceConfig
    watchlist: frozenset[tuple[str, str]]
    db_path_env: str | None
    sources: dict[str, SourceConfig]

    def scope_for(self, venue: str) -> str | None:
        source = self.sources.get(venue)
        return source.license_scope if source else None


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ConfigError(f"non-standard JSON constant {name}")


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a positive integer")
    return value


def _source(venue: str, raw: Any) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"sources.{venue} must be an object")
    unknown = sorted(set(raw) - _SOURCE_KEYS)
    if unknown:
        # Names only: the value of an unexpected key may be a pasted secret.
        raise ConfigError(f"sources.{venue} has unsupported keys {unknown}; credentials belong in the environment")
    scope = raw.get("license_scope")
    if scope not in LICENSE_SCOPES:
        raise ConfigError(f"sources.{venue}.license_scope must be one of {LICENSE_SCOPES}")
    hosts = raw.get("hosts")
    if not isinstance(hosts, dict) or not hosts or not all(
        isinstance(v, str) and v.startswith("https://") for v in hosts.values()
    ):
        raise ConfigError(f"sources.{venue}.hosts must map names to https:// base URLs")
    budgets_raw = raw.get("budgets_per_10s", {})
    if not isinstance(budgets_raw, dict):
        raise ConfigError(f"sources.{venue}.budgets_per_10s must be an object")
    budgets: dict[str, dict[str, int]] = {}
    for host, per_endpoint in budgets_raw.items():
        if host not in hosts or not isinstance(per_endpoint, dict):
            raise ConfigError(f"sources.{venue}.budgets_per_10s.{host} must name a configured host")
        budgets[host] = {
            ep: _positive_int(n, f"sources.{venue}.budgets_per_10s.{host}.{ep}") for ep, n in per_endpoint.items()
        }
    batch = _positive_int(raw.get("max_history_batch", VENUE_MAX_HISTORY_BATCH), f"sources.{venue}.max_history_batch")
    if batch > VENUE_MAX_HISTORY_BATCH:
        raise ConfigError(f"sources.{venue}.max_history_batch exceeds the venue cap of {VENUE_MAX_HISTORY_BATCH}")
    cred_env = raw.get("credential_env", {})
    if not isinstance(cred_env, dict) or not all(
        isinstance(v, str) and _ENV_NAME.match(v) for v in cred_env.values()
    ):
        # A value that is not an env-var NAME is most likely the key itself pasted in.
        raise ConfigError(f"sources.{venue}.credential_env must map to environment variable names")
    return SourceConfig(venue, scope, dict(hosts), budgets, batch, dict(cred_env))


def parse_config(raw: Any) -> PMConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ConfigError(f"unsupported top-level keys {unknown}")
    threshold = raw.get("liquidity_threshold_usd")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
        raise ConfigError("liquidity_threshold_usd must be a non-negative number")
    cad = raw.get("cadence")
    if not isinstance(cad, dict):
        raise ConfigError("cadence must be an object")
    default_s = _positive_int(cad.get("default_interval_s"), "cadence.default_interval_s")
    watch_s = _positive_int(cad.get("watchlist_interval_s"), "cadence.watchlist_interval_s")
    if watch_s > default_s:
        raise ConfigError("cadence.watchlist_interval_s must not be slower than default_interval_s")
    watch_raw = raw.get("watchlist", [])
    if not isinstance(watch_raw, list):
        raise ConfigError("watchlist must be a list")
    watchlist = set()
    for entry in watch_raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("venue"), str) or not isinstance(
            entry.get("market_id"), str
        ):
            raise ConfigError("watchlist entries need string venue and market_id")
        watchlist.add((entry["venue"], entry["market_id"]))
    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, dict) or not sources_raw:
        raise ConfigError("sources must be a non-empty object")
    db_env = raw.get("db_path_env")
    if db_env is not None and not (isinstance(db_env, str) and _ENV_NAME.match(db_env)):
        raise ConfigError("db_path_env must be an environment variable name")
    return PMConfig(
        liquidity_threshold_usd=float(threshold),
        cadence=CadenceConfig(default_s, watch_s),
        watchlist=frozenset(watchlist),
        db_path_env=db_env,
        sources={venue: _source(venue, body) for venue, body in sources_raw.items()},
    )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> PMConfig:
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read prediction-market config {path}") from exc
    try:
        raw = json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON") from exc
    return parse_config(raw)
