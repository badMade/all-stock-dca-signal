"""Strict, fail-closed loading for the bounded self-heal policy.

The version-controlled JSON uses smaller units for values whose canonical
seconds or byte counts resemble account identifiers. The public dataclass
converts those values back to the seconds-and-bytes names consumed by later
self-heal rocks, so the safety contract remains explicit without weakening the
repository's standing secret-literal guard.

Standard library only. No repair, observation, network, or subprocess logic.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

_SCHEMA_REFERENCE = "../schemas/self_heal.schema.json"

# A checkout keeps config/ beside the package. Rock 7 will install the same file
# under sys.prefix/share; checking both now means the strict loader does not need
# a later fallback that could accidentally manufacture defaults when packaging
# is incomplete.
_DEFAULT_CONFIG_PATHS = (
    Path(__file__).resolve().parent.parent / "config" / "self_heal.json",
    Path(sys.prefix) / "share" / "all-stock-dca" / "config" / "self_heal.json",
)

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 60 * _SECONDS_PER_MINUTE
_BYTES_PER_KIB = 1_024
_BYTES_PER_64_KIB_BLOCK = 64 * _BYTES_PER_KIB


class ConfigError(ValueError):
    """Raised when self-heal configuration cannot be trusted.

    Args:
        message: Human-readable refusal detail naming the offending key, path,
            or document-level condition.
    """


@dataclass(frozen=True)
class SelfHealConfig:
    """Validated self-heal safety thresholds in canonical runtime units.

    Attributes:
        enabled: Whether the repair half is operator-enabled. Observation is
            independently gated and remains outside this switch.
        max_repairs_per_day: Maximum repair admissions in one UTC day.
        max_repairs_per_epoch: Human-rearmed lifetime repair ceiling.
        breaker_failure_threshold: Failures in an epoch that latch the breaker.
        max_attempts_per_fingerprint: Attempts before a fingerprint quarantines.
        max_tick_seconds: Outer supervisor deadline for a complete tick.
        max_wall_clock_seconds: Local elapsed-time allowance for one repair.
        monthly_actions_minutes: Actions-minute allowance per budget period.
        ci_worst_case_minutes: Worst-case CI reservation for one repair.
        max_forward_skew_seconds: Largest accepted single forward clock jump.
        max_cumulative_skew_seconds: Cumulative accepted skew within an epoch.
        max_archive_bytes_per_incident: As-captured private incident byte cap.
        max_archive_bytes_per_run: Aggregate private archive byte cap per run.
        budget_period_seconds: Duration of the monotonic budget reset period.
        review_lease_days: Maximum time a repair may remain awaiting review.
        execution_lease_seconds: Maximum dispatch lease before abandonment.
    """

    enabled: bool
    max_repairs_per_day: int
    max_repairs_per_epoch: int
    breaker_failure_threshold: int
    max_attempts_per_fingerprint: int
    max_tick_seconds: int
    max_wall_clock_seconds: int
    monthly_actions_minutes: int
    ci_worst_case_minutes: int
    max_forward_skew_seconds: int
    max_cumulative_skew_seconds: int
    max_archive_bytes_per_incident: int
    max_archive_bytes_per_run: int
    budget_period_seconds: int
    review_lease_days: int
    execution_lease_seconds: int


@dataclass(frozen=True)
class _IntegerSetting:
    """One required JSON integer and its canonical-unit conversion."""

    canonical_name: str
    minimum: int
    maximum: int
    multiplier: int = 1


@dataclass(frozen=True)
class _OversizedInteger:
    """A valid JSON integer that exceeded Python's conversion safety limit."""

    digit_count: int


def _parse_json_integer(raw: str) -> int | _OversizedInteger:
    """Parse a JSON integer without leaking Python's digit-limit ValueError."""
    try:
        return int(raw)
    except ValueError:
        # Keeping only the length avoids retaining or echoing an attacker-sized
        # token. The field validator can still name the exact offending key.
        return _OversizedInteger(digit_count=len(raw.lstrip("-")))


