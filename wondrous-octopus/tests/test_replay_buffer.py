"""Tests for replay_buffer.py: reservoir behavior and state roundtrip."""

from __future__ import annotations

from collections import Counter

from replay_buffer import ReservoirBuffer


def test_reservoir_holds_capacity_and_counts_everything():
    buffer = ReservoirBuffer(capacity=10, seed=0)
    for index in range(100):
        buffer.add("AAA", f"2020-{index:03d}")
    assert len(buffer) == 10
    assert buffer.n_seen == 100


def test_reservoir_keeps_old_history_represented():
    # Uniform-over-history is the whole point: early items must survive at
    # roughly their share, not be flushed by recency.
    buffer = ReservoirBuffer(capacity=200, seed=1)
    for index in range(2000):
        era = "early" if index < 1000 else "late"
        buffer.add(era, str(index))
    eras = Counter(ticker for ticker, _date in (tuple(item) for item in buffer.items))
    assert 0.35 < eras["early"] / len(buffer) < 0.65


def test_sample_is_within_buffer_and_respects_count():
    buffer = ReservoirBuffer(capacity=50, seed=2)
    for index in range(30):
        buffer.add("AAA", str(index))
    sample = buffer.sample(10)
    assert len(sample) == 10
    assert all(ticker == "AAA" for ticker, _date in sample)
    assert len(buffer.sample(100)) == 30  # capped at what exists


def test_state_roundtrip_preserves_future_randomness():
    original = ReservoirBuffer(capacity=5, seed=3)
    for index in range(20):
        original.add("AAA", str(index))
    restored = ReservoirBuffer.from_state(original.to_state())
    # Same items, same n_seen, and the SAME future evictions/samples.
    for index in range(20, 40):
        original.add("BBB", str(index))
        restored.add("BBB", str(index))
    assert original.items == restored.items
    assert original.sample(3) == restored.sample(3)
