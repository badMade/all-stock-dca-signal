"""Tests for self-healing reason codes."""

from __future__ import annotations

import pytest

from selfheal import reasons


def test_admitted_is_valid_but_not_refusal() -> None:
    """Ensure ADMITTED is a valid reason but not a refusal."""
    assert reasons.is_valid_reason(reasons.ADMITTED)
    assert not reasons.is_refusal(reasons.ADMITTED)

    assert reasons.ADMITTED in reasons.ALL_REASONS
    assert reasons.ADMITTED not in reasons.REFUSAL_REASONS


def test_all_reasons_contain_known_constants() -> None:
    """Ensure the ALL_REASONS set includes the explicitly defined constants."""
    expected = {
        reasons.DISABLED,
        reasons.ALREADY_RUNNING,
        reasons.LIFETIME_BOUND_REACHED,
        reasons.BREAKER_LATCHED,
        reasons.RATE_LIMITED_DAILY,
        reasons.CLOCK_IMPLAUSIBLE,
        reasons.BUDGET_EXHAUSTED_MINUTES,
        reasons.FINGERPRINT_QUARANTINED,
        reasons.REPORT_ONLY_NO_ADAPTER,
        reasons.WALL_CLOCK_EXCEEDED,
        reasons.NO_NOVEL_INCIDENTS,
        reasons.FINGERPRINT_KEY_UNAVAILABLE,
        reasons.SELF_ORIGIN_DROPPED,
        reasons.PROVENANCE_PENDING,
        reasons.NOT_MAIN_REF,
        reasons.CONFIG_INVALID,
        reasons.STATE_UNREADABLE,
        reasons.ADMITTED,
    }
    assert reasons.ALL_REASONS == expected


def test_refusal_reasons_exclude_admitted() -> None:
    """Ensure REFUSAL_REASONS contains everything except ADMITTED."""
    assert reasons.REFUSAL_REASONS == reasons.ALL_REASONS - {reasons.ADMITTED}


@pytest.mark.parametrize("code", sorted(list(reasons.ALL_REASONS)))
def test_is_valid_reason_returns_true_for_all_known(code: str) -> None:
    """Every recognized reason code is valid."""
    assert reasons.is_valid_reason(code)


@pytest.mark.parametrize("code", sorted(list(reasons.REFUSAL_REASONS)))
def test_is_refusal_returns_true_for_all_refusals(code: str) -> None:
    """Every documented refusal code is recognized as a refusal."""
    assert reasons.is_refusal(code)


def test_is_valid_reason_false_for_unknown() -> None:
    """Unknown strings are not valid reasons."""
    assert not reasons.is_valid_reason("UNKNOWN_REASON")
    assert not reasons.is_valid_reason("")


def test_is_refusal_false_for_unknown() -> None:
    """Unknown strings are not refusals."""
    assert not reasons.is_refusal("UNKNOWN_REASON")
    assert not reasons.is_refusal("")
