"""conftest.py must reset near_close_dca's module-level in-flight outcomes dict before every test.

Why (2026-11-14, PR 2315): ``near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"]`` is process-global state.
Dozens of tests and several near_close_dca paths fill it, and nothing reset it between tests, so it stayed
populated for every later test in the same process. The serial suite passed only because the two tests that
assert it starts empty
(test_write_afternoon_failure_artifacts_excludes_dry_run_execution_only_records_from_mixed_batch and
test_write_afternoon_failure_artifacts_preserves_execution_only_evidence_in_fallback_sidecar) happened to run
while it was clean. Under ``pytest -n auto`` the order changes and they failed in 1 of 2 full runs; a probe
counted 1,068 tests running with the dict already populated.

These tests do not rely on test ordering (xdist may put two tests on different workers): they call the reset
helper directly and check that the autouse fixture is attached to every test.
"""

from __future__ import annotations

import sys

import conftest


def test_reset_clears_populated_outcomes():
    import near_close_dca

    near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES["outcomes"] = {"AAPL": {"status": "submitted"}}
    conftest._clear_in_flight_candidate_outcomes()
    assert not near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES.get("outcomes")


def test_reset_keeps_the_same_dict_object():
    # Production code and tests hold the module attribute by name; clearing the key, not rebinding the
    # attribute, keeps every existing reference pointing at the live dict.
    import near_close_dca

    before = near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES
    before["outcomes"] = {"MSFT": {}}
    conftest._clear_in_flight_candidate_outcomes()
    assert near_close_dca._IN_FLIGHT_CANDIDATE_OUTCOMES is before


def test_reset_does_not_import_near_close_dca(monkeypatch):
    # The fixture runs before all ~14.6k tests; it must not pay near_close_dca's import (a ~40k-line module)
    # for tests that never touch it.
    monkeypatch.delitem(sys.modules, "near_close_dca", raising=False)
    conftest._clear_in_flight_candidate_outcomes()
    assert "near_close_dca" not in sys.modules


def test_reset_fixture_is_autouse_for_every_test(request):
    assert "_reset_in_flight_candidate_outcomes" in request.fixturenames