# Every integer is bounded here even though the shipped document also carries a
# JSON Schema. Callers may supply an alternate path and are not required to run
# a schema library, so runtime validation is the safety boundary, not CI alone.
#
# Large values use smaller integer units. The 64-KiB block for the per-run cap is
# intentional: KiB alone would make its upper bound look account-shaped, while
# MiB would make the required 64-KiB lower bound fractional and weaken the
# integer-only policy.
_INTEGER_SETTINGS: dict[str, _IntegerSetting] = {
    "max_repairs_per_day": _IntegerSetting("max_repairs_per_day", 1, 24),
    "max_repairs_per_epoch": _IntegerSetting("max_repairs_per_epoch", 1, 50),
    "breaker_failure_threshold": _IntegerSetting("breaker_failure_threshold", 1, 20),
    "max_attempts_per_fingerprint": _IntegerSetting("max_attempts_per_fingerprint", 1, 10),
    "max_tick_seconds": _IntegerSetting("max_tick_seconds", 30, 3_600),
    "max_wall_clock_seconds": _IntegerSetting("max_wall_clock_seconds", 60, 7_200),
    "monthly_actions_minutes": _IntegerSetting("monthly_actions_minutes", 10, 2_000),
    "ci_worst_case_minutes": _IntegerSetting("ci_worst_case_minutes", 1, 600),
    "max_forward_skew_minutes": _IntegerSetting(
        "max_forward_skew_seconds", 1, 10_080, _SECONDS_PER_MINUTE
    ),
    "max_cumulative_skew_minutes": _IntegerSetting(
        "max_cumulative_skew_seconds", 1, 43_200, _SECONDS_PER_MINUTE
    ),
    "max_archive_kib_per_incident": _IntegerSetting(
        "max_archive_bytes_per_incident", 4, 65_536, _BYTES_PER_KIB
    ),
    "max_archive_64kib_blocks_per_run": _IntegerSetting(
        "max_archive_bytes_per_run", 1, 8_192, _BYTES_PER_64_KIB_BLOCK
    ),
    "budget_period_hours": _IntegerSetting("budget_period_seconds", 24, 2_160, _SECONDS_PER_HOUR),
    "review_lease_days": _IntegerSetting("review_lease_days", 1, 90),
    "execution_lease_seconds": _IntegerSetting("execution_lease_seconds", 60, 21_600),
}

_REQUIRED_KEYS = frozenset({"$schema", "enabled", *_INTEGER_SETTINGS})


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while refusing duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        # json.loads otherwise keeps the final value. A duplicated safety limit
        # must never let file order decide which ceiling the coordinator uses.
        if key in result:
            raise ConfigError(f"{key}: duplicate JSON key; refusing an ambiguous safety limit")
        result[key] = value
    return result


def _default_config_path() -> Path:
    """Return the checkout or installed config path, failing if neither exists."""
    for candidate in _DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(candidate) for candidate in _DEFAULT_CONFIG_PATHS)
    raise ConfigError(f"$config: packaged configuration file is missing; checked {checked}")


