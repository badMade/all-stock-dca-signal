"""Goal versions are bitemporal: what a goal said, and what we knew it said, are separate axes.

Written FIRST (docs/adr-goals-and-loss-hedge-shadow.md, Phase 2, ADR test 18). Fails until
``LedgerStore`` gains a ``goal_versions`` table with ``ingest_goal_versions``, ``goal_versions``
and ``goal_version_on``. All rows here are SYNTHETIC round numbers; no real goal is used.

The two axes:
* ``as_of``: when the goal version was asserted to be true (the effective timeline).
* ``known_at`` (via ``ingested_at``): when the ledger learned it. A correction ingested later must
  not change any read that asks "what did we know at T1?".

Synthetic history:
  v1 (goal growth-test, version 1): target 0.20, effective_from 2026-12-01, ingested at T1.
  v1 corrected (same goal_version 1 re-ingested as a correction): target 0.25, ingested at T2 > T1.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from market_data_ingest.ledger.store import LedgerStore

T0 = datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)  # v1 first learned
T2 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)  # correction learned later
V1 = {"goal_id": "growth-test", "goal_version": 1, "metric": "twr_cagr", "horizon_years": 5,
      "target": 0.20, "effective_from": datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)}
V1_CORRECTED = {**V1, "target": 0.25}


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store(tmp_path):
    clock = _Clock(T1)
    # The store keeps this same _Clock object, so moving ``clock.now`` moves the ledger's time.
    with LedgerStore(tmp_path / "ledger.duckdb", clock=clock) as s:
        s.clock = clock
        yield s


def test_before_the_correction_the_original_target_is_what_we_knew(store):
    store.clock.now = T1
    store.ingest_goal_versions([V1], source="config_goals_json", run_id="goals-1", as_of=T1)
    store.clock.now = T2
    store.ingest_goal_versions([V1_CORRECTED], source="config_goals_json", run_id="goals-2", as_of=T1)

    known_then = store.goal_versions(known_at=datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc))
    assert [r["target"] for r in known_then] == [0.20]


def test_after_the_correction_the_corrected_target_is_what_we_know(store):
    store.clock.now = T1
    store.ingest_goal_versions([V1], source="config_goals_json", run_id="goals-1", as_of=T1)
    store.clock.now = T2
    store.ingest_goal_versions([V1_CORRECTED], source="config_goals_json", run_id="goals-2", as_of=T1)

    known_now = store.goal_versions(known_at=datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc))
    assert [r["target"] for r in known_now] == [0.25]


def test_nothing_is_known_before_the_first_ingest(store):
    store.clock.now = T1
    store.ingest_goal_versions([V1], source="config_goals_json", run_id="goals-1", as_of=T1)
    assert store.goal_versions(known_at=T0) == []


def test_the_correction_is_appended_not_overwritten(store):
    store.clock.now = T1
    store.ingest_goal_versions([V1], source="config_goals_json", run_id="goals-1", as_of=T1)
    store.clock.now = T2
    store.ingest_goal_versions([V1_CORRECTED], source="config_goals_json", run_id="goals-2", as_of=T1)
    assert store.row_count("goal_versions") == 2


def test_goal_in_effect_on_a_day_respects_known_at(store):
    store.clock.now = T1
    store.ingest_goal_versions([V1], source="config_goals_json", run_id="goals-1", as_of=T1)
    store.clock.now = T2
    store.ingest_goal_versions([V1_CORRECTED], source="config_goals_json", run_id="goals-2", as_of=T1)

    dec_2 = date(2026, 12, 2)
    old_view = store.goal_version_on(dec_2, known_at=datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc))
    new_view = store.goal_version_on(dec_2, known_at=datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc))
    assert old_view["target"] == 0.20
    assert new_view["target"] == 0.25


def test_goal_is_not_in_effect_before_its_effective_day(store):
    store.clock.now = T1
    store.ingest_goal_versions([V1], source="config_goals_json", run_id="goals-1", as_of=T1)
    assert store.goal_version_on(date(2026, 11, 30), known_at=T2) is None
