"""Regression tests for the precompiled scheduled run-id prefix regex.

``_artifact_day_matches_scheduled_provenance`` used an uncompiled ``re.match`` in an
inner loop; the pattern is now module-level. These pin that the behavior is unchanged.
"""

from datetime import date

import near_close_dca
from near_close_dca import _artifact_day_matches_scheduled_provenance as matches


def _state(run_id):
    return {"run_classification": {"run_id": run_id}}


def test_pattern_is_precompiled_at_module_level():
    assert near_close_dca._SCHEDULED_RUN_ID_PREFIX_RE.pattern == r"^scheduled_([0-9]{8})_"


def test_run_id_date_matching_artifact_day():
    assert matches(_state("scheduled_20260803_1530ET"), date(2026, 8, 3)) is True


def test_run_id_date_not_matching_artifact_day():
    assert matches(_state("scheduled_20260803_1530ET"), date(2026, 8, 4)) is False


def test_non_scheduled_run_id_does_not_match():
    assert matches(_state("manual_20260803_1530ET"), date(2026, 8, 3)) is False


def test_invalid_calendar_date_in_run_id_is_ignored():
    assert matches(_state("scheduled_20261340_1530ET"), date(2026, 8, 3)) is False