def _read_document(path: Path) -> object:
    """Read and decode one JSON document without accepting duplicate keys."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"$config: configuration file {path} is missing") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"$config: configuration file {path} is unreadable: {exc}") from exc

    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_int=_parse_json_integer,
        )
    except ConfigError:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ConfigError(f"$document: configuration file {path} is not valid JSON: {exc}") from exc


def _validated_integer(key: str, value: object, setting: _IntegerSetting) -> int:
    """Validate one raw integer setting and return its canonical-unit value."""
    if isinstance(value, _OversizedInteger):
        raise ConfigError(
            f"{key}: integer literal has {value.digit_count} digits and exceeds the parser safety limit"
        )
    # Python's JSON decoder accepts NaN and infinities even though ordinary JSON
    # does not. Name that failure before the generic integer-type check so the
    # operator learns that the value is non-finite, not merely that it is a float.
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{key}: value must be finite, got {value!r}")
    # bool subclasses int, so this guard must precede isinstance(value, int).
    # Otherwise true could silently become a one-unit safety ceiling.
    if isinstance(value, bool):
        raise ConfigError(f"{key}: value must be an integer, not a boolean")
    if not isinstance(value, int):
        raise ConfigError(f"{key}: value must be an integer, got {type(value).__name__}")
    if value < setting.minimum:
        raise ConfigError(f"{key}: value {value} is below minimum {setting.minimum}")
    if value > setting.maximum:
        raise ConfigError(f"{key}: value {value} is above maximum {setting.maximum}")
    return value * setting.multiplier


def load(path: str | Path | None = None) -> SelfHealConfig:
    """Load and strictly validate the self-heal configuration.

    Args:
        path: Optional JSON path. When omitted, the version-controlled checkout
            path is tried first, followed by Rock 7's installed data-file path.

    Returns:
        A frozen :class:`SelfHealConfig` whose time and archive fields use
        canonical seconds and bytes.

    Raises:
        ConfigError: If the file is missing or unreadable, JSON is malformed or
            ambiguous, a key is missing or unknown, a value has the wrong type
            or lies outside its bound, or cumulative skew is not shorter than
            the budget reset period.
    """
    config_path = Path(path) if path is not None else _default_config_path()
    document = _read_document(config_path)
    if not isinstance(document, dict):
        raise ConfigError(
            f"$document: configuration must be a JSON object, got {type(document).__name__}"
        )

    unknown = sorted(set(document) - _REQUIRED_KEYS)
    if unknown:
        raise ConfigError(f"{unknown[0]}: unknown configuration key; allowed keys are fixed")
    missing = sorted(_REQUIRED_KEYS - set(document))
    if missing:
        raise ConfigError(f"{missing[0]}: required configuration key is missing")

    if document["$schema"] != _SCHEMA_REFERENCE:
        raise ConfigError(f"$schema: value must be exactly {_SCHEMA_REFERENCE!r}")

    enabled = document["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigError(f"enabled: value must be a boolean, got {type(enabled).__name__}")

    canonical: dict[str, int] = {}
    for key, setting in _INTEGER_SETTINGS.items():
        canonical[setting.canonical_name] = _validated_integer(key, document[key], setting)

    # A cumulative clock allowance at least as long as a reset period can walk
    # the durable anchor into a newly replenished budget. Strict inequality is
    # therefore a cross-field safety invariant, not a presentation preference.
    cumulative_skew = canonical["max_cumulative_skew_seconds"]
    budget_period = canonical["budget_period_seconds"]
    if cumulative_skew >= budget_period:
        raise ConfigError(
            "max_cumulative_skew_minutes: converted max_cumulative_skew_seconds "
            "must be strictly less than budget_period_hours converted to budget_period_seconds"
        )

    return SelfHealConfig(
        enabled=enabled,
        max_repairs_per_day=canonical["max_repairs_per_day"],
        max_repairs_per_epoch=canonical["max_repairs_per_epoch"],
        breaker_failure_threshold=canonical["breaker_failure_threshold"],
        max_attempts_per_fingerprint=canonical["max_attempts_per_fingerprint"],
        max_tick_seconds=canonical["max_tick_seconds"],
        max_wall_clock_seconds=canonical["max_wall_clock_seconds"],
        monthly_actions_minutes=canonical["monthly_actions_minutes"],
        ci_worst_case_minutes=canonical["ci_worst_case_minutes"],
        max_forward_skew_seconds=canonical["max_forward_skew_seconds"],
        max_cumulative_skew_seconds=canonical["max_cumulative_skew_seconds"],
        max_archive_bytes_per_incident=canonical["max_archive_bytes_per_incident"],
        max_archive_bytes_per_run=canonical["max_archive_bytes_per_run"],
        budget_period_seconds=canonical["budget_period_seconds"],
        review_lease_days=canonical["review_lease_days"],
        execution_lease_seconds=canonical["execution_lease_seconds"],
    )


__all__ = ["ConfigError", "SelfHealConfig", "load"]
