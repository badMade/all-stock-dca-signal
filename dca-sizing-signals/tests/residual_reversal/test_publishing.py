"""ResidualReversalPublisher: own file only, bounds, unscored rows, budget neutrality,
stale as-of, and nothing at all for a failed or fully guarded run."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from residual_reversal import (
    FILENAME,
    CorporateEvent,
    ResidualReversalPublisher,
    ResidualReversalSuggester,
    publish_outcome,
    residual_reversal_publisher,
)

from . import _synthetic as S
from .test_suggester import _setup


def _outcome(**kw):
    inputs, asof, _ = _setup(**kw)
    return ResidualReversalSuggester(lambda a: inputs, clock=S.clock).evaluate(asof), inputs, asof


@pytest.mark.parametrize("name", ["ml_sizing_adjustment.json", "comovement_cluster_cap_suggestions.json",
                                  "residual_reversal_suggestions.json.bak"])
def test_refuses_any_other_file(tmp_path, name):
    with pytest.raises(ValueError):
        ResidualReversalPublisher(tmp_path / name)


def test_publishes_the_batch(tmp_path):
    out, _, asof = _outcome()
    res = publish_outcome(out, residual_reversal_publisher(tmp_path))
    assert res.published, res
    data = json.loads((tmp_path / FILENAME).read_text())
    assert data["asof"] == asof.isoformat()
    assert data["research_only"] is True and data["budget_neutral"] is True
    assert data["eval_metric"] == {"mae": None, "scored": False, "status": "unscored_rule_based"}
    rows = {r["ticker"]: r for r in data["suggestions"]}
    assert set(rows) == {"AAA", "BBB", "CCC", "DDD"}
    assert sum(r["proposed_usd"] * r["multiplier"] for r in rows.values()) == pytest.approx(40.0)
    assert [p.name for p in tmp_path.iterdir()] == [FILENAME]  # no lock or tmp left behind


def test_failed_outcome_writes_nothing(tmp_path):
    def boom(asof):
        raise RuntimeError("x")
    out = ResidualReversalSuggester(boom).evaluate(S.START)
    res = publish_outcome(out, residual_reversal_publisher(tmp_path))
    assert not res.published and res.status == "blocked_fail_closed"
    assert not any(tmp_path.iterdir())


def test_all_guarded_writes_nothing(tmp_path):
    out, _, _ = _outcome(beta=0.0)
    res = publish_outcome(out, residual_reversal_publisher(tmp_path))
    assert not res.published and res.status == "blocked_all_guarded"
    assert not any(tmp_path.iterdir())


def test_non_neutral_batch_is_refused(tmp_path):
    out, _, asof = _outcome()
    rows = list(out.suggestions)
    i = next(k for k, s in enumerate(rows) if s.ticker == "CCC")  # inside the bounds, room to move
    rows[i] = replace(rows[i], multiplier_adjustment_pct=rows[i].multiplier_adjustment_pct + 1.0)
    res = residual_reversal_publisher(tmp_path).publish_batch(rows, asof)
    assert not res.published and res.status == "blocked_not_budget_neutral"


@pytest.mark.parametrize("change", [
    {"multiplier_adjustment_pct": 30.0},
    {"multiplier_adjustment_pct": float("nan")},
    {"source": "ml_forecaster"},
    {"scored": True},
    {"eval_metric_mae": 0.1},
    {"proposed_usd": 0.0},
])
def test_bad_row_is_refused(tmp_path, change):
    out, _, asof = _outcome()
    rows = list(out.suggestions)
    rows[0] = replace(rows[0], **change)
    res = residual_reversal_publisher(tmp_path).publish_batch(rows, asof)
    assert not res.published and res.status == "blocked_invalid_bounds"
    assert not (tmp_path / FILENAME).exists()


def test_duplicate_and_mismatched_asof_refused(tmp_path):
    out, _, asof = _outcome()
    pub = residual_reversal_publisher(tmp_path)
    assert pub.publish_batch(list(out.suggestions) + [out.suggestions[0]], asof).status == "blocked_invalid_bounds"
    assert pub.publish_batch(out.suggestions, asof + timedelta(days=1)).status == "blocked_invalid_bounds"
    assert pub.publish_batch([], asof).status == "blocked_invalid_bounds"


def test_older_asof_never_overwrites_newer(tmp_path):
    out, inputs, asof = _outcome()
    pub = residual_reversal_publisher(tmp_path)
    assert publish_outcome(out, pub).published
    older = ResidualReversalSuggester(lambda a: inputs, clock=S.clock).evaluate(asof - timedelta(days=1))
    res = publish_outcome(older, pub)
    assert not res.published and res.status == "blocked_stale_asof"
    assert json.loads((tmp_path / FILENAME).read_text())["asof"] == asof.isoformat()


def test_guarded_tickers_are_listed_in_detail(tmp_path):
    out, inputs, asof = _outcome()
    ev = CorporateEvent("8k_5.01", asof, asof)
    out = ResidualReversalSuggester(lambda a: replace(inputs, events_by_ticker={"CCC": [ev]}),
                                    clock=S.clock).evaluate(asof)
    assert publish_outcome(out, residual_reversal_publisher(tmp_path)).published
    data = json.loads((tmp_path / FILENAME).read_text())
    assert data["detail"]["guarded"] == {"CCC": "corporate_event"}
    assert "CCC" not in {r["ticker"] for r in data["suggestions"]}
