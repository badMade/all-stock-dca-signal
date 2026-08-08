"""Tests for richer order idempotency keys."""

from __future__ import annotations

import pytest

from scripts.idempotency import (
    build_idempotency_key,
    deterministic_ref_id,
    parse_idempotency_key,
)


def _key(**kw):
    base = dict(
        trade_date="2026-07-23",
        window="1230ET",
        symbol="txn",
        strategy_version="v2-preauthorized",
        tier="elevated",
    )
    base.update(kw)
    return build_idempotency_key(**base)


def test_key_includes_all_five_dimensions_plus_kind():
    key = _key()
    assert key == "graded-dca:buy:first:2026-07-23:1230et:TXN:v2-preauthorized:elevated"


def test_key_is_deterministic():
    assert _key() == _key()


def test_window_distinguishes_morning_from_afternoon():
    assert _key(window="1030ET") != _key(window="1520ET")


def test_kind_distinguishes_first_from_topup():
    assert _key(kind="first") != _key(kind="topup")


def test_strategy_version_and_tier_are_part_of_the_key():
    assert _key(strategy_version="v1") != _key(strategy_version="v2")
    assert _key(tier="normal") != _key(tier="stress")


def test_symbol_is_uppercased_for_stability():
    assert _key(symbol="txn") == _key(symbol="TXN")


def test_deterministic_ref_id_is_uuid_and_stable():
    ref = deterministic_ref_id(_key())
    # Same key -> same ref id.
    assert ref == deterministic_ref_id(_key())
    # Different key -> different ref id.
    assert ref != deterministic_ref_id(_key(window="1520ET"))
    # UUID-shaped.
    assert len(ref) == 36 and ref.count("-") == 4


def test_round_trip_parse():
    key = _key()
    parsed = parse_idempotency_key(key)
    assert parsed == {
        "kind": "first",
        "trade_date": "2026-07-23",
        "window": "1230et",
        "symbol": "TXN",
        "strategy_version": "v2-preauthorized",
        "tier": "elevated",
    }


def test_parse_rejects_foreign_key():
    assert parse_idempotency_key("some-other-system:xyz") is None


def test_empty_field_is_rejected():
    with pytest.raises(ValueError):
        _key(symbol="   ")


def test_lossless_encoding_distinguishes_special_chars():
    a = _key(strategy_version="v2/a")
    b = _key(strategy_version="v2-a")
    assert a != b
    assert deterministic_ref_id(a) != deterministic_ref_id(b)


def test_round_trip_parse_preserves_special_chars():
    key = _key(strategy_version="v2/a.b")
    assert parse_idempotency_key(key)["strategy_version"] == "v2/a.b"


def test_parse_rejects_non_string_input():
    assert parse_idempotency_key(None) is None
    assert parse_idempotency_key(123) is None

def test_deterministic_ref_id_rejects_empty_key():
    with pytest.raises(ValueError, match="key is required"):
        deterministic_ref_id("")

def test_build_idempotency_key_basic_example():
    """Verify the exact example from the docstring."""
    result = build_idempotency_key(
        trade_date="2026-07-23",
        window="1230et",
        symbol="TXN",
        strategy_version="v2-preauthorized",
        tier="elevated",
        kind="first"
    )
    assert result == "graded-dca:buy:first:2026-07-23:1230et:TXN:v2-preauthorized:elevated"

def test_build_idempotency_key_rejects_empty_fields():
    with pytest.raises(ValueError, match="idempotency key fields must be non-empty"):
        build_idempotency_key(
            trade_date="",
            window="1230et",
            symbol="txn",
            strategy_version="v2",
            tier="elevated"
        )
