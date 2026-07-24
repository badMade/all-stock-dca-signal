"""Tests for incident correlation IDs."""

from __future__ import annotations

from scripts import correlation


def test_build_is_deterministic_across_processes():
    a = correlation.build_correlation_id("2026-07-23", "midday", "1230ET")
    b = correlation.build_correlation_id("2026-07-23", "midday", "1230ET")
    assert a == b == "dca-2026-07-23-midday-1230et"


def test_build_normalizes_unsafe_characters():
    cid = correlation.build_correlation_id("2026/07/23", "mid day", "12:30 ET")
    assert cid == "dca-2026-07-23-mid-day-12-30-et"


def test_child_links_to_parent_but_is_distinct():
    parent = correlation.build_correlation_id("2026-07-23", "midday", "1230ET")
    child = correlation.child_correlation_id(parent, "recovery")
    assert child == f"{parent}/recovery"
    assert child != parent
    assert correlation.is_child(child)
    assert not correlation.is_child(parent)
    assert correlation.parent_of(child) == parent


def test_parent_of_non_child_is_none():
    parent = correlation.build_correlation_id("2026-07-23", "midday", "1230ET")
    assert correlation.parent_of(parent) is None


def test_new_correlation_id_is_unique_and_prefixed():
    a = correlation.new_correlation_id("adhoc")
    b = correlation.new_correlation_id("adhoc")
    assert a != b
    assert a.startswith("dca-adhoc-")


def test_child_requires_parent():
    import pytest

    with pytest.raises(ValueError):
        correlation.child_correlation_id("", "recovery")
