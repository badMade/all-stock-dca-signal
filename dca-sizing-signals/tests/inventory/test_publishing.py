"""Publishing to this package's OWN file only, and never when the outcome failed closed."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from dca_shared_interfaces.publishing import SuggestionPublisher
from inventory_signals import FILENAME, InventoryReservationPublisher, inventory_publisher, publish_outcome

from ._inputs import AS_OF, suggester, toy_inputs


def test_is_a_suggestion_publisher(tmp_path):
    assert isinstance(inventory_publisher(tmp_path), SuggestionPublisher)


@pytest.mark.parametrize("name", ["ml_sizing_adjustment.json", "comovement_cluster_cap_suggestions.json",
                                  "anything.json"])
def test_refuses_any_other_file(tmp_path, name):
    with pytest.raises(ValueError):
        InventoryReservationPublisher(tmp_path / name)


def test_publishes_batch_and_leaves_other_files_untouched(tmp_path):
    other = tmp_path / "ml_sizing_adjustment.json"
    other.write_text('{"untouched": true}')
    result = publish_outcome(suggester().evaluate(AS_OF), inventory_publisher(tmp_path))
    assert result.published, result
    assert other.read_text() == '{"untouched": true}'
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([FILENAME, "ml_sizing_adjustment.json"])
    data = json.loads((tmp_path / FILENAME).read_text())
    assert data["shadow_mode"] is True and data["research_only"] is True
    assert data["eval_metric"] == {"mae": None, "scored": False, "status": "unscored_rule_based"}
    assert data["slot"] == "09:35" and data["asof"] == AS_OF.isoformat()
    rows = {r["ticker"]: r for r in data["suggestions"]}
    assert rows["HEAVY"]["multiplier"] == pytest.approx(0.5)
    assert rows["LIGHT"]["max_buy_price"] is None
    assert all(r["multiplier"] <= 1.0 for r in rows.values())


def test_failed_outcome_writes_nothing(tmp_path):
    out = suggester(inputs=toy_inputs(candidate_tickers=[])).evaluate(AS_OF)
    result = publish_outcome(out, inventory_publisher(tmp_path))
    assert (result.published, result.status) == (False, "blocked_fail_closed")
    assert list(tmp_path.iterdir()) == []


def test_older_asof_never_overwrites_newer(tmp_path):
    pub = inventory_publisher(tmp_path)
    assert publish_outcome(suggester().evaluate(AS_OF), pub).published
    old = [replace(s, asof=AS_OF - timedelta(days=1)) for s in suggester().evaluate(AS_OF).suggestions]
    result = pub.publish_batch(old, AS_OF - timedelta(days=1), "09:35")
    assert result.status == "blocked_stale_asof"


def test_rejects_enlarging_or_scored_or_mismatched_rows(tmp_path):
    pub = inventory_publisher(tmp_path)
    rows = list(suggester().evaluate(AS_OF).suggestions)
    for bad in (
        [replace(rows[0], multiplier_adjustment_pct=5.0)],      # would enlarge a buy
        [replace(rows[0], multiplier_adjustment_pct=-90.0)],    # below the floor
        [replace(rows[0], scored=True)],
        [replace(rows[0], eval_metric_mae=0.1)],
        [replace(rows[0], source="ml_forecaster")],
        [replace(rows[0], max_buy_price=-1.0)],
        [replace(rows[0], slot="10:30")],
        [rows[0], rows[0]],
        [],
    ):
        result = pub.publish_batch(bad, AS_OF, "09:35")
        assert not result.published, bad
    assert not (tmp_path / FILENAME).exists()


def test_base_class_publish_path_also_refuses(tmp_path):
    # The shared MAE gate refuses the +inf sentinel; our override routes through the batch path.
    row = suggester().evaluate(AS_OF).suggestions[0]
    assert SuggestionPublisher.publish(inventory_publisher(tmp_path), row).status == "blocked_missing_eval_metric"
