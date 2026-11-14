"""Strict validation tests for :mod:`selfheal.config`."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import selfheal.config as config_module
from selfheal.config import ConfigError, load

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_CONFIG = REPO_ROOT / "config" / "self_heal.json"


def _valid_document() -> dict[str, object]:
    """Return an independent mutable copy of the shipped JSON object."""
    document = json.loads(SHIPPED_CONFIG.read_bytes())
    assert isinstance(document, dict)
    return document


def _write_document(tmp_path: Path, document: object) -> Path:
    """Write one JSON value and return its temporary path."""
    path = tmp_path / "self_heal.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_text(tmp_path: Path, text: str) -> Path:
    """Write exact JSON source and return its temporary path."""
    path = tmp_path / "self_heal.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_config_loads_with_required_defaults_and_canonical_units() -> None:
    loaded = load()

    assert loaded.enabled is True
    assert loaded.max_repairs_per_day == 1
    assert loaded.max_repairs_per_epoch == 10
    assert loaded.breaker_failure_threshold == 3
    assert loaded.max_attempts_per_fingerprint == 3
    assert loaded.max_tick_seconds == 600
    assert loaded.max_wall_clock_seconds == 1_800
    assert loaded.monthly_actions_minutes == 300
    assert loaded.ci_worst_case_minutes == 160
    assert loaded.max_forward_skew_seconds == 172_800
    assert loaded.max_cumulative_skew_seconds == 604_800
    assert loaded.max_archive_bytes_per_incident == 1_048_576
    assert loaded.max_archive_bytes_per_run == 67_108_864
    assert loaded.budget_period_seconds == 2_592_000
    assert loaded.review_lease_days == 14
    assert loaded.execution_lease_seconds == 3_600


def test_config_object_is_frozen_after_validation() -> None:
    loaded = load(SHIPPED_CONFIG)

    with pytest.raises(FrozenInstanceError):
        loaded.max_repairs_per_day = 2  # type: ignore[misc]


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    source = SHIPPED_CONFIG.read_text(encoding="utf-8")
    duplicate = source.replace(
        '  "max_repairs_per_day": 1,',
        '  "max_repairs_per_day": 1,\n  "max_repairs_per_day": 2,',
        1,
    )

    with pytest.raises(ConfigError, match=r"max_repairs_per_day: duplicate JSON key"):
        load(_write_text(tmp_path, duplicate))


def test_valid_non_object_json_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\$document: .*JSON object.*list"):
        load(_write_document(tmp_path, []))


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    document = _valid_document()
    document["unexpected_limit"] = 1

    with pytest.raises(ConfigError, match=r"unexpected_limit: unknown configuration key"):
        load(_write_document(tmp_path, document))


def test_missing_required_keys_are_rejected(tmp_path: Path) -> None:
    document = _valid_document()
    del document["max_tick_seconds"]

    with pytest.raises(ConfigError, match=r"max_tick_seconds: required configuration key is missing"):
        load(_write_document(tmp_path, document))


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing-self-heal.json"

    with pytest.raises(ConfigError, match=r"\$config: .*missing"):
        load(missing)


def test_missing_default_packaged_config_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing-packaged-self-heal.json"
    monkeypatch.setattr(config_module, "_DEFAULT_CONFIG_PATHS", (missing,))

    with pytest.raises(ConfigError, match=r"\$config: packaged configuration file is missing"):
        load()


def test_boolean_is_rejected_where_an_integer_is_expected(tmp_path: Path) -> None:
    document = _valid_document()
    document["max_tick_seconds"] = True

    with pytest.raises(ConfigError, match=r"max_tick_seconds: .*integer, not a boolean"):
        load(_write_document(tmp_path, document))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(tmp_path: Path, value: float) -> None:
    document = _valid_document()
    document["max_tick_seconds"] = value

    with pytest.raises(ConfigError, match=r"max_tick_seconds: value must be finite"):
        load(_write_document(tmp_path, document))


def test_negative_values_are_rejected(tmp_path: Path) -> None:
    document = _valid_document()
    document["monthly_actions_minutes"] = -1

    with pytest.raises(ConfigError, match=r"monthly_actions_minutes: .*below minimum 10"):
        load(_write_document(tmp_path, document))


def test_zero_is_rejected_when_minimum_is_at_least_one(tmp_path: Path) -> None:
    document = _valid_document()
    document["max_repairs_per_day"] = 0

    with pytest.raises(ConfigError, match=r"max_repairs_per_day: .*below minimum 1"):
        load(_write_document(tmp_path, document))


@pytest.mark.parametrize(
    ("key", "value", "failure"),
    [
        ("max_repairs_per_day", 25, "above maximum 24"),
        ("max_repairs_per_epoch", 51, "above maximum 50"),
        ("breaker_failure_threshold", 21, "above maximum 20"),
        ("max_attempts_per_fingerprint", 11, "above maximum 10"),
        ("max_tick_seconds", 3_601, "above maximum 3600"),
        ("max_wall_clock_seconds", 7_201, "above maximum 7200"),
        ("monthly_actions_minutes", 2_001, "above maximum 2000"),
        ("ci_worst_case_minutes", 601, "above maximum 600"),
        ("max_forward_skew_minutes", 10_081, "above maximum 10080"),
        ("max_cumulative_skew_minutes", 43_201, "above maximum 43200"),
        ("max_archive_kib_per_incident", 65_537, "above maximum 65536"),
        ("max_archive_64kib_blocks_per_run", 8_193, "above maximum 8192"),
        ("budget_period_hours", 2_161, "above maximum 2160"),
        ("review_lease_days", 91, "above maximum 90"),
        ("execution_lease_seconds", 21_601, "above maximum 21600"),
    ],
)
def test_values_above_every_declared_maximum_are_rejected(tmp_path: Path, key: str, value: int, failure: str) -> None:
    document = _valid_document()
    document[key] = value

    with pytest.raises(ConfigError, match=rf"{key}: .*{failure}"):
        load(_write_document(tmp_path, document))


@pytest.mark.parametrize(
    ("key", "value", "failure"),
    [
        ("max_repairs_per_day", 0, "below minimum 1"),
        ("max_repairs_per_epoch", 0, "below minimum 1"),
        ("breaker_failure_threshold", 0, "below minimum 1"),
        ("max_attempts_per_fingerprint", 0, "below minimum 1"),
        ("max_tick_seconds", 29, "below minimum 30"),
        ("max_wall_clock_seconds", 59, "below minimum 60"),
        ("monthly_actions_minutes", 9, "below minimum 10"),
        ("ci_worst_case_minutes", 0, "below minimum 1"),
        ("max_forward_skew_minutes", 0, "below minimum 1"),
        ("max_cumulative_skew_minutes", 0, "below minimum 1"),
        ("max_archive_kib_per_incident", 3, "below minimum 4"),
        ("max_archive_64kib_blocks_per_run", 0, "below minimum 1"),
        ("budget_period_hours", 23, "below minimum 24"),
        ("review_lease_days", 0, "below minimum 1"),
        ("execution_lease_seconds", 59, "below minimum 60"),
    ],
)
def test_values_below_every_declared_minimum_are_rejected(tmp_path: Path, key: str, value: int, failure: str) -> None:
    document = _valid_document()
    document[key] = value

    with pytest.raises(ConfigError, match=rf"{key}: .*{failure}"):
        load(_write_document(tmp_path, document))


def test_cross_field_cumulative_skew_must_be_shorter_than_budget_period(tmp_path: Path) -> None:
    document = _valid_document()
    document["max_cumulative_skew_minutes"] = 1_440
    document["budget_period_hours"] = 24

    with pytest.raises(
        ConfigError,
        match=r"max_cumulative_skew_minutes: .*strictly less than .*budget_period_hours",
    ):
        load(_write_document(tmp_path, document))


def test_enabled_requires_a_json_boolean(tmp_path: Path) -> None:
    document = _valid_document()
    document["enabled"] = "false"

    with pytest.raises(ConfigError, match=r"enabled: value must be a boolean"):
        load(_write_document(tmp_path, document))


def test_fractional_integer_setting_is_rejected(tmp_path: Path) -> None:
    document = _valid_document()
    document["max_tick_seconds"] = 600.5

    with pytest.raises(ConfigError, match=r"max_tick_seconds: value must be an integer"):
        load(_write_document(tmp_path, document))


def test_schema_pointer_must_name_the_shipped_schema(tmp_path: Path) -> None:
    document = _valid_document()
    document["$schema"] = "wrong.schema.json"

    with pytest.raises(ConfigError, match=r"\$schema: value must be exactly"):
        load(_write_document(tmp_path, document))


def test_malformed_json_is_rejected_as_a_document_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\$document: .*not valid JSON"):
        load(_write_text(tmp_path, "{"))


def test_json_decoder_value_errors_are_normalized_to_config_error(tmp_path: Path) -> None:
    source = SHIPPED_CONFIG.read_text(encoding="utf-8")
    oversized_integer = source.replace(
        '"max_tick_seconds": 600',
        f'"max_tick_seconds": {"1" * 5_000}',
        1,
    )

    with pytest.raises(ConfigError, match=r"max_tick_seconds: .*exceeds the parser safety limit"):
        load(_write_text(tmp_path, oversized_integer))
