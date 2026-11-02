"""Publishing through the shared SuggestionPublisher conventions, to this package's OWN
files only (ADR rule 2), and never when the outcome failed closed (ADR rule 3)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from comovement_signals import (
    CLUSTER_CAP_FILENAME,
    SELLOFF_FILENAME,
    ClusterCapSuggester,
    ComovementSuggestionPublisher,
    CorrelatedSelloffSuggester,
    cluster_cap_publisher,
    publish_outcome,
    selloff_publisher,
)
from dca_shared_interfaces.publishing import SuggestionPublisher

from ._inputs import AS_OF, TOY_CONFIG, clock, toy_inputs
from ._toy import day


def _cap_outcome(asof=AS_OF, inputs=None):
    inputs = inputs or toy_inputs(day_returns_session=asof)
    return ClusterCapSuggester(lambda a: inputs, config=TOY_CONFIG, clock=clock).evaluate(asof)


def test_file_names_are_distinct_and_not_the_ml_file():
    assert CLUSTER_CAP_FILENAME != SELLOFF_FILENAME
    assert "ml_sizing_adjustment.json" not in (CLUSTER_CAP_FILENAME, SELLOFF_FILENAME)


def test_publisher_is_a_suggestion_publisher(tmp_path):
    assert isinstance(cluster_cap_publisher(tmp_path), SuggestionPublisher)


@pytest.mark.parametrize("name", ["ml_sizing_adjustment.json", SELLOFF_FILENAME, "anything.json"])
def test_publisher_refuses_any_other_file(tmp_path, name):
    with pytest.raises(ValueError):
        ComovementSuggestionPublisher(tmp_path / name, signal="cluster_cap")


def test_publishes_own_file_only_and_leaves_others_untouched(tmp_path):
    other = tmp_path / "ml_sizing_adjustment.json"
    other.write_text('{"untouched": true}')
    before = other.read_bytes()

    r1 = publish_outcome(_cap_outcome(), cluster_cap_publisher(tmp_path))
    sell = CorrelatedSelloffSuggester(lambda a: toy_inputs(), config=TOY_CONFIG, clock=clock).evaluate(AS_OF)
    r2 = publish_outcome(sell, selloff_publisher(tmp_path))

    assert (r1.published, r2.published) == (True, True)
    assert other.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["ml_sizing_adjustment.json", CLUSTER_CAP_FILENAME, SELLOFF_FILENAME])  # no .lock/.tmp left


def test_payload_shape(tmp_path):
    publish_outcome(_cap_outcome(), cluster_cap_publisher(tmp_path))
    data = json.loads((tmp_path / CLUSTER_CAP_FILENAME).read_text())
    assert data["signal"] == "cluster_cap"
    assert data["asof"] == AS_OF.isoformat()
    assert data["research_only"] is True
    # Rule-based and not yet scored by the Phase 0 ledger: no MAE is claimed.
    assert data["eval_metric"]["mae"] is None
    assert data["bounds"] == {"min_multiplier": 0.25, "max_multiplier": 1.0}
    tickers = [s["ticker"] for s in data["suggestions"]]
    assert tickers == sorted(tickers) == ["A1", "A2", "A3", "B1", "C1"]
    a1 = data["suggestions"][0]
    assert a1["multiplier"] == pytest.approx(0.7)
    assert a1["multiplier_adjustment_pct"] == pytest.approx(-30.0)
    assert data["detail"]["clusters"][0] == ["A1", "A2", "A3"]


def test_failed_outcome_publishes_nothing(tmp_path):
    failed = _cap_outcome(inputs=toy_inputs(close_rows=[]))
    assert failed.ok is False
    res = publish_outcome(failed, cluster_cap_publisher(tmp_path))
    assert res.published is False
    assert res.status == "blocked_fail_closed"
    assert res.reason == "missing_prices"
    assert list(tmp_path.iterdir()) == []


def test_failed_outcome_leaves_previous_file_unchanged(tmp_path):
    pub = cluster_cap_publisher(tmp_path)
    assert publish_outcome(_cap_outcome(), pub).published
    before = (tmp_path / CLUSTER_CAP_FILENAME).read_bytes()
    publish_outcome(_cap_outcome(inputs=toy_inputs(close_rows=[])), pub)
    assert (tmp_path / CLUSTER_CAP_FILENAME).read_bytes() == before


def test_outcome_for_other_signal_refused(tmp_path):
    sell = CorrelatedSelloffSuggester(lambda a: toy_inputs(), config=TOY_CONFIG, clock=clock).evaluate(AS_OF)
    res = publish_outcome(sell, cluster_cap_publisher(tmp_path))
    assert (res.published, res.status) == (False, "blocked_wrong_signal")
    assert list(tmp_path.iterdir()) == []


def test_older_asof_never_overwrites_newer(tmp_path):
    pub = cluster_cap_publisher(tmp_path)
    assert publish_outcome(_cap_outcome(asof=day(9)), pub).published
    res = publish_outcome(_cap_outcome(asof=day(8)), pub)
    assert (res.published, res.status) == (False, "blocked_stale_asof")
    assert json.loads((tmp_path / CLUSTER_CAP_FILENAME).read_text())["asof"] == day(9).isoformat()


def test_same_asof_rerun_replaces(tmp_path):
    pub = cluster_cap_publisher(tmp_path)
    assert publish_outcome(_cap_outcome(), pub).published
    assert publish_outcome(_cap_outcome(), pub).published


@pytest.mark.parametrize("pct", [5.0, -80.0, float("nan")])
def test_out_of_bounds_suggestion_refused(tmp_path, pct):
    # +5% would be a multiplier above 1.0; -80% is below the 0.25 floor.
    out = _cap_outcome()
    bad = replace(out, suggestions=(replace(out.suggestions[0], multiplier_adjustment_pct=pct),) + out.suggestions[1:])
    res = publish_outcome(bad, cluster_cap_publisher(tmp_path))
    assert (res.published, res.status) == (False, "blocked_invalid_bounds")
    assert list(tmp_path.iterdir()) == []


def test_mixed_asof_batch_refused(tmp_path):
    out = _cap_outcome()
    bad = replace(out, suggestions=(replace(out.suggestions[0], asof=day(7)),) + out.suggestions[1:])
    res = publish_outcome(bad, cluster_cap_publisher(tmp_path))
    assert (res.published, res.status) == (False, "blocked_invalid_bounds")


def test_single_publish_writes_one_row(tmp_path):
    out = _cap_outcome()
    res = cluster_cap_publisher(tmp_path).publish(out.suggestions[0])
    assert res.published
    data = json.loads((tmp_path / CLUSTER_CAP_FILENAME).read_text())
    assert [s["ticker"] for s in data["suggestions"]] == ["A1"]
